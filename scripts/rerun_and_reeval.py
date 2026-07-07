#!/usr/bin/env python3
"""
Orchestrate WTA rerun + QA re-evaluation for rows with existing eval results.

Phase 1: Remove transcript rows from eval_results_table.jsonl
Phase 2: Find run_id + target_id for each non-transcript agent_call_id
Phase 3: Trigger ECS rerun tasks in parallel (RERUN_MODE=alerts)
Phase 4: Poll S3 for rerun results → accept each (patch alerts_table.jsonl)
Phase 5: Trigger ECS eval task with all new agent_call_ids

Usage:
    python scripts/rerun_and_reeval.py [--dry-run]
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BUCKET = "web-change-tracker-prod-artifacts-815039343351"
ALERTS_KEY = "alerts/alerts_table.jsonl"
EVAL_KEY = "alerts/eval_results_table.jsonl"
RERUNS_PREFIX = "alerts/reruns/"

ECS_CLUSTER = "web-change-tracker-prod"
TASK_DEFINITION = "arn:aws:ecs:us-east-1:815039343351:task-definition/web-change-tracker-prod:76"
CONTAINER_NAME = "web-change-tracker-prod"
SUBNETS = [
    "subnet-0cd0f843fd5a199c3",
    "subnet-02ed42b574ccb2aeb",
    "subnet-0593f56de1cf0f4a5",
    "subnet-09cbe5386e755836e",
    "subnet-0bb36a8620ea48716",
    "subnet-085bf95f96dcfd5c9",
]
SECURITY_GROUP = "sg-0813b03be31d51bbb"
REGION = "us-east-1"

TRANSCRIPT_ALERT_TYPE = "New Meeting Transcript Available"
POLL_INTERVAL_SECONDS = 30
MAX_POLL_MINUTES = 30


def _s3():
    return boto3.client("s3", region_name=REGION)


def _ecs():
    return boto3.client("ecs", region_name=REGION)


def _download_jsonl(key: str) -> list[dict]:
    try:
        body = _s3().get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
        rows = []
        for line in body.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows
    except Exception as e:
        log.warning("Could not download %s: %s", key, e)
        return []


def _upload_jsonl(key: str, rows: list[dict], dry_run: bool) -> None:
    content = "\n".join(json.dumps(r) for r in rows)
    if dry_run:
        log.info("[DRY RUN] Would upload %d rows to s3://%s/%s", len(rows), BUCKET, key)
        return
    _s3().put_object(Bucket=BUCKET, Key=key, Body=content.encode("utf-8"))
    log.info("Uploaded %d rows to s3://%s/%s", len(rows), BUCKET, key)


# ── Phase 1: Clean transcript eval results ────────────────────────────────────

def clean_transcript_eval_results(dry_run: bool) -> list[str]:
    """
    Remove transcript rows from eval_results_table.jsonl.
    Returns list of non-transcript agent_call_ids that still have eval results.
    """
    log.info("Phase 1: Cleaning transcript eval results...")
    eval_rows = _download_jsonl(EVAL_KEY)
    log.info("  Found %d total eval result rows", len(eval_rows))

    transcript_ids = {r["agent_call_id"] for r in eval_rows if r.get("alert_type") == TRANSCRIPT_ALERT_TYPE}
    kept = [r for r in eval_rows if r.get("alert_type") != TRANSCRIPT_ALERT_TYPE]
    removed = len(eval_rows) - len(kept)

    log.info("  Removing %d transcript rows (agent_call_ids: %s)", removed, transcript_ids)
    log.info("  Keeping %d non-transcript eval rows", len(kept))

    if removed > 0:
        _upload_jsonl(EVAL_KEY, kept, dry_run)

    non_transcript_call_ids = list({r["agent_call_id"] for r in kept})
    log.info("  %d unique non-transcript agent_call_ids to re-evaluate", len(non_transcript_call_ids))
    return non_transcript_call_ids


# ── Phase 2: Find run_id + target_id for each agent_call_id ──────────────────

def find_rerun_targets(agent_call_ids: list[str]) -> list[dict]:
    """
    Look up run_id and target_id for each agent_call_id in alerts_table.jsonl.
    Returns list of unique dicts: {run_id, target_id, original_call_ids: [...]}.
    """
    log.info("Phase 2: Looking up run_id + target_id for %d agent_call_ids...", len(agent_call_ids))
    alert_rows = _download_jsonl(ALERTS_KEY)
    call_id_set = set(agent_call_ids)

    # Map agent_call_id → (run_id, target_id)
    call_id_to_target: dict[str, tuple] = {}
    for row in alert_rows:
        cid = row.get("agent_call_id", "")
        if cid in call_id_set:
            call_id_to_target[cid] = (row.get("run_id", ""), row.get("target_id", ""))

    # Group by (run_id, target_id)
    target_to_calls: dict[tuple, list[str]] = {}
    for cid, (run_id, target_id) in call_id_to_target.items():
        if run_id and target_id:
            key = (run_id, target_id)
            target_to_calls.setdefault(key, []).append(cid)
        else:
            log.warning("  agent_call_id=%s has no run_id/target_id — skipping", cid[:8])

    not_found = call_id_set - set(call_id_to_target.keys())
    if not_found:
        log.warning("  %d agent_call_ids not found in alerts_table.jsonl: %s",
                    len(not_found), [x[:8] for x in not_found])

    targets = [
        {"run_id": run_id, "target_id": target_id, "original_call_ids": cids}
        for (run_id, target_id), cids in target_to_calls.items()
    ]
    log.info("  Found %d unique (run_id, target_id) pairs to rerun", len(targets))
    for t in targets:
        log.info("    run_id=%.8s target_id=%s (call_ids: %s)",
                 t["run_id"], t["target_id"], [c[:8] for c in t["original_call_ids"]])
    return targets


# ── Phase 3: Trigger ECS rerun tasks ─────────────────────────────────────────

def trigger_rerun_tasks(targets: list[dict], dry_run: bool) -> dict[str, str]:
    """
    Fire ECS RunTask for each (run_id, target_id). RERUN_MODE=alerts.
    Returns mapping of (run_id, target_id) key → ECS task ARN.
    """
    log.info("Phase 3: Triggering %d ECS rerun tasks...", len(targets))
    ecs = _ecs()
    task_arns: dict[str, str] = {}

    for t in targets:
        run_id = t["run_id"]
        target_id = t["target_id"]
        key = f"{run_id}|{target_id}"

        if dry_run:
            log.info("  [DRY RUN] Would trigger rerun for run_id=%.8s target_id=%s", run_id, target_id)
            task_arns[key] = "dry-run-task-arn"
            continue

        try:
            resp = ecs.run_task(
                cluster=ECS_CLUSTER,
                taskDefinition=TASK_DEFINITION,
                launchType="FARGATE",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": SUBNETS,
                        "securityGroups": [SECURITY_GROUP],
                        "assignPublicIp": "ENABLED",
                    }
                },
                overrides={
                    "containerOverrides": [{
                        "name": CONTAINER_NAME,
                        "environment": [
                            {"name": "RERUN_RUN_ID", "value": run_id},
                            {"name": "RERUN_TARGET_ID", "value": target_id},
                            {"name": "RERUN_MODE", "value": "alerts"},
                        ],
                    }]
                },
            )
            tasks = resp.get("tasks", [])
            failures = resp.get("failures", [])
            if tasks:
                arn = tasks[0].get("taskArn", "?")
                task_arns[key] = arn
                log.info("  Triggered rerun run_id=%.8s target_id=%s → task %s", run_id, target_id, arn.split("/")[-1])
            if failures:
                log.error("  ECS RunTask failures for run_id=%.8s: %s", run_id, failures)
        except Exception as e:
            log.error("  Failed to trigger rerun for run_id=%.8s target_id=%s: %s", run_id, target_id, e)

    return task_arns


# ── Phase 4: Poll + accept reruns ────────────────────────────────────────────

def _rerun_result_key(run_id: str, target_id: str) -> str:
    return f"{RERUNS_PREFIX}{run_id}/{target_id}/result.json"


def _read_rerun_result(run_id: str, target_id: str) -> dict | None:
    key = _rerun_result_key(run_id, target_id)
    try:
        body = _s3().get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
        return json.loads(body)
    except Exception:
        return None


def accept_reruns(targets: list[dict], dry_run: bool) -> list[str]:
    """
    Poll S3 for each rerun result, then accept by:
      - Removing original rows from alerts_table.jsonl
      - Appending rerun_rows
    Returns list of new agent_call_ids from accepted reruns.
    """
    log.info("Phase 4: Polling for rerun results (up to %d min)...", MAX_POLL_MINUTES)
    pending = {(t["run_id"], t["target_id"]): t for t in targets}
    completed: dict[tuple, dict] = {}
    deadline = time.time() + MAX_POLL_MINUTES * 60

    while pending and time.time() < deadline:
        for (run_id, target_id) in list(pending.keys()):
            result = _read_rerun_result(run_id, target_id)
            if result is not None:
                log.info("  Rerun complete: run_id=%.8s target_id=%s  rerun_rows=%d",
                         run_id, target_id, len(result.get("rerun_rows") or []))
                completed[(run_id, target_id)] = result
                del pending[(run_id, target_id)]

        if pending:
            log.info("  Waiting for %d rerun(s)... (sleeping %ds)", len(pending), POLL_INTERVAL_SECONDS)
            time.sleep(POLL_INTERVAL_SECONDS)

    if pending:
        log.error("Timed out waiting for reruns: %s", list(pending.keys()))

    if not completed:
        log.error("No reruns completed — cannot accept")
        return []

    # Patch alerts_table.jsonl: remove original rows, append rerun_rows
    log.info("Accepting %d completed rerun(s)...", len(completed))
    alert_rows = _download_jsonl(ALERTS_KEY)
    new_call_ids: list[str] = []

    # Collect all original agent_call_ids to remove
    original_ids_to_remove: set[str] = set()
    all_rerun_rows: list[dict] = []
    for (run_id, target_id), result in completed.items():
        orig_rows = result.get("original_rows") or []
        rerun_rows = result.get("rerun_rows") or []
        for r in orig_rows:
            cid = r.get("agent_call_id", "")
            if cid:
                original_ids_to_remove.add(cid)
        for r in rerun_rows:
            all_rerun_rows.append(r)
            cid = r.get("agent_call_id", "")
            if cid and cid not in new_call_ids:
                new_call_ids.append(cid)

    # Filter out original rows, append rerun rows
    kept_alert_rows = [r for r in alert_rows if r.get("agent_call_id") not in original_ids_to_remove]
    kept_alert_rows.extend(all_rerun_rows)

    log.info("  Removed %d original rows, added %d rerun rows → %d total",
             len(original_ids_to_remove), len(all_rerun_rows), len(kept_alert_rows))
    _upload_jsonl(ALERTS_KEY, kept_alert_rows, dry_run)

    # Delete rerun result files
    if not dry_run:
        for run_id, target_id in completed.keys():
            key = _rerun_result_key(run_id, target_id)
            try:
                _s3().delete_object(Bucket=BUCKET, Key=key)
                log.info("  Deleted rerun result: %s", key)
            except Exception as e:
                log.warning("  Could not delete rerun result %s: %s", key, e)

    log.info("  New agent_call_ids from reruns: %s", [c[:8] for c in new_call_ids])
    return new_call_ids


# ── Phase 5: Trigger QA eval ─────────────────────────────────────────────────

def trigger_eval(new_call_ids: list[str], dry_run: bool) -> None:
    """Trigger one ECS eval task covering all new agent_call_ids."""
    if not new_call_ids:
        log.error("No new agent_call_ids — skipping eval trigger")
        return

    ids_arg = ",".join(new_call_ids)
    log.info("Phase 5: Triggering QA eval for %d agent_call_id(s)...", len(new_call_ids))

    if dry_run:
        log.info("[DRY RUN] Would run: python -m eval.run_eval --agent-call-ids %s", ids_arg[:80])
        return

    try:
        resp = _ecs().run_task(
            cluster=ECS_CLUSTER,
            taskDefinition=TASK_DEFINITION,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": SUBNETS,
                    "securityGroups": [SECURITY_GROUP],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [{
                    "name": CONTAINER_NAME,
                    "command": ["python", "-m", "eval.run_eval", "--agent-call-ids", ids_arg],
                }]
            },
        )
        tasks = resp.get("tasks", [])
        failures = resp.get("failures", [])
        if tasks:
            arn = tasks[0].get("taskArn", "?")
            log.info("QA eval task started: %s", arn.split("/")[-1])
        if failures:
            log.error("ECS eval RunTask failures: %s", failures)
    except Exception as e:
        log.error("Failed to trigger eval task: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without making changes")
    args = parser.parse_args()

    if args.dry_run:
        log.info("=== DRY RUN MODE ===")

    # Phase 1
    non_transcript_ids = clean_transcript_eval_results(args.dry_run)
    if not non_transcript_ids:
        log.error("No non-transcript agent_call_ids found — nothing to do")
        sys.exit(1)

    # Phase 2
    targets = find_rerun_targets(non_transcript_ids)
    if not targets:
        log.error("Could not find run_id/target_id for any agent_call_id — nothing to rerun")
        sys.exit(1)

    # Phase 3
    trigger_rerun_tasks(targets, args.dry_run)

    if args.dry_run:
        log.info("[DRY RUN] Skipping poll/accept/eval phases")
        return

    # Phase 4
    new_call_ids = accept_reruns(targets, args.dry_run)

    # Phase 5
    trigger_eval(new_call_ids, args.dry_run)

    log.info("Done. QA eval task is running — results will appear in eval_results_table.jsonl.")


if __name__ == "__main__":
    main()
