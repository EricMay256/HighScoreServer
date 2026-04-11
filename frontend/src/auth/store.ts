import { useSyncExternalStore } from "react";
import type { JwtPayload } from "../api/types";

const ACCESS_KEY = "leaderboard_access_token";
const REFRESH_KEY = "leaderboard_refresh_token";

// ── Pub/sub ────────────────────────────────────────────────────────────────

type Listener = () => void;
const listeners = new Set<Listener>();

function emit(): void {
  for (const l of listeners) l();
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// ── Token accessors ────────────────────────────────────────────────────────

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}

export function setTokens(access: string, refresh: string): void {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
  emit();
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
  emit();
}

// ── JWT decoding (claims only — no signature verification) ─────────────────

function base64UrlDecode(input: string): string {
  // Convert base64url → base64, pad, then atob
  let b64 = input.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4;
  if (pad) b64 += "=".repeat(4 - pad);
  // atob → binary string → UTF-8 via decodeURIComponent trick
  const binary = atob(b64);
  try {
    return decodeURIComponent(
      binary
        .split("")
        .map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0"))
        .join(""),
    );
  } catch {
    return binary;
  }
}

function decodeJwt(token: string): JwtPayload | null {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const json = base64UrlDecode(parts[1]);
    return JSON.parse(json) as JwtPayload;
  } catch {
    return null;
  }
}

// ── useAuth hook ───────────────────────────────────────────────────────────

export interface AuthState {
  isAuthenticated: boolean;
  isGuest: boolean;
  username: string | null;
}

const UNAUTHENTICATED: AuthState = {
  isAuthenticated: false,
  isGuest: false,
  username: null,
};

// useSyncExternalStore requires a stable snapshot reference between calls
// when nothing has changed, otherwise React will warn about infinite loops.
// We cache the last computed snapshot and only recompute when the token changes.
let cachedToken: string | null = null;
let cachedSnapshot: AuthState = UNAUTHENTICATED;

function getSnapshot(): AuthState {
  const token = getAccessToken();
  if (token === cachedToken) return cachedSnapshot;

  cachedToken = token;
  if (!token) {
    cachedSnapshot = UNAUTHENTICATED;
    return cachedSnapshot;
  }

  const payload = decodeJwt(token);
  if (!payload) {
    cachedSnapshot = UNAUTHENTICATED;
    return cachedSnapshot;
  }

  // Treat expired tokens as unauthenticated for UI purposes. The API client
  // will still attempt a refresh on the next request — this is purely
  // about what the UI shows.
  const nowSec = Math.floor(Date.now() / 1000);
  if (payload.exp <= nowSec) {
    cachedSnapshot = UNAUTHENTICATED;
    return cachedSnapshot;
  }

  cachedSnapshot = {
    isAuthenticated: true,
    isGuest: payload.is_guest,
    username: payload.username,
  };
  return cachedSnapshot;
}

export function useAuth(): AuthState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}