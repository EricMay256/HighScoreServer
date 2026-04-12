// src/components/ClaimPanel.tsx
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { claim, ApiError } from "../api/client";
import type { TokenResponse } from "../api/types";

export default function ClaimPanel() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  // On success, claim() in the api client calls setTokens with the new
  // non-guest tokens. The auth store fires, App re-renders, useAuth().isGuest
  // flips to false, and App unmounts this panel automatically. The success
  // flash below is therefore only visible for the brief moment between
  // mutation resolving and React committing the parent re-render — in
  // practice it may not appear at all, which is fine: the panel disappearing
  // *is* the success signal.
  const mutation = useMutation<TokenResponse, ApiError, void>({
    mutationFn: () => claim({ email, password }),
  });

  const disabled = mutation.isPending || !email || !password;

  return (
    <div className="submit-panel">
      <h2 className="submit-title">Claim Account</h2>
      <p style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
        Add an email and password to keep your guest scores permanently.
      </p>

      <div className="submit-form">
        <div className="form-row">
          <label className="form-label" htmlFor="claim-email">Email</label>
          <input
            id="claim-email"
            type="email"
            className="form-input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="form-row">
          <label className="form-label" htmlFor="claim-password">Password</label>
          <input
            id="claim-password"
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
          {mutation.isPending ? "Claiming…" : "Claim Account"}
        </button>
        {mutation.isError && (
          <div className="form-result form-result--error">
            {mutation.error.detail}
          </div>
        )}
      </div>
    </div>
  );
}