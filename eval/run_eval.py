"""
QA evaluation pipeline entry point.

Usage:
  python eval/run_eval.py                        # evaluate last 50 eligible rows
  python eval/run_eval.py --limit 10             # evaluate last N rows
  python eval/run_eval.py --since 1750000000     # rows since Unix timestamp
  python eval/run_eval.py --dry-run              # print selected rows, no agent calls
  python eval/run_eval.py --agent-call-ids a,b   # evaluate specific rows

Triggered automatically after each Newsreel publication (trigger wired later).
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_LIMIT = 50


def _make_eval_run_id() -> str:
    return f"eval-{int(time.time())}"


def _eval_row_key(row: dict, group: list[dict]) -> str:
    """Stable unique key: agent_call_id alone for single rows, composite for siblings."""
    cid = row.get("agent_call_id", "")
    if len(group) > 1:
        lib_url = str(row.get("library_item_url") or "").strip()
        return f"{cid}|{lib_url}" if lib_url and lib_url.lower() != "n/a" else f"{cid}|{row.get('alert_title', '')}"
    return cid


def _run_row_file(path: str) -> None:
    """Subprocess entrypoint: evaluate exactly one row and store results.

    Each row runs in its own fresh Python process to prevent 'Event loop is closed'
    and glibc heap corruption (double free / SIGSEGV / exit 139) caused by asyncpg
    and the Agents SDK leaving stale C extension state across sequential asyncio.run()
    calls within a single long-lived process. The subprocess pattern mirrors
    run_doc_eval.py --group-file.
    """
    with open(path) as f:
        data = json.load(f)

    row = data["row"]
    sibling_rows = data.get("sibling_rows") or []
    before_html = data.get("before_html") or ""
    after_html = data.get("after_html") or ""
    all_rows = data.get("all_rows") or [row]
    eval_run_id = data["eval_run_id"]
    eval_timestamp = data["eval_timestamp"]

    from eval.eval_agent import evaluate_row
    from eval.result_store import store_eval_results

    scores = evaluate_row(
        row=row,
        before_html=before_html,
        after_html=after_html,
        reference_context="",
        sibling_rows=sibling_rows if sibling_rows else None,
    )

    key = _eval_row_key(row, all_rows)
    eval_row = {
        **row,
        "eval_run_id": eval_run_id,
        "eval_timestamp": eval_timestamp,
        "eval_scores": scores,
        "eval_row_key": key,
    }
    store_eval_results([eval_row], eval_run_id)


def run(
    limit: int = _DEFAULT_LIMIT,
    since_run_timestamp: int | None = None,
    agent_call_ids: list[str] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    import subprocess
    from eval.row_selector import load_eligible_rows
    from eval.html_fetcher import fetch_html_snapshots

    eval_run_id = _make_eval_run_id()
    eval_timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    log.info("Starting eval run %s", eval_run_id)

    rows = load_eligible_rows(
        limit=limit,
        since_run_timestamp=since_run_timestamp,
        agent_call_ids=agent_call_ids,
    )

    if not rows:
        log.info("No eligible rows found — nothing to evaluate")
        return []

    log.info("Evaluating %d rows", len(rows))

    if dry_run:
        for row in rows:
            print(json.dumps({
                "agent_call_id": row.get("agent_call_id"),
                "alert_type": row.get("alert_type"),
                "alert_title": row.get("alert_title"),
                "run_id": row.get("run_id"),
            }, indent=2))
        return rows

    # Group by agent_call_id so siblings are evaluated together.
    # Siblings share the same HTML — fetch once per group.
    groups: dict[str, list[dict]] = {}
    ungrouped: list[dict] = []
    for row in rows:
        cid = row.get("agent_call_id", "")
        if cid:
            groups.setdefault(cid, []).append(row)
        else:
            ungrouped.append(row)

    group_list = list(groups.values()) + [[r] for r in ungrouped]
    total_rows = sum(len(g) for g in group_list)
    evaluated = 0

    for group in group_list:
        representative = group[0]
        call_id = representative.get("agent_call_id", "unknown")

        # Fetch HTML once — all siblings share the same run/target/HTML
        before_html, after_html = fetch_html_snapshots(representative)

        for row in group:
            evaluated += 1
            siblings = [r for r in group if r is not row]
            log.info(
                "[%d/%d] Spawning eval agent_call_id=%s library_item=%s (%d sibling(s))",
                evaluated, total_rows, call_id,
                row.get("library_items_file_name") or row.get("alert_type"), len(siblings),
            )

            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump({
                    "row": row,
                    "sibling_rows": siblings,
                    "before_html": before_html,
                    "after_html": after_html,
                    "all_rows": group,
                    "eval_run_id": eval_run_id,
                    "eval_timestamp": eval_timestamp,
                }, f, default=str)
                tmp_path = f.name

            _MAX_SUBPROCESS_RETRIES = 2
            for attempt in range(_MAX_SUBPROCESS_RETRIES + 1):
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "eval.run_eval", "--row-file", tmp_path],
                        timeout=600,
                    )
                    if proc.returncode == 0:
                        break
                    # Negative return code = killed by signal (SIGABRT, SIGSEGV, etc.)
                    if proc.returncode < 0 and attempt < _MAX_SUBPROCESS_RETRIES:
                        log.warning(
                            "[%d/%d] Row crashed (signal %d) — retrying (attempt %d/%d)",
                            evaluated, total_rows, -proc.returncode,
                            attempt + 1, _MAX_SUBPROCESS_RETRIES,
                        )
                        continue
                    log.error(
                        "[%d/%d] Row failed (subprocess exit %d) agent_call_id=%s",
                        evaluated, total_rows, proc.returncode, call_id,
                    )
                except subprocess.TimeoutExpired:
                    log.error("[%d/%d] Row timed out after 600s agent_call_id=%s", evaluated, total_rows, call_id)
                    break
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    log.info("Eval run %s complete — %d rows evaluated", eval_run_id, evaluated)
    return []


def _load_secrets() -> None:
    try:
        from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception as e:
        log.warning("SSM loader failed — running without loaded secrets: %s", e)
    if not os.environ.get("OPENAI_API_KEY"):
        log.warning("OPENAI_API_KEY not set after secret loading — eval agent calls will fail")


def main():
    _load_secrets()
    parser = argparse.ArgumentParser(description="Run QA evaluation pipeline")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--since", type=int, default=None, dest="since_run_timestamp")
    parser.add_argument("--agent-call-ids", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--row-file", type=str, default=None,
                        help="Internal: path to a JSON file containing a pre-serialized row. "
                             "Run by subprocess; do not use directly.")
    args = parser.parse_args()

    if args.row_file:
        _run_row_file(args.row_file)
        return

    call_ids = None
    if args.agent_call_ids:
        call_ids = [x.strip() for x in args.agent_call_ids.split(",")]

    try:
        run(
            limit=args.limit,
            since_run_timestamp=args.since_run_timestamp,
            agent_call_ids=call_ids,
            dry_run=args.dry_run,
        )
    except Exception:
        import traceback
        log.error("Eval run failed with unhandled exception:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
