"""
Tests for agenda item matching, PDF agenda signals, and enhanced topic suggestion.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bubble.enrich_refs import (
    _extract_ref_numbers_from_name,
    _normalize_ref,
    _extract_all_normalized_refs,
    _extract_ref_prefix,
    _ref_prefix_matches_group,
    _is_placeholder_topic,
    _extract_title_search_keywords,
    _score_agenda_item_match,
    _parse_calendar_title_topics,
    _fuzzy_match_topic_to_candidates,
    _tokenize_for_matching,
    _resolve_agenda_items_for_resource,
    _resolve_topic_enhanced,
    _get_agenda_item_candidates,
)
from scrape.pdf_agenda_signals import (
    extract_agenda_signals,
    PdfAgendaSignals,
    signals_to_dict,
)


# ---------------------------------------------------------------------------
# PDF Agenda Signals
# ---------------------------------------------------------------------------


class TestExtractAgendaSignals(unittest.TestCase):
    def test_empty_text(self):
        signals = extract_agenda_signals("")
        self.assertEqual(signals.ref_numbers, ())
        self.assertEqual(signals.numbered_items, ())
        self.assertIsNone(signals.group_name_hint)
        self.assertFalse(signals.has_agenda_header)
        self.assertEqual(signals.structure_type, "none")

    def test_formal_agenda_with_ref_numbers(self):
        text = """
STATUTORY ACCOUNTING PRINCIPLES (E) WORKING GROUP

AGENDA

1. Ref #2024-16: Repacks and Derivative Instruments
2. Ref #2024-22: ASU 2024-01, Scope Application
3. Ref #2022-14: Tax Credits Project
"""
        signals = extract_agenda_signals(text)
        self.assertIn("2024-16", signals.ref_numbers)
        self.assertIn("2024-22", signals.ref_numbers)
        self.assertIn("2022-14", signals.ref_numbers)
        self.assertEqual(len(signals.ref_numbers), 3)
        self.assertTrue(signals.has_agenda_header)
        self.assertEqual(signals.structure_type, "formal_agenda")
        self.assertIsNotNone(signals.group_name_hint)
        self.assertIn("WORKING GROUP", signals.group_name_hint)

    def test_numbered_list_without_agenda_header(self):
        text = """
CAPITAL ADEQUACY (E) TASK FORCE

