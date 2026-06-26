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

    eval_rows = []
    for i, row in enumerate(rows, 1):
        call_id = row.get("agent_call_id", "unknown")
        log.info("[%d/%d] Evaluating agent_call_id=%s", i, len(rows), call_id)

        before_html, after_html = fetch_html_snapshots(row)
        reference_context = fetch_context(row)

        scores = evaluate_row(
            row=row,
            before_html=before_html,
            after_html=after_html,
            reference_context=reference_context,
        )

        eval_row = {
            **{k: v for k, v in row.items()},
            "eval_run_id": eval_run_id,
            "eval_timestamp": eval_timestamp,
            "eval_scores": scores,
        }
        eval_rows.append(eval_row)

    store_eval_results(eval_rows, eval_run_id)
    log.info("Eval run %s complete — %d rows evaluated", eval_run_id, len(eval_rows))
    return eval_rows


def main():
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
