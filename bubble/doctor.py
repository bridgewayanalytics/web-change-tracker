"""
CLI utility for Bubble read-only diagnostics.
Usage: python -m bubble.doctor <command> [options]

Commands:
  list-trees              List all trees (name + id).
  dump-tree               Print node count and sample nodes for a tree.
  find-node               Find tree nodes by name query.
  find-calendar           Find a calendar item by title and optional date.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from bubble.client import BubbleAPIError
from bubble.lookups import (
    find_calendar_item_by_title_date,
    find_tree_nodes_fuzzy,
    get_all_trees,
    get_tree_by_name,
    get_tree_nodes_in_tree,
)


def _safe_display(obj: Any) -> Any:
    """Return a copy safe for display: no keys that might hold secrets."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        skip = {"token", "authorization", "api_key", "key", "password", "secret"}
        return {k: _safe_display(v) for k, v in obj.items() if str(k).lower() not in skip}
    if isinstance(obj, list):
        return [_safe_display(x) for x in obj]
    return obj


def _node_display(node: dict) -> dict:
    """Minimal node for display: id + name only."""
    name = (node.get("Name") or node.get("name") or "").strip()
    nid = node.get("_id") or node.get("id")
    return {"_id": nid, "Name": name}


def cmd_list_trees() -> int:
    trees = get_all_trees()
    if not trees:
        print("No trees found.")
        return 0
    print(f"Trees ({len(trees)}):")
    for t in trees:
        name = (t.get("Name") or t.get("name") or "").strip() or "(no name)"
        nid = t.get("_id") or t.get("id") or "(no id)"
        print(f"  {name}")
        print(f"    id: {nid}")
    return 0


def cmd_dump_tree(tree_name: str, sample: int = 10) -> int:
    tree = get_tree_by_name(tree_name)
    if not tree:
        print(f"Tree not found: {tree_name!r}")
        return 1
    tree_id = tree.get("_id") or tree.get("id")
    name = tree.get("Name") or tree.get("name") or tree_name
    print(f"Tree: {name}")
    print(f"  id: {tree_id}")
    nodes = get_tree_nodes_in_tree(tree_id)
    print(f"  node count: {len(nodes)}")
    if not nodes:
        return 0
    show = nodes[:sample]
    print(f"  sample nodes (first {len(show)}):")
    for n in show:
        d = _node_display(n)
        print(f"    - {d.get('Name', '')!r}  id: {d.get('_id', '')}")
    return 0


def cmd_find_node(tree_name: str, query: str, limit: int = 20) -> int:
    tree = get_tree_by_name(tree_name)
    if not tree:
        print(f"Tree not found: {tree_name!r}")
        return 1
    tree_id = tree.get("_id") or tree.get("id")
    matches = find_tree_nodes_fuzzy(tree_id, query, limit=limit)
    if not matches:
        print(f"No nodes matching {query!r} in tree {tree_name!r}.")
        return 0
    print(f"Matches for {query!r} in tree {tree_name!r} ({len(matches)}):")
    for n in matches:
        d = _node_display(n)
        print(f"  - {d.get('Name', '')!r}  id: {d.get('_id', '')}")
    return 0


def cmd_find_calendar(title: str, date: str | None, tolerance_days: int = 7) -> int:
    item = find_calendar_item_by_title_date(
        title, start_dt_iso=date, tolerance_days=tolerance_days
    )
    if not item:
        print(f"No calendar item found for title {title!r}" + (f" near date {date!r}" if date else "") + ".")
        return 0
    safe = _safe_display(item)
    print("Calendar item:")
    # Human-readable key fields first
    for key in ("title", "date", "event description", "_id"):
        if key in safe and safe[key] is not None:
            print(f"  {key}: {safe[key]}")
    # Rest (excluding already printed)
    printed = {"title", "date", "event description", "_id"}
    for k, v in sorted(safe.items()):
        if k not in printed and v is not None and v != "":
            print(f"  {k}: {v}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bubble.doctor",
        description="Bubble read-only diagnostics (no secrets in output).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-trees", help="List all trees (name + id)")

    p_dump = sub.add_parser("dump-tree", help="Print node count and sample nodes for a tree")
    p_dump.add_argument("--tree-name", required=True, metavar="NAME", help="Tree name (e.g. Organization/Publisher)")
    p_dump.add_argument("--sample", type=int, default=10, help="Number of sample nodes to print (default 10)")

    p_node = sub.add_parser("find-node", help="Find tree nodes by name query")
    p_node.add_argument("--tree-name", required=True, metavar="NAME", help="Tree name")
    p_node.add_argument("--query", required=True, metavar="Q", help="Search query (substring match)")
    p_node.add_argument("--limit", type=int, default=20, help="Max matches (default 20)")

    p_cal = sub.add_parser("find-calendar", help="Find a calendar item by title and optional date")
    p_cal.add_argument("--title", required=True, metavar="T", help="Title (substring match)")
    p_cal.add_argument("--date", default=None, metavar="ISO", help="Optional date (e.g. 2026-02-25)")
    p_cal.add_argument("--tolerance-days", type=int, default=7, help="Days before/after date (default 7)")

    args = parser.parse_args()

    try:
        if args.command == "list-trees":
            return cmd_list_trees()
        if args.command == "dump-tree":
            return cmd_dump_tree(args.tree_name, sample=args.sample)
        if args.command == "find-node":
            return cmd_find_node(args.tree_name, args.query, limit=args.limit)
        if args.command == "find-calendar":
            return cmd_find_calendar(args.title, args.date, tolerance_days=args.tolerance_days)
    except BubbleAPIError as e:
        print("Bubble API error:", str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
