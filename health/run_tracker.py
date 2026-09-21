"""
Pipeline health tracker — accumulated checkpoint data for a single pipeline run.

Usage in spike.py:
    from health.run_tracker import RunTracker
    tracker = RunTracker(run_id, run_timestamp, targets_total=len(targets))
    ...
    tracker.finalize()   # writes health/latest.json + health/runs/*.json to S3, emails on failures

Module-level functions (record_fetch, record_agent, etc.) are no-ops when no
tracker is active, so instrumentation calls in fetch_page() etc. never crash
the pipeline.
"""

import json
import logging
import os
import time

log = logging.getLogger(__name__)

_ACTIVE: "RunTracker | None" = None

STATUS_GREEN = "green"
STATUS_YELLOW = "yellow"
STATUS_RED = "red"

# Thresholds
_PLAYWRIGHT_RED_RATE = 0.80    # >80% playwright failures → red
_PLAYWRIGHT_YELLOW_RATE = 0.20 # >20% playwright failures → yellow
_AGENT_YELLOW_RATE = 0.20      # >20% agent failures → yellow
# Zero real alerts is suspicious when this many targets showed page changes
_ZERO_ALERT_MIN_CHANGES = 3


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

class _FetchResult:
    __slots__ = ("playwright_tried", "playwright_ok", "playwright_error", "final_method", "success")

    def __init__(self, playwright_tried, playwright_ok, playwright_error, final_method, success):
        self.playwright_tried = playwright_tried
        self.playwright_ok = playwright_ok
        self.playwright_error = playwright_error
        self.final_method = final_method
        self.success = success


class _TargetHealth:
    __slots__ = ("target_id", "url", "fetch", "page_changed", "first_run",
                 "agent_called", "agent_ok", "pgvector_used",
                 "real_alert_count", "doc_extraction_count",
                 "doc_attempted", "doc_failed", "doc_errors",
                 "snapshot_stored", "snapshot_error",
                 "error")

    def __init__(self, target_id: str, url: str = ""):
        self.target_id = target_id
        self.url = url
        self.fetch: _FetchResult | None = None
        self.page_changed = False
        self.first_run = False
        self.agent_called = False
        self.agent_ok: bool | None = None
        self.pgvector_used: bool | None = None
        self.real_alert_count = 0
        self.doc_extraction_count = 0
        self.doc_attempted = 0
        self.doc_failed = 0
        self.doc_errors: list[str] = []
        self.snapshot_stored: bool | None = None
        self.snapshot_error: str | None = None
        self.error: str | None = None


# ---------------------------------------------------------------------------
# Tracker class
# ---------------------------------------------------------------------------

