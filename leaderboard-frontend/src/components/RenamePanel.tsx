// src/components/RenamePanel.tsx
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { rename, ApiError } from "../api/client";
import type { AccessTokenResponse } from "../api/types";
import { useAuth } from "../auth/store";

export default function RenamePanel() {
  const auth = useAuth();
  const [username, setUsername] = useState("");

  // rename() stores the reissued access token, so useAuth().username updates
  // as soon as the mutation settles. It used to return nothing, leaving the
  // JWT's username claim stale for up to an hour until the next refresh.
  // Only the access token: a rename does not invalidate the refresh token.
  const mutation = useMutation<AccessTokenResponse, ApiError, void>({
    mutationFn: () => rename({ username }),
    onSuccess: () => setUsername(""),
  });

  const disabled = mutation.isPending || !username.trim();

  return (
    <div className="submit-panel">
      <h2 className="submit-title">Change Username</h2>

      <div className="submit-form">
        <div className="form-row">
          <label className="form-label" htmlFor="rename-input">New Username</label>
          <input
            id="rename-input"
            type="text"
            className="form-input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder={auth.username ?? "username"}
          />
        </div>
        <button
          type="button"
          className="submit-btn"
          onClick={() => mutation.mutate()}
          disabled={disabled}
        >
          {mutation.isPending ? "Updating…" : "Rename"}
        </button>
        {mutation.isSuccess && (
          <div className="form-result form-result--success">
            Username updated. Will appear in the header after your next session refresh.
          </div>
        )}
        {mutation.isError && (
          <div className="form-result form-result--error">
            {mutation.error.detail}
          </div>
        )}
      </div>
    </div>
  );
}