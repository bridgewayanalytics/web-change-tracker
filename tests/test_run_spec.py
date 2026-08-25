"""Tests for config.run_spec: compute_run_spec, validate_run_spec, render_run_spec_summary."""

import json
import unittest
from unittest.mock import MagicMock

from config.run_spec import (
    RunSpec,
    compute_run_spec,
    render_run_spec_summary,
    validate_run_spec,
)


class TestComputeRunSpec(unittest.TestCase):
    """compute_run_spec derives RunSpec from args and env."""

    def test_defaults_from_empty_args_and_env(self):
        args = MagicMock()
        env = {}
        spec = compute_run_spec(args, env)
        self.assertEqual(spec.artifact_output_dir, "debug")
        self.assertFalse(spec.s3_artifact_upload_enabled)
        self.assertFalse(spec.validation_fail_fast)

    def test_artifact_dir_from_env(self):
        args = MagicMock()
        env = {"ARTIFACT_OUTPUT_DIR": "out"}
        spec = compute_run_spec(args, env)
        self.assertEqual(spec.artifact_output_dir, "out")

    def test_s3_artifact_upload_from_env(self):
        args = MagicMock()
        env = {"S3_ARTIFACT_UPLOAD_ENABLED": "true"}
        spec = compute_run_spec(args, env)
        self.assertTrue(spec.s3_artifact_upload_enabled)

    def test_validation_fail_fast_from_env(self):
        args = MagicMock()
        env = {"RUN_SPEC_VALIDATION_FAIL_FAST": "1"}
        spec = compute_run_spec(args, env)
        self.assertTrue(spec.validation_fail_fast)


class TestValidateRunSpec(unittest.TestCase):
    """validate_run_spec runs without error on a clean spec."""

    def test_empty_spec_validates_cleanly(self):
        spec = RunSpec()
        validate_run_spec(spec, env={})
        self.assertEqual(len(spec.validation_warnings), 0)


class TestRenderRunSpecSummary(unittest.TestCase):
    """render_run_spec_summary produces human and JSON output."""

    def test_human_contains_key_fields(self):
        spec = RunSpec(
            artifact_output_dir="debug",
            s3_artifact_upload_enabled=False,
        )
        human, json_str = render_run_spec_summary(spec)
        self.assertIn("--- RunSpec ---", human)
        self.assertIn("artifact_output_dir=debug", human)

    def test_json_roundtrip(self):
        spec = RunSpec(artifact_output_dir="debug")
        human, json_str = render_run_spec_summary(spec)
        data = json.loads(json_str)
        self.assertEqual(data["artifact_output_dir"], "debug")
