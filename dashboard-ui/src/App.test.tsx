import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const overview = {
  generated_at: "2026-07-17T12:00:00+00:00",
  repo_root: "/repo",
  inventory_totals: { all: 2, active: 1, legacy: 1 },
  totals: { batches: 1, needs_attention: 1, unscored: 7, unknown_unscored_batches: 0, sendable: 3, airtable_ready: 0, finished: 1 },
  pipeline: [
    { key: "discovery", label: "Google discovery", count: 20 },
    { key: "enrichment", label: "Enriched", count: 18 },
    { key: "scoring", label: "LLM scored", count: 11 },
    { key: "packets", label: "Verified packets", count: 3 },
    { key: "airtable", label: "Airtable confirmed", count: 2 },
  ],
  attention: [],
  filters: { verticals: ["detailing"], markets: ["cleveland"], progress: [], health: [], lifecycles: ["active", "legacy"] },
  source_health: { ignored_noncanonical_artifacts: 4, authority: "Current artifacts and run-state receipts" },
};

const batch = {
  batch: "detailing_cleveland_medium_001",
  lifecycle: "active",
  legacy: null,
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
  reconciliation: { status: "reconciled", matched: 11, unmatched_enriched: 7, unmatched_scored: 0, ambiguous: 0 },
  current_sendability: { status: "evaluated", count: 2 },
  historical_packets: null,
  stages: { discovery: { status: "complete", count: 18 }, enrichment: { status: "complete", count: 18 }, scoring: { status: "in_progress", count: 11 }, packets: { status: "stale", count: 3 }, airtable: { status: "not_started", count: 0 } },
  issues: [],
  latest_activity_at: "2026-07-17T12:00:00+00:00",
  next_action: "/score-dry detailing_cleveland_medium_001",
  pending_approval: { active: false, stage: "", expires_at: "" },
  llm_usage: { available: false, calls: 0, call_cap: 0, tokens: 0, token_cap: 0, estimated_usd: 0, usd_cap: 0, stopped_by_cap: false, stop_reason: "", updated_at: "" },
  airtable: { dry_run: { status: "not_run", timestamp: "", post: 0, patch: 0, skip: 0, warnings: [] }, live: { status: "not_run", timestamp: "", post: 0, patch: 0, skip: 0 }, custom: { status: "not_run", cohort: "", timestamp: "", records: 0, verification_patch: 0 }, evidence: { status: "unknown", timestamp: "", records: null }, current_input_hash: "" },
  artifacts: {},
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function response(payload: unknown) {
  return { ok: true, json: () => Promise.resolve(payload) };
}

function mockDashboard(items: Array<{ lifecycle: string } & Record<string, unknown>>) {
  vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(response(
    url.startsWith("/api/overview")
      ? {
          ...overview,
          inventory_totals: {
            all: items.length,
            active: items.filter((item) => item.lifecycle === "active").length,
            legacy: items.filter((item) => item.lifecycle === "legacy").length,
          },
        }
      : { generated_at: overview.generated_at, total: items.length, items },
  ))));
}

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

  it("shows legacy history without calling it current sendable", async () => {
    const legacy = {
      ...batch,
      batch: "detailing_columbus_medium_001",
      lifecycle: "legacy",
      legacy: {
        legacy_since: "2026-07-20",
        reason: "pre_commercial_readiness_schema",
        note: "Preserved history",
      },
      counts: { ...batch.counts, enriched: 140, scored: 140, unscored: null, sendable: null, manual_review: null },
      reconciliation: { status: "unknown", matched: 0, unmatched_enriched: null, unmatched_scored: 140, ambiguous: 0 },
      current_sendability: { status: "incompatible", count: null },
      historical_packets: { sendable: 16, manual_review: 28, watchlist: 0, no_packet: 96 },
    };
    mockDashboard([legacy]);

    render(<MemoryRouter><App /></MemoryRouter>);

    expect(await screen.findByText("Legacy", { selector: ".lifecycle-badge" })).toBeInTheDocument();
    expect(screen.getByText("16 historical packet-ready")).toBeInTheDocument();
    expect(screen.getByText("Unknown", { selector: ".unscored-value" })).toBeInTheDocument();
    expect(screen.queryByText("16 sendable")).not.toBeInTheDocument();
  });

  it("sends the selected lifecycle to the batch API", async () => {
    mockDashboard([batch]);
    render(<MemoryRouter><App /></MemoryRouter>);
    await screen.findByText(batch.batch);

    fireEvent.change(screen.getByLabelText("Lifecycle"), { target: { value: "legacy" } });

    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("lifecycle=legacy"), expect.anything());
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

  it("ignores a superseded overview refresh after filters change", async () => {
    const oldOverview = deferred<ReturnType<typeof response>>();
    const oldBatches = deferred<ReturnType<typeof response>>();
    const newer = { ...batch, batch: "detailing_cleveland_medium_002" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(overview))
      .mockResolvedValueOnce(response({ generated_at: overview.generated_at, total: 1, items: [batch] }))
      .mockReturnValueOnce(oldOverview.promise)
      .mockReturnValueOnce(oldBatches.promise)
      .mockResolvedValueOnce(response(overview))
      .mockResolvedValueOnce(response({ generated_at: overview.generated_at, total: 1, items: [newer] }));
    vi.stubGlobal("fetch", fetchMock);
    render(<MemoryRouter><App /></MemoryRouter>);
    await screen.findByText(batch.batch);
    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    fireEvent.change(screen.getByLabelText("Market"), { target: { value: "cleveland" } });
    expect(await screen.findByText(newer.batch)).toBeInTheDocument();
    const oldSignal = fetchMock.mock.calls[2][1].signal as AbortSignal;
    expect(oldSignal.aborted).toBe(true);
    await act(async () => {
      oldOverview.resolve(response(overview));
      oldBatches.resolve(response({ generated_at: overview.generated_at, total: 1, items: [batch] }));
      await Promise.resolve();
    });
    expect(screen.getByText(newer.batch)).toBeInTheDocument();
    expect(screen.queryByText(batch.batch)).not.toBeInTheDocument();
  });

  it("ignores an older detail page after a newer lead query", async () => {
    const oldDetail = deferred<ReturnType<typeof response>>();
    const oldLeads = deferred<ReturnType<typeof response>>();
    const initialLeads = { generated_at: overview.generated_at, batch: batch.batch, total: 2, page: 1, page_size: 1, pages: 2, items: [{ key: "one", scoring_state: "scored", business_name: "Initial Lead", city: "Cleveland", website_url: "", google_maps_url: "", score_total: 80, priority: "A", packet_tier: "Sendable Packet", sendable: true, sendability_state: "current", declared_sendable: true, readiness_mismatch: false, money_signal: true, ai_confidence: "high", blockers: "" }] };
    const newerLeads = { ...initialLeads, total: 1, pages: 1, items: [{ ...initialLeads.items[0], key: "new", business_name: "New Query Lead" }] };
    const staleLeads = { ...initialLeads, page: 2, items: [{ ...initialLeads.items[0], key: "old", business_name: "Old Page Lead" }] };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(batch))
      .mockResolvedValueOnce(response(initialLeads))
      .mockReturnValueOnce(oldDetail.promise)
      .mockReturnValueOnce(oldLeads.promise)
      .mockResolvedValueOnce(response(batch))
      .mockResolvedValueOnce(response(newerLeads));
    vi.stubGlobal("fetch", fetchMock);
    render(<MemoryRouter initialEntries={[`/batches/${batch.batch}`]}><App /></MemoryRouter>);
    await screen.findByText("Initial Lead");
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    fireEvent.change(screen.getByLabelText("Find business"), { target: { value: "New" } });
    expect(await screen.findByText("New Query Lead")).toBeInTheDocument();
    await act(async () => {
      oldDetail.resolve(response(batch));
      oldLeads.resolve(response(staleLeads));
      await Promise.resolve();
    });
    expect(screen.getByText("New Query Lead")).toBeInTheDocument();
    expect(screen.queryByText("Old Page Lead")).not.toBeInTheDocument();
  });
});
