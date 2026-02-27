"""
Unit tests for bubble.enrich_refs pure functions and apply_ai_classification.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bubble.enrich_refs import (
    apply_ai_classification,
    classify_resource_type_deterministic,
    infer_naic_group_path,
    strip_bbcode,
    TOPIC_AI_CONFIDENCE_THRESHOLD,
)
from bubble.enrich_refs import _parse_ai_classification_response as parse_ai_response
from bubble.enrich_refs import _build_topic_candidates
from bubble.enrich_refs import _resolve_topic_suggestion_ai
from bubble.enrich_refs import _resolve_organization_naic_node
from bubble.enrich_refs import _resolve_calendar_by_naic_group
from bubble.enrich_refs import _normalize_for_matching, _resolve_naic_group_node


class TestInferNaicGroupPath(unittest.TestCase):
    def test_list_passthrough(self):
        self.assertEqual(
            infer_naic_group_path(["NAIC", "E", "Working Groups"]),
            ["NAIC", "E", "Working Groups"],
        )

    def test_string_with_separator(self):
        self.assertEqual(
            infer_naic_group_path("NAIC › E › Working Groups › X"),
            ["NAIC", "E", "Working Groups", "X"],
        )

    def test_none(self):
        self.assertEqual(infer_naic_group_path(None), [])

    def test_empty_string(self):
        self.assertEqual(infer_naic_group_path(""), [])

    def test_empty_list(self):
        self.assertEqual(infer_naic_group_path([]), [])

    def test_strips_whitespace(self):
        self.assertEqual(
            infer_naic_group_path("  NAIC  ›  E  ›  WG  "),
            ["NAIC", "E", "WG"],
        )


class TestClassifyResourceTypeDeterministic(unittest.TestCase):
    # --- section_type mapping (highest priority) ---

    def test_section_type_docs_maps_to_publication(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Random", section_type="docs"),
            "Publication",
        )

    def test_section_type_event_links_maps_to_agenda_and_materials(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Random", section_type="event_links"),
            "Agenda & Materials",
        )

    def test_section_type_events_maps_to_agenda_and_materials(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Random", section_type="events"),
            "Agenda & Materials",
        )

    def test_section_type_takes_priority_over_keywords(self):
        result = classify_resource_type_deterministic(
            title="News Update", section_type="event_links",
        )
        self.assertEqual(result, "Agenda & Materials")

    # --- keyword heuristics ---

    def test_agenda_keywords_map_to_agenda_and_materials(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Meeting Agenda"),
            "Agenda & Materials",
        )
        self.assertEqual(
            classify_resource_type_deterministic(url="https://example.com/call_materials/x.pdf"),
            "Agenda & Materials",
        )
        self.assertEqual(
            classify_resource_type_deterministic(notes="webex link and materials"),
            "Agenda & Materials",
        )

    def test_in_the_weeds(self):
        self.assertEqual(
            classify_resource_type_deterministic(notes="Technical deep-dive and exposure draft"),
            "In the Weeds",
        )

    def test_news_keywords_map_to_newsreel(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Committee News Update"),
            "Newsreel",
        )

    def test_podcast_keywords(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Weekly Podcast Episode"),
            "Podcasts & Webinars",
        )

    def test_guidance_keywords(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="New Guidance on Risk"),
            "Existing Requirements & Guidance",
        )

    def test_proposed_keywords(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Proposed Changes to Regulation"),
            "Proposed Guidance & Support Materials",
        )

    # --- fallback ---

    def test_unknown_content_falls_back_to_other(self):
        self.assertEqual(
            classify_resource_type_deterministic(title="Random Document", url="", notes=""),
            "Other",
        )

    def test_all_empty_falls_back_to_other(self):
        self.assertEqual(classify_resource_type_deterministic(), "Other")


class TestParseAiClassificationResponse(unittest.TestCase):
    def test_valid_json(self):
        out = parse_ai_response('{"type1_node_name": "News", "topic_node_path": ["NAIC", "E"], "confidence": 0.85}')
        self.assertIsNotNone(out)
        self.assertEqual(out["type1_node_name"], "News")
        self.assertEqual(out["topic_node_path"], ["NAIC", "E"])
        self.assertEqual(out["confidence"], 0.85)

    def test_invalid_json_returns_none(self):
        self.assertIsNone(parse_ai_response("not json"))
        self.assertIsNone(parse_ai_response(""))

    def test_markdown_code_block_stripped(self):
        out = parse_ai_response('```json\n{"type1_node_name": "Agenda/Materials", "topic_node_path": [], "confidence": 0.7}\n```')
        self.assertIsNotNone(out)
        self.assertEqual(out["type1_node_name"], "Agenda/Materials")
        self.assertEqual(out["confidence"], 0.7)

    def test_confidence_clamped(self):
        out = parse_ai_response('{"type1_node_name": null, "topic_node_path": [], "confidence": 1.5}')
        self.assertEqual(out["confidence"], 0.0)

    def test_missing_confidence_defaults_zero(self):
        out = parse_ai_response('{"type1_node_name": "News", "topic_node_path": []}')
        self.assertEqual(out["confidence"], 0.0)


class TestApplyAiClassification(unittest.TestCase):
    def test_below_threshold_returns_none(self):
        t1, topic = apply_ai_classification(
            {},
            {},
            type1_nodes_by_name={"News": "n1"},
            topic_tree_id=None,
            confidence_threshold=0.7,
            ai_response={"type1_node_name": "News", "topic_node_path": [], "confidence": 0.5},
        )
        self.assertIsNone(t1)
        self.assertIsNone(topic)

    def test_above_threshold_and_type1_exists(self):
        t1, topic = apply_ai_classification(
            {},
            {},
            type1_nodes_by_name={"Agenda/Materials": "tid-123"},
            topic_tree_id=None,
            confidence_threshold=0.7,
            ai_response={"type1_node_name": "Agenda/Materials", "topic_node_path": [], "confidence": 0.83},
        )
        self.assertEqual(t1, "tid-123")
        self.assertIsNone(topic)

    def test_type1_name_not_in_map_returns_none(self):
        t1, topic = apply_ai_classification(
            {},
            {},
            type1_nodes_by_name={"News": "n1"},
            topic_tree_id=None,
            ai_response={"type1_node_name": "Other", "topic_node_path": [], "confidence": 0.9},
        )
        self.assertIsNone(t1)
        self.assertIsNone(topic)

    def test_none_ai_response_returns_none(self):
        t1, topic = apply_ai_classification(
            {},
            {},
            type1_nodes_by_name={},
            topic_tree_id=None,
            ai_response=None,
        )
        self.assertIsNone(t1)
        self.assertIsNone(topic)


# ---------------------------------------------------------------------------
# BBCode normalizer
# ---------------------------------------------------------------------------

class TestStripBbcode(unittest.TestCase):
    def test_simple_bold(self):
        self.assertEqual(strip_bbcode("[b]Investments[/b]"), "Investments")

    def test_nested_tags(self):
        self.assertEqual(strip_bbcode("[b][i]Bold Italic[/i][/b]"), "Bold Italic")

    def test_color_tag(self):
        self.assertEqual(strip_bbcode("[color=red]Warning[/color]"), "Warning")

    def test_no_bbcode_passthrough(self):
        self.assertEqual(strip_bbcode("Plain text"), "Plain text")

    def test_empty_string(self):
        self.assertEqual(strip_bbcode(""), "")

    def test_preserves_non_bbcode_brackets(self):
        self.assertEqual(strip_bbcode("array[0] is valid"), "array[0] is valid")

    def test_collapses_extra_whitespace(self):
        self.assertEqual(strip_bbcode("[b]  NAIC  [/b]  Investments"), "NAIC Investments")

    def test_strips_zero_width_spaces(self):
        self.assertEqual(strip_bbcode("NAIC\u200bInvestments"), "NAICInvestments")


# ---------------------------------------------------------------------------
# Topic candidate builder
# ---------------------------------------------------------------------------

class TestBuildTopicCandidates(unittest.TestCase):
    """Test _build_topic_candidates with a mock snapshot."""

    def _snapshot(self, nodes):
        return {
            "trees": [{"_id": "tree-1", "name": "Chronicles"}],
            "tree_nodes": nodes,
        }

    def test_basic_nodes(self):
        snap = self._snapshot([
            {"_id": "n1", "name": "Investments", "parent_tree": "tree-1"},
            {"_id": "n2", "name": "Property & Casualty", "parent_tree": "tree-1"},
        ])
        result = _build_topic_candidates("Chronicles", snap)
        self.assertIn("Investments", result)
        self.assertIn("investments", result)
        self.assertEqual(result["Investments"], "n1")
        self.assertEqual(result["Property & Casualty"], "n2")

    def test_bbcode_names_normalized(self):
        snap = self._snapshot([
            {"_id": "n1", "name": "[b]NAIC Investments[/b]", "parent_tree": "tree-1"},
        ])
        result = _build_topic_candidates("Chronicles", snap)
        self.assertIn("NAIC Investments", result)
        self.assertIn("naic investments", result)
        self.assertEqual(result["NAIC Investments"], "n1")
        # Original BBCode name should also be stored
        self.assertIn("[b]NAIC Investments[/b]", result)

    def test_nameless_nodes_skipped(self):
        snap = self._snapshot([
            {"_id": "n1", "name": None, "parent_tree": "tree-1"},
            {"_id": "n2", "name": "", "parent_tree": "tree-1"},
            {"_id": "n3", "name": "Valid", "parent_tree": "tree-1"},
        ])
        result = _build_topic_candidates("Chronicles", snap)
        self.assertNotIn("", result)
        self.assertIn("Valid", result)
        self.assertEqual(len([k for k in result if result[k] == "n1"]), 0)

    def test_wrong_tree_excluded(self):
        snap = {
            "trees": [
                {"_id": "tree-1", "name": "Chronicles"},
                {"_id": "tree-2", "name": "Other Tree"},
            ],
            "tree_nodes": [
                {"_id": "n1", "name": "In Chronicles", "parent_tree": "tree-1"},
                {"_id": "n2", "name": "In Other", "parent_tree": "tree-2"},
            ],
        }
        result = _build_topic_candidates("Chronicles", snap)
        self.assertIn("In Chronicles", result)
        self.assertNotIn("In Other", result)

    def test_empty_tree_returns_empty(self):
        snap = self._snapshot([])
        result = _build_topic_candidates("Chronicles", snap)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# Topic suggestion AI resolver
# ---------------------------------------------------------------------------

class TestResolveTopicSuggestionAi(unittest.TestCase):
    CANDIDATES = {
        "NAIC Investments": "node-inv",
        "naic investments": "node-inv",
        "Property & Casualty": "node-pc",
        "property & casualty": "node-pc",
        "Life Insurance": "node-life",
        "life insurance": "node-life",
    }

    def _resource(self, name="Test Resource", url="https://example.com", notes=""):
        return {"Name": name, "URL": url, "notes": notes}

    def _context(self, label="", org_path=None):
        return {"label": label, "org_path": org_path or []}

    def _make_chat_fn(self, topic_name, confidence):
        """Return a stub _chat_fn that returns the given AI response."""
        def stub(messages, reasoning_effort="low"):
            return {"topic_name": topic_name, "confidence": confidence}
        return stub

    def test_resolved_with_exact_match(self):
        chat_fn = self._make_chat_fn("NAIC Investments", 0.9)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=chat_fn,
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["node_id"], "node-inv")
        self.assertEqual(result["topic_name"], "NAIC Investments")
        self.assertAlmostEqual(result["confidence"], 0.9)

    def test_resolved_case_insensitive(self):
        chat_fn = self._make_chat_fn("naic investments", 0.8)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=chat_fn,
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["node_id"], "node-inv")

    def test_low_confidence_below_threshold(self):
        chat_fn = self._make_chat_fn("NAIC Investments", 0.3)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=chat_fn,
        )
        self.assertEqual(result["status"], "low_confidence")
        self.assertIsNone(result["node_id"])

    def test_ai_returns_null(self):
        chat_fn = self._make_chat_fn(None, 0)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=chat_fn,
        )
        self.assertEqual(result["status"], "ai_null")
        self.assertIsNone(result["node_id"])

    def test_ai_returns_name_not_in_candidates(self):
        chat_fn = self._make_chat_fn("Fake Topic", 0.95)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=chat_fn,
        )
        self.assertEqual(result["status"], "not_in_candidates")
        self.assertIsNone(result["node_id"])

    def test_ai_exception_returns_unresolved(self):
        def failing_fn(messages, reasoning_effort="low"):
            raise RuntimeError("API down")
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=failing_fn,
        )
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNone(result["node_id"])

    def test_empty_candidates_returns_unresolved(self):
        chat_fn = self._make_chat_fn("Anything", 0.9)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), {}, _chat_fn=chat_fn,
        )
        self.assertEqual(result["status"], "unresolved")

    def test_bbcode_name_resolved_via_stripping(self):
        candidates = {
            "NAIC Investments": "node-inv",
            "naic investments": "node-inv",
            "[b]NAIC Investments[/b]": "node-inv",
        }
        # AI returns clean name, but only BBCode version is "primary" — should still resolve
        chat_fn = self._make_chat_fn("NAIC Investments", 0.85)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), candidates, _chat_fn=chat_fn,
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["node_id"], "node-inv")

    def test_candidates_sent_included_in_result(self):
        chat_fn = self._make_chat_fn("Life Insurance", 0.9)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=chat_fn,
        )
        self.assertIsInstance(result["candidates_sent"], list)
        self.assertGreater(len(result["candidates_sent"]), 0)

    def test_confidence_clamped_to_bounds(self):
        chat_fn = self._make_chat_fn("Life Insurance", 1.5)
        result = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=chat_fn,
        )
        self.assertLessEqual(result["confidence"], 1.0)

        chat_fn2 = self._make_chat_fn("Life Insurance", -0.3)
        result2 = _resolve_topic_suggestion_ai(
            self._resource(), self._context(), self.CANDIDATES, _chat_fn=chat_fn2,
        )
        self.assertGreaterEqual(result2["confidence"], 0.0)


# ---------------------------------------------------------------------------
# Organization resolution (NAIC node under Organization tree)
# ---------------------------------------------------------------------------

class TestResolveOrganizationNaicNode(unittest.TestCase):
    """Test _resolve_organization_naic_node using snapshot mode."""

    def _snapshot(self, trees, tree_nodes):
        return {"trees": trees, "tree_nodes": tree_nodes}

    def test_resolves_naic_node_from_snapshot(self):
        snap = self._snapshot(
            trees=[{"_id": "tree-org", "name": "Organization"}],
            tree_nodes=[
                {"_id": "node-naic", "name": "NAIC", "parent_tree": "tree-org"},
                {"_id": "node-other", "name": "State Regulators", "parent_tree": "tree-org"},
            ],
        )
        nid, evidence = _resolve_organization_naic_node("Organization", snap)
        self.assertEqual(nid, "node-naic")
        self.assertEqual(evidence["resolved_id"], "node-naic")
        self.assertNotIn("failure", evidence)

    def test_resource_organization_becomes_list(self):
        """Simulate what enrich_refs does: set Organization = [node_id]."""
        snap = self._snapshot(
            trees=[{"_id": "tree-org", "name": "Organization"}],
            tree_nodes=[
                {"_id": "node-naic", "name": "NAIC", "parent_tree": "tree-org"},
            ],
        )
        nid, _ = _resolve_organization_naic_node("Organization", snap)
        resource = {"Name": "Test Doc", "Organization": "NAIC"}
        if nid:
            resource["Organization"] = [nid]
        self.assertEqual(resource["Organization"], ["node-naic"])

    def test_tree_not_found_returns_none_with_evidence(self):
        snap = self._snapshot(
            trees=[{"_id": "tree-other", "name": "Other Tree"}],
            tree_nodes=[],
        )
        nid, evidence = _resolve_organization_naic_node("Organization", snap)
        self.assertIsNone(nid)
        self.assertEqual(evidence["failure"], "tree_not_found")
        self.assertIn("Other Tree", evidence["available_trees"])

    def test_naic_node_not_found_returns_none_with_evidence(self):
        snap = self._snapshot(
            trees=[{"_id": "tree-org", "name": "Organization"}],
            tree_nodes=[
                {"_id": "node-state", "name": "State Regulators", "parent_tree": "tree-org"},
            ],
        )
        nid, evidence = _resolve_organization_naic_node("Organization", snap)
        self.assertIsNone(nid)
        self.assertEqual(evidence["failure"], "naic_node_not_found")
        self.assertIn("State Regulators", evidence["node_names_sample"])

    def test_nodes_in_different_tree_ignored(self):
        snap = self._snapshot(
            trees=[
                {"_id": "tree-org", "name": "Organization"},
                {"_id": "tree-other", "name": "Resources Types"},
            ],
            tree_nodes=[
                {"_id": "node-naic-wrong", "name": "NAIC", "parent_tree": "tree-other"},
                {"_id": "node-foo", "name": "Foo", "parent_tree": "tree-org"},
            ],
        )
        nid, evidence = _resolve_organization_naic_node("Organization", snap)
        self.assertIsNone(nid)
        self.assertEqual(evidence["failure"], "naic_node_not_found")

    def test_uses_parent_tree_field(self):
        snap = self._snapshot(
            trees=[{"_id": "tree-org", "name": "Organization"}],
            tree_nodes=[
                {"_id": "node-naic", "name": "NAIC", "parent_tree": "tree-org"},
            ],
        )
        nid, evidence = _resolve_organization_naic_node("Organization", snap)
        self.assertEqual(nid, "node-naic")

    def test_empty_snapshot(self):
        snap = self._snapshot(trees=[], tree_nodes=[])
        nid, evidence = _resolve_organization_naic_node("Organization", snap)
        self.assertIsNone(nid)
        self.assertEqual(evidence["failure"], "tree_not_found")

    def test_naic_selected_alongside_organization_publisher(self):
        """NAIC is selected even when Organization/Publisher is also present and not its parent."""
        snap = self._snapshot(
            trees=[{"_id": "tree-org", "name": "Organization"}],
            tree_nodes=[
                {"_id": "node-pub", "name": "Organization/Publisher", "parent_tree": "tree-org"},
                {"_id": "node-naic", "name": "NAIC", "parent_tree": "tree-org"},
                {"_id": "node-other", "name": "State Regulators", "parent_tree": "tree-org"},
            ],
        )
        nid, evidence = _resolve_organization_naic_node("Organization", snap)
        self.assertEqual(nid, "node-naic")
        self.assertEqual(evidence["resolved_name"], "NAIC")
        self.assertNotIn("failure", evidence)

    def test_naic_case_insensitive(self):
        """Normalized matching picks up 'naic' regardless of case."""
        snap = self._snapshot(
            trees=[{"_id": "tree-org", "name": "Organization"}],
            tree_nodes=[
                {"_id": "node-pub", "name": "Organization/Publisher", "parent_tree": "tree-org"},
                {"_id": "node-naic", "name": "Naic", "parent_tree": "tree-org"},
            ],
        )
        nid, evidence = _resolve_organization_naic_node("Organization", snap)
        self.assertEqual(nid, "node-naic")


# ---------------------------------------------------------------------------
# Related calendar items via NAIC group tree node
# ---------------------------------------------------------------------------

class TestResolveCalendarByNaicGroup(unittest.TestCase):
    """Test _resolve_calendar_by_naic_group using snapshot fixtures."""

    def _snapshot(self):
        """Snapshot with one NAIC group node and 2 calendar items in the date window."""
        return {
            "trees": [
                {"_id": "tree-org", "name": "Organization"},
            ],
            "tree_nodes": [
                {"_id": "node-naic", "name": "NAIC", "parent_tree": "tree-org"},
                {"_id": "node-e", "name": "E Committee", "parent_tree": "tree-org",
                 "parent": "node-naic"},
                {"_id": "node-tf", "name": "Capital Adequacy (E) Task Force", "parent_tree": "tree-org",
                 "parent": "node-e"},
            ],
            "calendar_items": [
                {
                    "_id": "cal-1",
                    "title": "Capital Adequacy Task Force - Spring Meeting",
                    "date": "2026-03-15",
                    "NAIC Group (tree node)": "node-tf",
                },
                {
                    "_id": "cal-2",
                    "title": "Capital Adequacy Task Force - Follow-up",
                    "date": "2026-03-17",
                    "NAIC Group (tree node)": "node-tf",
                },
                {
                    "_id": "cal-unrelated",
                    "title": "Other Meeting",
                    "date": "2026-03-16",
                    "NAIC Group (tree node)": "node-e",
                },
            ],
        }

    def _context(self, org_path=None, label=""):
        return {"org_path": org_path or [], "label": label}

    def test_both_items_linked_with_date(self):
        snap = self._snapshot()
        ctx = self._context(
            org_path=["NAIC", "E Committee"],
            label="Capital Adequacy (E) Task Force",
        )
        selected_ids, detail, status, evidence = _resolve_calendar_by_naic_group(
            ctx, "Organization", date_iso="2026-03-16",
            window_days=7, no_date_cap=3, bubble_snapshot=snap,
        )
        self.assertIn("cal-1", selected_ids)
        self.assertIn("cal-2", selected_ids)
        self.assertNotIn("cal-unrelated", selected_ids)
        self.assertIn(status, ("RESOLVED", "MULTI_RESOLVED"))
        self.assertEqual(evidence["group_node_id"], "node-tf")
        self.assertEqual(evidence["date_used"], "2026-03-16")

    def test_unrelated_group_excluded(self):
        snap = self._snapshot()
        ctx = self._context(
            org_path=["NAIC", "E Committee"],
            label="Capital Adequacy (E) Task Force",
        )
        selected_ids, _, _, _ = _resolve_calendar_by_naic_group(
            ctx, "Organization", date_iso="2026-03-16",
            window_days=7, no_date_cap=3, bubble_snapshot=snap,
        )
        self.assertNotIn("cal-unrelated", selected_ids)

    def test_no_date_caps_results(self):
        snap = self._snapshot()
        # Add more future calendar items
        for j in range(5):
            snap["calendar_items"].append({
                "_id": f"cal-future-{j}",
                "title": f"Future meeting {j}",
                "date": f"2099-06-{10 + j:02d}",
                "NAIC Group (tree node)": "node-tf",
            })
        ctx = self._context(
            org_path=["NAIC", "E Committee"],
            label="Capital Adequacy (E) Task Force",
        )
        selected_ids, _, _, evidence = _resolve_calendar_by_naic_group(
            ctx, "Organization", date_iso=None,
            window_days=7, no_date_cap=3, bubble_snapshot=snap,
        )
        self.assertLessEqual(len(selected_ids), 3)
        self.assertIn("no date", evidence.get("note", ""))

    def test_empty_path_unresolved(self):
        snap = self._snapshot()
        ctx = self._context(org_path=[], label="")
        selected_ids, _, status, evidence = _resolve_calendar_by_naic_group(
            ctx, "Organization", date_iso="2026-03-16",
            window_days=7, no_date_cap=3, bubble_snapshot=snap,
        )
        self.assertEqual(selected_ids, [])
        self.assertEqual(status, "UNRESOLVED")
        self.assertEqual(evidence["failure"], "empty_path")

    def test_group_not_found_unresolved(self):
        snap = self._snapshot()
        ctx = self._context(
            org_path=["NAIC", "Nonexistent Committee"],
            label="Nonexistent Task Force",
        )
        selected_ids, _, status, evidence = _resolve_calendar_by_naic_group(
            ctx, "Organization", date_iso="2026-03-16",
            window_days=7, no_date_cap=3, bubble_snapshot=snap,
        )
        self.assertEqual(selected_ids, [])
        self.assertEqual(status, "UNRESOLVED")
        self.assertIn(evidence["failure"], ("group_node_not_found", "no_match"))

    def test_date_outside_window_excluded(self):
        snap = self._snapshot()
        ctx = self._context(
            org_path=["NAIC", "E Committee"],
            label="Capital Adequacy (E) Task Force",
        )
        selected_ids, _, _, _ = _resolve_calendar_by_naic_group(
            ctx, "Organization", date_iso="2026-01-01",
            window_days=7, no_date_cap=3, bubble_snapshot=snap,
        )
        self.assertEqual(selected_ids, [])

    def test_evidence_contains_debug_keys(self):
        snap = self._snapshot()
        ctx = self._context(
            org_path=["NAIC", "E Committee"],
            label="Capital Adequacy (E) Task Force",
        )
        _, _, _, evidence = _resolve_calendar_by_naic_group(
            ctx, "Organization", date_iso="2026-03-16",
            window_days=7, no_date_cap=3, bubble_snapshot=snap,
        )
        self.assertIn("group_node_id", evidence)
        self.assertIn("date_used", evidence)
        self.assertIn("candidate_count", evidence)
        self.assertIn("chosen_ids", evidence)
        self.assertIn("method", evidence)
        self.assertEqual(evidence["method"], "naic_group")


# ---------------------------------------------------------------------------
# Normalized NAIC group name matching
# ---------------------------------------------------------------------------

class TestNormalizeForMatching(unittest.TestCase):
    def test_strips_parenthesised_codes(self):
        self.assertEqual(_normalize_for_matching("Blanks (E) Working Group"), "blanks working group")

    def test_strips_multiple_parens(self):
        self.assertEqual(
            _normalize_for_matching("Life Actuarial (A) Task Force (LATF)"),
            "life actuarial task force",
        )

    def test_removes_punctuation(self):
        self.assertEqual(_normalize_for_matching("Risk-Based Capital"), "riskbased capital")

    def test_collapses_whitespace(self):
        self.assertEqual(_normalize_for_matching("  Some   Group  "), "some group")

    def test_empty_string(self):
        self.assertEqual(_normalize_for_matching(""), "")

    def test_plain_name_lowered(self):
        self.assertEqual(_normalize_for_matching("NAIC"), "naic")


class TestResolveNaicGroupNode(unittest.TestCase):
    """Test that _resolve_naic_group_node matches labels to Bubble node names with (E) etc."""

    def _snapshot(self, nodes):
        return {
            "trees": [{"_id": "tree-org", "name": "Organization"}],
            "tree_nodes": nodes,
        }

    def test_blanks_working_group_resolves(self):
        snap = self._snapshot([
            {"_id": "n1", "name": "Blanks (E) Working Group", "parent_tree": "tree-org"},
            {"_id": "n2", "name": "Capital Adequacy (E) Task Force", "parent_tree": "tree-org"},
        ])
        nid, evidence = _resolve_naic_group_node(
            "Organization", ["NAIC", "E", "Working Groups", "Blanks Working Group"], snap,
        )
        self.assertEqual(nid, "n1")
        self.assertEqual(evidence["match_type"], "exact_normalized")
        self.assertEqual(evidence["original_label"], "Blanks Working Group")

    def test_life_actuarial_task_force_resolves(self):
        snap = self._snapshot([
            {"_id": "n1", "name": "Life Actuarial (A) Task Force", "parent_tree": "tree-org"},
            {"_id": "n2", "name": "Other Node", "parent_tree": "tree-org"},
        ])
        nid, evidence = _resolve_naic_group_node(
            "Organization", ["NAIC", "A", "Life Actuarial Task Force"], snap,
        )
        self.assertEqual(nid, "n1")
        self.assertEqual(evidence["chosen_raw_name"], "Life Actuarial (A) Task Force")

    def test_exact_match_preferred_over_substring(self):
        snap = self._snapshot([
            {"_id": "n-short", "name": "Capital (E) Task Force", "parent_tree": "tree-org"},
            {"_id": "n-long", "name": "Capital (E) Task Force Extended", "parent_tree": "tree-org"},
        ])
        nid, evidence = _resolve_naic_group_node(
            "Organization", ["Capital Task Force"], snap,
        )
        self.assertEqual(nid, "n-short")
        self.assertEqual(evidence["match_type"], "exact_normalized")

    def test_no_match_returns_none_with_evidence(self):
        snap = self._snapshot([
            {"_id": "n1", "name": "Some Other Group", "parent_tree": "tree-org"},
        ])
        nid, evidence = _resolve_naic_group_node(
            "Organization", ["Nonexistent Group"], snap,
        )
        self.assertIsNone(nid)
        self.assertEqual(evidence["failure"], "no_match")

    def test_empty_path_returns_none(self):
        snap = self._snapshot([])
        nid, evidence = _resolve_naic_group_node("Organization", [], snap)
        self.assertIsNone(nid)
        self.assertEqual(evidence["failure"], "empty_path")

    def test_substring_fallback_when_no_exact_match(self):
        snap = self._snapshot([
            {"_id": "n1", "name": "Reinsurance (E) Task Force", "parent_tree": "tree-org"},
        ])
        nid, evidence = _resolve_naic_group_node(
            "Organization", ["Reinsurance Task Force"], snap,
        )
        self.assertEqual(nid, "n1")
        self.assertEqual(evidence["match_type"], "exact_normalized")

    def test_evidence_includes_debug_fields(self):
        snap = self._snapshot([
            {"_id": "n1", "name": "Test (X) Group", "parent_tree": "tree-org"},
        ])
        nid, evidence = _resolve_naic_group_node(
            "Organization", ["Test Group"], snap,
        )
        self.assertIn("original_label", evidence)
        self.assertIn("normalized_label", evidence)
        self.assertIn("candidate_matches", evidence)
        self.assertIn("chosen_node_id", evidence)

    def test_nameless_nodes_skipped(self):
        snap = self._snapshot([
            {"_id": "n1", "name": None, "parent_tree": "tree-org"},
            {"_id": "n2", "name": "Valid (E) Group", "parent_tree": "tree-org"},
        ])
        nid, evidence = _resolve_naic_group_node(
            "Organization", ["Valid Group"], snap,
        )
        self.assertEqual(nid, "n2")

    def test_wrong_tree_nodes_excluded(self):
        snap = {
            "trees": [
                {"_id": "tree-org", "name": "Organization"},
                {"_id": "tree-other", "name": "Other"},
            ],
            "tree_nodes": [
                {"_id": "n1", "name": "My Group", "parent_tree": "tree-other"},
            ],
        }
        nid, evidence = _resolve_naic_group_node(
            "Organization", ["My Group"], snap,
        )
        self.assertIsNone(nid)
        self.assertEqual(evidence["failure"], "no_nodes_loaded")


if __name__ == "__main__":
    unittest.main()
