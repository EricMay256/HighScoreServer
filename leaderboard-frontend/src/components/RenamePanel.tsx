// src/components/RenamePanel.tsx
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { rename, ApiError } from "../api/client";
import { useAuth } from "../auth/store";

export default function RenamePanel() {
  const auth = useAuth();
  const [username, setUsername] = useState("");

  // Rename does NOT return new tokens — the existing JWT keeps its old
  // username claim until it expires (~60 min) and the next refresh issues
  // a fresh one. This means useAuth().username will lag behind the actual
  // server state until that refresh happens. Acceptable for portfolio
  // scope; the alternative is having /api/auth/rename return a new token
  // pair, which is a server-side change worth considering as a TODO.
  const mutation = useMutation<void, ApiError, void>({
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