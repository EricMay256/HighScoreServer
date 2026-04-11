import {
  getAccessToken,
  getRefreshToken,
  setTokens,
  clearTokens,
} from "../auth/store";
import type {
  ClaimRequest,
  GameModeConfig,
  LeaderboardResponse,
  LoginRequest,
  Period,
  RegisterRequest,
  RenameRequest,
  ScoreResponse,
  ScoreSubmission,
  TokenResponse,
} from "./types";

const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "";

// ── Error type ─────────────────────────────────────────────────────────────

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function extractDetail(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
    return JSON.stringify(body);
  } catch {
    return res.statusText || "Unknown error";
  }
}

// ── Single-flight refresh ──────────────────────────────────────────────────

// Module-scoped: concurrent 401s share one refresh attempt.
// Resolves to true on success, false on failure (tokens already cleared).
let refreshPromise: Promise<boolean> | null = null;

function refreshTokens(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;

  refreshPromise = (async () => {
    const refresh_token = getRefreshToken();
    if (!refresh_token) {
      clearTokens();
      return false;
    }
    try {
      const res = await fetch(`${BASE_URL}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token }),
      });
      if (!res.ok) {
        clearTokens();
        return false;
      }
      const tokens = (await res.json()) as TokenResponse;
      setTokens(tokens.access_token, tokens.refresh_token);
      return true;
    } catch {
      clearTokens();
      return false;
    } finally {
      // Cleared in a microtask so concurrent awaiters all see the resolved
      // value before the slot is freed for the next refresh cycle.
      queueMicrotask(() => {
        refreshPromise = null;
      });
    }
  })();

  return refreshPromise;
}

// ── Core request function ──────────────────────────────────────────────────

interface RequestOptions {
  method?: string;
  body?: unknown;
  auth?: boolean; // attach Bearer token; enables 401-refresh-retry
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = false } = opts;

  const doFetch = async (): Promise<Response> => {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (auth) {
      const token = getAccessToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;
    }
    return fetch(`${BASE_URL}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  };

  let res = await doFetch();

  // 401-refresh-and-retry, only for authenticated requests, only once.
  if (res.status === 401 && auth) {
    const ok = await refreshTokens();
    if (ok) {
      res = await doFetch();
    }
    // If refresh failed, fall through and surface the original 401 below.
  }

  if (!res.ok) {
    const detail = await extractDetail(res);
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ── Leaderboard endpoints ──────────────────────────────────────────────────

export function getScores(
  game_mode: string,
  period: Period = "alltime",
): Promise<LeaderboardResponse> {
  const qs = new URLSearchParams({ game_mode, period }).toString();
  return request<LeaderboardResponse>(`/api/leaderboard/scores?${qs}`);
}

export function getGameModes(): Promise<GameModeConfig[]> {
  return request<GameModeConfig[]>("/api/leaderboard/game_modes");
}

export function getLatestScores(): Promise<ScoreResponse[]> {
  return request<ScoreResponse[]>("/api/leaderboard/latest");
}

export function submitScore(
  submission: ScoreSubmission,
): Promise<ScoreResponse> {
  return request<ScoreResponse>("/api/leaderboard/scores", {
    method: "POST",
    body: submission,
    auth: true,
  });
}

// ── Auth endpoints ─────────────────────────────────────────────────────────

async function authCall(
  path: string,
  body: unknown,
): Promise<TokenResponse> {
  const tokens = await request<TokenResponse>(path, { method: "POST", body });
  setTokens(tokens.access_token, tokens.refresh_token);
  return tokens;
}

export function register(body: RegisterRequest): Promise<TokenResponse> {
  return authCall("/api/auth/register", body);
}

export function login(body: LoginRequest): Promise<TokenResponse> {
  return authCall("/api/auth/login", body);
}

export function guestLogin(): Promise<TokenResponse> {
  return authCall("/api/auth/guest", {});
}

// Manual refresh — most callers shouldn't need this since the interceptor
// handles 401s automatically. Exposed for proactive session extension.
export async function refresh(): Promise<TokenResponse> {
  const ok = await refreshTokens();
  if (!ok) throw new ApiError(401, "Refresh failed");
  // Tokens are now stored; return current values for callers that want them.
  return {
    access_token: getAccessToken() ?? "",
    refresh_token: getRefreshToken() ?? "",
    token_type: "bearer",
  };
}

export async function logout(): Promise<void> {
  const refresh_token = getRefreshToken();
  try {
    if (refresh_token) {
      await request<void>("/api/auth/logout", {
        method: "POST",
        body: { refresh_token },
      });
    }
  } finally {
    clearTokens();
  }
}

export function rename(body: RenameRequest): Promise<void> {
  return request<void>("/api/auth/rename", {
    method: "POST",
    body,
    auth: true,
  });
}

export async function claim(body: ClaimRequest): Promise<TokenResponse> {
  const tokens = await request<TokenResponse>("/api/auth/claim", {
    method: "POST",
    body,
    auth: true,
  });
  setTokens(tokens.access_token, tokens.refresh_token);
  return tokens;
}