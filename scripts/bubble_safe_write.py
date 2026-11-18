#!/usr/bin/env python3
"""
Safe operator-side wrapper for ad-hoc Bubble writes.

Automatically snapshots the pre-write state of a Bubble record (and the
post-write state on success) to S3 under `snapshots/bubble/<type>/<id>/`
before any PATCH or DELETE lands, so the write can always be reconstructed
or reverted. Mirrors the executor's `snapshotBubbleRecord()` for
dashboard-triggered writes, but covers the operator-CLI path that the
Nov 10 incident taught us is NOT currently snapshotted.

Usage:

    # Inspect a record (snapshot only, no write)
    AWS_PROFILE=bridgeway python scripts/bubble_safe_write.py \\
        --type calendaritem --id 1789598655253x668569308291924000 --inspect

    # PATCH a record (snapshots pre-state, then applies PATCH, then snapshots post-state)
    AWS_PROFILE=bridgeway python scripts/bubble_safe_write.py \\
        --type calendaritem --id 1789598655253x668569308291924000 \\
        --patch '{"Topics - dt": ["abc123…"]}'

    # DELETE a record (snapshots pre-state, then DELETEs)
    AWS_PROFILE=bridgeway python scripts/bubble_safe_write.py \\
        --type agendaitem --id 1780749123275x739856002318684200 --delete

Snapshots go to:
    s3://<CHANGELOG_BUCKET>/snapshots/bubble/<type>/<id>/<utc-iso>_<phase>.json

To reconstruct the pre-state of a specific write:
    aws s3 ls s3://<bucket>/snapshots/bubble/calendaritem/<id>/
    # then `aws s3 cp` the matching timestamp's `_pre.json` file.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

_CONTENT_SPACE_ID = "1768998437948x865417918648382000"
_EIDARIX_VERSION = os.environ.get("EIDARIX_VERSION", "live")
_OBJ_BASE = "https://eidarix.bridgewayanalytics.com/api/1.1/obj"
_OBJ_VERSIONED = f"https://eidarix.bridgewayanalytics.com/version-{_EIDARIX_VERSION}/api/1.1/obj"
_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_BUCKET = os.environ.get("CHANGELOG_BUCKET") or _DEFAULT_BUCKET

# Types that must go through the VERSIONED URL (per content-gate.md §3):
# calendaritem UPDATE and both type-lookup endpoints.
_VERSIONED_TYPES = {"calendaritem", "calendaritemtype", "libraryitemtype"}


def _api_key() -> str:
    """Fetch the Bubble API key from env or SSM (mirror of executor's getBubbleKey)."""
    key = os.environ.get("BUBBLE_API_KEY", "").strip()
    if key:
        return key
    try:
        import boto3
        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        resp = ssm.get_parameter(
            Name="/naic-dashboard/prod/bubble_api_key",
            WithDecryption=True,
        )
        return resp["Parameter"]["Value"].strip()
    except Exception as e:
        print(f"ERROR: could not fetch Bubble API key: {e}", file=sys.stderr)
        sys.exit(2)


def _base_for(obj_type: str) -> str:
    return _OBJ_VERSIONED if obj_type in _VERSIONED_TYPES else _OBJ_BASE


def _snapshot(obj_type: str, obj_id: str, phase: str, api_key: str) -> dict | None:
    """GET the record and store the body in S3 at a timestamped key.
    phase is 'pre' or 'post'. Returns the record body on success, None otherwise."""
    url = f"{_base_for(obj_type)}/{obj_type}/{obj_id}"
    r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    if not r.ok:
        print(f"[WARN] snapshot {phase} GET {obj_type}/{obj_id} failed: HTTP {r.status_code}", file=sys.stderr)
        return None
    body = r.json().get("response", {})
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = f"snapshots/bubble/{obj_type}/{obj_id}/{ts}_{phase}.json"
    try:
        import boto3
        s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        s3.put_object(
            Bucket=_BUCKET,
            Key=key,
            Body=json.dumps(body, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        print(f"[snapshot-{phase}] s3://{_BUCKET}/{key}")
    except Exception as e:
        print(f"[WARN] S3 put failed for {key}: {e}", file=sys.stderr)
    return body


def _patch(obj_type: str, obj_id: str, payload: dict, api_key: str) -> bool:
    url = f"{_base_for(obj_type)}/{obj_type}/{obj_id}"
    r = requests.patch(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if not r.ok:
        print(f"[ERROR] PATCH {obj_type}/{obj_id} failed: HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return False
    print(f"[PATCH] {obj_type}/{obj_id} HTTP {r.status_code}")
    return True


def _delete(obj_type: str, obj_id: str, api_key: str) -> bool:
    url = f"{_base_for(obj_type)}/{obj_type}/{obj_id}"
    r = requests.delete(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    if not r.ok:
        print(f"[ERROR] DELETE {obj_type}/{obj_id} failed: HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return False
    print(f"[DELETE] {obj_type}/{obj_id} HTTP {r.status_code}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type", required=True, help="Bubble data type (calendaritem, libraryitem, agendaitem, …)")
    parser.add_argument("--id", required=True, help="Bubble record _id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--inspect", action="store_true", help="Snapshot and print the record; no write")
    group.add_argument("--patch", metavar="JSON", help="PATCH payload as a JSON string")
    group.add_argument("--delete", action="store_true", help="DELETE the record")
    args = parser.parse_args()

    api_key = _api_key()

    pre = _snapshot(args.type, args.id, "pre", api_key)
    if pre is None:
        print("ERROR: could not snapshot pre-state; refusing to write.", file=sys.stderr)
        sys.exit(2)

    if args.inspect:
        print(json.dumps(pre, indent=2))
        return

    if args.patch:
        try:
            payload = json.loads(args.patch)
        except json.JSONDecodeError as e:
            print(f"ERROR: --patch JSON invalid: {e}", file=sys.stderr)
            sys.exit(2)
        if not _patch(args.type, args.id, payload, api_key):
            sys.exit(1)
        _snapshot(args.type, args.id, "post", api_key)
        return

    if args.delete:
        if not _delete(args.type, args.id, api_key):
            sys.exit(1)
        # No post-snapshot for DELETE — record no longer exists.
        return


if __name__ == "__main__":
    main()
