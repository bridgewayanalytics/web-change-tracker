#!/usr/bin/env python3
"""
Bulk QA evaluation for web tracking agent rows.

Selects new-schema alert rows that have bubble_action set and haven't been evaluated yet,
then runs eval.run_eval in batches via --agent-call-ids.

Old-schema rows (nested events/library_items/agenda_items arrays, pre-May 2026) are excluded
because the eval agent expects the flat field format.

Usage:
  python3 scripts/bulk_qa_eval.py                         # dry run: show stats + first batch
  python3 scripts/bulk_qa_eval.py --execute               # run all batches locally
  python3 scripts/bulk_qa_eval.py --ecs                   # dispatch all batches to ECS (laptop can close)
  python3 scripts/bulk_qa_eval.py --ecs --execute         # same as --ecs (--execute implied for ECS)
  python3 scripts/bulk_qa_eval.py --limit 100 --execute   # cap at 100 agent_call_ids
  python3 scripts/bulk_qa_eval.py --include-done          # re-run already-QA'd rows too
  python3 scripts/bulk_qa_eval.py --batch-size 20         # adjust batch size
  python3 scripts/bulk_qa_eval.py --ids-only              # print IDs one per line (for piping)
  python3 scripts/bulk_qa_eval.py --approved-only         # only ingest_status=approved rows

ECS mode:
  Dispatches each batch as an ECS Fargate task and exits immediately — no local compute needed.
  Default batch size in ECS mode is 100 (so ~5 tasks for 507 rows). Tasks run concurrently
  on AWS; your laptop can be closed as soon as the task ARNs are printed.

  Note: concurrent ECS tasks write to the same JSONL file. If two tasks finish at exactly the
  same time, one may overwrite the other's results. This is rare and harmless — re-running
  bulk_qa_eval.py will pick up any rows that were missed (already-evaluated rows are skipped).
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_ALERTS_KEY = "alerts/alerts_table.jsonl"
_RESULTS_KEY = "alerts/eval_results_table.jsonl"
_DEFAULT_BATCH_SIZE = 25
_DEFAULT_ECS_BATCH_SIZE = 100  # fewer tasks = less concurrent write risk

# ECS config (mirrors dashboard/src/app/api/eval/alerts/route.ts)
_ECS_CLUSTER = "web-change-tracker-prod"
_ECS_TASK_DEFINITION = "web-change-tracker-prod"
_ECS_CONTAINER = "web-change-tracker-prod"
_ECS_SECURITY_GROUP = "sg-0813b03be31d51bbb"
_ECS_SUBNETS = [
    "subnet-0cd0f843fd5a199c3",
    "subnet-02ed42b574ccb2aeb",
    "subnet-0593f56de1cf0f4a5",
    "subnet-09cbe5386e755836e",
    "subnet-0bb36a8620ea48716",
    "subnet-085bf95f96dcfd5c9",
]


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _ecs_client():
    import boto3
    return boto3.client("ecs", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _load_jsonl(client, bucket: str, key: str) -> list[dict]:
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"].read().decode("utf-8")
    except Exception as e:
        if "NoSuchKey" in type(e).__name__ or "404" in str(e):
            return []
        raise
    rows = []
    for line in body.strip().split("\n"):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def _is_old_schema(row: dict) -> bool:
    """Old schema has non-empty nested arrays for events/library_items/agenda_items."""
    for key in ("events", "library_items", "agenda_items"):
        val = row.get(key)
        if isinstance(val, list) and val:
            return True
    return False


def _has_bubble_action(row: dict) -> bool:
    ba = row.get("bubble_action")
    if not ba or not isinstance(ba, dict):
        return False
    return bool(ba.get("event") or ba.get("library_item") or ba.get("agenda_items"))


def _has_real_scores(eval_row: dict) -> bool:
    """Return True if the eval result has actual field scores, not just an error entry."""
    scores = eval_row.get("eval_scores")
    if not isinstance(scores, dict) or not scores:
        return False
    # Error-only results look like {"error": "..."} — no real field scores
    return not (len(scores) == 1 and "error" in scores)


def _already_qad_call_ids(results: list[dict]) -> set[str]:
    """Return set of agent_call_ids that have real (non-error) QA results."""
    ids: set[str] = set()
    for row in results:
        if not _has_real_scores(row):
            continue  # error result — treat as not done so it gets re-evaluated
        key = row.get("eval_row_key") or row.get("agent_call_id") or ""
        if not key:
            continue
        cid = key.split("|")[0]  # composite keys: "agent_call_id|library_item_url"
        if cid:
            ids.add(cid)
    return ids


def _dispatch_ecs_task(ecs, batch: list[str], batch_num: int) -> str | None:
    """Fire one ECS Fargate task for this batch. Returns task ARN or None on failure."""
    ids_str = ",".join(batch)
    command = ["python", "-m", "eval.run_eval", "--agent-call-ids", ids_str]
    try:
        resp = ecs.run_task(
            cluster=_ECS_CLUSTER,
            taskDefinition=_ECS_TASK_DEFINITION,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": _ECS_SUBNETS,
                    "securityGroups": [_ECS_SECURITY_GROUP],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [{
                    "name": _ECS_CONTAINER,
                    "command": command,
                }]
            },
        )
        tasks = resp.get("tasks", [])
        failures = resp.get("failures", [])
        if failures:
            log.error("Batch %d ECS failures: %s", batch_num, failures)
            return None
        if not tasks:
            log.error("Batch %d: ECS returned no task ARN", batch_num)
            return None
        return tasks[0]["taskArn"]
    except Exception as e:
        log.error("Batch %d: ECS RunTask failed: %s", batch_num, e)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk QA eval for web tracking agent rows")
    parser.add_argument("--execute", action="store_true",
                        help="Actually run eval batches locally (default: dry run)")
    parser.add_argument("--ecs", action="store_true",
                        help="Dispatch batches to ECS instead of running locally (laptop can close)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max agent_call_ids to process")
    parser.add_argument("--batch-size", type=int, default=None,
                        help=f"agent_call_ids per batch (default {_DEFAULT_BATCH_SIZE} local, "
                             f"{_DEFAULT_ECS_BATCH_SIZE} ECS)")
    parser.add_argument("--include-done", action="store_true",
                        help="Include already-QA'd rows (re-evaluate)")
    parser.add_argument("--ids-only", action="store_true",
                        help="Print agent_call_ids only, one per line")
    parser.add_argument("--approved-only", action="store_true",
                        help="Only rows with ingest_status=approved")
    args = parser.parse_args()

    use_ecs = args.ecs
    batch_size = args.batch_size or (_DEFAULT_ECS_BATCH_SIZE if use_ecs else _DEFAULT_BATCH_SIZE)

    bucket = _get_bucket()
    client = _s3_client()

    log.info("Loading alerts_table.jsonl from s3://%s/%s ...", bucket, _ALERTS_KEY)
    all_rows = _load_jsonl(client, bucket, _ALERTS_KEY)
    log.info("Loaded %d alert rows", len(all_rows))

    log.info("Loading eval_results_table.jsonl ...")
    results = _load_jsonl(client, bucket, _RESULTS_KEY)
    already_done = _already_qad_call_ids(results)
    log.info("Already QA'd: %d unique agent_call_ids", len(already_done))

    # Walk rows most-recent-first, deduplicate by agent_call_id
    all_rows_sorted = sorted(all_rows, key=lambda r: r.get("run_timestamp", 0), reverse=True)

    transcript_skipped = 0
    old_schema_skipped = 0
    no_bubble_action_skipped = 0
    not_approved_skipped = 0
    already_done_skipped = 0

    seen_call_ids: set[str] = set()
    eligible_call_ids: list[str] = []

    for row in all_rows_sorted:
        cid = row.get("agent_call_id", "")
        if not cid or cid in seen_call_ids:
            continue
        seen_call_ids.add(cid)

        if row.get("alert_type") == "New Meeting Transcript Available":
            transcript_skipped += 1
            continue

        if _is_old_schema(row):
            old_schema_skipped += 1
            continue

        if not _has_bubble_action(row):
            no_bubble_action_skipped += 1
            continue

        if args.approved_only and row.get("ingest_status") != "approved":
            not_approved_skipped += 1
            continue

        if not args.include_done and cid in already_done:
            already_done_skipped += 1
            continue

        eligible_call_ids.append(cid)

    if args.limit:
        eligible_call_ids = eligible_call_ids[: args.limit]

    n_batches = (
        (len(eligible_call_ids) + batch_size - 1) // batch_size
        if eligible_call_ids
        else 0
    )

    if args.ids_only:
        for cid in eligible_call_ids:
            print(cid)
        return

    mode = "ECS" if use_ecs else "local"
    print(f"\n=== Bulk QA Eval ({mode}) — {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"Total rows loaded:           {len(all_rows)}")
    print(f"Unique agent_call_ids:       {len(seen_call_ids)}")
    print(f"  Transcript rows skipped:   {transcript_skipped}")
    print(f"  Old-schema rows skipped:   {old_schema_skipped}")
    print(f"  No bubble_action skipped:  {no_bubble_action_skipped}")
    if args.approved_only:
        print(f"  Non-approved skipped:      {not_approved_skipped}")
    print(f"  Already QA'd skipped:      {already_done_skipped}")
    print(f"Eligible agent_call_ids:     {len(eligible_call_ids)}")
    print(f"Batch size:                  {batch_size}")
    print(f"Batches to run:              {n_batches}")
    print()

    if not eligible_call_ids:
        print("Nothing to do.")
        return

    batches = [
        eligible_call_ids[i : i + batch_size]
        for i in range(0, len(eligible_call_ids), batch_size)
    ]

    if not args.execute and not use_ecs:
        print("Dry run — pass --execute to run locally, or --ecs to dispatch to ECS.")
        print(f"First batch preview ({min(5, len(batches[0]))} of {len(batches[0])} IDs):")
        for cid in batches[0][:5]:
            print(f"  {cid}")
        if len(batches[0]) > 5:
            print(f"  ... and {len(batches[0]) - 5} more")
        return

    # ── ECS mode ──────────────────────────────────────────────────────────────
    if use_ecs:
        ecs = _ecs_client()
        print(f"Dispatching {n_batches} ECS task(s) ...")
        print(f"Each task evaluates up to {batch_size} agent_call_ids on AWS Fargate.")
        print(f"You can close your laptop after all task ARNs are printed.\n")

        task_arns: list[str] = []
        failed: list[int] = []

        for i, batch in enumerate(batches, 1):
            arn = _dispatch_ecs_task(ecs, batch, i)
            if arn:
                task_id = arn.split("/")[-1]
                print(f"  [{i}/{n_batches}] Dispatched — task {task_id}")
                task_arns.append(arn)
            else:
                print(f"  [{i}/{n_batches}] FAILED to dispatch")
                failed.append(i)

        print(f"\n=== ECS dispatch complete ===")
        print(f"Tasks launched:  {len(task_arns)}")
        if failed:
            print(f"Failed batches:  {failed}")
        print(f"\nMonitor in CloudWatch: /ecs/web-change-tracker-prod")
        print(f"Re-run this script after tasks finish to pick up any write-conflict gaps.")

        if failed:
            sys.exit(1)
        return

    # ── Local mode ────────────────────────────────────────────────────────────
    failed_batches: list[int] = []

    for i, batch in enumerate(batches, 1):
        ids_str = ",".join(batch)
        cmd = [sys.executable, "-m", "eval.run_eval", "--agent-call-ids", ids_str]
        print(f"\n[{i}/{n_batches}] Running batch of {len(batch)} agent_call_ids ...")
        t0 = time.time()
        try:
            subprocess.run(cmd, check=True)
            elapsed = time.time() - t0
            print(f"  Batch {i} completed in {elapsed:.0f}s")
        except subprocess.CalledProcessError as e:
            elapsed = time.time() - t0
            print(f"  Batch {i} FAILED after {elapsed:.0f}s (exit code {e.returncode})")
            failed_batches.append(i)

    print(f"\n=== Done ===")
    print(f"Batches run: {n_batches}")
    if failed_batches:
        print(f"Failed batches: {failed_batches}")
        sys.exit(1)
    else:
        print("All batches succeeded.")


if __name__ == "__main__":
    main()
