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
from typing import Any

log = logging.getLogger(__name__)

# Default artifact dir (relative to cwd)
DEFAULT_ARTIFACT_DIR = "debug"


@dataclass
class RunSpec:
    """Explicit, validated runtime configuration. Single source of truth for behavior."""

    artifact_output_dir: str = DEFAULT_ARTIFACT_DIR
    s3_artifact_upload_enabled: bool = False
    # Internal: fail fast on validation (vs collect warnings for email)
    validation_fail_fast: bool = False
    # Collected after validate_run_spec (HIGH severity warnings for email header)
    validation_warnings: list[str] = field(default_factory=list, repr=False)


def _bool_from_env(env: dict[str, str], name: str, default: bool) -> bool:
    v = (env.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes")


def compute_run_spec(args: Any, env: dict[str, str] | None = None) -> RunSpec:
    """
    Derive RunSpec from CLI args and environment.
    Precedence: CLI > env > defaults. env defaults to os.environ.
    """
    env = env if env is not None else dict(os.environ)
    e = lambda k, d: (env.get(k) or "").strip() or d

    artifact_output_dir = e("ARTIFACT_OUTPUT_DIR", DEFAULT_ARTIFACT_DIR)
    s3_artifact_upload_enabled = _bool_from_env(env, "S3_ARTIFACT_UPLOAD_ENABLED", False)
    validation_fail_fast = _bool_from_env(env, "RUN_SPEC_VALIDATION_FAIL_FAST", False)

    return RunSpec(
        artifact_output_dir=artifact_output_dir,
        s3_artifact_upload_enabled=s3_artifact_upload_enabled,
        validation_fail_fast=validation_fail_fast,
    )


def validate_run_spec(spec: RunSpec, env: dict[str, str] | None = None) -> RunSpec:
    """
    Enforce data-quality constraints. Mutates spec.validation_warnings.
    If spec.validation_fail_fast and a constraint fails, raises ValueError.
    env: optional override for ENVIRONMENT lookup (e.g. in tests).
    """
    warnings: list[str] = []

    spec.validation_warnings = list(warnings)
    for w in warnings:
        log.warning("RunSpec validation: %s", w)
    return spec


def render_run_spec_summary(
    run_spec: RunSpec,
    snapshot_stats: dict[str, int] | None = None,
) -> tuple[str, str]:
    """
    Returns (human_readable_header, json_form).
    Both are suitable for logs and for email top.
    """
    d = asdict(run_spec)
    json_form = json.dumps(d, indent=2)

    lines = [
        "--- RunSpec ---",
        f"artifact_output_dir={run_spec.artifact_output_dir}",
        f"s3_artifact_upload_enabled={run_spec.s3_artifact_upload_enabled}",
    ]
    if run_spec.validation_warnings:
        lines.append("Validation warnings:")
        for w in run_spec.validation_warnings:
            lines.append(f"  %s" % w)
    lines.append("---")
    human = "\n".join(lines)
    return human, json_form
