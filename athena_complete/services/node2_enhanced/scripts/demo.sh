#!/usr/bin/env bash
# End-to-end walkthrough of Node 2.
#
# The walkthrough mutates state — it assigns FMEA owners, verifies a measurement,
# raises an ECN and passes a gate — so run it against a freshly seeded database
# or step 4 will show the already-remediated result:
#
#   python3 scripts/seed.py --reset
#   uvicorn app.main:app --port 8002
#   ./scripts/demo.sh
#
#   ./scripts/demo.sh
#   BASE=http://host:9000 ./scripts/demo.sh
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8002}"
API="$BASE/api/v1"
step() { printf '\n\033[1;34m── %s\033[0m\n' "$1"; }
PY() { python3 -c "$1"; }

step "0 · health"
curl -sf "$BASE/health" | python3 -c "
import json,sys
h=json.load(sys.stdin); db=h['database']; c=h['capabilities']
print(f\"  status={h['status']} v{h['version']} env={h['environment']}\")
print(f\"  db: {db['dialect']} -> {db['target']} reachable={db['reachable']} seeded={db['seeded']}\")
print(f\"  {c['gate_rules']} gate rules across {len(c['gates'])} gates\")
print(f\"  compliance vault: {c['compliance_vault']} · creepage minimum {c['creepage_min_mm_adapter']}mm\")
print('  counts: ' + ', '.join(f'{k}={v}' for k,v in h['counts'].items()))
" || { echo "service not reachable at $BASE"; exit 1; }

step "1 · sign in as the programme gatekeeper"
TOKEN=$(curl -sf -X POST "$API/auth/token" -H 'content-type: application/json' \
  -d '{"username":"p.nair"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
AUTH="authorization: Bearer $TOKEN"
ENG=$(curl -sf -X POST "$API/auth/token" -H 'content-type: application/json' \
  -d '{"username":"a.mehta"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
QA=$(curl -sf -X POST "$API/auth/token" -H 'content-type: application/json' \
  -d '{"username":"s.khan"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
CCB=$(curl -sf -X POST "$API/auth/token" -H 'content-type: application/json' \
  -d '{"username":"ccb.chair"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
echo "  gatekeeper p.nair, engineer a.mehta, quality s.khan, change board ccb.chair"

step "2 · the multi-SKU hierarchy — KIT-NPA-01 over three packed units"
curl -sf "$API/bom/KIT-NPA-01/explode" -H "$AUTH" | python3 -c "
import json,sys
e=json.load(sys.stdin)
print(f\"  {e['sku']} rev {e['revision']} — {e['line_count']} lines, {e['max_level']} levels, \"
      f\"\${e['rolled_up_cost_usd']} rolled up\")
for n in e['tree']:
    kids=f\" ({len(n['children'])} children)\" if n['children'] else ''
    cost=f\" \${n['extended_cost_usd']}\" if n['extended_cost_usd'] else ''
    print(f\"    {n['part_number']:<16} x{n['quantity_per_parent']}{cost}{kids}\")
    for c in n['children'][:3]:
        pin='pinned' if c['revision_pinned'] else 'floating'
        print(f\"        {c['part_number']:<16} x{c['extended_quantity']} rev {c['revision']} [{pin}]\")
    if len(n['children'])>3: print(f\"        ... +{len(n['children'])-3} more\")
"

step "3 · where-used: the powerbank cell reaches the kit two levels up"
curl -sf "$API/bom/PB-CEL-10K3S/where-used" -H "$AUTH" | python3 -c "
import json,sys
for w in json.load(sys.stdin): print(f\"  L{w['level']}  {w['path']}\")
"

step "4 · evaluate G2 for the bundled kit — THE CONTRACT"
curl -s -o /tmp/n2_g2.json -w "  HTTP %{http_code}\n" \
  -X POST "$API/plm/gates/G2_DESIGN_EVT/evaluate" -H "$AUTH" \
  -H 'content-type: application/json' -d '{"sku":"KIT-NPA-01"}'
python3 -c "
import json
d=json.load(open('/tmp/n2_g2.json'))
print(json.dumps({k:d[k] for k in ('gate','status','evaluation_timestamp','blocking_reasons','passed_artifacts')}, indent=2))
print(f\"  [additive] rules {d['rules_evaluated']} · passed {d['rules_passed']} · failed {d['rules_failed']} \"
      f\"· n/a {d['rules_not_applicable']} · gate_state {d['gate_state']} · transition {d['transition_applied']}\")
print('  [additive] advisories:')
for a in d['advisories']: print(f\"    {a['rule_id']}: {a['detail'][:100]}\")
"

step "5 · remediate — assign a named owner to the High AP items"
for row in D-01 P-05; do
  curl -sf -X POST "$API/fmea/$row/owner" -H "authorization: Bearer $QA" \
    -H 'content-type: application/json' \
    -d '{"owner_username":"r.iyer","target_date":"2026-09-30"}' | python3 -c "
import json,sys
f=json.load(sys.stdin)
print(f\"  {f['row_ref']} ({f['sku']}) {f['item_function'][:38]:<38} owner -> {f['owner_username']}\")
"
done

step "6 · remediate — re-measure adapter creepage on the released layout"
curl -sf -X POST "$API/design-parameters/CREEPAGE_AD_PCB_PRI_SEC/verify?measured_value=3.6" \
  -H "authorization: Bearer $ENG" | python3 -c "
import json,sys
p=json.load(sys.stdin)
print(f\"  {p['code']}: {p['measured_value']}mm against {p['min_value']}mm minimum -> \"
      f\"meets={p['meets_minimum']} placeholder={p['is_placeholder']}\")
"

step "7 · re-evaluate G2 for the adapter — now it passes and transitions"
curl -s -o /tmp/n2_ad.json -w "  HTTP %{http_code}\n" \
  -X POST "$API/plm/gates/G2/evaluate" -H "$AUTH" \
  -H 'content-type: application/json' -d '{"sku":"AD-20W-PKD"}'
python3 -c "
import json
d=json.load(open('/tmp/n2_ad.json'))
print(f\"  {d['gate']} {d['status']} · {d['previous_gate_state']} -> {d['gate_state']} \"
      f\"(transition_applied={d['transition_applied']}, by {d['evaluated_by']})\")
print(f\"  evidence: {', '.join(d['passed_artifacts'][:4])} ...\")
"

step "8 · raise an ECN — substitute the powerbank cell"
curl -sf -X POST "$API/bom/parts" -H "authorization: Bearer $ENG" \
  -H 'content-type: application/json' -d '{
    "part_number":"PB-CEL-10K4S","name":"Li-polymer cell pack (alternate vendor)",
    "part_type":"COMPONENT","supply_mode":"SKD_IMPORT",
    "supplier":"Vendor M — Battery Cells","sourcing_status":"Alternate qualified",
    "safety_critical":true}' >/dev/null 2>&1
curl -sf -X POST "$API/bom/parts/PB-CEL-10K4S/revisions" -H "authorization: Bearer $ENG" \
  -H 'content-type: application/json' -d '{"rev":"A","unit_cost_usd":"2.18"}' >/dev/null 2>&1
curl -sf -X POST "$API/bom/parts/PB-CEL-10K4S/revisions/A/release" \
  -H "authorization: Bearer $ENG" >/dev/null 2>&1

curl -sf -X POST "$API/ecn" -H "authorization: Bearer $ENG" \
  -H 'content-type: application/json' -d '{
    "title":"Substitute powerbank cell pack to the alternate vendor",
    "reason":"Primary cell vendor lead time moved to 16 weeks against a 10-week plan; qualify the alternate to protect the festive ramp.",
    "category":"SUPPLY_CONTINUITY","primary_sku":"PB-10K-PKD",
    "affected_items":[{"part_number":"PB-CEL-10K3S","change_type":"SUBSTITUTE",
                       "replacement_part_number":"PB-CEL-10K4S",
                       "detail":"Same form factor and capacity, different supplier."}]}' \
  -o /tmp/n2_ecn.json
python3 -c "
import json
e=json.load(open('/tmp/n2_ecn.json'))
print(f\"  {e['ecn_number']} [{e['state']}] {e['title']}\")
print(f\"  impact summary: {e['impact_summary']}\")
print('  flagged Pending Re-Review:')
for i in e['impacts']:
    print(f\"    {i['object_type']:<20} {i['object_ref']:<22} {i['sku'] or '-':<12}\")
    print(f\"        via {i['where_used_path']}\")
    print(f\"        {i['reason'][:110]}\")
open('/tmp/n2_ecn_no','w').write(e['ecn_number'])
"

step "9 · the open change now blocks the powerbank's gate"
curl -s -X POST "$API/plm/gates/G2/evaluate" -H "$AUTH" \
  -H 'content-type: application/json' -d '{"sku":"PB-10K-PKD"}' | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"  {d['status']}\")
for b in d['blocking_reasons']: print(f\"    {b['rule_id']}: {b['detail'][:140]}\")
"

step "10 · approval is refused while re-reviews are outstanding"
ECN=$(cat /tmp/n2_ecn_no)
curl -s -o /tmp/n2_dec.json -w "  HTTP %{http_code} " -X POST "$API/ecn/$ECN/decision" \
  -H "authorization: Bearer $CCB" -H 'content-type: application/json' \
  -d '{"decision":"approve"}'
python3 -c "import json;print(json.load(open('/tmp/n2_dec.json'))['detail'][:170])"

step "11 · CAD vault webhook — signed, idempotent, change-detecting"
python3 - <<'PYEOF' > /tmp/n2_hook.json
import hashlib, json
print(json.dumps({
  "event_id": "evt-demo-001", "event_type": "revision.released",
  "occurred_at": "2026-08-14T09:00:00Z", "part_number": "AD-PCB-20W", "revision": "D",
  "file_format": "STEP AP242", "file_name": "AD-PCB-20W_MASTER_REV_D.step",
  "checksum_sha256": hashlib.sha256(b"rev-d").hexdigest(),
  "artifact_type": "CAD_MASTER_MODEL", "is_parametric": True,
  "change_summary": "Creepage increased to 3.6 mm with a 20% guard band.",
}))
PYEOF
INT=$(curl -sf -X POST "$API/auth/token" -H 'content-type: application/json' \
  -d '{"username":"cad.vault"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
SIG=$(curl -sf -X POST "$API/webhooks/cad-vault/sign" -H "authorization: Bearer $INT" \
  -H 'content-type: application/json' --data-binary @/tmp/n2_hook.json \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['signature'])")
for attempt in first replay; do
  curl -s -X POST "$API/webhooks/cad-vault/revision" -H "authorization: Bearer $INT" \
    -H 'content-type: application/json' -H "x-athena-signature: $SIG" \
    --data-binary @/tmp/n2_hook.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"  duplicate={d.get('duplicate')} rev_created={d.get('created_revision')} \"
      f\"ecn={d.get('triggered_ecn')} — {d.get('message','')[:90]}\")
"
done
echo "  unsigned attempt:"
curl -s -o /dev/null -w "    HTTP %{http_code} (refused)\n" -X POST "$API/webhooks/cad-vault/revision" \
  -H "authorization: Bearer $INT" -H 'content-type: application/json' \
  --data-binary @/tmp/n2_hook.json

step "12 · compliance vault — BIS CRS / WPC ETA / UN 38.3"
curl -sf -X POST "$API/compliance/refresh?sku=NB-100-PKD" -H "authorization: Bearer $ENG" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"  mode={d['mode']} checked={d['checked']} updated={d['updated']}\")
for c in d['certificates']:
    print(f\"    {c['scheme']:<14} {c['status']:<12} {c['certificate_number'] or '-':<22} \"
          f\"expires {c['expires_on'] or '-'}\")
for w in d['warnings']: print(f'    ! {w[:150]}')
"

step "13 · gate state across the programme"
curl -sf "$API/plm/gates" -H "$AUTH" | python3 -c "
import json,sys
rows=json.load(sys.stdin)
by={}
for g in rows: by.setdefault(g['sku'],{})[g['gate_code'][:2]]=g['state']
gates=['G0','G1','G2','G3','G4','G5','G6','G7']
print('  SKU               ' + ' '.join(f'{g:<9}' for g in gates))
for sku, st in sorted(by.items()):
    print(f'  {sku:<18}' + ' '.join(f\"{st.get(g,'-')[:9]:<9}\" for g in gates))
"

echo
echo "done."
