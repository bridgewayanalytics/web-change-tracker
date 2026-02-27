"""
Unit tests for bubble.lookups — tree node helpers with live-API field schema.

Validates:
- name=None nodes are skipped safely in node maps and path traversal
- parent_tree (not "Tree") is the FK field used for filtering
- name (lowercase) is the display field
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bubble.lookups import (
    _tree_node_name,
    _tree_node_id,
    _tree_node_parent,
    find_tree_node_by_path,
    find_tree_nodes_fuzzy,
    clear_lookups_cache,
)


TREE_ID = "tree-abc"

MOCK_NODES = [
    {
        "_id": "node-blank",
        "parent_tree": TREE_ID,
        "level": 0,
    },
    {
        "_id": "node-org",
        "name": "Organization/Publisher",
        "parent_tree": TREE_ID,
    },
    {
        "_id": "node-naic",
        "name": "NAIC",
        "parent_tree": TREE_ID,
    },
    {
        "_id": "node-e-committee",
        "name": "Financial Condition (E) Committee",
        "parent_tree": TREE_ID,
        "parent": "node-naic",
    },
    {
        "_id": "node-reinsurance",
        "name": "Reinsurance (E) Task Force",
        "parent_tree": TREE_ID,
        "parent": "node-e-committee",
    },
    {
        "_id": "node-other-tree",
        "name": "Unrelated Node",
        "parent_tree": "other-tree-id",
    },
]


def _mock_list_all(type_name, constraints=None, page_size=100):
    """Simulate client.list_all for Tree node type with parent_tree constraint."""
    if constraints:
        for c in constraints:
            if c.get("key") == "parent_tree" and c.get("constraint_type") == "equals":
                tid = c["value"]
                return iter([n for n in MOCK_NODES if n.get("parent_tree") == tid])
    return iter(MOCK_NODES)


class TestTreeNodeName(unittest.TestCase):
    def test_lowercase_name(self):
        self.assertEqual(_tree_node_name({"name": "NAIC"}), "NAIC")

    def test_uppercase_Name_fallback(self):
        self.assertEqual(_tree_node_name({"Name": "Fallback"}), "Fallback")

    def test_none_name_returns_empty(self):
        self.assertEqual(_tree_node_name({}), "")
        self.assertEqual(_tree_node_name({"name": None}), "")

    def test_strips_whitespace(self):
        self.assertEqual(_tree_node_name({"name": "  NAIC  "}), "NAIC")


class TestTreeNodeId(unittest.TestCase):
    def test_underscore_id(self):
        self.assertEqual(_tree_node_id({"_id": "abc"}), "abc")

    def test_id_fallback(self):
        self.assertEqual(_tree_node_id({"id": "xyz"}), "xyz")

    def test_missing_returns_none(self):
        self.assertIsNone(_tree_node_id({}))


class TestTreeNodeParent(unittest.TestCase):
    def test_string_parent(self):
        self.assertEqual(_tree_node_parent({"parent": "p1"}), "p1")

    def test_dict_parent(self):
        self.assertEqual(_tree_node_parent({"parent": {"_id": "p2"}}), "p2")

    def test_parent_node_key(self):
        self.assertEqual(_tree_node_parent({"parent_node": "p3"}), "p3")

    def test_missing_returns_none(self):
        self.assertIsNone(_tree_node_parent({}))


class TestFindTreeNodeByPath(unittest.TestCase):
    def setUp(self):
        clear_lookups_cache()
        self._patcher = patch("bubble.lookups._client")
        mock_client_fn = self._patcher.start()
        self.mock_client = MagicMock()
        self.mock_client.list_all = _mock_list_all
        mock_client_fn.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()
        clear_lookups_cache()

    def test_finds_root_node(self):
        node = find_tree_node_by_path(TREE_ID, ["NAIC"])
        self.assertIsNotNone(node)
        self.assertEqual(node["_id"], "node-naic")

    def test_finds_nested_node(self):
        node = find_tree_node_by_path(TREE_ID, ["NAIC", "Financial Condition (E) Committee"])
        self.assertIsNotNone(node)
        self.assertEqual(node["_id"], "node-e-committee")

    def test_finds_deeply_nested(self):
        node = find_tree_node_by_path(
            TREE_ID, ["NAIC", "Financial Condition (E) Committee", "Reinsurance (E) Task Force"]
        )
        self.assertIsNotNone(node)
        self.assertEqual(node["_id"], "node-reinsurance")

    def test_missing_segment_returns_none(self):
        node = find_tree_node_by_path(TREE_ID, ["NAIC", "Nonexistent"])
        self.assertIsNone(node)

    def test_empty_path_returns_none(self):
        self.assertIsNone(find_tree_node_by_path(TREE_ID, []))

    def test_name_none_node_skipped(self):
        """The node with name=None (node-blank) must not interfere with path traversal."""
        node = find_tree_node_by_path(TREE_ID, ["NAIC"])
        self.assertIsNotNone(node)
        self.assertEqual(node["_id"], "node-naic")

    def test_case_insensitive(self):
        node = find_tree_node_by_path(TREE_ID, ["naic"])
        self.assertIsNotNone(node)
        self.assertEqual(node["_id"], "node-naic")

    def test_does_not_cross_trees(self):
        """Nodes from a different tree (parent_tree != TREE_ID) must not appear."""
        node = find_tree_node_by_path(TREE_ID, ["Unrelated Node"])
        self.assertIsNone(node)


class TestFindTreeNodesFuzzy(unittest.TestCase):
    def setUp(self):
        clear_lookups_cache()
        self._patcher = patch("bubble.lookups._client")
        mock_client_fn = self._patcher.start()
        self.mock_client = MagicMock()
        self.mock_client.list_all = _mock_list_all
        mock_client_fn.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()
        clear_lookups_cache()

    def test_fuzzy_finds_match(self):
        results = find_tree_nodes_fuzzy(TREE_ID, "Reinsurance")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["_id"], "node-reinsurance")

    def test_name_none_node_not_in_results(self):
        results = find_tree_nodes_fuzzy(TREE_ID, "")
        self.assertEqual(results, [])
        all_results = find_tree_nodes_fuzzy(TREE_ID, "a")
        ids = [r["_id"] for r in all_results]
        self.assertNotIn("node-blank", ids)


class TestNodeMapSkipsNone(unittest.TestCase):
    """Integration-style: _get_all_type1_node_names from enrich_refs skips name=None."""

    def setUp(self):
        clear_lookups_cache()
        self._patcher = patch("bubble.lookups._client")
        mock_client_fn = self._patcher.start()
        self.mock_client = MagicMock()
        self.mock_client.list_all = _mock_list_all
        self.mock_client.search = MagicMock(return_value={
            "results": [{"_id": TREE_ID, "Name": "TestTree"}],
        })
        mock_client_fn.return_value = self.mock_client

    def tearDown(self):
        self._patcher.stop()
        clear_lookups_cache()

    def test_get_all_type1_node_names_skips_nameless(self):
        from bubble.enrich_refs import _get_all_type1_node_names
        pairs = _get_all_type1_node_names("TestTree")
        names = [name for name, _ in pairs]
        self.assertNotIn("", names)
        self.assertTrue(all(name for name, _ in pairs))
        self.assertIn("NAIC", names)

    def test_build_type1_nodes_by_name_no_exception(self):
        from bubble.enrich_refs import _build_type1_nodes_by_name
        result = _build_type1_nodes_by_name("TestTree")
        self.assertIsInstance(result, dict)
        self.assertNotIn("", result)


if __name__ == "__main__":
    unittest.main()
