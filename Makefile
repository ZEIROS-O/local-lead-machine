SHELL := /bin/bash
PY := .venv/bin/python

BATCH ?= manual_existing
VERTICAL ?= hardscaping
LIMIT ?= 12
BATCH_SIZE ?= 5
BATCH_ID ?= batch_001
LANE ?= all
RUNNER ?= codex
CONFIG ?= search_configs/$(BATCH).json
MAX_SEARCHES ?=
MAX_DETAILS ?=
MAX_ENRICH ?=

ENRICHED := output/enriched_$(BATCH).jsonl
SCORED_MANUAL := output/scored_$(BATCH)_manual.csv
QUEUE_DIR := review_queue_$(BATCH)
BATCH_DIR := $(QUEUE_DIR)/batches
SCORES_DIR := $(QUEUE_DIR)/scores
PACKETS_DIR := packets_$(BATCH)_manual
SEARCH_REPORT := reports/search_$(BATCH).md
ICP_REPORT := reports/icp_lanes_$(BATCH).md

.PHONY: test compile status pipeline search search-summary batch batch-lane icp-report save-batch validate-batch import-batch score score-batch-subscription packets inspect airtable-dry airtable-live full-from-search full-after-llm full-subscription clean-manual dashboard dashboard-api dashboard-ui dashboard-build dashboard-test

status:
	@git status --short

test:
	$(PY) -m unittest discover -s tests

compile:
	$(PY) -m py_compile enrich_leads.py score_leads.py make_llm_queue.py make_llm_batch.py import_llm_score_bundle.py score_batch_subscription.py make_packets.py run_pipeline.py upload_airtable.py feedback_report.py make_search_batch.py make_icp_lane_report.py annotate_legacy_scoring.py collect_review_signals.py collect_external_signals.py scoring_calibration.py lead_machine/icp_lanes.py lead_machine/signals.py lead_machine/review_velocity.py lead_machine/review_pain_verifier.py lead_machine/scoring_profiles.py lead_machine/scoring_v3.py lead_machine/scoring_calibration.py lead_machine/external_signals.py

dashboard: dashboard-build
	$(PY) -m lead_machine.dashboard

dashboard-api:
	$(PY) -m lead_machine.dashboard

dashboard-ui:
	npm --prefix dashboard-ui run dev

dashboard-build:
	npm --prefix dashboard-ui run build

dashboard-test:
	$(PY) -m unittest tests.test_dashboard
	npm --prefix dashboard-ui test

pipeline:
	$(PY) run_pipeline.py --batch $(BATCH) --vertical $(VERTICAL) --skip-search --limit $(LIMIT) --no-screenshots

search:
	$(PY) make_search_batch.py --config $(CONFIG) --batch $(BATCH) --no-screenshots $(if $(MAX_SEARCHES),--max-searches $(MAX_SEARCHES),) $(if $(MAX_DETAILS),--max-details $(MAX_DETAILS),) $(if $(MAX_ENRICH),--max-enrich $(MAX_ENRICH),)

search-summary:
	@cat $(SEARCH_REPORT)

batch:
	mkdir -p $(BATCH_DIR)
	$(PY) make_llm_batch.py --input $(ENRICHED) --output-dir $(BATCH_DIR) --batch-size $(BATCH_SIZE) --limit $(LIMIT) --only-candidates
	@echo
	@echo "Batch prompt:"
	@ls -lah $(BATCH_DIR)
	@echo
	@echo "Expected score files:"
	@cat $(BATCH_DIR)/$(BATCH_ID)_expected_files.txt

batch-lane:
	$(PY) make_llm_batch.py --input $(ENRICHED) --output-dir review_queue_$(BATCH)_$(LANE)/batches --batch-size $(BATCH_SIZE) --limit $(LIMIT) --only-candidates --lane $(LANE)

icp-report:
	$(PY) make_icp_lane_report.py --input $(ENRICHED) --output $(ICP_REPORT)

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

score-batch-subscription:
	$(PY) score_batch_subscription.py --queue-dir $(QUEUE_DIR) --runner $(RUNNER)

packets:
	rm -rf $(PACKETS_DIR)
	$(PY) make_packets.py --inputs $(SCORED_MANUAL) --output-dir $(PACKETS_DIR)

inspect:
	$(PY) -c 'import csv; from pathlib import Path; path=Path("$(SCORED_MANUAL)"); rows=list(csv.DictReader(path.open(newline="", encoding="utf-8"))); print("Rows:", len(rows)); [print(r.get("Lead Priority"), "| packet=", r.get("Packet Eligible"), "| sendable=", r.get("Sendable"), "|", r.get("Score Total"), "|", r.get("Business Name"), "|", r.get("Best Service Angle"), "| blockers:", r.get("Send Blockers")) for r in rows]'

airtable-dry:
	$(PY) upload_airtable.py --input $(SCORED_MANUAL) --only-sendable --packet-dir $(PACKETS_DIR) --dry-run

airtable-live:
	$(PY) upload_airtable.py --input $(SCORED_MANUAL) --only-sendable --packet-dir $(PACKETS_DIR) --live

full-from-search: search batch

full-after-llm: validate-batch import-batch score packets inspect airtable-dry

full-subscription: score-batch-subscription score packets inspect airtable-dry

clean-manual:
	rm -rf $(PACKETS_DIR)
	rm -f $(SCORED_MANUAL)
