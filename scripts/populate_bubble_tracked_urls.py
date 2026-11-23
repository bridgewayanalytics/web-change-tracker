#!/usr/bin/env python3
"""
Populate the Bubble `organization` records with `tracked_url` from targets.json.

Default mode: dry-run. Prints the full PATCH plan + per-target match reasoning.
Only writes when `--apply` is passed AND the Bubble schema has a `tracked_url` field.

Match strategy (priority order):
  1. Explicit override in bubble_target_overrides.json (keyed by target_id)
  2. Exact normalized-name match (strip non-alphanumeric, lowercase)
  3. Normalized name after stripping "(E)" / "(A)" / "(EX)" / "(E/A)" etc. committee letters
  4. Same as (3) after also stripping "(Also under ...)" and similar annotations
  5. Fuzzy match >= 0.85 similarity (only reports; does not auto-apply)

Overrides file schema (JSON):
  {
    "<target_id>": {
      "bubble_org_id": "<_id>",         # required for the mapping to apply
      "bubble_org_name": "<for logs>",  # optional, cosmetic
      "reason": "<why this override exists>"
    }
  }
"""
from __future__ import annotations

import argparse, json, re, sys
from difflib import SequenceMatcher
from pathlib import Path

import requests

BASE = "https://eidarix.bridgewayanalytics.com/api/1.1/obj"
# NOTE: key loaded from env or falls back to the hardcoded value in bubble/bridgemind.py.
import os as _os
KEY = _os.environ.get("BUBBLE_API_KEY") or "0a951ec86c08a59e274411913ce6aec3"

REPO = Path(__file__).resolve().parent.parent
TARGETS_FILE = REPO / "targets.json"
OVERRIDES_FILE = REPO / "bubble_target_overrides.json"
TRACKED_URL_FIELD = "URL"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _norm_strip_paren(s: str) -> str:
    return _norm(re.sub(r"\([^)]+\)", "", (s or "")))


def _norm_strip_also(s: str) -> str:
    # Drop trailing "(Also under ...)" annotations too
    s = re.sub(r"\(also under[^)]+\)", "", (s or ""), flags=re.IGNORECASE)
    return _norm_strip_paren(s)


def _fetch_all_orgs() -> list[dict]:
    orgs: list[dict] = []
    cursor = 0
    while True:
        r = requests.get(
            f"{BASE}/organization",
            headers={"Authorization": f"Bearer {KEY}"},
            params={"limit": 100, "cursor": cursor},
            timeout=30,
        )
        r.raise_for_status()
        resp = r.json().get("response", {})
        batch = resp.get("results", [])
        orgs.extend(batch)
        remaining = resp.get("remaining", 0)
        if remaining <= 0:
            break
        cursor += resp.get("count", len(batch))
    return orgs


def _match(target: dict, orgs: list[dict], overrides: dict) -> dict:
    """Return a result dict for one target. Keys: target, match_type, bubble_org, score, reason."""
    tid = target["id"]
    label = target["label"]

    # 1) Explicit override
    if tid in overrides:
        ov = overrides[tid]
        oid = ov.get("bubble_org_id")
        if not oid:
            return {"target": target, "match_type": "override_incomplete", "bubble_org": None,
                    "score": None, "reason": ov.get("reason", "override missing bubble_org_id")}
        found = next((o for o in orgs if o["_id"] == oid), None)
        if not found:
            return {"target": target, "match_type": "override_bad_id", "bubble_org": None,
                    "score": None, "reason": f"override points to {oid!r} which doesn't exist"}
        return {"target": target, "match_type": "override", "bubble_org": found,
                "score": 1.0, "reason": ov.get("reason", "manual override")}

    ln = _norm(label)
    ln_p = _norm_strip_paren(label)
    ln_a = _norm_strip_also(label)

    # 2) Exact normalized
    exact = [o for o in orgs if _norm(o.get("Name", "")) == ln]
    if len(exact) == 1:
        return {"target": target, "match_type": "exact", "bubble_org": exact[0],
                "score": 1.0, "reason": "exact normalized-name match"}

    # 3) Paren-strip
    paren = [o for o in orgs if _norm_strip_paren(o.get("Name", "")) == ln_p]
    if len(paren) == 1:
        return {"target": target, "match_type": "paren_strip", "bubble_org": paren[0],
                "score": 1.0, "reason": "match after stripping (X) committee letter"}

    # 4) Also-strip
    also = [o for o in orgs if _norm_strip_also(o.get("Name", "")) == ln_a]
    if len(also) == 1:
        return {"target": target, "match_type": "also_strip", "bubble_org": also[0],
                "score": 1.0, "reason": "match after stripping '(Also under…)' + (X) committee letter"}

    # 5) Fuzzy — report only, do not auto-apply
    best = None
    best_score = 0.0
    for o in orgs:
        name = o.get("Name", "")
        s = max(
            SequenceMatcher(None, ln, _norm(name)).ratio(),
            SequenceMatcher(None, ln_p, _norm_strip_paren(name)).ratio(),
            SequenceMatcher(None, ln_a, _norm_strip_also(name)).ratio(),
        )
        if s > best_score:
            best_score = s
            best = o
    return {"target": target, "match_type": "no_match", "bubble_org": best,
            "score": best_score, "reason": "no exact/paren/also match; best fuzzy shown for review"}


