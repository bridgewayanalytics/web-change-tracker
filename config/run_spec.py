"""
Production run spec: single source of truth for runtime flags.
Derived from CLI args + env with precedence: CLI > env > defaults.
Validated for data quality; summary emitted to logs and email.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

# Default artifact dir (relative to cwd)
DEFAULT_ARTIFACT_DIR = "debug"
# Min calendar_items in snapshot below which we warn (matching quality)
SNAPSHOT_MIN_CALENDAR_ITEMS = 200


@dataclass
class RunSpec:
    """Explicit, validated runtime configuration. Single source of truth for behavior."""

    ai_enrich_enabled: bool = False
    ai_reference_fields_blocked: bool = True  # True = AI must not set ref fields (current behavior)
    bubble_enrich_enabled: bool = False
    bubble_mode: Literal["LIVE", "SNAPSHOT"] = "LIVE"
    bubble_snapshot_path: str | None = None
    calendar_link_tolerance_days: int = 7
    calendar_lookback_days: int = 30  # if used for calendar API
    max_calendar_api_results: int = 100  # if used
    artifact_output_dir: str = DEFAULT_ARTIFACT_DIR
    s3_artifact_upload_enabled: bool = False
    prod_observe_mode: bool = False
    e2e_bubble_verify: bool = False
    dry_run_bubble: bool = True  # No Bubble write API calls (prod must stay true)
    pdf_meeting_meta_enabled: bool = False  # Extract PDF meeting metadata (default ON when prod_observe_mode)
    # Set after bubble_healthcheck() when bubble_enrich_enabled and bubble_mode=LIVE
    bubble_live_ok: bool | None = None  # True=healthcheck passed, False=failed, None=not run
    # Internal: fail fast on validation (vs collect warnings for email)
    validation_fail_fast: bool = False
    # Collected after validate_run_spec (HIGH severity warnings for email header)
    validation_warnings: list[str] = field(default_factory=list, repr=False)


def _bool_from_env(env: dict[str, str], name: str, default: bool) -> bool:
    v = (env.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes")


def _int_from_env(env: dict[str, str], name: str, default: int) -> int:
    try:
        return int((env.get(name) or "").strip() or default)
    except ValueError:
        return default


def compute_run_spec(args: Any, env: dict[str, str] | None = None) -> RunSpec:
    """
    Derive RunSpec from CLI args and environment.
    Precedence: CLI > env > defaults. env defaults to os.environ.
    """
    env = env if env is not None else dict(os.environ)
    e = lambda k, d: (env.get(k) or "").strip() or d

    # CLI overrides
    ai_enrich = getattr(args, "ai_enrich", False)
    no_ai = getattr(args, "no_ai", False)
    bubble_enrich = getattr(args, "bubble_enrich", False)
    e2e_bubble = getattr(args, "e2e_bubble", False)
    e2e_verify = getattr(args, "e2e_bubble_verify", False)
    bubble_snapshot_limit = getattr(args, "bubble_snapshot_limit", 200)

    # Env defaults
    ai_enrichment_enabled_env = (e("AI_ENRICHMENT_ENABLED", "") or "").strip().lower() in ("1", "true", "yes")
    if not bubble_enrich and ai_enrichment_enabled_env:
        bubble_enrich = True  # existing behavior: AI_ENRICHMENT_ENABLED turns on bubble_enrich

    ai_enrich_enabled = ai_enrich or (ai_enrichment_enabled_env and not no_ai)
    # ai_reference_fields_blocked: True = we block AI from writing ref fields (current ai_enrichment behavior)
    ai_ref_blocked = _bool_from_env(env, "AI_REFERENCE_FIELDS_BLOCKED", True)
    bubble_enrich_enabled = bubble_enrich

    if e2e_bubble or e2e_verify:
        bubble_mode: Literal["LIVE", "SNAPSHOT"] = "SNAPSHOT"
        bubble_snapshot_path = None  # built at runtime, not from path
    else:
        bubble_mode = "LIVE"
        bubble_snapshot_path = e("BUBBLE_SNAPSHOT_PATH", "") or None

    calendar_link_tolerance_days = _int_from_env(env, "CALENDAR_LINK_TOLERANCE_DAYS", 7)
    calendar_lookback_days = _int_from_env(env, "CALENDAR_LOOKBACK_DAYS", 30)
    max_calendar_api_results = _int_from_env(env, "MAX_CALENDAR_API_RESULTS", 100)

    artifact_output_dir = e("ARTIFACT_OUTPUT_DIR", DEFAULT_ARTIFACT_DIR)
    s3_artifact_upload_enabled = _bool_from_env(env, "S3_ARTIFACT_UPLOAD_ENABLED", False)
    prod_observe_mode = _bool_from_env(env, "PROD_OBSERVE_MODE", False)
    validation_fail_fast = _bool_from_env(env, "RUN_SPEC_VALIDATION_FAIL_FAST", False)
    dry_run_bubble = getattr(args, "dry_run_bubble", True)
    if "DRY_RUN_BUBBLE" in env:
        dry_run_bubble = _bool_from_env(env, "DRY_RUN_BUBBLE", True)
    # PDF meeting meta: default ON when prod_observe_mode, else OFF unless --pdf-meeting-meta
    no_pdf_meeting_meta = getattr(args, "no_pdf_meeting_meta", False)
    pdf_meeting_meta = getattr(args, "pdf_meeting_meta", False)
    if no_pdf_meeting_meta:
        pdf_meeting_meta_enabled = False
    elif pdf_meeting_meta:
        pdf_meeting_meta_enabled = True
    else:
        pdf_meeting_meta_enabled = prod_observe_mode

    return RunSpec(
        ai_enrich_enabled=ai_enrich_enabled,
        ai_reference_fields_blocked=ai_ref_blocked,
        bubble_enrich_enabled=bubble_enrich_enabled,
        bubble_mode=bubble_mode,
        bubble_snapshot_path=bubble_snapshot_path or None,
        bubble_live_ok=None,
        calendar_link_tolerance_days=calendar_link_tolerance_days,
        calendar_lookback_days=calendar_lookback_days,
        max_calendar_api_results=max_calendar_api_results,
        artifact_output_dir=artifact_output_dir,
        s3_artifact_upload_enabled=s3_artifact_upload_enabled,
        prod_observe_mode=prod_observe_mode,
        e2e_bubble_verify=e2e_verify,
        dry_run_bubble=dry_run_bubble,
        pdf_meeting_meta_enabled=pdf_meeting_meta_enabled,
        validation_fail_fast=validation_fail_fast,
    )


def validate_run_spec(spec: RunSpec, env: dict[str, str] | None = None) -> RunSpec:
    """
    Enforce data-quality constraints. Mutates spec.validation_warnings.
    If spec.validation_fail_fast and a constraint fails, raises ValueError.
    env: optional override for ENVIRONMENT lookup (e.g. in tests).
    """
    warnings: list[str] = []
    _env = env if env is not None else dict(os.environ)

    def fail(msg: str) -> None:
        if spec.validation_fail_fast:
            raise ValueError(msg)
        warnings.append(msg)

    # Prod observe mode constraints
    if spec.prod_observe_mode:
        if not spec.bubble_enrich_enabled:
            fail("[HIGH] prod_observe_mode requires bubble_enrich_enabled=true")
        if not spec.ai_reference_fields_blocked:
            fail("[HIGH] prod_observe_mode requires ai_reference_fields_blocked=true")
        if spec.artifact_output_dir in ("", "off") or spec.artifact_output_dir is None:
            fail("[HIGH] prod_observe_mode requires debug artifacts enabled (artifact_output_dir set)")
        if not spec.dry_run_bubble:
            fail("[HIGH] prod_observe_mode requires dry_run_bubble=true (no Bubble write API calls)")
        if spec.bubble_mode != "LIVE" and not spec.e2e_bubble_verify:
            # E2E snapshot without verify is allowed for tests
            pass  # no hard requirement

    # AI enrich with refs not blocked => HIGH warning
    if spec.ai_enrich_enabled and not spec.ai_reference_fields_blocked:
        fail("[HIGH] ai_enrich_enabled=true but ai_reference_fields_blocked=false: reference fields may be overwritten by AI")

    # Production env (from ENVIRONMENT) and bubble_enrich disabled => HIGH warning
    env_val = (_env.get("ENVIRONMENT") or "").strip().lower()
    is_production = env_val in ("production", "prod")
    if is_production and not spec.bubble_enrich_enabled:
        fail("[HIGH] Production environment with bubble_enrich_enabled=false: refs unresolved (low quality)")

    spec.validation_warnings = list(warnings)
    for w in warnings:
        log.warning("RunSpec validation: %s", w)
    return spec


def add_snapshot_warnings(spec: RunSpec, snapshot_stats: dict[str, int]) -> None:
    """
    Append SNAPSHOT-mode warnings to spec (e.g. low calendar_items).
    Call after snapshot is built so counts are available.
    """
    if spec.bubble_mode != "SNAPSHOT":
        return
    n_cal = snapshot_stats.get("calendar_items", 0)
    if n_cal < SNAPSHOT_MIN_CALENDAR_ITEMS:
        w = f"[WARN] SNAPSHOT mode with calendar_items={n_cal} < {SNAPSHOT_MIN_CALENDAR_ITEMS}: matching quality may be poor"
        spec.validation_warnings.append(w)
        log.warning("RunSpec: %s", w)


def upload_artifacts_to_s3(artifact_dir: str, run_timestamp: int) -> list[str]:
    """
    Upload reference_resolution_report.json and verify_report.json to S3 when
    ARTIFACT_BUCKET (and optionally ARTIFACT_PREFIX) are set. Returns list of S3 URIs uploaded.
    """
    bucket = (os.environ.get("ARTIFACT_BUCKET") or "").strip()
    if not bucket:
        return []
    prefix = (os.environ.get("ARTIFACT_PREFIX") or "artifacts/").strip()
    if not prefix.endswith("/"):
        prefix += "/"
    root = Path(artifact_dir)
    names = ["reference_resolution_report.json", "verify_report.json", "pdf_meeting_meta.json"]
    uris: list[str] = []
    try:
        import boto3
        from datetime import datetime, timezone
        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("s3", region_name=region)
        dt = datetime.fromtimestamp(run_timestamp, tz=timezone.utc)
        key_prefix = f"{prefix}{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/run-{run_timestamp}"
        for name in names:
            path = root / name
            if not path.exists():
                continue
            key = f"{key_prefix}/{name}"
            body = path.read_bytes()
            client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
            uris.append(f"s3://{bucket}/{key}")
        for u in uris:
            log.info("Uploaded artifact: %s", u)
    except Exception as e:
        log.warning("S3 artifact upload failed: %s", e)
    return uris


def render_debug_metric_summary(
    snapshot_stats: dict[str, int] | None,
    resolution_by_field: dict[str, dict[str, int]],
    bubble_live_ok: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Build debug metric summary: Bubble snapshot counts, per-field resolved/unresolved, calendar-too-small warning.
    bubble_live_ok: result of startup healthcheck when bubble_mode=LIVE (None=not run).
    Returns (human_text, dict_for_json). Log and include in email header so matching quality is visible.
    """
    lines = ["--- Debug metric summary ---"]
    out: dict[str, Any] = {"resolution_by_field": resolution_by_field, "bubble_live_ok": bubble_live_ok}

    # Aggregate Bubble HTTP request metrics for this run (if client module is loaded)
    bubble_request_stats: dict[str, Any] | None = None
    try:
        from bubble.client import get_bubble_request_stats
        bubble_request_stats = get_bubble_request_stats()
    except Exception:
        bubble_request_stats = None

    if snapshot_stats:
        n_cal = snapshot_stats.get("calendar_items", 0)
        n_res = snapshot_stats.get("resources", 0)
        n_trees = snapshot_stats.get("tree_nodes", 0)
        lines.append(f"Bubble snapshot: calendar_items={n_cal}, resources={n_res}, tree_nodes={n_trees}")
        out["snapshot"] = snapshot_stats
        if n_cal < SNAPSHOT_MIN_CALENDAR_ITEMS:
            w = f"Calendar item candidate set too small ({n_cal} < {SNAPSHOT_MIN_CALENDAR_ITEMS}): matching quality may be poor"
            lines.append(f"[WARN] {w}")
            out["calendar_candidates_warning"] = w
            log.warning("Debug metric: %s", w)
    else:
        lines.append("Bubble snapshot: not loaded (LIVE mode or no E2E)")
        out["snapshot"] = None

    if bubble_request_stats is not None:
        total = bubble_request_stats.get("total", 0)
        failures = bubble_request_stats.get("failures", 0)
        successes = bubble_request_stats.get("successes", max(0, total - failures))
        lines.append(f"Bubble API requests: total={total}, successes={successes}, failures={failures}")
        out["bubble_requests"] = {
            "total": total,
            "successes": successes,
            "failures": failures,
        }
        if failures:
            log.warning("Debug metric: Bubble API failures detected (failures=%s)", failures)

    if bubble_live_ok is not None:
        lines.append(f"bubble_live_ok={bubble_live_ok}")
    from bubble.reference_resolution import format_resolution_summary
    lines.append(format_resolution_summary(resolution_by_field))
    lines.append("---")
    return "\n".join(lines), out


