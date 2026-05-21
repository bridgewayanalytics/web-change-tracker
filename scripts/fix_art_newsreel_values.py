#!/usr/bin/env python3
"""
One-time fix: convert art_newsreel_relevance string values ("true"/"false")
to the correct object format {"status": "Yes"/"No", "reference": ""}.
"""
import json
import sys

INPUT = "/tmp/alerts_table.jsonl"
OUTPUT = "/tmp/alerts_table_fixed.jsonl"

fixed = 0
total = 0

with open(INPUT) as f_in, open(OUTPUT, "w") as f_out:
    for line in f_in:
        line = line.strip()
        if not line:
            continue
        total += 1
        row = json.loads(line)

        val = row.get("art_newsreel_relevance")
        if isinstance(val, str) and val.lower() in ("true", "false"):
            status = "Yes" if val.lower() == "true" else "No"
            row["art_newsreel_relevance"] = {"status": status, "reference": ""}
            fixed += 1
        elif isinstance(val, bool):
            status = "Yes" if val else "No"
            row["art_newsreel_relevance"] = {"status": status, "reference": ""}
            fixed += 1

        f_out.write(json.dumps(row) + "\n")

print(f"Total rows: {total}")
print(f"Fixed: {fixed}")
print(f"Output: {OUTPUT}")
