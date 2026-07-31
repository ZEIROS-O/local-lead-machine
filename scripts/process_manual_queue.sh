#!/usr/bin/env bash
set -euo pipefail

QUEUE="${1:?queue dir required}"
INPUT="${2:?input enriched jsonl required}"
OUTPUT="${3:?output scored csv required}"
PACKETS="${4:?packet output dir required}"
MODE="${5:-dry}"   # dry or live
VERTICAL="${6:-service}"

if [ ! -d "$QUEUE/batches" ]; then
  echo "[ERROR] Missing queue batches dir: $QUEUE/batches"
  exit 1
fi

shopt -s nullglob
RESPONSES=("$QUEUE"/batches/batch_*_response.json)

if [ "${#RESPONSES[@]}" -eq 0 ]; then
  echo "[ERROR] No response files found in $QUEUE/batches"
  echo "Expected files like: batch_001_response.json"
  exit 1
fi

echo "[INFO] Queue: $QUEUE"
echo "[INFO] Input: $INPUT"
echo "[INFO] Output: $OUTPUT"
echo "[INFO] Packets: $PACKETS"
echo "[INFO] Vertical: $VERTICAL"
echo "[INFO] Responses found: ${#RESPONSES[@]}"

rm -rf "$QUEUE/scores"
mkdir -p "$QUEUE/scores"

for resp in "${RESPONSES[@]}"; do
  echo "[CHECK] $resp"
  python -m json.tool "$resp" > /tmp/llm_score_checked.json

  echo "[IMPORT] $resp"
  python import_llm_score_bundle.py \
    --input "$resp" \
    --scores-dir "$QUEUE/scores"
done

echo "[SCORE]"
python score_leads.py \
  --mode manual-llm \
  --only-scored \
  --input "$INPUT" \
  --vertical "$VERTICAL" \
  --scores-dir "$QUEUE/scores" \
  --output "$OUTPUT"

echo "[PACKETS]"
rm -rf "$PACKETS"
python make_packets.py \
  --inputs "$OUTPUT" \
  --output-dir "$PACKETS"

echo "[INSPECT]"
python - "$OUTPUT" <<'PY'
import csv, sys
from pathlib import Path

path = Path(sys.argv[1])
rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))

print("Rows:", len(rows))
sendable = 0
for r in rows:
    if r.get("Sendable") == "Yes":
        sendable += 1
    print(
        r.get("Lead Priority"),
        "| sendable=", r.get("Sendable"),
        "| packet=", r.get("Packet Eligible"),
        "| score=", r.get("Score Total"),
        "|", r.get("Business Name"),
        "|", r.get("Offer Fit"),
        "| blockers:", r.get("Send Blockers")
    )
print("Sendable:", sendable)
PY

echo "[AIRTABLE DRY-RUN]"
python upload_airtable.py \
  --input "$OUTPUT" \
  --packet-dir "$PACKETS"

if [ "$MODE" = "live" ]; then
  echo "[AIRTABLE LIVE]"
  python upload_airtable.py \
    --input "$OUTPUT" \
    --packet-dir "$PACKETS" \
    --live
else
  echo "[DONE] Dry-run only. Re-run with final arg 'live' to sync."
fi