def _current_url(org_id: str) -> str:
    """Read the current URL field. Empty string if unset."""
    r = requests.get(
        f"{BASE}/organization/{org_id}",
        headers={"Authorization": f"Bearer {KEY}"},
        timeout=20,
    )
    r.raise_for_status()
    rec = r.json().get("response", {})
    return str(rec.get(TRACKED_URL_FIELD, "") or "").strip()


def _patch_tracked_url(org_id: str, url: str, dry_run: bool) -> tuple[bool, str]:
    # Idempotent: skip if the field is already set to the same URL.
    try:
        existing = _current_url(org_id)
    except Exception as e:
        existing = ""
    if existing and existing == url.strip():
        return True, "SKIP (already correct)"
    if dry_run:
        return True, f"DRY-RUN (would write: {existing!r} -> {url!r})"
    r = requests.patch(
        f"{BASE}/organization/{org_id}",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={TRACKED_URL_FIELD: url},
        timeout=30,
    )
    if r.status_code in (200, 204):
        action = "overwrote" if existing else "wrote"
        return True, f"HTTP {r.status_code} ({action})"
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually PATCH Bubble. Without this flag the script is dry-run only.")
    ap.add_argument("--only", help="Comma-separated target_ids to include (default: all 29)")
    args = ap.parse_args()

    targets = json.loads(TARGETS_FILE.read_text())
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        targets = [t for t in targets if t["id"] in wanted]

    overrides = {}
    if OVERRIDES_FILE.exists():
        overrides = json.loads(OVERRIDES_FILE.read_text())

    print(f"Loaded {len(targets)} targets, {len(overrides)} overrides")
    print(f"Fetching Bubble orgs...")
    orgs = _fetch_all_orgs()
    print(f"Fetched {len(orgs)} orgs\n")

    results = [_match(t, orgs, overrides) for t in targets]

    groups = {}
    for r in results:
        groups.setdefault(r["match_type"], []).append(r)

    print("=" * 80)
    print("MATCH SUMMARY")
    print("=" * 80)
    for mt in ("override", "exact", "paren_strip", "also_strip",
               "no_match", "override_bad_id", "override_incomplete"):
        if mt in groups:
            print(f"  {mt:<25s}  {len(groups[mt]):>3d}")
    print()

    safe = [r for r in results if r["match_type"] in ("override", "exact", "paren_strip", "also_strip")]
    skipped = [r for r in results if r["match_type"] == "no_match"]
    broken = [r for r in results if r["match_type"] in ("override_bad_id", "override_incomplete")]

    print("=" * 80)
    print(f"WILL PATCH ({len(safe)})")
    print("=" * 80)
    for r in safe:
        t = r["target"]; o = r["bubble_org"]
        print(f"  {t['id']:<50s}  ->  {o['Name']:<60s}  [{r['match_type']}]")
        print(f"      Bubble _id : {o['_id']}")
        print(f"      tracked_url: {t['url']}")
        print()

    if skipped:
        print("=" * 80)
        print(f"SKIPPED ({len(skipped)}) — need manual override entry")
        print("=" * 80)
        # Collect keyword-based top candidates per target so manual review has IDs.
        # Keywords are picked from the target label.
        def _top_candidates(label: str, n: int = 4) -> list[tuple[dict, float]]:
            ln = _norm(label)
            ln_p = _norm_strip_paren(label)
            ln_a = _norm_strip_also(label)
            ranked = []
            for o in orgs:
                name = o.get("Name", "")
                s = max(
                    SequenceMatcher(None, ln, _norm(name)).ratio(),
                    SequenceMatcher(None, ln_p, _norm_strip_paren(name)).ratio(),
                    SequenceMatcher(None, ln_a, _norm_strip_also(name)).ratio(),
                )
                ranked.append((o, s))
            ranked.sort(key=lambda x: -x[1])
            return ranked[:n]

        for r in skipped:
            t = r["target"]
            print(f"  {t['id']:<50s}  target.label={t['label']!r}")
            print(f"      target.url: {t['url']}")
            print(f"      Top candidates:")
            for cand, score in _top_candidates(t["label"]):
                print(f"        sim={score:.2f}  _id={cand['_id']}  Name={cand['Name']!r}")
            print()

    if broken:
        print("=" * 80)
        print(f"BROKEN OVERRIDES ({len(broken)})")
        print("=" * 80)
        for r in broken:
            t = r["target"]
            print(f"  {t['id']}  reason: {r['reason']}")

    if not args.apply:
        print("=" * 80)
        print(f"DRY-RUN. Would PATCH {len(safe)} orgs with `{TRACKED_URL_FIELD}`.")
        print(f"Re-run with --apply to actually write.")
        print("=" * 80)
        return 0

    print("=" * 80)
    print(f"APPLYING {len(safe)} PATCH calls...")
    print("=" * 80)
    ok = fail = 0
    for r in safe:
        t = r["target"]; o = r["bubble_org"]
        success, msg = _patch_tracked_url(o["_id"], t["url"], dry_run=False)
        status = "OK" if success else "FAIL"
        print(f"  [{status}] {t['id']:<50s}  -> {o['Name'][:50]:<50s}  {msg}")
        if success: ok += 1
        else: fail += 1
    print(f"\nDone. {ok} succeeded, {fail} failed.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
