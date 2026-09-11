"""
Pre-vectorize doc extraction PDFs into pgvector, then re-trigger QA evaluation.

This is a one-time recovery script for rows where PDF vectorization previously
failed (e.g. NAIC CDN 403s during bulk QA runs). It:

  1. Loads all eligible doc extraction rows from S3
  2. Checks which PDFs are not yet cached in pgvector (arm_if_ready = None)
  3. Fetches + vectorizes each PDF one at a time with --delay seconds between
     requests (avoids CDN rate-limiting)
  4. Deletes stale eval results for affected agent_call_ids so they'll be
     re-evaluated fresh
  5. Triggers QA evaluation via ECS (one task per batch of --batch-size IDs)
  6. Polls until all QA tasks complete

Usage:
  python3 -m scripts.prevectorize_docs [--dry-run] [--delay 30] [--skip-qa]
"""

import argparse
import json
import logging
import os
import sys
import time

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BUCKET = "web-change-tracker-prod-artifacts-815039343351"
DOC_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"
ALERTS_KEY = "alerts/alerts_table.jsonl"
DOC_RESULTS_KEY = "alerts/doc_eval_results_table.jsonl"
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

DEFAULT_DELAY = 30
DEFAULT_BATCH_SIZE = 35   # stay well within ECS CMD override size limits
POLL_INTERVAL = 20


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def _load_secrets() -> None:
    ssm = boto3.client("ssm", region_name=REGION)

    if not os.environ.get("CHATKIT_INTERNAL_API_KEY", "").strip():
        try:
            result = ssm.get_parameter(
                Name="/web-change-tracker/prod/chatkit_internal_api_key",
                WithDecryption=True,
            )
            os.environ["CHATKIT_INTERNAL_API_KEY"] = result["Parameter"]["Value"].strip()
            log.info("Loaded CHATKIT_INTERNAL_API_KEY from SSM")
        except Exception as e:
            log.error("Could not load CHATKIT_INTERNAL_API_KEY from SSM: %s", e)
            sys.exit(1)

    # DB creds needed for arm_if_ready (pgvector connection used internally by
    # chat-api — the API key is sufficient; DB creds are only needed if we ever
    # call pgvector search tools directly, which we don't here).
    # Load OpenAI key anyway in case any transitive import needs it.
    try:
        from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception as e:
        log.debug("SSM loader (openai/db): %s", e)


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _s3():
    return boto3.client("s3", region_name=REGION)


def _load_jsonl(client, key: str) -> list[dict]:
    try:
        body = client.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    except client.exceptions.NoSuchKey:
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


def _load_eligible_rows(client) -> list[dict]:
    """All doc extraction rows eligible for QA (same criteria as run_doc_eval bulk path)."""
    all_rows = _load_jsonl(client, DOC_EXTRACTIONS_KEY)
    log.info("Loaded %d total doc extraction rows", len(all_rows))

    # Upper bound: run_timestamp of most recently synced-to-Bubble alert
    upper_bound = None
    alerts = _load_jsonl(client, ALERTS_KEY)
    for a in alerts:
        if a.get("bubble_sync_status") == "synced":
            ts = a.get("run_timestamp", "")
            if ts and (upper_bound is None or ts > upper_bound):
                upper_bound = ts
    log.info("Upper bound (latest Bubble sync): %s", upper_bound or "none")

    eligible = [
        r for r in all_rows
        if r.get("extraction_source") != "transcript"
        and r.get("library_item_url", "").strip().lower() not in ("", "n/a")
        and r.get("agent_call_id")
        and (upper_bound is None or r.get("run_timestamp", "") <= upper_bound)
    ]
    log.info("Eligible rows: %d", len(eligible))
    return eligible


# ---------------------------------------------------------------------------
# Vectorization
# ---------------------------------------------------------------------------

def _is_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].rstrip("/").endswith(".pdf")


def _unique_pdf_urls(rows: list[dict]) -> list[tuple[str, str, list[str]]]:
    """Return list of (url, doc_name, [agent_call_ids]) deduplicated by URL."""
    url_map: dict[str, tuple[str, list[str]]] = {}
    for row in rows:
        url = (row.get("library_item_url") or "").strip()
        cid = row.get("agent_call_id", "")
        name = str(row.get("library_item_title") or row.get("document_title") or "")
        if not url or not _is_pdf_url(url):
            continue
        if url not in url_map:
            url_map[url] = (name, [])
        if cid and cid not in url_map[url][1]:
            url_map[url][1].append(cid)
    return [(url, name, cids) for url, (name, cids) in url_map.items()]


