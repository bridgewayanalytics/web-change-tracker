#!/usr/bin/env bash
# Validate Bubble Data API endpoints used by the app (trees, tree nodes, calendar items, resources).
# Run from repo root. Uses BUBBLE_API_URL and BUBBLE_API_KEY from env or SSM.
# Usage: ./scripts/validate_bubble_api.sh
# Or:    BUBBLE_API_URL=... BUBBLE_API_KEY=... ./scripts/validate_bubble_api.sh

set -e

if [[ -z "${BUBBLE_API_URL:-}" || -z "${BUBBLE_API_KEY:-}" ]]; then
  echo "Loading BUBBLE_API_URL and BUBBLE_API_KEY from SSM..."
  export BUBBLE_API_URL=$(aws ssm get-parameter --region us-east-1 --name "/web-change-tracker/prod/bubble_api_url" --query "Parameter.Value" --output text 2>/dev/null || true)
  export BUBBLE_API_KEY=$(aws ssm get-parameter --with-decryption --region us-east-1 --name "/web-change-tracker/prod/bubble_api_key" --query "Parameter.Value" --output text 2>/dev/null || true)
fi
if [[ -z "${BUBBLE_API_URL:-}" || -z "${BUBBLE_API_KEY:-}" ]]; then
  echo "ERROR: Set BUBBLE_API_URL and BUBBLE_API_KEY (or have SSM params configured)."
  exit 1
fi

# Tree names (match ECS / enrich_refs defaults)
BUBBLE_ORGANIZATION_TREE="${BUBBLE_ORGANIZATION_TREE:-Organization}"
BUBBLE_TYPE1_TREE="${BUBBLE_TYPE1_TREE:-Resources Types}"

echo "Base URL: ${BUBBLE_API_URL}"
echo ""

