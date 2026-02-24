"""Tests for config.run_spec: compute_run_spec, validate_run_spec, render_run_spec_summary."""

import json
import unittest
from unittest.mock import MagicMock

from config.run_spec import (
    RunSpec,
    add_snapshot_warnings,
    compute_run_spec,
    render_run_spec_summary,
    validate_run_spec,
    SNAPSHOT_MIN_CALENDAR_ITEMS,
)


class TestComputeRunSpec(unittest.TestCase):
    """compute_run_spec derives RunSpec from args and env."""

    def test_defaults_from_empty_args_and_env(self):
        args = MagicMock(
            ai_enrich=False,
            no_ai=False,
            bubble_enrich=False,
            e2e_bubble=False,
            e2e_bubble_verify=False,
            bubble_snapshot_limit=200,
        )
        env = {}
        spec = compute_run_spec(args, env)
        self.assertFalse(spec.ai_enrich_enabled)
        self.assertTrue(spec.ai_reference_fields_blocked)
        self.assertFalse(spec.bubble_enrich_enabled)
        self.assertEqual(spec.bubble_mode, "LIVE")
        self.assertFalse(spec.e2e_bubble_verify)
        self.assertEqual(spec.artifact_output_dir, "debug")

    def test_cli_overrides_env(self):
        args = MagicMock(
            ai_enrich=True,
            no_ai=False,
            bubble_enrich=True,
            e2e_bubble=False,
            e2e_bubble_verify=False,
            bubble_snapshot_limit=100,
        )
        env = {"AI_ENRICHMENT_ENABLED": "false", "ARTIFACT_OUTPUT_DIR": "out"}
        spec = compute_run_spec(args, env)
        self.assertTrue(spec.ai_enrich_enabled)
        self.assertTrue(spec.bubble_enrich_enabled)
        self.assertEqual(spec.artifact_output_dir, "out")

    def test_e2e_bubble_sets_snapshot_mode(self):
        args = MagicMock(
            ai_enrich=False,
            no_ai=False,
            bubble_enrich=False,
            e2e_bubble=True,
            e2e_bubble_verify=False,
            bubble_snapshot_limit=200,
        )
        spec = compute_run_spec(args, {})
        self.assertEqual(spec.bubble_mode, "SNAPSHOT")

    def test_e2e_bubble_verify_sets_snapshot_mode(self):
        args = MagicMock(
            ai_enrich=False,
            no_ai=False,
            bubble_enrich=False,
            e2e_bubble=False,
            e2e_bubble_verify=True,
            bubble_snapshot_limit=200,
        )
        spec = compute_run_spec(args, {})
        self.assertEqual(spec.bubble_mode, "SNAPSHOT")
        self.assertTrue(spec.e2e_bubble_verify)

    def test_env_ai_ref_blocked_false(self):
        args = MagicMock(
            ai_enrich=False,
            no_ai=False,
            bubble_enrich=False,
            e2e_bubble=False,
            e2e_bubble_verify=False,
            bubble_snapshot_limit=200,
            dry_run_bubble=True,
        )
        env = {"AI_REFERENCE_FIELDS_BLOCKED": "0"}
        spec = compute_run_spec(args, env)
        self.assertFalse(spec.ai_reference_fields_blocked)

    def test_dry_run_bubble_default_true(self):
        args = MagicMock(
            ai_enrich=False,
            no_ai=False,
            bubble_enrich=False,
            e2e_bubble=False,
            e2e_bubble_verify=False,
            bubble_snapshot_limit=200,
            dry_run_bubble=True,
        )
        spec = compute_run_spec(args, {})
        self.assertTrue(spec.dry_run_bubble)