def prevectorize(
    rows: list[dict],
    delay: int = DEFAULT_DELAY,
    dry_run: bool = False,
) -> list[str]:
    """
    Vectorize all uncached PDF documents.
    Returns sorted list of agent_call_ids whose documents are now (or were already) vectorized.
    """
    from bubble.doc_extraction_ingest import arm_if_ready, ingest_and_arm
    from eval.doc_eval_agent import _fetch_pdf_text

    url_entries = _unique_pdf_urls(rows)
    log.info("Unique PDF URLs to process: %d", len(url_entries))

    needs_vectorize: list[tuple[str, str, list[str]]] = []
    already_cached_call_ids: set[str] = set()

    log.info("Checking pgvector cache status for each URL...")
    for url, doc_name, call_ids in url_entries:
        ns = arm_if_ready(url)
        if ns:
            log.info("  CACHED   %s", doc_name[:70])
            already_cached_call_ids.update(call_ids)
        else:
            log.info("  MISSING  %s", doc_name[:70])
            needs_vectorize.append((url, doc_name, call_ids))

    log.info(
        "Cache check complete — already cached: %d URLs (%d call_ids) | need vectorization: %d URLs",
        len(url_entries) - len(needs_vectorize),
        len(already_cached_call_ids),
        len(needs_vectorize),
    )

    if dry_run:
        log.info("[DRY RUN] Would vectorize %d URLs:", len(needs_vectorize))
        for url, doc_name, call_ids in needs_vectorize:
            log.info("  %s  (%d call_ids)", doc_name[:70], len(call_ids))
        return []

    newly_vectorized_call_ids: set[str] = set()

    for i, (url, doc_name, call_ids) in enumerate(needs_vectorize, 1):
        log.info("[%d/%d] Fetching PDF: %s", i, len(needs_vectorize), doc_name[:60])
        pdf_text = _fetch_pdf_text(url)

        if pdf_text:
            log.info("[%d/%d] PDF fetched (%d chars)", i, len(needs_vectorize), len(pdf_text))
        else:
            log.warning("[%d/%d] PDF fetch failed — will attempt ingest anyway (may reuse cache)", i, len(needs_vectorize))

        ns = ingest_and_arm(url, pdf_text, doc_name)
        if ns:
            log.info("[%d/%d] Vectorized → %s", i, len(needs_vectorize), ns)
            newly_vectorized_call_ids.update(call_ids)
        else:
            log.warning("[%d/%d] Vectorization failed for %s", i, len(needs_vectorize), doc_name[:60])

        if i < len(needs_vectorize):
            log.info("Waiting %ds before next request...", delay)
            time.sleep(delay)

    all_affected = sorted(already_cached_call_ids | newly_vectorized_call_ids)
    log.info(
        "Pre-vectorization complete: %d newly vectorized, %d already cached = %d total affected call_ids",
        len(newly_vectorized_call_ids),
        len(already_cached_call_ids),
        len(all_affected),
    )
    return all_affected


# ---------------------------------------------------------------------------
# Stale eval result cleanup
# ---------------------------------------------------------------------------

def delete_stale_eval_results(client, agent_call_ids: list[str], dry_run: bool = False) -> int:
    """Remove all doc eval results for the given agent_call_ids so bulk eval re-evaluates them."""
    id_set = set(agent_call_ids)
    rows = _load_jsonl(client, DOC_RESULTS_KEY)
    kept = [r for r in rows if r.get("agent_call_id") not in id_set]
    removed = len(rows) - len(kept)

    if dry_run:
        log.info("[DRY RUN] Would remove %d stale eval results for %d call_ids", removed, len(id_set))
        return removed

    new_body = "\n".join(json.dumps(r) for r in kept)
    if new_body:
        new_body += "\n"
    client.put_object(Bucket=BUCKET, Key=DOC_RESULTS_KEY, Body=new_body.encode("utf-8"))
    log.info("Removed %d stale eval results for %d call_ids", removed, len(id_set))
    return removed


# ---------------------------------------------------------------------------
# ECS QA trigger
# ---------------------------------------------------------------------------