class RunTracker:
    def __init__(self, run_id: str, run_timestamp: int, targets_total: int):
        global _ACTIVE
        self.run_id = run_id
        self.run_timestamp = run_timestamp
        self.targets_total = targets_total
        self.start_time = time.time()
        self._targets: dict[str, _TargetHealth] = {}
        self._storage_ok: bool | None = None
        self._storage_error: str | None = None
        self._email_send_ok: bool | None = None
        self._email_send_error: str | None = None
        self._org_tree_live: bool | None = None
        _ACTIVE = self

    def _target(self, target_id: str, url: str = "") -> _TargetHealth:
        if target_id not in self._targets:
            self._targets[target_id] = _TargetHealth(target_id=target_id, url=url)
        return self._targets[target_id]

    def record_fetch(self, target_id: str, url: str, playwright_tried: bool,
                     playwright_ok: "bool | None", playwright_error: "str | None",
                     final_method: str, success: bool) -> None:
        t = self._target(target_id, url)
        t.fetch = _FetchResult(playwright_tried, playwright_ok, playwright_error, final_method, success)

    def record_change(self, target_id: str, page_changed: bool, first_run: bool) -> None:
        t = self._target(target_id)
        t.page_changed = page_changed
        t.first_run = first_run

    def record_agent(self, target_id: str, agent_ok: bool, pgvector_used: bool,
                     real_alert_count: int, error: "str | None" = None) -> None:
        t = self._target(target_id)
        t.agent_called = True
        t.agent_ok = agent_ok
        t.pgvector_used = pgvector_used
        t.real_alert_count = real_alert_count
        if error:
            t.error = error

    def record_doc_extraction(self, target_id: str, doc_count: int) -> None:
        self._target(target_id).doc_extraction_count += doc_count

    def record_doc_extraction_attempt(self, target_id: str, name: str, ok: bool, error: "str | None" = None) -> None:
        t = self._target(target_id)
        t.doc_attempted += 1
        if ok:
            t.doc_extraction_count += 1
        else:
            t.doc_failed += 1
            if error and len(t.doc_errors) < 3:
                t.doc_errors.append(f"{name[:50]}: {error[:120]}")

    def record_snapshot(self, target_id: str, ok: bool, error: "str | None" = None) -> None:
        t = self._target(target_id)
        t.snapshot_stored = ok
        if error:
            t.snapshot_error = error[:200]

    def record_email_send(self, ok: bool, error: "str | None" = None) -> None:
        self._email_send_ok = ok
        self._email_send_error = error

    def record_storage(self, ok: bool, error: "str | None" = None) -> None:
        self._storage_ok = ok
        self._storage_error = error

    def record_org_tree(self, ok: bool) -> None:
        """Record whether the live Bubble org tree fetch succeeded."""
        self._org_tree_live = ok

    def record_target_error(self, target_id: str, url: str, error: str) -> None:
        self._target(target_id, url).error = error

    # ------------------------------------------------------------------
    # Status computation
    # ------------------------------------------------------------------

    def _compute_status(self) -> "tuple[str, list[str]]":
        flags_red: list[str] = []
        flags_yellow: list[str] = []

        targets = list(self._targets.values())
        fetched = [t for t in targets if t.fetch is not None]
        pw_tried = [t for t in fetched if t.fetch and t.fetch.playwright_tried]
        pw_failed = [t for t in pw_tried if t.fetch and t.fetch.playwright_ok is False]
        fetch_failed = [t for t in fetched if t.fetch and not t.fetch.success]

        # Playwright failure rate
        if pw_tried:
            pw_fail_rate = len(pw_failed) / len(pw_tried)
            if pw_fail_rate >= _PLAYWRIGHT_RED_RATE:
                sample_err = ""
                if pw_failed and pw_failed[0].fetch and pw_failed[0].fetch.playwright_error:
                    sample_err = f" Error: {pw_failed[0].fetch.playwright_error[:200]}"
                flags_red.append(
                    f"Playwright failed on {len(pw_failed)}/{len(pw_tried)} targets "
                    f"({pw_fail_rate*100:.0f}%).{sample_err}"
                )
            elif pw_fail_rate >= _PLAYWRIGHT_YELLOW_RATE:
                ids = ", ".join(t.target_id for t in pw_failed[:3])
                flags_yellow.append(
                    f"Playwright degraded: {len(pw_failed)}/{len(pw_tried)} targets failed. Examples: {ids}"
                )

        # All fetches failed
        if fetched and len(fetch_failed) == len(fetched):
            flags_red.append(
                f"All {len(fetched)} target fetches failed — pipeline produced no usable HTML."
            )

        # S3 storage failure
        if self._storage_ok is False:
            flags_red.append(f"S3 storage write failed: {self._storage_error or 'unknown error'}")

        # Agent failure rate
        agent_called = [t for t in targets if t.agent_called]
        agent_failed = [t for t in agent_called if t.agent_ok is False]
        if agent_called and len(agent_failed) / len(agent_called) >= _AGENT_YELLOW_RATE:
            flags_yellow.append(
                f"Agent call failures: {len(agent_failed)}/{len(agent_called)} targets failed."
            )

        # pgvector completely unavailable
        pgvector_targets = [t for t in targets if t.agent_called and t.pgvector_used is not None]
        if pgvector_targets and all(not t.pgvector_used for t in pgvector_targets):
            flags_yellow.append(
                f"pgvector unavailable — all {len(pgvector_targets)} agent calls ran without knowledge base access."
            )

        # Zero real alerts with meaningful page changes
        total_real_alerts = sum(t.real_alert_count for t in targets)
        targets_with_change = [t for t in targets if t.page_changed and not t.first_run]
        if total_real_alerts == 0 and len(targets_with_change) >= _ZERO_ALERT_MIN_CHANGES:
            flags_yellow.append(
                f"Zero real alerts despite {len(targets_with_change)} targets with page changes. "
                f"Possible HTML capture or agent configuration issue."
            )

        # Doc extraction failures
        total_doc_attempted = sum(t.doc_attempted for t in targets)
        total_doc_failed = sum(t.doc_failed for t in targets)
        if total_doc_attempted > 0 and total_doc_failed > 0:
            rate = total_doc_failed / total_doc_attempted
            sample_errors = "; ".join(
                e for t in targets for e in t.doc_errors
            )[:200]
            msg = (
                f"Doc extraction: {total_doc_failed}/{total_doc_attempted} documents failed"
                + (f" — {sample_errors}" if sample_errors else "")
            )
            if rate >= 0.5:
                flags_red.append(msg)
            else:
                flags_yellow.append(msg)

        # HTML snapshot write failures (breaks rerun feature)
        snapshot_failed = [t for t in targets if t.snapshot_stored is False]
        if snapshot_failed:
            ids = ", ".join(t.target_id for t in snapshot_failed[:3])
            sample = snapshot_failed[0].snapshot_error or "unknown"
            flags_yellow.append(
                f"HTML snapshot storage failed for {len(snapshot_failed)} target(s) "
                f"({ids}): {sample[:120]}"
            )

        # Alert email failed to send when there were real changes
        if self._email_send_ok is False and total_real_alerts > 0:
            flags_yellow.append(
                f"Alert email failed to send: {self._email_send_error or 'unknown error'}"
            )

        # Org tree fell back to static file — organization assignments in agent output may be stale
        if self._org_tree_live is False:
            flags_yellow.append(
                "Org tree: Bubble API fetch failed — agents ran with static prompts/org_tree.txt fallback. "
                "Organization field assignments may be stale or incomplete."
            )

        if flags_red:
            return STATUS_RED, flags_red + flags_yellow
        if flags_yellow:
            return STATUS_YELLOW, flags_yellow
        return STATUS_GREEN, []

    # ------------------------------------------------------------------
    # Report building
    # ------------------------------------------------------------------

    def _build_report(self) -> dict:
        targets = list(self._targets.values())
        pw_tried = [t for t in targets if t.fetch and t.fetch.playwright_tried]
        pw_ok = [t for t in pw_tried if t.fetch and t.fetch.playwright_ok]
        pw_failed = [t for t in pw_tried if t.fetch and t.fetch.playwright_ok is False]

        status, flags = self._compute_status()

        return {
            "run_id": self.run_id,
            "run_timestamp": self.run_timestamp,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_seconds": int(time.time() - self.start_time),
            "status": status,
            "flags": flags,
            "summary": {
                "targets_total": self.targets_total,
                "targets_fetched": len([t for t in targets if t.fetch and t.fetch.success]),
                "targets_fetch_failed": len([t for t in targets if t.fetch and not t.fetch.success]),
                "targets_changed": len([t for t in targets if t.page_changed or t.first_run]),
                "playwright_tried": len(pw_tried),
                "playwright_ok": len(pw_ok),
                "playwright_failed": len(pw_failed),
                "agents_called": len([t for t in targets if t.agent_called]),
                "agents_ok": len([t for t in targets if t.agent_ok]),
                "pgvector_connected": len([t for t in targets if t.pgvector_used]),
                "real_alerts": sum(t.real_alert_count for t in targets),
                "doc_extractions_ok": sum(t.doc_extraction_count for t in targets),
                "doc_extractions_attempted": sum(t.doc_attempted for t in targets),
                "doc_extractions_failed": sum(t.doc_failed for t in targets),
                "snapshots_failed": len([t for t in targets if t.snapshot_stored is False]),
                "email_sent": self._email_send_ok,
                "storage_ok": self._storage_ok,
                "org_tree_live": self._org_tree_live,
            },
            # First 10 playwright errors for the email body
            "playwright_errors": [
                {
                    "target_id": t.target_id,
                    "url": t.url,
                    "error": (t.fetch.playwright_error or "unknown")[:200] if t.fetch else "unknown",
                }
                for t in pw_failed[:10] if t.fetch
            ],
            # First 10 general target errors
            "target_errors": [
                {"target_id": t.target_id, "error": (t.error or "")[:200]}
                for t in targets if t.error
            ][:10],
        }

    # ------------------------------------------------------------------
    # Finalize — write S3 + email
    # ------------------------------------------------------------------

    def finalize(self) -> None:
        global _ACTIVE
        try:
            report = self._build_report()
            self._write_s3(report)
            self._send_email_if_needed(report)
            log.info(
                "HEALTH_RUN_COMPLETE status=%s flags=%d run_id=%s duration=%ds",
                report["status"], len(report["flags"]), self.run_id, report["duration_seconds"],
            )
        except Exception:
            log.warning("RunTracker.finalize() failed — non-fatal", exc_info=True)
        finally:
            _ACTIVE = None

    def _bucket(self) -> str:
        return (
            os.environ.get("CHANGELOG_BUCKET", "").strip()
            or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
            or "web-change-tracker-prod-artifacts-815039343351"
        )

    def _write_s3(self, report: dict) -> None:
        bucket = self._bucket()
        if not bucket:
            log.warning("health: no S3 bucket — skipping health report write")
            return
        import boto3
        client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        body = json.dumps(report, indent=2, default=str).encode("utf-8")
        client.put_object(Bucket=bucket, Key="health/latest.json", Body=body, ContentType="application/json")
        dt = time.strftime("%Y-%m-%d_%H", time.gmtime(self.run_timestamp))
        client.put_object(Bucket=bucket, Key=f"health/runs/{dt}_{self.run_id}.json", Body=body, ContentType="application/json")

    def _send_email_if_needed(self, report: dict) -> None:
        if report["status"] == STATUS_GREEN:
            return

        from_email = os.environ.get("FROM_EMAIL", "").strip()
        to_csv = os.environ.get("TO_EMAILS", "").strip()
        email_enabled = os.environ.get("SEND_EMAIL", os.environ.get("EMAIL_ENABLED", "")).lower() in ("1", "true", "yes")
        ses_region = os.environ.get("SES_REGION", "us-east-1")

        if not email_enabled or not from_email or not to_csv:
            log.info("health: status=%s but email not configured — skipping health alert", report["status"])
            return

        recipients = [e.strip() for e in to_csv.split(",") if e.strip()]
        subject = _build_subject(report)
        body = _build_body(report)

        import boto3
        client = boto3.client("ses", region_name=ses_region)
        for recipient in recipients:
            try:
                client.send_email(
                    Source=from_email,
                    Destination={"ToAddresses": [recipient]},
                    Message={
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                    },
                )
                log.info("health: sent %s alert to %s", report["status"], recipient)
            except Exception as exc:
                log.warning("health: failed to send to %s: %s", recipient, exc)


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------

