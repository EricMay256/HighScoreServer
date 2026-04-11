// Mirrors app/models.py and app/auth_routes.py request/response shapes.
// Field names are snake_case to match the API exactly — no client-side mapping.

export type Period = "alltime" | "daily" | "weekly";
export type SortOrder = "ASC" | "DESC";

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
}

export interface LeaderboardResponse {
  scores: ScoreResponse[];
  total_count: number;
}

export interface ScoreSubmission {
  score: number;
  game_mode: string;
}

export interface GameModeConfig {
  name: string;
  sort_order: SortOrder;
  label: string | null;
  requires_auth: boolean;
}

export interface GameModeCreate {
  name: string;
  sort_order?: SortOrder; // defaults to "DESC" server-side
  label?: string | null;
  requires_auth?: boolean;
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