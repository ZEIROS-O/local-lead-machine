import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, Route, Routes, useParams } from "react-router-dom";
import { getJson, params } from "./api";
import type { BatchSummary, BatchesResponse, Health, LeadsResponse, Overview, Progress } from "./types";

const REFRESH_MS = 30_000;

const progressLabels: Record<Progress, string> = {
  configured: "Configured",
  searched: "Discovered",
  enriched: "Enriched",
  partial_scoring: "Partial scoring",
  scored: "Scored",
  packets_current: "Packets current",
  airtable_ready: "Airtable ready",
  finished: "Finished",
};

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (match) => match.toUpperCase());
}

function formatTime(value: string): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat().format(value);
}

function countText(value: number | null): string {
  return value === null ? "Unknown" : formatNumber(value);
}

function StatusPill({ health, label }: { health?: Health; label: string }) {
  return <span className={`status-pill ${health ? `status-${health}` : ""}`}>{label}</span>;
}

function AppShell({ children, refreshedAt, stale, onRefresh }: { children: React.ReactNode; refreshedAt?: string; stale?: boolean; onRefresh?: () => void }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link className="brand" to="/" aria-label="Local Lead Machine dashboard home">
          <span className="brand-mark" aria-hidden="true"><span /><span /><span /></span>
          <span><strong>Local Lead Machine</strong><small>Pipeline control</small></span>
        </Link>
        <div className="topbar-status">
          <span className={`live-indicator ${stale ? "is-stale" : ""}`} aria-hidden="true" />
          <span>{stale ? "Last known data" : "Read-only live view"}</span>
          {refreshedAt && <time dateTime={refreshedAt}>{formatTime(refreshedAt)}</time>}
          {onRefresh && <button className="refresh-button" onClick={onRefresh}>Refresh data</button>}
        </div>
      </header>
      {stale && <div className="stale-banner" role="status">The latest refresh failed. Showing the last successful artifact snapshot.</div>}
      <main id="main-content">{children}</main>
      <footer><span>Artifact authority: current files + run-state receipts</span><span>No pipeline commands are exposed</span></footer>
    </div>
  );
}

function MetricStrip({ overview }: { overview: Overview }) {
  const metrics = [
    ["Active batches", overview.totals.batches],
    ["Need attention", overview.totals.needs_attention],
    ["Unscored leads", overview.totals.unscored],
    ["Current sendable", overview.totals.sendable],
    ["Airtable ready", overview.totals.airtable_ready],
    ["Finished", overview.totals.finished],
  ] as const;
  return (
    <div className="metric-strip" aria-label="Pipeline totals">
      {metrics.map(([label, value]) => (
        <div className="metric" key={label}><span>{label}</span><strong>{formatNumber(value)}</strong></div>
      ))}
    </div>
  );
}

function PipelineRail({ stages }: { stages: Overview["pipeline"] }) {
  const maximum = Math.max(...stages.map((stage) => stage.count), 1);
  return (
    <section className="pipeline-section" aria-labelledby="pipeline-title">
      <div className="section-heading">
        <div><span className="eyebrow">Current throughput</span><h2 id="pipeline-title">Pipeline, end to end</h2></div>
        <p>Rows across the selected batches. Exact counts are shown; widths share one scale.</p>
      </div>
      <div className="pipeline-rail">
        {stages.map((stage, index) => (
          <div className="pipeline-stage" key={stage.key} style={{ "--stage-index": index } as React.CSSProperties}>
            <div className="stage-index">0{index + 1}</div>
            <div className="stage-copy"><span>{stage.label}</span><strong>{formatNumber(stage.count)}</strong></div>
            <div className="stage-track" aria-hidden="true"><span style={{ transform: `scaleX(${stage.count / maximum})` }} /></div>
          </div>
        ))}
      </div>
    </section>
  );
}

interface Filters {
  vertical: string;
  market: string;
  lifecycle: string;
  view: string;
  q: string;
}

