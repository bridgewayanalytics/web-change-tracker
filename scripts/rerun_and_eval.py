"""
Re-run the web tracking agent on previously QA-evaluated rows, auto-accept the
results, then trigger a fresh QA eval pass.

Use this after updating WTA instructions to see the effect on evaluated rows
without going through the dashboard rerun / accept flow manually.

Usage:
  python scripts/rerun_and_eval.py            # full run
  python scripts/rerun_and_eval.py --dry-run  # print plan, no ECS calls / S3 writes
"""

import argparse
import json
import logging
import time

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BUCKET = "web-change-tracker-prod-artifacts-815039343351"
EVAL_KEY = "alerts/eval_results_table.jsonl"
ALERTS_KEY = "alerts/alerts_table.jsonl"
REGION = "us-east-1"

CLUSTER = "web-change-tracker-prod"
TASK_DEFINITION = "web-change-tracker-prod"
CONTAINER = "web-change-tracker-prod"
SECURITY_GROUP = "sg-0813b03be31d51bbb"
SUBNETS = [
    "subnet-0cd0f843fd5a199c3",
    "subnet-02ed42b574ccb2aeb",
    "subnet-0593f56de1cf0f4a5",
    "subnet-09cbe5386e755836e",
    "subnet-0bb36a8620ea48716",
    "subnet-085bf95f96dcfd5c9",
]

POLL_INTERVAL = 15   # seconds between ECS status checks
RERUN_TIMEOUT = 600  # seconds to wait per rerun task


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _load_jsonl(s3, key: str) -> list[dict]:
    try:
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        return []
    rows = []
    for line in body.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


# ---------------------------------------------------------------------------
# ECS helpers
# ---------------------------------------------------------------------------

def _trigger_rerun(ecs, run_id: str, target_id: str, dry_run: bool) -> str | None:
    if dry_run:
        log.info("[DRY RUN] rerun  run_id=%-20s target_id=%s", run_id, target_id)
        return None
    resp = ecs.run_task(
        cluster=CLUSTER,
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
                "name": CONTAINER,
                "environment": [
                    {"name": "RERUN_RUN_ID",   "value": run_id},
                    {"name": "RERUN_TARGET_ID", "value": target_id},
                    {"name": "RERUN_MODE",      "value": "alerts"},
                ],
            }]
        },
    )
    failures = resp.get("failures", [])
    if failures:
        log.error("ECS RunTask failed for run_id=%s target_id=%s: %s", run_id, target_id, failures)
        return None
    arn = resp["tasks"][0]["taskArn"]
    log.info("Triggered rerun  run_id=%-20s target_id=%-30s task=%s", run_id, target_id, arn.split("/")[-1])
    return arn


def _poll_task(ecs, task_arn: str) -> str:
    """Block until the ECS task stops. Returns 'success' or 'failed:<detail>'."""
    deadline = time.time() + RERUN_TIMEOUT
    while time.time() < deadline:
        desc = ecs.describe_tasks(cluster=CLUSTER, tasks=[task_arn])
        tasks = desc.get("tasks", [])
        if not tasks:
            return "failed:disappeared"
        t = tasks[0]
        status = t.get("lastStatus", "")
        if status == "STOPPED":
            exit_code = t.get("containers", [{}])[0].get("exitCode", -1)
            reason = t.get("stoppedReason", "")
            if exit_code == 0:
                return "success"
            return f"failed:exit={exit_code} reason={reason}"
        log.info("  task %-12s  status=%s", task_arn.split("/")[-1][:12], status)
        time.sleep(POLL_INTERVAL)
    return "failed:timeout"


def _trigger_eval(ecs, call_ids: list[str], dry_run: bool) -> str | None:
    ids_str = ",".join(call_ids)
    if dry_run:
        log.info("[DRY RUN] QA eval for %d call_id(s): %s…", len(call_ids), ids_str[:80])
        return None
    resp = ecs.run_task(
        cluster=CLUSTER,
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
                "name": CONTAINER,
                "command": ["python", "-m", "eval.run_eval", "--agent-call-ids", ids_str],
            }]
        },
    )
    failures = resp.get("failures", [])
    if failures:
        log.error("ECS RunTask failed for QA eval: %s", failures)
        return None
    arn = resp["tasks"][0]["taskArn"]
    log.info("Triggered QA eval for %d row(s) → task=%s", len(call_ids), arn.split("/")[-1])
    return arn


# ---------------------------------------------------------------------------
# Accept logic (mirrors /api/rerun/accept)
# ---------------------------------------------------------------------------