def render_run_spec_summary(
    run_spec: RunSpec,
    snapshot_stats: dict[str, int] | None = None,
) -> tuple[str, str]:
    """
    Returns (human_readable_header, json_form).
    Both are suitable for logs and for email top.
    snapshot_stats optional: e.g. {"calendar_items": N, "resources": M, "tree_nodes": K}.
    """
    d = asdict(run_spec)
    # Exclude validation_warnings from JSON if you want; we include for visibility
    if snapshot_stats is not None:
        d["snapshot_stats"] = snapshot_stats
    json_form = json.dumps(d, indent=2)

    lines = [
        "--- RunSpec ---",
        f"ai_enrich_enabled={run_spec.ai_enrich_enabled}",
        f"ai_reference_fields_blocked={run_spec.ai_reference_fields_blocked}",
        f"bubble_enrich_enabled={run_spec.bubble_enrich_enabled}",
        f"bubble_mode={run_spec.bubble_mode}",
        f"bubble_live_ok={run_spec.bubble_live_ok}",
        f"dry_run_bubble={run_spec.dry_run_bubble}",
        f"e2e_bubble_verify={run_spec.e2e_bubble_verify}",
        f"artifact_output_dir={run_spec.artifact_output_dir}",
        f"s3_artifact_upload_enabled={run_spec.s3_artifact_upload_enabled}",
        f"prod_observe_mode={run_spec.prod_observe_mode}",
        f"pdf_meeting_meta_enabled={run_spec.pdf_meeting_meta_enabled}",
    ]
    if snapshot_stats:
        lines.append(f"snapshot: calendar_items={snapshot_stats.get('calendar_items', 0)}, resources={snapshot_stats.get('resources', 0)}, tree_nodes={snapshot_stats.get('tree_nodes', 0)}")
        n_cal = snapshot_stats.get("calendar_items", 0)
        if run_spec.bubble_mode == "SNAPSHOT" and n_cal < SNAPSHOT_MIN_CALENDAR_ITEMS:
            lines.append(f"[WARN] SNAPSHOT mode with calendar_items={n_cal} < {SNAPSHOT_MIN_CALENDAR_ITEMS}: matching quality may be poor")
    if run_spec.validation_warnings:
        lines.append("Validation warnings:")
        for w in run_spec.validation_warnings:
            lines.append(f"  %s" % w)
    lines.append("---")
    human = "\n".join(lines)
    return human, json_form