function FilterBar({ filters, setFilters, options }: { filters: Filters; setFilters: (value: Filters) => void; options: Overview["filters"] }) {
  return (
    <div className="filter-bar" aria-label="Batch filters">
      <label><span>Find batch</span><input value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} placeholder="Vertical, market, or batch" /></label>
      <label><span>Vertical</span><select value={filters.vertical} onChange={(event) => setFilters({ ...filters, vertical: event.target.value })}><option value="">All verticals</option>{options.verticals.map((value) => <option key={value}>{value}</option>)}</select></label>
      <label><span>Market</span><select value={filters.market} onChange={(event) => setFilters({ ...filters, market: event.target.value })}><option value="">All markets</option>{options.markets.map((value) => <option key={value}>{value}</option>)}</select></label>
      <label><span>Lifecycle</span><select value={filters.lifecycle} onChange={(event) => setFilters({ ...filters, lifecycle: event.target.value })}><option value="all">All batches</option><option value="active">Active</option><option value="legacy">Legacy</option></select></label>
      <label><span>View</span><select value={filters.view} onChange={(event) => setFilters({ ...filters, view: event.target.value })}><option value="">All batches</option><option value="attention">Needs attention</option><option value="unscored">Has unscored leads</option><option value="airtable_ready">Airtable ready</option><option value="finished">Finished</option></select></label>
    </div>
  );
}

function AttentionQueue({ items }: { items: Overview["attention"] }) {
  return (
    <aside className="attention-panel" aria-labelledby="attention-title">
      <div className="section-heading compact"><div><span className="eyebrow">Triage first</span><h2 id="attention-title">Needs attention</h2></div><span className="queue-count">{items.length}</span></div>
      {items.length ? <ol>{items.slice(0, 8).map((item, index) => (
        <li key={`${item.batch}-${item.code}-${index}`}>
          <span className={`severity-mark ${item.severity}`} aria-hidden="true" />
          <div><Link to={`/batches/${item.batch}`}>{item.batch}</Link><p>{item.message}</p><code>{item.next_action}</code></div>
        </li>
      ))}</ol> : <div className="empty-state"><strong>No active warnings</strong><p>Current canonical batches have no artifact or receipt issues.</p></div>}
    </aside>
  );
}

function BatchTable({ batches }: { batches: BatchSummary[] }) {
  return (
    <div className="table-wrap">
      <table className="batch-table">
        <thead><tr><th>Batch</th><th>Progress</th><th>Enriched / scored</th><th>Unscored</th><th>Packet mix</th><th>Airtable</th><th>Latest activity</th><th>Next action</th></tr></thead>
        <tbody>{batches.map((batch) => (
          <tr key={batch.batch}>
            <td data-label="Batch"><Link className="batch-link" to={`/batches/${batch.batch}`}>{batch.batch}</Link><span className={`lifecycle-badge lifecycle-${batch.lifecycle}`}>{humanize(batch.lifecycle)}</span><small>{humanize(batch.vertical)} · {humanize(batch.market)} · {batch.size}</small></td>
            <td data-label="Progress"><StatusPill health={batch.health} label={progressLabels[batch.progress]} /></td>
            <td data-label="Enriched / scored"><strong className="tabular">{batch.counts.enriched} / {batch.counts.scored}</strong></td>
            <td data-label="Unscored"><span className={`unscored-value ${batch.counts.unscored ? "attention-number" : "quiet-number"}`}>{countText(batch.counts.unscored)}</span></td>
            <td data-label="Packet mix">{batch.lifecycle === "legacy" && batch.historical_packets ? <span className="packet-mix historical-packet"><span>{batch.historical_packets.sendable} historical packet-ready</span> <i /> <span>{batch.historical_packets.manual_review} historical manual</span></span> : <span className="packet-mix"><b>{countText(batch.counts.sendable)}</b> current sendable <i /> <b>{countText(batch.counts.manual_review)}</b> manual</span>}</td>
            <td data-label="Airtable"><span className={`receipt-status receipt-${batch.airtable.evidence.status}`}>{humanize(batch.airtable.evidence.status)}</span></td>
            <td data-label="Latest activity"><time dateTime={batch.latest_activity_at}>{formatTime(batch.latest_activity_at)}</time></td>
            <td data-label="Next action"><code>{batch.next_action}</code></td>
          </tr>
        ))}</tbody>
      </table>
      {!batches.length && <div className="empty-state table-empty"><strong>No batches match these filters</strong><p>Clear a filter to return to the full canonical inventory.</p></div>}
    </div>
  );
}

function OverviewPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [filters, setFilters] = useState<Filters>({ vertical: "", market: "", lifecycle: "all", view: "", q: "" });
  const [error, setError] = useState("");
  const request = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });

  const load = useCallback(async (manual = false) => {
    request.current.controller?.abort();
    const controller = new AbortController();
    const generation = request.current.generation + 1;
    request.current = { generation, controller };
    try {
      const base = { vertical: filters.vertical, market: filters.market, refresh: manual || undefined };
      const batchQuery: Record<string, string | boolean | undefined> = { ...base, q: filters.q, lifecycle: filters.lifecycle };
      if (filters.view === "attention") batchQuery.health = "needs_attention";
      if (filters.view === "unscored") batchQuery.unscored = true;
      if (filters.view === "airtable_ready") batchQuery.progress = "airtable_ready";
      if (filters.view === "finished") batchQuery.finished = true;
      const [overviewData, batchData] = await Promise.all([
        getJson<Overview>(`/api/overview${params(base)}`, controller.signal),
        getJson<BatchesResponse>(`/api/batches${params(batchQuery)}`, controller.signal),
      ]);
      if (request.current.generation === generation && !controller.signal.aborted) {
        setOverview(overviewData);
        setBatches(batchData.items);
        setError("");
      }
    } catch (caught) {
      if (request.current.generation === generation && !controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : "Dashboard data could not be refreshed");
      }
    }
  }, [filters]);

  useEffect(() => {
    void load();
    const interval = window.setInterval(() => void load(), REFRESH_MS);
    return () => {
      window.clearInterval(interval);
      request.current.controller?.abort();
    };
  }, [load]);

  const manualRefresh = () => void load(true);
  if (!overview) return <AppShell stale={Boolean(error)}><div className="loading-screen"><div className="loading-rail" /><strong>Reading pipeline artifacts…</strong>{error && <p>{error}</p>}</div></AppShell>;

  return (
    <AppShell refreshedAt={overview.generated_at} stale={Boolean(error)} onRefresh={manualRefresh}>
      <div className="page overview-page">
        <section className="intro-grid">
          <div><span className="eyebrow">Operations overview</span><h1>Know where every batch stands.</h1><p>Current artifact truth from Google discovery through verified Airtable receipts.</p></div>
          <div className="source-note"><span>Source boundary</span><strong>{overview.source_health.authority}</strong><small>{overview.source_health.ignored_noncanonical_artifacts} legacy or debug artifact names intentionally excluded</small></div>
        </section>
        <MetricStrip overview={overview} />
        <PipelineRail stages={overview.pipeline} />
        <section className="operations-grid">
          <div className="inventory-section">
            <div className="section-heading"><div><span className="eyebrow">Batch inventory</span><h2>Available work</h2></div><p>{batches.length} of {overview.inventory_totals.all} batch{overview.inventory_totals.all === 1 ? "" : "es"} in this view</p></div>
            <FilterBar filters={filters} setFilters={setFilters} options={overview.filters} />
            <BatchTable batches={batches} />
          </div>
          <AttentionQueue items={overview.attention} />
        </section>
      </div>
    </AppShell>
  );
}

function DetailStageRail({ batch }: { batch: BatchSummary }) {
  const stages = [
    ["01", "Google discovery", batch.stages.discovery],
    ["02", "Enrichment", batch.stages.enrichment],
    ["03", "LLM scoring", batch.stages.scoring],
    ["04", "Sendability + packets", batch.stages.packets],
    ["05", "Airtable", batch.stages.airtable],
  ] as const;
  return <div className="detail-stage-rail">{stages.map(([number, label, stage]) => <div className={`detail-stage stage-${stage.status}`} key={label}><span>{number}</span><div><small>{label}</small><strong>{formatNumber(stage.count)}</strong><em>{humanize(stage.status)}</em></div></div>)}</div>;
}

function UsagePanel({ batch }: { batch: BatchSummary }) {
  const usage = batch.llm_usage;
  return <section className="detail-panel usage-panel"><div className="panel-heading"><span>Latest scoring usage</span><small>{usage.updated_at ? formatTime(usage.updated_at) : "No usage artifact"}</small></div>{usage.available ? <><div className="usage-number"><strong>${usage.estimated_usd.toFixed(3)}</strong><span>of ${usage.usd_cap.toFixed(2)} run cap</span></div><dl><div><dt>Calls</dt><dd>{usage.calls} / {usage.call_cap || "—"}</dd></div><div><dt>Tokens</dt><dd>{formatNumber(usage.tokens)} / {usage.token_cap ? formatNumber(usage.token_cap) : "—"}</dd></div><div><dt>Cap stop</dt><dd>{usage.stopped_by_cap ? usage.stop_reason || "Yes" : "No"}</dd></div></dl></> : <div className="empty-state"><strong>No usage receipt</strong><p>This batch has no canonical LLM usage file.</p></div>}</section>;
}