1. Roll call
2. CLOs and ABS update
3. Tax Credit Structures review
4. Short-Term Investments discussion
"""
        signals = extract_agenda_signals(text)
        self.assertTrue(len(signals.numbered_items) >= 3)
        self.assertIn("TASK FORCE", signals.group_name_hint)

    def test_sapwg_ref_pattern(self):
        text = "Discussion of SAPWG#2024-04 regarding bond definitions."
        signals = extract_agenda_signals(text)
        self.assertIn("2024-04", signals.ref_numbers)

    def test_ref_with_en_dash(self):
        text = "Ref #2024\u201316: Derivative instruments"
        signals = extract_agenda_signals(text)
        # En-dash should be normalized to hyphen
        self.assertIn("2024-16", signals.ref_numbers)

    def test_no_structure(self):
        text = "This is a general analysis of market conditions in 2024."
        signals = extract_agenda_signals(text)
        self.assertEqual(signals.structure_type, "none")

    def test_signals_to_dict(self):
        signals = PdfAgendaSignals(
            ref_numbers=("2024-16",),
            numbered_items=("Item 1", "Item 2"),
            group_name_hint="SAPWG",
            has_agenda_header=True,
            structure_type="formal_agenda",
        )
        d = signals_to_dict(signals)
        self.assertEqual(d["ref_numbers"], ["2024-16"])
        self.assertEqual(d["structure_type"], "formal_agenda")
        self.assertTrue(d["has_agenda_header"])


# ---------------------------------------------------------------------------
# Ref Number Extraction from Resource Name
# ---------------------------------------------------------------------------


class TestExtractRefNumbersFromName(unittest.TestCase):
    def test_sapwg_ref(self):
        refs = _extract_ref_numbers_from_name("Bond Definition – Debt Securities (SAPWG#2024-01)")
        self.assertIn("2024-01", refs)

    def test_ref_hash(self):
        refs = _extract_ref_numbers_from_name("Discussion of #2024-16")
        self.assertIn("2024-16", refs)

    def test_ref_keyword(self):
        refs = _extract_ref_numbers_from_name("See Ref #2024-22 for details")
        self.assertIn("2024-22", refs)

    def test_no_ref(self):
        refs = _extract_ref_numbers_from_name("Meeting Materials - Spring 2024")
        self.assertEqual(refs, [])

    def test_multiple_refs(self):
        refs = _extract_ref_numbers_from_name("SAPWG#2024-01 and Ref #2024-16")
        self.assertEqual(len(refs), 2)


class TestNormalizeRef(unittest.TestCase):
    def test_strips_group_prefix(self):
        self.assertEqual(_normalize_ref("SAPWG#2024-04"), "2024-04")

    def test_normalizes_en_dash(self):
        self.assertEqual(_normalize_ref("2024\u201316"), "2024-16")

    def test_plain_ref(self):
        self.assertEqual(_normalize_ref("2024-22"), "2024-22")


class TestExtractAllNormalizedRefs(unittest.TestCase):
    def test_single_prefixed(self):
        self.assertEqual(_extract_all_normalized_refs("RBC-IRE-WG#2025-22"), ["2025-22"])

    def test_suffixed_ref(self):
        self.assertEqual(_extract_all_normalized_refs("Proposal 2025-22-IRE"), ["2025-22"])

    def test_suffixed_mod(self):
        self.assertEqual(_extract_all_normalized_refs("2025-22-IRE MOD"), ["2025-22"])

    def test_multi_ref_field(self):
        refs = _extract_all_normalized_refs("SAPWG#2019-21 and LRBCWG#2024-8")
        self.assertIn("2019-21", refs)
        self.assertIn("2024-8", refs)
        self.assertEqual(len(refs), 2)

    def test_non_numeric_ref(self):
        # "IR9" doesn't match YYYY-NN pattern
        self.assertEqual(_extract_all_normalized_refs("RBC-IRE-WG#IR9"), [])

    def test_empty(self):
        self.assertEqual(_extract_all_normalized_refs(""), [])


class TestIsPlaceholderTopic(unittest.TestCase):
    def test_calendar_events_placeholder(self):
        self.assertTrue(_is_placeholder_topic("Calendar Events with no Topic"))

    def test_real_topic(self):
        self.assertFalse(_is_placeholder_topic("Collateralized Loan Obligations (CLOs)"))

    def test_no_topic(self):
        self.assertTrue(_is_placeholder_topic("No Topic"))

    def test_empty(self):
        self.assertFalse(_is_placeholder_topic(""))


# ---------------------------------------------------------------------------
# Ref Prefix Extraction and Group Matching
# ---------------------------------------------------------------------------


class TestExtractTitleSearchKeywords(unittest.TestCase):
    def test_strips_meeting_noise(self):
        """Meeting/committee/date words should be stripped."""
        kws = _extract_title_search_keywords(
            "Capital Adequacy (E) Task Force November 18, 2024 Meeting Agenda"
        )
        # Should NOT include: Task, Force, Meeting, Agenda, November, 2024
        lower_kws = [k.lower() for k in kws]
        self.assertNotIn("meeting", lower_kws)
        self.assertNotIn("agenda", lower_kws)
        self.assertNotIn("november", lower_kws)
        self.assertNotIn("task", lower_kws)
        self.assertNotIn("force", lower_kws)
        # Should include distinctive words
        self.assertIn("capital", lower_kws)
        self.assertIn("adequacy", lower_kws)

    def test_preserves_distinctive_terms(self):
        kws = _extract_title_search_keywords(
            "Collateralized Loan Obligation (CLO) Exposure Analysis"
        )
        lower_kws = [k.lower() for k in kws]
        self.assertIn("collateralized", lower_kws)

    def test_insurance_topic(self):
        kws = _extract_title_search_keywords(
            "Instructions and Guidance on the Application of the Prudent Person Principle"
        )
        lower_kws = [k.lower() for k in kws]
        self.assertIn("instructions", lower_kws)
        self.assertIn("application", lower_kws)
        self.assertIn("principle", lower_kws)

    def test_returns_max_3(self):
        kws = _extract_title_search_keywords(
            "Extremely Long Title With Many Distinctive Important Keywords Here"
        )
        self.assertLessEqual(len(kws), 3)

    def test_empty(self):
        self.assertEqual(_extract_title_search_keywords(""), [])

    def test_removes_ref_patterns(self):
        kws = _extract_title_search_keywords(
            "Repack & Derivative Investments SAPWG#2024-16"
        )
        lower_kws = [k.lower() for k in kws]
        self.assertIn("investments", lower_kws)
        self.assertIn("derivative", lower_kws)
        # Ref pattern should be stripped
        self.assertNotIn("2024-16", lower_kws)


class TestExtractRefPrefix(unittest.TestCase):
    def test_rbc_ire_wg(self):
        self.assertEqual(_extract_ref_prefix("RBC-IRE-WG#2025-22"), "rbc-ire-wg")

    def test_sapwg(self):
        self.assertEqual(_extract_ref_prefix("SAPWG#2025-22"), "sapwg")

    def test_bwg(self):
        self.assertEqual(_extract_ref_prefix("BWG#2023-12 Modified"), "bwg")

    def test_bare_ref(self):
        self.assertEqual(_extract_ref_prefix("2025-22"), "")

    def test_empty(self):
        self.assertEqual(_extract_ref_prefix(""), "")

    def test_proposal_prefix(self):
        # "Proposal 2025-22-IRE" — no group prefix before the number
        self.assertEqual(_extract_ref_prefix("Proposal 2025-22-IRE"), "")

    def test_text_ref(self):
        self.assertEqual(_extract_ref_prefix("Academy Structured RBC Project"), "")


class TestRefPrefixMatchesGroup(unittest.TestCase):
    def test_rbc_ire_matches_rbc_group(self):
        self.assertTrue(_ref_prefix_matches_group(
            "rbc-ire-wg",
            "Risk Based Capital Investment Risk and Evaluation (E) Working Group",
        ))

    def test_sapwg_does_not_match_rbc_group(self):
        self.assertFalse(_ref_prefix_matches_group(
            "sapwg",
            "Risk Based Capital Investment Risk and Evaluation (E) Working Group",
        ))

    def test_sapwg_matches_sapwg_group(self):
        self.assertTrue(_ref_prefix_matches_group(
            "sapwg",
            "Statutory Accounting Principles (E) Working Group",
        ))

    def test_empty_prefix_is_compatible(self):
        self.assertTrue(_ref_prefix_matches_group("", "Any Group Name"))

    def test_empty_group_is_compatible(self):
        self.assertTrue(_ref_prefix_matches_group("sapwg", ""))


# ---------------------------------------------------------------------------
# Agenda Item Scoring
# ---------------------------------------------------------------------------


class TestScoreAgendaItemMatch(unittest.TestCase):
    """Tier 1 scoring is ref-only. Title/content matching is handled by the LLM tier."""

    def _make_item(self, ba_title="", ref="", item_id="item1"):
        return {"_id": item_id, "BA title": ba_title, "BA Ref #": ref}

    def test_ref_match_from_name_scores_high(self):
        item = self._make_item(ref="SAPWG#2024-04")
        score, ev = _score_agenda_item_match(
            item, "Bond Definition (SAPWG#2024-04)", None,
        )
        self.assertGreaterEqual(score, 3.0)
        self.assertEqual(ev["ref_match_source"], "resource_name")

    def test_ref_match_from_pdf_signals(self):
        item = self._make_item(ref="Ref #2024-16")
        pdf_signals = {"ref_numbers": ["2024-16", "2024-22"], "numbered_items": []}
        score, ev = _score_agenda_item_match(item, "Meeting Materials", pdf_signals)
        self.assertGreaterEqual(score, 3.0)
        self.assertEqual(ev["ref_match_source"], "pdf_text")

    def test_no_ref_means_zero_score(self):
        """Items without numeric refs score 0 — they go to the LLM tier."""
        item = self._make_item(ba_title="Cryptocurrency Investments", ref="")
        score, ev = _score_agenda_item_match(item, "Cryptocurrency Update", None)
        self.assertEqual(score, 0.0)

    def test_no_match(self):
        item = self._make_item(ba_title="Tax Credit Investments", ref="SAPWG#2024-99")
        score, ev = _score_agenda_item_match(item, "Unrelated Document", None)
        self.assertEqual(score, 0.0)

    def test_ref_match_suffixed_bubble_ref(self):
        item = self._make_item(ref="RBC-IRE-WG#2025-22")
        pdf_signals = {"ref_numbers": ["2025-22", "2025-27"], "numbered_items": []}
        score, ev = _score_agenda_item_match(item, "Meeting Materials", pdf_signals)
        self.assertGreaterEqual(score, 3.0)
        self.assertEqual(ev["ref_match_source"], "pdf_text")

    def test_ref_match_proposal_suffix(self):
        item = self._make_item(ref="Proposal 2025-22-IRE")
        pdf_signals = {"ref_numbers": ["2025-22"], "numbered_items": []}
        score, ev = _score_agenda_item_match(item, "Meeting Materials", pdf_signals)
        self.assertGreaterEqual(score, 3.0)

    def test_ref_match_multi_ref_bubble_field(self):
        item = self._make_item(ref="SAPWG#2019-21 and LRBCWG#2024-8")
        pdf_signals = {"ref_numbers": ["2019-21"], "numbered_items": []}
        score, ev = _score_agenda_item_match(item, "Meeting Materials", pdf_signals)
        self.assertGreaterEqual(score, 3.0)

    def test_cross_group_penalty_applied(self):
        """SAPWG ref should be penalized when group is RBC-IRE."""
        item = self._make_item(ref="SAPWG#2025-22")
        item["__retrieval_source"] = "ref_fallback"
        pdf_signals = {"ref_numbers": ["2025-22"], "numbered_items": []}
        score, ev = _score_agenda_item_match(
            item, "Meeting Materials", pdf_signals,
            naic_group_name="Risk Based Capital Investment Risk and Evaluation (E) Working Group",
        )
        self.assertLess(score, 1.5)
        self.assertTrue(ev.get("cross_group_penalty"))
        self.assertFalse(ev.get("group_match"))

    def test_same_group_no_penalty(self):
        """RBC-IRE-WG ref should NOT be penalized when group is RBC-IRE."""
        item = self._make_item(ref="RBC-IRE-WG#2025-22")
        item["__retrieval_source"] = "ref_fallback"
        pdf_signals = {"ref_numbers": ["2025-22"], "numbered_items": []}
        score, ev = _score_agenda_item_match(
            item, "Meeting Materials", pdf_signals,
            naic_group_name="Risk Based Capital Investment Risk and Evaluation (E) Working Group",
        )
        self.assertGreaterEqual(score, 3.0)
        self.assertTrue(ev.get("group_match"))
        self.assertNotIn("cross_group_penalty", ev)

    def test_group_scoped_items_never_penalized(self):
        """Items from group-scoped retrieval should never get cross-group penalty."""
        item = self._make_item(ref="SAPWG#2025-22")
        pdf_signals = {"ref_numbers": ["2025-22"], "numbered_items": []}
        score, ev = _score_agenda_item_match(
            item, "Meeting Materials", pdf_signals,
            naic_group_name="Risk Based Capital Investment Risk and Evaluation (E) Working Group",
        )
        self.assertGreaterEqual(score, 3.0)
        self.assertNotIn("cross_group_penalty", ev)


# ---------------------------------------------------------------------------
# Calendar Title Topic Parsing
# ---------------------------------------------------------------------------


class TestParseCalendarTitleTopics(unittest.TestCase):
    def test_standard_format(self):
        topics = _parse_calendar_title_topics(
            "NAIC SAPWG | Cryptocurrency; Bond Definition and Reporting; CECL"
        )
        self.assertEqual(len(topics), 3)
        self.assertEqual(topics[0], "Cryptocurrency")
        self.assertEqual(topics[1], "Bond Definition and Reporting")

    def test_single_topic(self):
        topics = _parse_calendar_title_topics(
            "NAIC E-Committee | Investment Oversight Framework"
        )
        self.assertEqual(topics, ["Investment Oversight Framework"])

    def test_no_pipe(self):
        topics = _parse_calendar_title_topics("NAIC SAPWG Meeting")
        self.assertEqual(topics, [])

    def test_empty(self):
        topics = _parse_calendar_title_topics("")
        self.assertEqual(topics, [])


class TestFuzzyMatchTopicToCandidates(unittest.TestCase):
    def setUp(self):
        self.candidates = {
            "Cryptocurrency": "id1",
            "cryptocurrency": "id1",
            "Bond Definition and Reporting": "id2",
            "bond definition and reporting": "id2",
            "CECL": "id3",
            "cecl": "id3",
            "CLOs and ABS": "id4",
            "clos and abs": "id4",
        }

    def test_exact_match(self):
        matches = _fuzzy_match_topic_to_candidates(["Cryptocurrency"], self.candidates)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1], "id1")

    def test_case_insensitive(self):
        matches = _fuzzy_match_topic_to_candidates(["cryptocurrency"], self.candidates)
        self.assertEqual(len(matches), 1)

    def test_multiple_matches(self):
        matches = _fuzzy_match_topic_to_candidates(
            ["Cryptocurrency", "CECL"], self.candidates
        )
        self.assertEqual(len(matches), 2)

    def test_no_match(self):
        matches = _fuzzy_match_topic_to_candidates(
            ["Nonexistent Topic"], self.candidates
        )
        self.assertEqual(matches, [])


# ---------------------------------------------------------------------------
# Agenda Item Candidate Retrieval (Snapshot)
# ---------------------------------------------------------------------------


class TestGetAgendaItemCandidates(unittest.TestCase):
    def test_snapshot_filter_by_discussed_at(self):
        snapshot = {
            "agenda_items": [
                {"_id": "a1", "Discussed at": "group1", "BA title": "Item 1", "BA Ref #": ""},
                {"_id": "a2", "Discussed at": "group2", "BA title": "Item 2", "BA Ref #": ""},
                {"_id": "a3", "Discussed at list": ["group1"], "BA title": "Item 3", "BA Ref #": ""},
            ]
        }
        candidates, source = _get_agenda_item_candidates("group1", snapshot)
        ids = [c["_id"] for c in candidates]
        self.assertIn("a1", ids)
        self.assertIn("a3", ids)
        self.assertNotIn("a2", ids)
        self.assertEqual(source, "group_scoped")

    def test_empty_snapshot(self):
        snapshot = {"agenda_items": []}
        candidates, source = _get_agenda_item_candidates("group1", snapshot)
        self.assertEqual(candidates, [])
        self.assertEqual(source, "none")

    def test_no_group_id(self):
        candidates, source = _get_agenda_item_candidates("", None)
        self.assertEqual(candidates, [])
        self.assertEqual(source, "none")

    def test_ref_fallback_when_group_has_no_ref_overlap(self):
        """Items with null Discussed at should be found via ref fallback."""
        snapshot = {
            "agenda_items": [
                {"_id": "a1", "Discussed at": "group1", "BA title": "Unrelated Item", "BA Ref #": "SAPWG#2024-99"},
                {"_id": "a2", "BA title": "CLO Structure", "BA Ref #": "RBC-IRE-WG#2025-22"},
                {"_id": "a3", "BA title": "Another Item", "BA Ref #": "SAPWG#2025-27"},
            ]
        }
        candidates, source = _get_agenda_item_candidates(
            "group1", snapshot, fallback_ref_numbers=["2025-22", "2025-27"]
        )
        ids = [c["_id"] for c in candidates]
        # Should include group-scoped a1 + ref-fallback a2 and a3
        self.assertIn("a1", ids)
        self.assertIn("a2", ids)
        self.assertIn("a3", ids)
        self.assertEqual(source, "ref_fallback")

    def test_ref_fallback_skipped_when_group_has_ref_overlap(self):
        """If group-scoped candidates already cover the refs, skip fallback."""
        snapshot = {
            "agenda_items": [
                {"_id": "a1", "Discussed at": "group1", "BA title": "CLO Structure", "BA Ref #": "RBC-IRE-WG#2025-22"},
                {"_id": "a2", "BA title": "Orphaned Item", "BA Ref #": "SAPWG#2025-22"},
            ]
        }
        candidates, source = _get_agenda_item_candidates(
            "group1", snapshot, fallback_ref_numbers=["2025-22"]
        )
        ids = [c["_id"] for c in candidates]
        # a1 is group-scoped and covers 2025-22, so no fallback needed
        self.assertIn("a1", ids)
        self.assertNotIn("a2", ids)
        self.assertEqual(source, "group_scoped")

    def test_title_fallback_finds_unlinked_items(self):
        """Items with no Discussed at or refs should be found via title keyword search."""
        snapshot = {
            "agenda_items": [
                {"_id": "a1", "Discussed at": "group1", "BA title": "Unrelated Thing", "BA Ref #": "SAPWG#2024-99"},
                {"_id": "a2", "BA title": "Collateral Loans", "BA Ref #": "Collateral Loans"},
                {"_id": "a3", "BA title": "Tax Credit Structures", "BA Ref #": "CA#2024-26"},
            ]
        }
        candidates, source = _get_agenda_item_candidates(
            "group1", snapshot,
            resource_name="Collateral Loans - Meeting Materials",
        )
        ids = [c["_id"] for c in candidates]
        self.assertIn("a1", ids)  # group-scoped
        self.assertIn("a2", ids)  # title fallback — "Collateral" matches
        self.assertEqual(source, "title_fallback")

    def test_title_fallback_supplements_group_scoped(self):
        """Title fallback supplements group-scoped results to find sparse items."""
        snapshot = {
            "agenda_items": [
                {"_id": "a1", "Discussed at": "group1", "BA title": "Collateral Loans", "BA Ref #": "CL-01"},
                {"_id": "a2", "BA title": "Also Collateral Loans", "BA Ref #": ""},
            ]
        }
        candidates, source = _get_agenda_item_candidates(
            "group1", snapshot,
            resource_name="Collateral Loans Analysis",
        )
        ids = [c["_id"] for c in candidates]
        self.assertIn("a1", ids)   # group-scoped
        self.assertIn("a2", ids)   # title fallback supplements
        self.assertEqual(source, "title_fallback")

    def test_title_fallback_tags_retrieval_source(self):
        """Title fallback items should be tagged with __retrieval_source."""
        snapshot = {
            "agenda_items": [
                {"_id": "a1", "BA title": "Prudent Person Principle Application", "BA Ref #": "BMA_PPP"},
            ]
        }
        candidates, source = _get_agenda_item_candidates(
            "group1", snapshot,
            resource_name="Instructions on the Prudent Person Principle",
        )
        self.assertEqual(source, "title_fallback")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["__retrieval_source"], "title_fallback")


# ---------------------------------------------------------------------------
# Full Agenda Item Matching
# ---------------------------------------------------------------------------


class TestResolveAgendaItemsForResource(unittest.TestCase):
    def test_ref_match(self):
        snapshot = {
            "agenda_items": [
                {
                    "_id": "a1",
                    "Discussed at": "group1",
                    "BA title": "Bond Definition",
                    "BA Ref #": "SAPWG#2024-04",
                    "Topics": ["topic1"],
                },
                {
                    "_id": "a2",
                    "Discussed at": "group1",
                    "BA title": "Tax Credits",
                    "BA Ref #": "SAPWG#2024-10",
                    "Topics": ["topic2"],
                },
            ]
        }
        resource = {"Name": "Bond Definition (SAPWG#2024-04)", "URL": "https://example.com/doc.pdf"}
        result = _resolve_agenda_items_for_resource(
            resource, {}, "group1", snapshot
        )
        self.assertIn("a1", result["matched_ids"])
        self.assertNotIn("a2", result["matched_ids"])
        self.assertEqual(result["method"], "ref_match")
        self.assertIn("topic1", result["inherited_topic_ids"])

    def test_no_candidates(self):
        snapshot = {"agenda_items": []}
        resource = {"Name": "Something"}
        result = _resolve_agenda_items_for_resource(resource, {}, "group1", snapshot)
        self.assertEqual(result["matched_ids"], [])
        self.assertEqual(result["method"], "none")

    def test_ref_fallback_matches_unlinked_items(self):
        """Items with no Discussed at should be found via ref fallback from PDF signals."""
        snapshot = {
            "agenda_items": [
                {
                    "_id": "a1",
                    "Discussed at": "group1",
                    "BA title": "Unrelated Item",
                    "BA Ref #": "SAPWG#2024-99",
                    "Topics": [],
                },
                {
                    "_id": "a2",
                    # No Discussed at — would be missed by group-scoped query
                    "BA title": "CLO RBC Structure",
                    "BA Ref #": "RBC-IRE-WG#2025-22",
                    "Topics": ["topic_clo"],
                },
            ]
        }
        resource = {
            "Name": "Meeting Materials",
            "__pdf_agenda_signals": {
                "ref_numbers": ["2025-22"],
                "numbered_items": [],
            },
        }
        result = _resolve_agenda_items_for_resource(
            resource, {}, "group1", snapshot
        )
        self.assertIn("a2", result["matched_ids"])
        self.assertEqual(result["method"], "ref_match")
        self.assertEqual(result["retrieval_source"], "ref_fallback")
        self.assertIn("topic_clo", result["inherited_topic_ids"])

    def test_two_tier_ref_plus_ai(self):
        """Tier 1 matches by ref, Tier 2 (AI) matches remaining group-scoped items."""
        snapshot = {
            "agenda_items": [
                {
                    "_id": "a1",
                    "Discussed at": "group1",
                    "BA title": "CLO RBC Structure",
                    "BA Ref #": "RBC-IRE-WG#2025-22",
                    "Topics": ["topic_clo"],
                },
                {
                    "_id": "a2",
                    "Discussed at": "group1",
                    "BA title": "Academy Structured Securities Project",
                    "BA Ref #": "Academy Project",
                    "Topics": ["topic_academy"],
                },
            ]
        }
        resource = {
            "Name": "Meeting Materials",
            "__pdf_agenda_signals": {
                "ref_numbers": ["2025-22"],
                "numbered_items": [
                    "Hear an Update from the American Academy of Actuaries",
                ],
            },
        }

        # Mock AI returns a2 as a match
        def mock_chat_fn(messages, **kwargs):
            return {"matches": [1], "confidence": 0.85}

        result = _resolve_agenda_items_for_resource(
            resource, {"label": "Test Group"}, "group1", snapshot,
            use_ai=True, _chat_fn=mock_chat_fn,
        )
        # a1 matched by ref (Tier 1), a2 matched by AI (Tier 2)
        self.assertIn("a1", result["matched_ids"])
        self.assertIn("a2", result["matched_ids"])
        self.assertEqual(result["method"], "ref_match+ai")
        self.assertTrue(result["ai_used"])
        self.assertIn("topic_clo", result["inherited_topic_ids"])
        self.assertIn("topic_academy", result["inherited_topic_ids"])

    def test_ai_only_when_no_ref_matches(self):
        """When no ref matches, only the LLM tier runs."""
        snapshot = {
            "agenda_items": [
                {
                    "_id": "a1",
                    "Discussed at": "group1",
                    "BA title": "Academy Project",
                    "BA Ref #": "Academy Project",
                    "Topics": ["topic_a"],
                },
            ]
        }
        resource = {"Name": "Meeting Materials"}

        def mock_chat_fn(messages, **kwargs):
            return {"matches": [1], "confidence": 0.9}

        result = _resolve_agenda_items_for_resource(
            resource, {"label": "Test Group"}, "group1", snapshot,
            use_ai=True, _chat_fn=mock_chat_fn,
        )
        self.assertIn("a1", result["matched_ids"])
        self.assertEqual(result["method"], "ai")
        self.assertTrue(result["ai_used"])

    def test_no_ai_when_disabled(self):
        """With use_ai=False, non-ref items are not matched."""
        snapshot = {
            "agenda_items": [
                {
                    "_id": "a1",
                    "Discussed at": "group1",
                    "BA title": "Academy Project",
                    "BA Ref #": "Academy Project",
                    "Topics": [],
                },
            ]
        }
        resource = {"Name": "Meeting Materials"}
        result = _resolve_agenda_items_for_resource(
            resource, {}, "group1", snapshot, use_ai=False,
        )
        self.assertEqual(result["matched_ids"], [])
        self.assertEqual(result["method"], "none")

    def test_title_fallback_with_ai_matches_sparse_items(self):
        """Sparse agenda items (no Discussed at, no numeric refs) found via title + AI."""
        snapshot = {
            "agenda_items": [
                {
                    "_id": "a1",
                    # No Discussed at, no numeric ref — invisible to group + ref retrieval
                    "BA title": "BMA Request for Comment on Prudent Person Principle",
                    "BA Ref #": "BMA_PPP",
                    "Topics": ["topic_bma"],
                },
                {
                    "_id": "a2",
                    "BA title": "Unrelated Banking Regulation",
                    "BA Ref #": "FED-01",
                    "Topics": ["topic_fed"],
                },
            ]
        }
        resource = {
            "Name": "Instructions and Guidance on the Application of the Prudent Person Principle",
        }

        def mock_chat_fn(messages, **kwargs):
            return {"matches": [1], "confidence": 0.9}

        result = _resolve_agenda_items_for_resource(
            resource, {"label": "Bermuda Monetary Authority"}, "group1", snapshot,
            use_ai=True, _chat_fn=mock_chat_fn,
        )
        self.assertIn("a1", result["matched_ids"])
        self.assertTrue(result["ai_used"])
        self.assertIn("topic_bma", result["inherited_topic_ids"])

    def test_no_group_id(self):
        result = _resolve_agenda_items_for_resource({"Name": "X"}, {}, None, None)
        self.assertEqual(result["matched_ids"], [])


# ---------------------------------------------------------------------------
# Enhanced Topic Suggestion
# ---------------------------------------------------------------------------


class TestResolveTopicEnhanced(unittest.TestCase):
    def setUp(self):
        self.topic_candidates = {
            "Bond Definition and Reporting": "topic_bond",
            "bond definition and reporting": "topic_bond",
            "Cryptocurrency": "topic_crypto",
            "cryptocurrency": "topic_crypto",
            "Tax Credits": "topic_tax",
            "tax credits": "topic_tax",
            "Calendar Events with no Topic": "topic_placeholder",
            "calendar events with no topic": "topic_placeholder",
        }

    def test_inherit_from_agenda_items(self):
        resource = {"Name": "Bond Definition Doc"}
        agenda_result = {
            "matched_ids": ["a1"],
            "inherited_topic_ids": ["topic_bond"],
            "method": "ref_match",
        }
        result = _resolve_topic_enhanced(
            resource, {}, self.topic_candidates,
            matched_agenda_items_result=agenda_result,
        )
        self.assertEqual(result["topic_id"], "topic_bond")
        self.assertEqual(result["source"], "agenda_item_inheritance")

    def test_calendar_title_parse_single_topic(self):
        resource = {"Name": "Some Doc", "Related calendar items": ["cal1"]}
        calendar_payload = [
            {"_id": "cal1", "title": "NAIC SAPWG | Cryptocurrency"},
        ]
        result = _resolve_topic_enhanced(
            resource, {}, self.topic_candidates,
            matched_agenda_items_result=None,
            calendar_payload=calendar_payload,
            linked_calendar_ids=["cal1"],
        )
        self.assertEqual(result["topic_id"], "topic_crypto")
        self.assertEqual(result["source"], "calendar_title_parse")

    def test_calendar_title_real_plus_placeholder(self):
        """One real topic + a placeholder should resolve to the real topic."""
        resource = {"Name": "Some Doc", "Related calendar items": ["cal1"]}
        calendar_payload = [
            {"_id": "cal1", "title": "NAIC RBC-IRE-WG | Cryptocurrency; Calendar Events with no Topic"},
        ]
        result = _resolve_topic_enhanced(
            resource, {}, self.topic_candidates,
            matched_agenda_items_result=None,
            calendar_payload=calendar_payload,
            linked_calendar_ids=["cal1"],
            use_ai=False,
        )
        self.assertEqual(result["topic_id"], "topic_crypto")
        self.assertEqual(result["source"], "calendar_title_parse")

    def test_calendar_title_multiple_topics_no_auto_assign(self):
        resource = {"Name": "Some Doc", "Related calendar items": ["cal1"]}
        calendar_payload = [
            {"_id": "cal1", "title": "NAIC SAPWG | Cryptocurrency; Tax Credits"},
        ]
        result = _resolve_topic_enhanced(
            resource, {}, self.topic_candidates,
            matched_agenda_items_result=None,
            calendar_payload=calendar_payload,
            linked_calendar_ids=["cal1"],
            use_ai=False,
        )
        # Multiple topics found — should not auto-assign
        self.assertIsNone(result["topic_id"])
        self.assertEqual(result["source"], "unresolved")
        self.assertEqual(len(result["calendar_title_topics"]), 2)

    def test_ai_fallback(self):
        resource = {"Name": "Some Doc", "URL": "https://example.com", "notes": "", "parent": ""}
        mock_ai_result = {
            "topic_name": "Cryptocurrency",
            "node_id": "topic_crypto",
            "confidence": 0.85,
            "status": "resolved",
            "candidates_sent": [],
        }

        def mock_chat_fn(messages, **kwargs):
            return {"topic_name": "Cryptocurrency", "confidence": 0.85}

        result = _resolve_topic_enhanced(
            resource, {"label": "test", "org_path": []}, self.topic_candidates,
            matched_agenda_items_result=None,
            use_ai=True,
            _chat_fn=mock_chat_fn,
        )
        self.assertEqual(result["topic_id"], "topic_crypto")
        self.assertEqual(result["source"], "ai_classification")

    def test_no_signals(self):
        resource = {"Name": "Unknown Document"}
        result = _resolve_topic_enhanced(
            resource, {}, self.topic_candidates,
            matched_agenda_items_result=None,
            use_ai=False,
        )
        self.assertIsNone(result["topic_id"])
        self.assertEqual(result["source"], "unresolved")


# ---------------------------------------------------------------------------
# Tokenize for matching
# ---------------------------------------------------------------------------


class TestTokenizeForMatching(unittest.TestCase):
    def test_basic(self):
        tokens = _tokenize_for_matching("Bond Definition and Reporting")
        self.assertIn("bond", tokens)
        self.assertIn("definition", tokens)
        self.assertIn("reporting", tokens)

    def test_short_words_excluded(self):
        tokens = _tokenize_for_matching("A B CD")
        self.assertNotIn("a", tokens)
        self.assertNotIn("b", tokens)
        self.assertIn("cd", tokens)

    def test_empty(self):
        self.assertEqual(_tokenize_for_matching(""), set())


if __name__ == "__main__":
    unittest.main()
