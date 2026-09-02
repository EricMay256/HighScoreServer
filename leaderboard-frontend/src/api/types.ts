// Mirrors app/models.py and app/auth_routes.py request/response shapes.
// Field names are snake_case to match the API exactly — no client-side mapping.
//
// Hand-maintained, not generated. Last reconciled 2026-09-02 against
// app/models.py at d388043 and app/auth_routes.py at 48b66fb. When the server
// models change, diff against /openapi.json and update this file in the same
// change. Run submission (RunSubmission) is deliberately absent: the SPA does
// not post runs.

export type Period = "alltime" | "daily" | "weekly";
export type SortOrder = "ASC" | "DESC";
export type ScoringStrategy = "best" | "cumulative";

// ── Leaderboard ────────────────────────────────────────────────────────────

export interface ScoreResponse {
  id: number;
  player: string;
  score: number;
  game_mode: string;
  period: string | null;
  submitted_at: string; // ISO 8601
  rank: number | null;
  percentile: number | null;
  // Raw and cumulative submissions return false/0. A score produced by a
  // validated run sets these.
  validated: boolean;
  validation_tier: number;
}

export interface LeaderboardResponse {
  scores: ScoreResponse[];
  total_count: number;
}

export interface ScoreSubmission {
  score: number;
  game_mode: string;
  // Required only when the target mode is cumulative; the server validates
  // that against the looked-up mode.
  idempotency_key?: string | null;
}

export interface GameModeConfig {
  name: string;
  sort_order: SortOrder;
  label: string | null;
  requires_claimed_account: boolean;
  // Surfaced read-only; the client does not enforce them.
  required_tier: number; // 0 = raw via /scores, >=1 = run required via /runs
  scoring_strategy: ScoringStrategy;
  game_key: string | null;
  max_score: number | null; // null inherits the global cap
}

export interface GameModeCreate {
  name: string;
  sort_order?: SortOrder; // defaults to "DESC" server-side
  label?: string | null;
  requires_claimed_account?: boolean;
  required_tier?: number;
  scoring_strategy?: ScoringStrategy;
  game_key?: string | null;
  max_score?: number | null;
}

// ── Auth ───────────────────────────────────────────────────────────────────

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string; // "bearer"
}

export interface RegisterRequest {
  username: string;
  email: string;
  password: string;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface RefreshRequest {
  refresh_token: string;
}

export interface ClaimRequest {
  email: string;
  password: string;
}

export interface RenameRequest {
  username: string;
}

// ── JWT payload (decoded client-side for UI state only) ────────────────────

export interface JwtPayload {
  sub: string;       // user_id as string
  username: string;
  is_guest: boolean;
  exp: number;       // unix seconds
  iat: number;
}