function AirtablePanel({ batch }: { batch: BatchSummary }) {
  const { dry_run: dry, live, custom, evidence } = batch.airtable;
  return <section className="detail-panel airtable-panel"><div className="panel-heading"><span>Airtable receipts</span><small>Evidence: {humanize(evidence.status)}{evidence.records === null ? "" : ` · ${evidence.records} rows`}</small></div><div className="receipt-grid"><div><span>Dry-run</span><strong className={`receipt-status receipt-${dry.status}`}>{humanize(dry.status)}</strong><small>{formatTime(dry.timestamp)}</small><dl><div><dt>POST</dt><dd>{dry.post}</dd></div><div><dt>PATCH</dt><dd>{dry.patch}</dd></div><div><dt>SKIP</dt><dd>{dry.skip}</dd></div></dl></div><div><span>Live</span><strong className={`receipt-status receipt-${live.status}`}>{humanize(live.status)}</strong><small>{formatTime(live.timestamp)}</small><dl><div><dt>POST</dt><dd>{live.post}</dd></div><div><dt>PATCH</dt><dd>{live.patch}</dd></div><div><dt>SKIP</dt><dd>{live.skip}</dd></div></dl></div><div><span>Custom cohort</span><strong className={`receipt-status receipt-${custom.status}`}>{humanize(custom.status)}</strong><small>{custom.cohort ? humanize(custom.cohort) : "No custom receipt"}</small><dl><div><dt>ROWS</dt><dd>{custom.records}</dd></div><div><dt>VERIFIED</dt><dd>{custom.verification_patch}</dd></div><div><dt>AT</dt><dd>{formatTime(custom.timestamp)}</dd></div></dl></div></div></section>;
}

function ArtifactPanel({ batch }: { batch: BatchSummary }) {
  return <section className="artifact-panel"><div className="panel-heading"><span>Artifact manifest</span><small>Current paths, no downloads</small></div><ul>{Object.entries(batch.artifacts).map(([key, artifact]) => <li key={key}><span className={`artifact-dot ${artifact.exists ? "exists" : "missing"}`} aria-hidden="true" /><div><strong>{humanize(key)}</strong><code>{artifact.path}</code></div><span>{artifact.rows === null ? "—" : `${artifact.rows} rows`}</span><time>{formatTime(artifact.updated_at)}</time></li>)}</ul></section>;
}

function LeadTable({ response }: { response: LeadsResponse }) {
  const sendabilityLabel = (lead: LeadsResponse["items"][number]) => {
    if (lead.sendability_state === "historical") return "Historical";
    if (lead.sendability_state === "not_scored") return "Not scored";
    if (lead.sendability_state === "incompatible") return "Unknown — incompatible";
    return lead.sendable ? "Yes" : lead.readiness_mismatch ? "No — contract mismatch" : "No";
  };
  return <div className="table-wrap"><table className="lead-table"><thead><tr><th>Business</th><th>Scoring</th><th>Score</th><th>Priority</th><th>Packet tier</th><th>Money signal</th><th>Sendable</th><th>Confidence</th><th>Blockers</th></tr></thead><tbody>{response.items.map((lead, index) => <tr key={lead.key || `${lead.business_name}-${index}`}><td data-label="Business"><strong>{lead.business_name || "Unnamed lead"}</strong><small>{lead.city}</small><span className="row-links">{lead.website_url && <a href={lead.website_url} target="_blank" rel="noreferrer">Website</a>}{lead.google_maps_url && <a href={lead.google_maps_url} target="_blank" rel="noreferrer">Maps</a>}</span></td><td data-label="Scoring"><span className={`scoring-state ${lead.scoring_state}`}>{humanize(lead.scoring_state)}</span></td><td data-label="Score"><strong className="tabular">{lead.score_total || "—"}</strong></td><td data-label="Priority">{lead.priority || "—"}</td><td data-label="Packet tier">{lead.packet_tier || "—"}</td><td data-label="Money signal">{lead.money_signal ? "Present" : "No"}</td><td data-label="Sendable">{sendabilityLabel(lead)}</td><td data-label="Confidence">{lead.ai_confidence || "—"}</td><td data-label="Blockers"><span className="blocker-text">{lead.blockers || "None recorded"}</span></td></tr>)}</tbody></table>{!response.items.length && <div className="empty-state table-empty"><strong>No leads match this view</strong><p>Adjust the scoring-state filter or search term.</p></div>}</div>;
}

