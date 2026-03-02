#!/usr/bin/env python3
"""Smoke-test the NAIC Group → Calendar Item resolver against LIVE Bubble.

Usage:
    python3 scripts/bubble_resolver_smoke.py "Risk-Based Capital Investment Risk Evaluation Working Group"
    python3 scripts/bubble_resolver_smoke.py "Life Actuarial Task Force" --date 2026-03-02

Requires BUBBLE_API_KEY and BUBBLE_API_URL env vars.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bubble import lookups
from bubble.enrich_refs import (
    ORGANIZATION_TREE_NAME,
    _normalize_for_matching,
    _resolve_naic_group_node,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke-test NAIC Group calendar resolver")
    p.add_argument("label", help="Target group label (e.g. 'Life Actuarial Task Force')")
    p.add_argument("--date", default=None, help="Optional meeting date ISO (e.g. 2026-03-02)")
    p.add_argument("--window", type=int, default=7, help="Date window days (default 7)")
    p.add_argument("--limit", type=int, default=20, help="Max calendar items (default 20)")
    args = p.parse_args()

    print(f"\n=== NAIC Group Calendar Resolver Smoke ===\n")
    print(f"  Label:       {args.label}")
    print(f"  Normalized:  {_normalize_for_matching(args.label)}")
    print(f"  Tree:        {ORGANIZATION_TREE_NAME}")
    print(f"  Date:        {args.date or '(none — upcoming items)'}")
    print(f"  Window:      ±{args.window} days")
    print()

    # Step 1: Resolve group node
    nid, evidence = _resolve_naic_group_node(ORGANIZATION_TREE_NAME, [args.label])
    if nid:
        raw_name = evidence.get("chosen_raw_name", "?")
        print(f"  [OK]  Group node: '{raw_name}' → id={nid}")
        print(f"        Match type: {evidence.get('match_type')}")
        if evidence.get("token_overlap_score"):
            print(f"        Token overlap score: {evidence['token_overlap_score']}")
    else:
        print(f"  [FAIL] Group node not found: {evidence.get('failure')}")
        if evidence.get("candidate_matches"):
            print(f"         Candidates: {evidence['candidate_matches'][:5]}")
        if evidence.get("token_scored_top5"):
            print(f"         Top token matches:")
            for m in evidence["token_scored_top5"]:
                print(f"           {m['score']:.3f}  '{m['raw']}'  id={m['id']}")
        # Show near-misses: top 10 nodes sorted by token overlap
        from bubble.enrich_refs import _build_naic_group_node_map
        node_map = _build_naic_group_node_map(ORGANIZATION_TREE_NAME)
        norm = _normalize_for_matching(args.label)
        label_tokens = set(norm.split())
        near: list[tuple[float, str, str]] = []
        for nk, entries in node_map.items():
            nt = set(nk.split())
            if not nt:
                continue
            score = len(label_tokens & nt) / len(label_tokens | nt)
            for raw, nid_ in entries:
                near.append((score, raw, nid_))
        near.sort(key=lambda x: x[0], reverse=True)
        print(f"\n         Top 10 node names by token overlap:")
        for sc, raw, nid_ in near[:10]:
            print(f"           {sc:.3f}  '{raw}'  id={nid_}")
        print(f"\n  Evidence: {json.dumps(evidence, indent=2, default=str)}")
        return 1

    # Step 2: Query calendar items
    print()
    cal_items, meta = lookups.search_calendar_items_by_naic_group(
        nid,
        date_iso=args.date,
        window_days=args.window,
        limit=args.limit,
    )
    print(f"  Date mode:   {meta.get('date_mode')}")
    print(f"  Constraints: {json.dumps(meta.get('constraints', []), indent=2)}")
    print(f"  Results:     {len(cal_items)}")

    if meta.get("error"):
        print(f"  [ERROR]      {meta['error']}")
        if meta.get("traceback"):
            print(f"  Traceback:\n{meta['traceback']}")

    if cal_items:
        print(f"\n  Sample items:")
        for item in cal_items[:10]:
            cid = item.get("_id") or item.get("id")
            title = (item.get("title") or item.get("Title") or "")[:60]
            date = (item.get("date") or "")[:10]
            print(f"    - {cid}  {date}  {title}")
    else:
        print(f"\n  No calendar items returned.")

    print(f"\n  Full lookup meta:\n{json.dumps(meta, indent=2, default=str)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
