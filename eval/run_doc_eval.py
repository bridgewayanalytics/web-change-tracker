"""
Document extraction QA evaluation pipeline entry point.

Usage:
  python -m eval.run_doc_eval                        # evaluate last 20 eligible rows
  python -m eval.run_doc_eval --limit 10             # evaluate last N rows
  python -m eval.run_doc_eval --agent-call-ids a,b   # evaluate specific rows by agent_call_id
  python -m eval.run_doc_eval --dry-run              # print selected rows, no agent calls

Eligible rows: most recent rows from document_extractions_table.jsonl,
excluding transcript rows (extraction_source == "transcript") and rows
with no real library_item_url.
"""

import argparse
import json
import logging
import os
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20
_DOC_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"
_ALERTS_KEY = "alerts/alerts_table.jsonl"
_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _load_alert_lookup() -> dict[tuple[str, str], dict]:
    """Return a dict keyed by (agent_call_id, library_item_url) → alert row."""
    bucket = _get_bucket()
    client = _s3_client()
    lookup: dict[tuple[str, str], dict] = {}
    try:
        resp = client.get_object(Bucket=bucket, Key=_ALERTS_KEY)
        lines = resp["Body"].read().decode("utf-8").strip().split("\n")
    except Exception as e:
        log.warning("Could not load alerts_table.jsonl for alert context: %s", e)
        return lookup
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            cid = row.get("agent_call_id", "")
            url = row.get("library_item_url", "") or ""
            if cid:
                lookup[(cid, url)] = row
        except json.JSONDecodeError:
            continue
    return lookup


def _load_doc_rows(agent_call_ids: list[str] | None, limit: int, library_item_url: str | None = None) -> list[dict]:
    bucket = _get_bucket()
    client = _s3_client()
    try:
        resp = client.get_object(Bucket=bucket, Key=_DOC_EXTRACTIONS_KEY)
        lines = resp["Body"].read().decode("utf-8").strip().split("\n")
    except Exception as e:
        log.error("Failed to load document_extractions_table.jsonl: %s", e)
        return []

    all_rows: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            all_rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if agent_call_ids is not None:
        result = [r for r in all_rows if r.get("agent_call_id") in agent_call_ids]
        if library_item_url:
            result = [r for r in result if r.get("library_item_url") == library_item_url]
        log.info("Selected %d row(s) by agent_call_id", len(result))
        return result

    # Filter: skip transcript rows and rows with no real document URL
    eligible = [
        r for r in all_rows
        if r.get("extraction_source") != "transcript"
        and r.get("library_item_url", "").strip().lower() not in ("", "n/a")
        and r.get("agent_call_id")
    ]

    # Most recent first
    eligible.sort(key=lambda r: r.get("run_timestamp", ""), reverse=True)

    # Deduplicate at the agent_call level (not row level), take most recent N calls
    seen: set[str] = set()
    selected: list[dict] = []
    for row in eligible:
        cid = row.get("agent_call_id", "")
        if cid not in seen:
            seen.add(cid)
        selected.append(row)  # collect ALL rows per call
        if len(seen) >= limit:
            break

    log.info("Selected %d doc extraction rows across %d call(s) for evaluation", len(selected), len(seen))
    return selected


def _make_eval_run_id() -> str:
    return f"doc-eval-{int(time.time())}"


def _group_by_call(rows: list[dict]) -> list[list[dict]]:
    """Group rows by agent_call_id, preserving order of first appearance."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        cid = row.get("agent_call_id", "unknown")
        groups.setdefault(cid, []).append(row)
    return list(groups.values())


def run(
    limit: int = _DEFAULT_LIMIT,
    agent_call_ids: list[str] | None = None,
    library_item_url: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    from eval.doc_eval_agent import evaluate_doc_extraction_call, make_doc_eval_row_key
    from eval.doc_result_store import store_doc_eval_results

    eval_run_id = _make_eval_run_id()
    eval_timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    log.info("Starting doc eval run %s", eval_run_id)

    rows = _load_doc_rows(agent_call_ids, limit, library_item_url)
    if not rows:
        log.info("No eligible doc extraction rows found")
        return []

    alert_lookup = _load_alert_lookup()
    groups = _group_by_call(rows)
    log.info("Evaluating %d doc extraction row(s) across %d call(s)", len(rows), len(groups))

    if dry_run:
        for row in rows:
            print(json.dumps({
                "agent_call_id": row.get("agent_call_id"),
                "eval_row_key": make_doc_eval_row_key(row),
                "document_title": row.get("document_title"),
                "library_item_url": row.get("library_item_url"),
                "run_id": row.get("run_id"),
                "run_timestamp": row.get("run_timestamp"),
            }, indent=2))
        return rows

    eval_rows = []
    for i, group in enumerate(groups, 1):
        call_id = group[0].get("agent_call_id", "unknown")
        lib_url = group[0].get("library_item_url", "") or ""
        alert_row = alert_lookup.get((call_id, lib_url)) or alert_lookup.get((call_id, ""))
        log.info(
            "[%d/%d] Evaluating agent_call_id=%s rows=%d document=%s alert_found=%s",
            i, len(groups), call_id, len(group),
            group[0].get("document_title") or lib_url,
            alert_row is not None,
        )

        score_results = evaluate_doc_extraction_call(rows=group, alert_row=alert_row)

        # Merge per-row scores back into source rows
        for row, score_result in zip(group, score_results):
            eval_row_key = score_result.get("eval_row_key") or make_doc_eval_row_key(row)
            eval_rows.append({
                **row,
                "eval_run_id": eval_run_id,
                "eval_timestamp": eval_timestamp,
                "eval_scores": score_result.get("eval_scores", {}),
                "overall_summary": score_result.get("overall_summary"),
                "eval_row_key": eval_row_key,
            })

    store_doc_eval_results(eval_rows, eval_run_id)
    log.info("Doc eval run %s complete — %d rows evaluated", eval_run_id, len(eval_rows))
    return eval_rows


def _load_secrets() -> None:
    try:
        from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception as e:
        log.warning("SSM loader failed: %s", e)
    if not os.environ.get("OPENAI_API_KEY"):
        log.warning("OPENAI_API_KEY not set — eval agent calls will fail")


def main():
    _load_secrets()
    parser = argparse.ArgumentParser(description="Run document extraction QA evaluation pipeline")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--agent-call-ids", type=str, default=None)
    parser.add_argument("--library-item-url", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    call_ids = None
    if args.agent_call_ids:
        call_ids = [x.strip() for x in args.agent_call_ids.split(",")]

    try:
        run(
            limit=args.limit,
            agent_call_ids=call_ids,
            library_item_url=args.library_item_url,
            dry_run=args.dry_run,
        )
    except Exception:
        import traceback
        log.error("Doc eval run failed:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