function BatchDetailPage() {
  const { batch: batchId = "" } = useParams();
  const [batch, setBatch] = useState<BatchSummary | null>(null);
  const [leads, setLeads] = useState<LeadsResponse | null>(null);
  const [leadState, setLeadState] = useState("all");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const request = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });

  const load = useCallback(async (manual = false) => {
    request.current.controller?.abort();
    const controller = new AbortController();
    const generation = request.current.generation + 1;
    request.current = { generation, controller };
    try {
      const refresh = manual || undefined;
      const [detail, leadData] = await Promise.all([
        getJson<BatchSummary>(`/api/batches/${encodeURIComponent(batchId)}${params({ refresh })}`, controller.signal),
        getJson<LeadsResponse>(`/api/batches/${encodeURIComponent(batchId)}/leads${params({ state: leadState, q: query, page, page_size: 50, refresh })}`, controller.signal),
      ]);
      if (request.current.generation === generation && !controller.signal.aborted) {
        setBatch(detail); setLeads(leadData); setError("");
      }
    } catch (caught) {
      if (request.current.generation === generation && !controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : "Batch could not be read");
      }
    }
  }, [batchId, leadState, query, page]);

  useEffect(() => {
    void load();
    const interval = window.setInterval(() => void load(), REFRESH_MS);
    return () => {
      window.clearInterval(interval);
      request.current.controller?.abort();
    };
  }, [load]);
  const manualRefresh = () => void load(true);
  if (!batch || !leads) return <AppShell stale={Boolean(error)}><div className="loading-screen"><div className="loading-rail" /><strong>Reading batch artifacts…</strong>{error && <p>{error}</p>}<Link to="/">Return to overview</Link></div></AppShell>;

  return <AppShell refreshedAt={leads.generated_at} stale={Boolean(error)} onRefresh={manualRefresh}><div className="page detail-page"><nav className="breadcrumb" aria-label="Breadcrumb"><Link to="/">Pipeline overview</Link><span>/</span><span>{batch.batch}</span></nav><section className="detail-header"><div><span className="eyebrow">{humanize(batch.vertical)} · {humanize(batch.market)} · {batch.size}</span><h1>{batch.batch}</h1><p>{batch.counts.enriched} enriched, {batch.counts.scored} scored, {countText(batch.counts.unscored)} waiting.</p>{batch.legacy && <p className="legacy-note">Historical batch · {batch.legacy.note || humanize(batch.legacy.reason)}</p>}</div><div className="header-status"><span className={`lifecycle-badge lifecycle-${batch.lifecycle}`}>{humanize(batch.lifecycle)}</span><StatusPill health={batch.health} label={progressLabels[batch.progress]} /><small>Latest artifact {formatTime(batch.latest_activity_at)}</small></div></section><DetailStageRail batch={batch} />{batch.issues.length > 0 && <section className="issue-list" aria-labelledby="batch-issues"><div><span className="eyebrow">Current health</span><h2 id="batch-issues">{batch.issues.length} item{batch.issues.length === 1 ? "" : "s"} to resolve</h2></div><ul>{batch.issues.map((issue) => <li key={issue.code}><span className={`severity-mark ${issue.severity}`} /><div><strong>{humanize(issue.code)}</strong><p>{issue.message}</p></div></li>)}</ul></section>}<section className="next-action"><span>Recommended next command</span><code>{batch.next_action}</code><small>Display only. This dashboard cannot execute commands.</small></section><div className="detail-grid"><UsagePanel batch={batch} /><AirtablePanel batch={batch} /></div><ArtifactPanel batch={batch} /><section className="lead-section"><div className="section-heading"><div><span className="eyebrow">Lead triage</span><h2>Current batch rows</h2></div><p>{leads.total} lead{leads.total === 1 ? "" : "s"}; contact fields intentionally excluded</p></div><div className="lead-toolbar"><label><span>Find business</span><input value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} placeholder="Business or city" /></label><label><span>Scoring state</span><select value={leadState} onChange={(event) => { setLeadState(event.target.value); setPage(1); }}><option value="all">All leads</option><option value="unscored">Unscored only</option><option value="scored">Scored only</option></select></label></div><LeadTable response={leads} />{leads.pages > 1 && <nav className="pagination" aria-label="Lead pages"><button disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Previous page</button><span>Page {leads.page} of {leads.pages}</span><button disabled={page >= leads.pages} onClick={() => setPage((value) => value + 1)}>Next page</button></nav>}</section></div></AppShell>;
}

export default function App() {
  return <Routes><Route path="/" element={<OverviewPage />} /><Route path="/batches/:batch" element={<BatchDetailPage />} /><Route path="*" element={<AppShell><div className="loading-screen"><strong>Dashboard page not found</strong><Link to="/">Return to pipeline overview</Link></div></AppShell>} /></Routes>;
}