def _accept_rerun(s3, run_id: str, target_id: str, dry_run: bool) -> int:
    result_key = f"alerts/reruns/{run_id}/{target_id}/result.json"
    try:
        result_body = s3.get_object(Bucket=BUCKET, Key=result_key)["Body"].read().decode()
    except Exception as e:
        log.warning("No rerun result at %s: %s", result_key, e)
        return 0

    result = json.loads(result_body)
    rerun_rows: list[dict] = (
        result.get("rerun_rows")
        or ([result["rerun"]] if result.get("rerun") else [])
    )
    if not rerun_rows:
        log.warning("Rerun result empty for run_id=%s target_id=%s", run_id, target_id)
        return 0

    # Preserve identity + lifecycle fields from the matching original row.
    # Reruns regenerate agent output but must not clobber human-set lifecycle
    # state (ingest_status, bubble_sync_status, recording/transcript keys).
    originals: list[dict] = result.get("original_rows") or (
        [result["original"]] if result.get("original") else []
    )
    orig_by_call_id = {o.get("agent_call_id"): o for o in originals if o.get("agent_call_id")}
    first_orig = originals[0] if originals else {}
    preserve_identity = ["run_id", "target_id", "run_timestamp", "source_url", "config_hash"]
    preserve_lifecycle = [
        "ingest_status", "bubble_sync_status", "bubble_sync_error",
        "bubble_event_id", "bubble_library_item_id",
        "recording_s3_key", "transcript_s3_key", "transcript_chunks_s3_key",
        "manual_transcript_s3_key",
    ]
    for row in rerun_rows:
        orig = orig_by_call_id.get(row.get("agent_call_id")) or first_orig
        for f in preserve_identity:
            if not row.get(f):
                row[f] = orig.get(f) or (run_id if f == "run_id" else target_id if f == "target_id" else None)
        for f in preserve_lifecycle:
            if f in orig and orig[f] is not None:
                row[f] = orig[f]

    if dry_run:
        log.info("[DRY RUN] accept  %d row(s)  run_id=%s target_id=%s", len(rerun_rows), run_id, target_id)
        for r in rerun_rows:
            log.info("  org=%s", r.get("organization"))
        return len(rerun_rows)

    existing = _load_jsonl(s3, ALERTS_KEY)
    kept = [r for r in existing if not (r.get("run_id") == run_id and r.get("target_id") == target_id)]
    patched = kept + rerun_rows
    patched.sort(key=lambda r: str(r.get("run_timestamp", "")), reverse=True)

    new_body = "\n".join(json.dumps(r, default=str) for r in patched) + "\n"
    s3.put_object(
        Bucket=BUCKET, Key=ALERTS_KEY,
        Body=new_body.encode(), ContentType="application/x-ndjson",
    )
    log.info("Accepted  %d row(s)  run_id=%s  target_id=%s", len(rerun_rows), run_id, target_id)
    return len(rerun_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan only — no ECS calls, no S3 writes")
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=REGION)
    ecs = boto3.client("ecs", region_name=REGION)

    # 1. Load previously evaluated rows
    log.info("Reading %s …", EVAL_KEY)
    eval_rows = _load_jsonl(s3, EVAL_KEY)
    if not eval_rows:
        log.warning("eval_results_table.jsonl is empty — nothing to do")
        return
    log.info("Found %d eval result row(s)", len(eval_rows))

    # 2. Collect unique (run_id, target_id) pairs and all agent_call_ids
    pairs: dict[tuple[str, str], list[str]] = {}  # (run_id, target_id) → [call_ids]
    for row in eval_rows:
        run_id    = str(row.get("run_id", "")).strip()
        target_id = str(row.get("target_id", "")).strip()
        call_id   = str(row.get("agent_call_id", "")).strip()
        if run_id and target_id:
            key = (run_id, target_id)
            pairs.setdefault(key, [])
            if call_id and call_id not in pairs[key]:
                pairs[key].append(call_id)

    log.info("%d unique (run_id, target_id) pair(s) to rerun", len(pairs))

    # 3. Trigger all reruns
    task_arns: dict[tuple[str, str], str] = {}
    for (run_id, target_id) in pairs:
        arn = _trigger_rerun(ecs, run_id, target_id, dry_run=args.dry_run)
        if arn:
            task_arns[(run_id, target_id)] = arn

    # 4. Wait for each rerun and accept
    for (run_id, target_id), arn in task_arns.items():
        log.info("Waiting for rerun task %s …", arn.split("/")[-1])
        status = _poll_task(ecs, arn)
        log.info("Rerun task done: %s  (run_id=%s target_id=%s)", status, run_id, target_id)
        if status == "success":
            _accept_rerun(s3, run_id, target_id, dry_run=args.dry_run)
        else:
            log.error("Rerun failed — skipping accept for run_id=%s target_id=%s", run_id, target_id)

    if args.dry_run:
        # Show what would be accepted even without a running task
        for (run_id, target_id) in pairs:
            _accept_rerun(s3, run_id, target_id, dry_run=True)

    # 5. Collect NEW agent_call_ids from accepted rows in alerts_table.jsonl.
    #    Reruns generate new agent_call_ids — the old IDs from eval_results_table
    #    no longer exist after accept. Must use the new IDs for QA eval.
    log.info("Collecting new agent_call_ids from accepted rows …")
    current_alerts = _load_jsonl(s3, ALERTS_KEY)
    pair_set = set(pairs.keys())
    new_call_ids: set[str] = set()
    for row in current_alerts:
        if (row.get("run_id"), row.get("target_id")) in pair_set:
            cid = str(row.get("agent_call_id", "")).strip()
            if cid:
                new_call_ids.add(cid)
    all_call_ids = list(new_call_ids)
    log.info("Found %d new agent_call_id(s) to evaluate", len(all_call_ids))

    if all_call_ids:
        log.info("Triggering QA eval for %d call_id(s) …", len(all_call_ids))
        _trigger_eval(ecs, all_call_ids, dry_run=args.dry_run)
    else:
        log.warning("No agent_call_ids found — QA eval not triggered")

    log.info(
        "Done. %d rerun(s) triggered, %d accepted, %d eval row(s) queued.",
        len(task_arns),
        len(task_arns),
        len(all_call_ids),
    )


if __name__ == "__main__":
    main()
