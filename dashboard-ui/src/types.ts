export type Health = "healthy" | "attention" | "blocked";
export type Progress =
  | "configured"
  | "searched"
  | "enriched"
  | "partial_scoring"
  | "scored"
  | "packets_current"
  | "airtable_ready"
  | "finished";

export interface Issue {
  code: string;
  severity: "attention" | "blocked";
  message: string;
}

export interface BatchSummary {
  batch: string;
  vertical: string;
  market: string;
  size: string;
  sequence: string;
  location: string;
  query_count: number;
  progress: Progress;
  progress_rank: number;
  health: Health;
  health_rank: number;
  counts: {
    raw: number;
    deduped: number;
    enriched: number;
    scored: number;
    unscored: number;
    packets: number;
    sendable: number;
    manual_review: number;
    watchlist: number;
    no_packet: number;
  };
  stages: Record<string, { status: string; count: number }>;
  issues: Issue[];
  latest_activity_at: string;
  next_action: string;
  pending_approval: { active: boolean; stage: string; expires_at: string };
  llm_usage: {
    available: boolean;
    calls: number;
    call_cap: number;
    tokens: number;
    token_cap: number;
    estimated_usd: number;
    usd_cap: number;
    stopped_by_cap: boolean;
    stop_reason: string;
    updated_at: string;
  };
  airtable: {
    dry_run: { status: string; timestamp: string; post: number; patch: number; skip: number; warnings: string[] };
    live: { status: string; timestamp: string; post: number; patch: number; skip: number };
    current_input_hash: string;
  };
  artifacts: Record<string, { path: string; exists: boolean; rows: number | null; size_bytes: number | null; updated_at: string }>;
}

export interface Overview {
  generated_at: string;
  repo_root: string;
  totals: {
    batches: number;
    needs_attention: number;
    unscored: number;
    sendable: number;
    airtable_ready: number;
    finished: number;
  };
  pipeline: Array<{ key: string; label: string; count: number }>;
  attention: Array<Issue & { batch: string; vertical: string; market: string; latest_activity_at: string; next_action: string }>;
  filters: { verticals: string[]; markets: string[]; progress: string[]; health: string[] };
  source_health: { ignored_noncanonical_artifacts: number; authority: string };
}

export interface BatchesResponse {
  generated_at: string;
  total: number;
  items: BatchSummary[];
}

export interface LeadRow {
  key: string;
  scoring_state: "scored" | "unscored";
  business_name: string;
  city: string;
  website_url: string;
  google_maps_url: string;
  score_total: string | number;
  priority: string;
  packet_tier: string;
  sendable: boolean;
  money_signal: boolean;
  ai_confidence: string;
  blockers: string;
}

export interface LeadsResponse {
  generated_at: string;
  batch: string;
  total: number;
  page: number;
  page_size: number;
  pages: number;
  items: LeadRow[];
}
