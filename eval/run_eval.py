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
import time
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_LIMIT = 50


def _make_eval_run_id() -> str:
    return f"eval-{int(time.time())}"


def run(
    limit: int = _DEFAULT_LIMIT,
    since_run_timestamp: int | None = None,
    agent_call_ids: list[str] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    from eval.row_selector import load_eligible_rows
    from eval.html_fetcher import fetch_html_snapshots
    from eval.context_builder import fetch_context
    from eval.eval_agent import evaluate_row
    from eval.result_store import store_eval_results

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

    # Group rows by agent_call_id so siblings are evaluated together.
    # Siblings share the same HTML — fetch once per group.
    groups: dict[str, list[dict]] = {}
    ungrouped: list[dict] = []
    for row in rows:
        cid = row.get("agent_call_id", "")
        if cid:
            groups.setdefault(cid, []).append(row)
        else:
            ungrouped.append(row)

    def _eval_row_key(row: dict, group: list[dict]) -> str:
        """Stable unique key: agent_call_id alone for single rows, composite for siblings."""
        cid = row.get("agent_call_id", "")
        if len(group) > 1:
            lib_url = str(row.get("library_item_url") or "").strip()
            return f"{cid}|{lib_url}" if lib_url and lib_url.lower() != "n/a" else f"{cid}|{row.get('alert_title', '')}"
        return cid

    eval_rows = []
    group_list = list(groups.values()) + [[r] for r in ungrouped]
    total_rows = sum(len(g) for g in group_list)
    evaluated = 0

    for group in group_list:
        representative = group[0]
        call_id = representative.get("agent_call_id", "unknown")

        # Fetch HTML once — all siblings share the same run/target/HTML
        before_html, after_html = fetch_html_snapshots(representative)
        reference_context = fetch_context(representative)

        for row in group:
            evaluated += 1
            siblings = [r for r in group if r is not row]
            log.info(
                "[%d/%d] Evaluating agent_call_id=%s library_item=%s (%d sibling(s))",
                evaluated, total_rows, call_id,
                row.get("library_items_file_name") or row.get("alert_type"), len(siblings),
            )

            scores = evaluate_row(
                row=row,
                before_html=before_html,
                after_html=after_html,
                reference_context=reference_context,
                sibling_rows=siblings if siblings else None,
            )

            eval_row_key = _eval_row_key(row, group)
            eval_row = {
                **{k: v for k, v in row.items()},
                "eval_run_id": eval_run_id,
                "eval_timestamp": eval_timestamp,
                "eval_scores": scores,
                "eval_row_key": eval_row_key,
            }
            eval_rows.append(eval_row)

    store_eval_results(eval_rows, eval_run_id)
    log.info("Eval run %s complete — %d rows evaluated", eval_run_id, len(eval_rows))
    return eval_rows


def _load_secrets() -> None:
    try:
        from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception as e:
        log.debug("SSM loader skipped or failed: %s", e)


def main():
    _load_secrets()
    parser = argparse.ArgumentParser(description="Run QA evaluation pipeline")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--since", type=int, default=None, dest="since_run_timestamp")
    parser.add_argument("--agent-call-ids", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    call_ids = None
    if args.agent_call_ids:
        call_ids = [x.strip() for x in args.agent_call_ids.split(",")]

    run(
        limit=args.limit,
        since_run_timestamp=args.since_run_timestamp,
        agent_call_ids=call_ids,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
