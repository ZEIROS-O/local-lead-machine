import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const overview = {
  generated_at: "2026-07-17T12:00:00+00:00",
  repo_root: "/repo",
  totals: { batches: 2, needs_attention: 1, unscored: 7, sendable: 3, airtable_ready: 0, finished: 1 },
  pipeline: [
    { key: "discovery", label: "Google discovery", count: 20 },
    { key: "enrichment", label: "Enriched", count: 18 },
    { key: "scoring", label: "LLM scored", count: 11 },
    { key: "packets", label: "Verified packets", count: 3 },
    { key: "airtable", label: "Airtable confirmed", count: 2 },
  ],
  attention: [],
  filters: { verticals: ["detailing"], markets: ["cleveland"], progress: [], health: [] },
  source_health: { ignored_noncanonical_artifacts: 4, authority: "Current artifacts and run-state receipts" },
};

const batch = {
  batch: "detailing_cleveland_medium_001",
  vertical: "detailing",
  market: "cleveland",
  size: "medium",
  sequence: "001",
  location: "Cleveland OH",
  query_count: 2,
  progress: "partial_scoring",
  progress_rank: 3,
  health: "attention",
  health_rank: 1,
  counts: { raw: 20, deduped: 18, enriched: 18, scored: 11, unscored: 7, packets: 3, sendable: 2, manual_review: 1, watchlist: 1, no_packet: 7 },
  stages: { discovery: { status: "complete", count: 18 }, enrichment: { status: "complete", count: 18 }, scoring: { status: "in_progress", count: 11 }, packets: { status: "stale", count: 3 }, airtable: { status: "not_started", count: 0 } },
  issues: [],
  latest_activity_at: "2026-07-17T12:00:00+00:00",
  next_action: "/score-dry detailing_cleveland_medium_001",
  pending_approval: { active: false, stage: "", expires_at: "" },
  llm_usage: { available: false, calls: 0, call_cap: 0, tokens: 0, token_cap: 0, estimated_usd: 0, usd_cap: 0, stopped_by_cap: false, stop_reason: "", updated_at: "" },
  airtable: { dry_run: { status: "not_run", timestamp: "", post: 0, patch: 0, skip: 0, warnings: [] }, live: { status: "not_run", timestamp: "", post: 0, patch: 0, skip: 0 }, current_input_hash: "" },
  artifacts: {},
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("dashboard", () => {
  it("renders source-backed overview and batch state", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(url.startsWith("/api/overview") ? overview : { generated_at: overview.generated_at, total: 1, items: [batch] }),
    })));
    render(<MemoryRouter><App /></MemoryRouter>);
    expect(await screen.findByText("Know where every batch stands.")).toBeInTheDocument();
    expect(screen.getByText("detailing_cleveland_medium_001")).toBeInTheDocument();
    expect(screen.getByText("7", { selector: ".attention-number" })).toBeInTheDocument();
    expect(screen.getByText("Current artifacts and run-state receipts")).toBeInTheDocument();
  });

  it("keeps a useful stale state when refresh fails", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve(overview) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ generated_at: overview.generated_at, total: 1, items: [batch] }) })
      .mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    render(<MemoryRouter><App /></MemoryRouter>);
    await screen.findByText("Know where every batch stands.");
    expect(screen.queryByText("Last known data")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    expect(await screen.findByText("Last known data")).toBeInTheDocument();
    expect(screen.getByText("Know where every batch stands.")).toBeInTheDocument();
  });
});