class TestValidateRunSpec(unittest.TestCase):
    """validate_run_spec enforces data-quality constraints."""

    def test_prod_observe_missing_bubble_enrich_fails_when_fail_fast(self):
        spec = RunSpec(
            prod_observe_mode=True,
            bubble_enrich_enabled=False,
            ai_reference_fields_blocked=True,
            artifact_output_dir="debug",
            validation_fail_fast=True,
        )
        with self.assertRaises(ValueError) as ctx:
            validate_run_spec(spec, env={})
        self.assertIn("bubble_enrich_enabled", str(ctx.exception))

    def test_prod_observe_missing_bubble_enrich_warns_when_not_fail_fast(self):
        spec = RunSpec(
            prod_observe_mode=True,
            bubble_enrich_enabled=False,
            ai_reference_fields_blocked=True,
            artifact_output_dir="debug",
            validation_fail_fast=False,
        )
        validate_run_spec(spec, env={})
        self.assertTrue(any("bubble_enrich" in w for w in spec.validation_warnings))

    def test_ai_enrich_with_refs_not_blocked_warns(self):
        spec = RunSpec(
            ai_enrich_enabled=True,
            ai_reference_fields_blocked=False,
            validation_fail_fast=False,
        )
        validate_run_spec(spec, env={})
        self.assertTrue(any("ai_reference_fields_blocked" in w or "reference fields" in w for w in spec.validation_warnings))

    def test_ai_enrich_with_refs_not_blocked_fails_when_fail_fast(self):
        spec = RunSpec(
            ai_enrich_enabled=True,
            ai_reference_fields_blocked=False,
            validation_fail_fast=True,
        )
        with self.assertRaises(ValueError):
            validate_run_spec(spec, env={})

    def test_production_env_bubble_enrich_false_warns(self):
        spec = RunSpec(
            bubble_enrich_enabled=False,
            validation_fail_fast=False,
        )
        validate_run_spec(spec, env={"ENVIRONMENT": "production"})
        self.assertTrue(any("Production" in w or "bubble_enrich" in w for w in spec.validation_warnings))

    def test_normal_production_run_no_warnings(self):
        spec = RunSpec(
            ai_enrich_enabled=True,
            ai_reference_fields_blocked=True,
            bubble_enrich_enabled=True,
            prod_observe_mode=False,
            validation_fail_fast=False,
        )
        validate_run_spec(spec, env={"ENVIRONMENT": "production"})
        self.assertEqual(len(spec.validation_warnings), 0)

    def test_prod_observe_requires_dry_run_bubble(self):
        spec = RunSpec(
            prod_observe_mode=True,
            bubble_enrich_enabled=True,
            ai_reference_fields_blocked=True,
            artifact_output_dir="debug",
            dry_run_bubble=False,
            validation_fail_fast=False,
        )
        validate_run_spec(spec, env={})
        self.assertTrue(any("dry_run_bubble" in w for w in spec.validation_warnings))


class TestAddSnapshotWarnings(unittest.TestCase):
    """add_snapshot_warnings adds low calendar_items warning in SNAPSHOT mode."""

    def test_snapshot_low_calendar_items_warns(self):
        spec = RunSpec(bubble_mode="SNAPSHOT", validation_warnings=[])
        add_snapshot_warnings(spec, {"calendar_items": 50, "resources": 100, "tree_nodes": 20})
        self.assertTrue(any(str(SNAPSHOT_MIN_CALENDAR_ITEMS) in w for w in spec.validation_warnings))

    def test_snapshot_above_threshold_no_warning(self):
        spec = RunSpec(bubble_mode="SNAPSHOT", validation_warnings=[])
        add_snapshot_warnings(spec, {"calendar_items": 300, "resources": 100})
        self.assertEqual(len(spec.validation_warnings), 0)

    def test_live_mode_no_snapshot_warning(self):
        spec = RunSpec(bubble_mode="LIVE", validation_warnings=[])
        add_snapshot_warnings(spec, {"calendar_items": 10})
        self.assertEqual(len(spec.validation_warnings), 0)


class TestRenderRunSpecSummary(unittest.TestCase):
    """render_run_spec_summary produces human and JSON output."""

    def test_human_contains_key_fields(self):
        spec = RunSpec(
            ai_enrich_enabled=True,
            bubble_enrich_enabled=True,
            bubble_mode="LIVE",
        )
        human, json_str = render_run_spec_summary(spec)
        self.assertIn("--- RunSpec ---", human)
        self.assertIn("ai_enrich_enabled=True", human)
        self.assertIn("bubble_mode=LIVE", human)

    def test_json_roundtrip(self):
        spec = RunSpec(artifact_output_dir="debug")
        human, json_str = render_run_spec_summary(spec)
        data = json.loads(json_str)
        self.assertEqual(data["artifact_output_dir"], "debug")
        self.assertIn("ai_enrich_enabled", data)

    def test_snapshot_stats_included(self):
        spec = RunSpec(bubble_mode="SNAPSHOT")
        stats = {"calendar_items": 150, "resources": 80, "tree_nodes": 25}
        human, json_str = render_run_spec_summary(spec, snapshot_stats=stats)
        self.assertIn("calendar_items=150", human)
        data = json.loads(json_str)
        self.assertEqual(data["snapshot_stats"], stats)