# 1) List trees (no constraint)
echo "=== 1. Trees (list) ==="
TREE_JSON=$(curl -s -G -H "Authorization: Bearer $BUBBLE_API_KEY" --data-urlencode "limit=50" "$BUBBLE_API_URL/tree")
TREE_COUNT=$(echo "$TREE_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=(d.get("response") or d).get("results",[]); print(len(r))' 2>/dev/null || echo "0")
echo "count: $TREE_COUNT"
if [[ "$TREE_COUNT" -gt 0 ]]; then
  echo "$TREE_JSON" | python3 -c '
import sys,json
d=json.load(sys.stdin)
r=(d.get("response") or d).get("results",[])
for x in r[:5]: print("  -", repr(x.get("Name") or x.get("name")), "  _id:", x.get("_id") or x.get("id"))
if len(r)>5: print("  ...")
' 2>/dev/null || true
fi
echo ""

# 1b) Raw tree nodes (no filter) - show field names so we can fix "Tree" constraint if needed
echo "=== 1b. Tree nodes (raw list, first 3) - check which field links to Tree ==="
TREENODE_RAW=$(curl -s -G -H "Authorization: Bearer $BUBBLE_API_KEY" --data-urlencode "limit=5" "$BUBBLE_API_URL/treenode")
TREENODE_RAW_COUNT=$(echo "$TREENODE_RAW" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=(d.get("response") or d).get("results",[]); print(len(r))' 2>/dev/null || echo "0")
echo "count: $TREENODE_RAW_COUNT"
if [[ "$TREENODE_RAW_COUNT" -gt 0 ]]; then
  echo "$TREENODE_RAW" | python3 -c '
import sys,json
d=json.load(sys.stdin)
r=(d.get("response") or d).get("results",[])
if r:
  first = r[0]
  print("  First node keys:", list(first.keys()))
  for k in ("Tree","tree","Parent","parent","Parent node","parent_node"):
    if k in first: print("  ", k, "=", first[k])
' 2>/dev/null || true
else
  echo "  (no tree nodes in app; Tree node type may be empty or use a different API type name)"
fi
echo ""

# 2) Tree by name (Organization) + tree nodes under it
echo "=== 2. Tree by Name + Tree nodes (Organization) ==="
ORG_TREE_JSON=$(curl -s -G -H "Authorization: Bearer $BUBBLE_API_KEY" \
  --data-urlencode "constraints=[{\"key\":\"Name\",\"constraint_type\":\"equals\",\"value\":\"$BUBBLE_ORGANIZATION_TREE\"}]" \
  --data-urlencode "limit=1" "$BUBBLE_API_URL/tree")
ORG_TREE_ID=$(echo "$ORG_TREE_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=(d.get("response") or d).get("results",[]); print(r[0].get("_id") or r[0].get("id") if r else "")' 2>/dev/null || echo "")
if [[ -z "$ORG_TREE_ID" ]]; then
  echo "  Organization tree not found (name=$BUBBLE_ORGANIZATION_TREE). Check BUBBLE_ORGANIZATION_TREE."
else
  echo "  tree _id: $ORG_TREE_ID"
  NODES_JSON=$(curl -s -G -H "Authorization: Bearer $BUBBLE_API_KEY" \
    --data-urlencode "constraints=[{\"key\":\"parent_tree\",\"constraint_type\":\"equals\",\"value\":\"$ORG_TREE_ID\"}]" \
    --data-urlencode "limit=20" "$BUBBLE_API_URL/treenode")
  NODE_COUNT=$(echo "$NODES_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=(d.get("response") or d).get("results",[]); print(len(r))' 2>/dev/null || echo "0")
  echo "  tree nodes under Organization: $NODE_COUNT"
  echo "$NODES_JSON" | python3 -c '
import sys,json
d=json.load(sys.stdin)
r=(d.get("response") or d).get("results",[])
for x in r[:8]: print("    -", (x.get("Name") or x.get("name") or ""), "  id:", x.get("_id") or x.get("id"))
if len(r)>8: print("    ...")
' 2>/dev/null || true
fi
echo ""

# 3) Type1 tree (Resources Types) + nodes
echo "=== 3. Type1 tree + nodes ($BUBBLE_TYPE1_TREE) ==="
TYPE1_TREE_JSON=$(curl -s -G -H "Authorization: Bearer $BUBBLE_API_KEY" \
  --data-urlencode "constraints=[{\"key\":\"Name\",\"constraint_type\":\"equals\",\"value\":\"$BUBBLE_TYPE1_TREE\"}]" \
  --data-urlencode "limit=1" "$BUBBLE_API_URL/tree")
TYPE1_TREE_ID=$(echo "$TYPE1_TREE_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=(d.get("response") or d).get("results",[]); print(r[0].get("_id") or r[0].get("id") if r else "")' 2>/dev/null || echo "")
if [[ -z "$TYPE1_TREE_ID" ]]; then
  echo "  Type1 tree not found (name=$BUBBLE_TYPE1_TREE). Check BUBBLE_TYPE1_TREE."
else
  echo "  tree _id: $TYPE1_TREE_ID"
  TYPE1_NODES_JSON=$(curl -s -G -H "Authorization: Bearer $BUBBLE_API_KEY" \
    --data-urlencode "constraints=[{\"key\":\"parent_tree\",\"constraint_type\":\"equals\",\"value\":\"$TYPE1_TREE_ID\"}]" \
    --data-urlencode "limit=20" "$BUBBLE_API_URL/treenode")
  TYPE1_COUNT=$(echo "$TYPE1_NODES_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=(d.get("response") or d).get("results",[]); print(len(r))' 2>/dev/null || echo "0")
  echo "  tree nodes (Type1 options): $TYPE1_COUNT"
  echo "$TYPE1_NODES_JSON" | python3 -c '
import sys,json
d=json.load(sys.stdin)
r=(d.get("response") or d).get("results",[])
for x in r[:10]: print("    -", (x.get("Name") or x.get("name") or ""), "  id:", x.get("_id") or x.get("id"))
if len(r)>10: print("    ...")
' 2>/dev/null || true
fi
echo ""

# 4) Calendar items
echo "=== 4. Calendar items (list) ==="
CAL_JSON=$(curl -s -G -H "Authorization: Bearer $BUBBLE_API_KEY" --data-urlencode "limit=15" "$BUBBLE_API_URL/calendaritem")
CAL_COUNT=$(echo "$CAL_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=(d.get("response") or d).get("results",[]); print(len(r))' 2>/dev/null || echo "0")
echo "count: $CAL_COUNT"
if [[ "$CAL_COUNT" -gt 0 ]]; then
  echo "$CAL_JSON" | python3 -c '
import sys,json
d=json.load(sys.stdin)
r=(d.get("response") or d).get("results",[])
for x in r[:5]: print("  -", (x.get("title") or x.get("Title") or "")[:60], "  date:", x.get("date"), "  id:", (x.get("_id") or x.get("id")))
if len(r)>5: print("  ...")
' 2>/dev/null || true
fi
echo ""

# 5) Resources
echo "=== 5. Resources (list) ==="
RES_JSON=$(curl -s -G -H "Authorization: Bearer $BUBBLE_API_KEY" --data-urlencode "limit=10" "$BUBBLE_API_URL/resource")
RES_COUNT=$(echo "$RES_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=(d.get("response") or d).get("results",[]); print(len(r))' 2>/dev/null || echo "0")
echo "count: $RES_COUNT"
if [[ "$RES_COUNT" -gt 0 ]]; then
  echo "$RES_JSON" | python3 -c '
import sys,json
d=json.load(sys.stdin)
r=(d.get("response") or d).get("results",[])
for x in r[:5]: print("  -", (x.get("Name") or x.get("name") or "")[:50], "  URL:", (x.get("URL") or "")[:50], "  id:", (x.get("_id") or x.get("id")))
if len(r)>5: print("  ...")
' 2>/dev/null || true
fi
echo ""

echo "=== Done ==="