def _build_subject(report: dict) -> str:
    s = report["summary"]
    run_dt = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(report["run_timestamp"]))
    flags = report["flags"]
    if report["status"] == STATUS_RED:
        first = flags[0][:80] if flags else "critical failure"
        return f"🚨 CRITICAL — Web Tracker pipeline: {first} [{run_dt}]"
    else:
        return f"⚠️  WARNING — Web Tracker pipeline degraded: {len(flags)} issue(s) [{run_dt}]"


def _build_body(report: dict) -> str:
    s = report["summary"]
    run_dt = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(report["run_timestamp"]))
    label = {"green": "✅ Healthy", "yellow": "⚠️  Degraded", "red": "🚨 Critical"}.get(report["status"], report["status"])

    lines = [
        "Web Tracker Pipeline Health Report",
        "=" * 50,
        f"Run ID:   {report['run_id']}",
        f"Run time: {run_dt}",
        f"Duration: {report['duration_seconds']}s",
        f"Status:   {label}",
        "",
        "ISSUES",
        "-" * 50,
    ]
    for i, flag in enumerate(report["flags"], 1):
        lines.append(f"{i}. {flag}")

    lines += [
        "",
        "CHECKPOINT SUMMARY",
        "-" * 50,
        f"Targets:        {s['targets_fetched']}/{s['targets_total']} fetched successfully"
            + (f"  ({s['targets_fetch_failed']} failed)" if s["targets_fetch_failed"] else ""),
        f"Playwright:     {s['playwright_ok']}/{s['playwright_tried']} OK"
            + (f"  ({s['playwright_failed']} failed)" if s["playwright_failed"] else ""),
        f"Changes found:  {s['targets_changed']} targets",
        f"Agent calls:    {s['agents_ok']}/{s['agents_called']} succeeded",
        f"pgvector:       {s['pgvector_connected']} targets connected",
        f"Real alerts:    {s['real_alerts']}",
        f"Doc extracts:   {s['doc_extractions_ok']}/{s['doc_extractions_attempted']} succeeded"
            + (f"  ({s['doc_extractions_failed']} failed)" if s["doc_extractions_failed"] else ""),
        f"Snapshots:      {'✗ ' + str(s['snapshots_failed']) + ' failed' if s['snapshots_failed'] else '✓ OK'}",
        f"Alert email:    {'✓ sent' if s['email_sent'] else '✗ FAILED' if s['email_sent'] is False else 'N/A (no changes)'}",
        f"S3 storage:     {'✓ OK' if s['storage_ok'] else '✗ FAILED' if s['storage_ok'] is False else 'unknown'}",
        f"Org tree:       {'✓ live (Bubble API)' if s['org_tree_live'] else '✗ fallback (static file)' if s['org_tree_live'] is False else 'N/A'}",
    ]

    if report.get("playwright_errors"):
        lines += ["", "PLAYWRIGHT ERRORS (first 10)", "-" * 50]
        for e in report["playwright_errors"]:
            lines.append(f"  {e['target_id']}")
            lines.append(f"    {e['error']}")

    if report.get("target_errors"):
        lines += ["", "TARGET ERRORS (first 10)", "-" * 50]
        for e in report["target_errors"]:
            lines.append(f"  {e['target_id']}: {e['error']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level convenience functions — no-ops when no tracker is active
# ---------------------------------------------------------------------------

def record_fetch(*, url: str, target_id: str = "", playwright_tried: bool,
                 playwright_ok: "bool | None", playwright_error: "str | None",
                 final_method: str, success: bool) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_fetch(target_id or url, url, playwright_tried, playwright_ok,
                             playwright_error, final_method, success)
    except Exception:
        pass


def record_change(*, target_id: str, page_changed: bool, first_run: bool) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_change(target_id, page_changed, first_run)
    except Exception:
        pass


def record_agent(*, target_id: str, agent_ok: bool, pgvector_used: bool,
                 real_alert_count: int, error: "str | None" = None) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_agent(target_id, agent_ok, pgvector_used, real_alert_count, error)
    except Exception:
        pass


def record_doc_extraction(*, target_id: str, doc_count: int) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_doc_extraction(target_id, doc_count)
    except Exception:
        pass


def record_doc_extraction_attempt(*, target_id: str, name: str, ok: bool, error: "str | None" = None) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_doc_extraction_attempt(target_id, name, ok, error)
    except Exception:
        pass


def record_snapshot(*, target_id: str, ok: bool, error: "str | None" = None) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_snapshot(target_id, ok, error)
    except Exception:
        pass


def record_email_send(*, ok: bool, error: "str | None" = None) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_email_send(ok, error)
    except Exception:
        pass


def record_storage(*, ok: bool, error: "str | None" = None) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_storage(ok, error)
    except Exception:
        pass


def record_target_error(*, target_id: str, url: str, error: str) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_target_error(target_id, url, error)
    except Exception:
        pass


def record_org_tree(*, ok: bool) -> None:
    if _ACTIVE is None:
        return
    try:
        _ACTIVE.record_org_tree(ok)
    except Exception:
        pass
