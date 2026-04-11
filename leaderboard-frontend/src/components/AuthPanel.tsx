// src/components/AuthPanel.tsx
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { login, register, guestLogin, ApiError } from "../api/client";
import type { TokenResponse } from "../api/types";

type Tab = "login" | "register" | "guest";

export default function AuthPanel() {
  // Inner tab state is local UI only — not server state, so useState is
  // correct. The auth store's pub/sub is what drives the App-level switch
  // between AuthPanel and the logged-in panels; this just picks which
  // form to show inside AuthPanel.
  const [tab, setTab] = useState<Tab>("login");

  return (
    <div className="submit-panel">
      <h2 className="submit-title">Account</h2>

      <nav className="period-tabs" aria-label="Auth mode">
        <button
          type="button"
          className={`period-tab ${tab === "login" ? "period-tab--active" : ""}`}
          onClick={() => setTab("login")}
        >Login</button>
        <button
          type="button"
          className={`period-tab ${tab === "register" ? "period-tab--active" : ""}`}
          onClick={() => setTab("register")}
        >Register</button>
        <button
          type="button"
          className={`period-tab ${tab === "guest" ? "period-tab--active" : ""}`}
          onClick={() => setTab("guest")}
        >Guest</button>
      </nav>

      {tab === "login" && <LoginForm />}
      {tab === "register" && <RegisterForm />}
      {tab === "guest" && <GuestForm />}
    </div>
  );
}

// ── Login ──────────────────────────────────────────────────────────────

function LoginForm() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  // No onSuccess handler needed — the api client's login() calls setTokens
  // internally, which fires the auth store's pub/sub, which re-renders App,
  // which unmounts AuthPanel entirely. This component just needs to know
  // when the request errors.
  const mutation = useMutation<TokenResponse, ApiError, void>({
    mutationFn: () => login({ username, password }),
  });

  const disabled = mutation.isPending || !username || !password;

  return (
    <div className="submit-form">
      <div className="form-row">
        <label className="form-label" htmlFor="login-username">Username</label>
        <input
          id="login-username"
          type="text"
          className="form-input"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
      </div>
      <div className="form-row">
        <label className="form-label" htmlFor="login-password">Password</label>
        <input
          id="login-password"
          type="password"
          className="form-input"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      <button
        type="button"
        className="submit-btn"
        onClick={() => mutation.mutate()}
        disabled={disabled}
      >
        {mutation.isPending ? "Logging in…" : "Login"}
      </button>
      {mutation.isError && (
        <div className="form-result form-result--error">{mutation.error.detail}</div>
      )}
    </div>
  );
}

// ── Register ───────────────────────────────────────────────────────────

function RegisterForm() {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const mutation = useMutation<TokenResponse, ApiError, void>({
    mutationFn: () => register({ username, email, password }),
  });

  const disabled = mutation.isPending || !username || !email || !password;

  return (
    <div className="submit-form">
      <div className="form-row">
        <label className="form-label" htmlFor="reg-username">Username</label>
        <input
          id="reg-username" type="text" className="form-input"
          value={username} onChange={(e) => setUsername(e.target.value)}
        />
      </div>
      <div className="form-row">
        <label className="form-label" htmlFor="reg-email">Email</label>
        <input
          id="reg-email" type="email" className="form-input"
          value={email} onChange={(e) => setEmail(e.target.value)}
        />
      </div>
      <div className="form-row">
        <label className="form-label" htmlFor="reg-password">Password</label>
        <input
          id="reg-password" type="password" className="form-input"
          value={password} onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      <button
        type="button" className="submit-btn"
        onClick={() => mutation.mutate()} disabled={disabled}
      >
        {mutation.isPending ? "Creating…" : "Register"}
      </button>
      {mutation.isError && (
        <div className="form-result form-result--error">{mutation.error.detail}</div>
      )}
    </div>
  );
}

// ── Guest ──────────────────────────────────────────────────────────────

function GuestForm() {
  const mutation = useMutation<TokenResponse, ApiError, void>({
    mutationFn: () => guestLogin(),
  });

  return (
    <div className="submit-form">
      <p style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
        Play without an account. You can claim it later to keep your scores.
      </p>
      <button
        type="button" className="submit-btn"
        onClick={() => mutation.mutate()} disabled={mutation.isPending}
      >
        {mutation.isPending ? "Creating…" : "Continue as Guest"}
      </button>
      {mutation.isError && (
        <div className="form-result form-result--error">{mutation.error.detail}</div>
      )}
    </div>
  );
}