def _trigger_qa_batch(ecs, call_ids: list[str]) -> str | None:
    ids_str = ",".join(call_ids)
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
                "command": [
                    "python", "-m", "eval.run_doc_eval",
                    "--agent-call-ids", ids_str,
                ],
            }]
        },
    )
    failures = resp.get("failures", [])
    if failures:
        log.error("ECS RunTask failed: %s", failures)
        return None
    arn = resp["tasks"][0]["taskArn"]
    log.info("Triggered QA for %d call_id(s) → task %s", len(call_ids), arn.split("/")[-1])
    return arn


def trigger_qa(agent_call_ids: list[str], batch_size: int = DEFAULT_BATCH_SIZE, dry_run: bool = False) -> list[str]:
    """Fire QA ECS tasks in batches. Returns list of task ARNs."""
    if not agent_call_ids:
        log.info("No agent_call_ids to evaluate")
        return []

    batches = [agent_call_ids[i:i + batch_size] for i in range(0, len(agent_call_ids), batch_size)]
    log.info("Triggering QA for %d call_id(s) across %d batch(es)", len(agent_call_ids), len(batches))

    if dry_run:
        for b in batches:
            log.info("[DRY RUN] Would trigger QA for %d call_ids: %s…", len(b), ",".join(b[:3]))
        return []

    ecs = boto3.client("ecs", region_name=REGION)
    task_arns = []
    for i, batch in enumerate(batches, 1):
        log.info("Launching batch %d/%d (%d call_ids)...", i, len(batches), len(batch))
        arn = _trigger_qa_batch(ecs, batch)
        if arn:
            task_arns.append(arn)
        if i < len(batches):
            time.sleep(2)  # small gap between launches

    return task_arns


def poll_tasks(task_arns: list[str]) -> None:
    """Poll until all ECS tasks stop, logging progress."""
    if not task_arns:
        return

    ecs = boto3.client("ecs", region_name=REGION)
    pending = set(task_arns)
    log.info("Polling %d QA task(s) until complete...", len(pending))

    while pending:
        desc = ecs.describe_tasks(cluster=CLUSTER, tasks=list(pending))
        for task in desc.get("tasks", []):
            arn = task["taskArn"]
            status = task.get("lastStatus", "")
            if status == "STOPPED":
                exit_code = (task.get("containers") or [{}])[0].get("exitCode", -1)
                reason = task.get("stoppedReason", "")
                short = arn.split("/")[-1][:12]
                if exit_code == 0:
                    log.info("Task %s SUCCEEDED", short)
                else:
                    log.error("Task %s FAILED exit=%s reason=%s", short, exit_code, reason)
                pending.discard(arn)
        if pending:
            log.info("%d task(s) still running...", len(pending))
            time.sleep(POLL_INTERVAL)

    log.info("All QA tasks complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Pre-vectorize docs then re-trigger doc QA eval")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing anything")
    parser.add_argument("--delay", type=int, default=DEFAULT_DELAY, help="Seconds between PDF fetches (default: 30)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Call_ids per QA ECS task")
    parser.add_argument("--skip-qa", action="store_true", help="Pre-vectorize only, skip QA trigger")
    parser.add_argument("--skip-prevectorize", action="store_true", help="Skip vectorization, go straight to QA")
    args = parser.parse_args()

    _load_secrets()

    client = _s3()
    rows = _load_eligible_rows(client)

    affected_call_ids: list[str] = []

    if not args.skip_prevectorize:
        affected_call_ids = prevectorize(rows, delay=args.delay, dry_run=args.dry_run)
    else:
        # When skipping pre-vectorize, collect all eligible call_ids
        affected_call_ids = sorted({r.get("agent_call_id") for r in rows if r.get("agent_call_id")})
        log.info("Skipping pre-vectorize — %d eligible call_ids", len(affected_call_ids))

    if not affected_call_ids:
        log.info("No affected call_ids — nothing to evaluate.")
        return

    if not args.skip_qa:
        log.info("Clearing stale eval results for %d call_ids...", len(affected_call_ids))
        delete_stale_eval_results(client, affected_call_ids, dry_run=args.dry_run)

        task_arns = trigger_qa(affected_call_ids, batch_size=args.batch_size, dry_run=args.dry_run)

        if task_arns and not args.dry_run:
            poll_tasks(task_arns)
    else:
        log.info("--skip-qa set — done. Affected call_ids (%d):", len(affected_call_ids))
        for cid in affected_call_ids:
            print(cid)


if __name__ == "__main__":
    main()
