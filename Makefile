SHELL := /bin/bash
PY := .venv/bin/python

BATCH ?= manual_existing
VERTICAL ?= hardscaping
LIMIT ?= 12
BATCH_SIZE ?= 5
BATCH_ID ?= batch_001

ENRICHED := output/enriched_$(BATCH).jsonl
SCORED_MANUAL := output/scored_$(BATCH)_manual.csv
QUEUE_DIR := review_queue_$(BATCH)
BATCH_DIR := $(QUEUE_DIR)/batches
SCORES_DIR := $(QUEUE_DIR)/scores
PACKETS_DIR := packets_$(BATCH)_manual

.PHONY: test compile status pipeline batch save-batch validate-batch import-batch score packets inspect airtable-dry airtable-live full-after-llm clean-manual

status:
	@git status --short

test:
	$(PY) -m unittest discover -s tests

compile:
	$(PY) -m py_compile enrich_leads.py score_leads.py make_llm_queue.py make_llm_batch.py import_llm_score_bundle.py make_packets.py run_pipeline.py upload_airtable.py feedback_report.py

pipeline:
	$(PY) run_pipeline.py --batch $(BATCH) --vertical $(VERTICAL) --skip-search --limit $(LIMIT) --no-screenshots

batch:
	mkdir -p $(BATCH_DIR)
	$(PY) make_llm_batch.py --input $(ENRICHED) --output-dir $(BATCH_DIR) --batch-size $(BATCH_SIZE) --limit $(LIMIT) --only-candidates
	@echo
	@echo "Batch prompt:"
	@ls -lah $(BATCH_DIR)
	@echo
	@echo "Expected score files:"
	@cat $(BATCH_DIR)/$(BATCH_ID)_expected_files.txt

save-batch:
	mkdir -p $(BATCH_DIR)
	powershell.exe -NoProfile -Command "Get-Clipboard -Raw" > $(BATCH_DIR)/$(BATCH_ID)_response.json
	$(PY) -c 'from pathlib import Path; p=Path("$(BATCH_DIR)/$(BATCH_ID)_response.json"); text=p.read_text(encoding="utf-8-sig").strip(); p.write_text(text + "\n", encoding="utf-8"); print(f"Saved cleaned batch response: {p}")'

validate-batch:
	$(PY) -m json.tool $(BATCH_DIR)/$(BATCH_ID)_response.json > /tmp/$(BATCH_ID)_checked.json

import-batch:
	$(PY) import_llm_score_bundle.py --input $(BATCH_DIR)/$(BATCH_ID)_response.json --scores-dir $(SCORES_DIR)

score:
	$(PY) score_leads.py --mode manual-llm --only-scored --input $(ENRICHED) --scores-dir $(SCORES_DIR) --output $(SCORED_MANUAL)

packets:
	rm -rf $(PACKETS_DIR)
	$(PY) make_packets.py --inputs $(SCORED_MANUAL) --output-dir $(PACKETS_DIR)

inspect:
	$(PY) -c 'import csv; from pathlib import Path; path=Path("$(SCORED_MANUAL)"); rows=list(csv.DictReader(path.open(newline="", encoding="utf-8"))); print("Rows:", len(rows)); [print(r.get("Lead Priority"), "| packet=", r.get("Packet Eligible"), "| sendable=", r.get("Sendable"), "|", r.get("Score Total"), "|", r.get("Business Name"), "|", r.get("Best Service Angle"), "| blockers:", r.get("Send Blockers")) for r in rows]'

airtable-dry:
	$(PY) upload_airtable.py --input $(SCORED_MANUAL) --only-sendable

airtable-live:
	$(PY) upload_airtable.py --input $(SCORED_MANUAL) --only-sendable --live

full-after-llm: validate-batch import-batch score packets inspect airtable-dry

clean-manual:
	rm -rf $(PACKETS_DIR)
	rm -f $(SCORED_MANUAL)
