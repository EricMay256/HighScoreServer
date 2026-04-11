// src/components/SubmitPanel.tsx
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { submitScore, ApiError } from "../api/client";
import type { ScoreResponse } from "../api/types";

interface SubmitPanelProps {
  gameMode: string;
}

export default function SubmitPanel({ gameMode }: SubmitPanelProps) {
  const [scoreInput, setScoreInput] = useState<string>("");
  const queryClient = useQueryClient();

  const mutation = useMutation<ScoreResponse, ApiError, number>({
    mutationFn: (score) => submitScore({ score, game_mode: gameMode }),
    onSuccess: () => {
      // Invalidate every cached leaderboard query (all modes, all periods).
      // The default key match is prefix-based, so ["scores"] matches every
      // ["scores", mode, period] entry. The next time the user views any
      // leaderboard view it refetches fresh data.
      // Note: invalidation bypasses staleTime, so the 30s freshness window
      // configured in main.tsx doesn't suppress this refetch.
      queryClient.invalidateQueries({ queryKey: ["scores"] });
      setScoreInput("");
    },
  });

  const handleSubmit = () => {
    const parsed = Number(scoreInput);
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) return;
    mutation.mutate(parsed);
  };

  const disabled = mutation.isPending || scoreInput.trim() === "";

  return (
    <div className="submit-panel">
      <h2 className="submit-title">Submit Score · {gameMode}</h2>

      <div className="submit-form">
        <div className="form-row">
          <label className="form-label" htmlFor="score-input">Score</label>
          <input
            id="score-input"
            type="number"
            className="form-input form-input--score"
            value={scoreInput}
            onChange={(e) => setScoreInput(e.target.value)}
            placeholder="0"
          />
        </div>

        <div className="form-row--inline">
          <button
            type="button"
            className="submit-btn"
            onClick={handleSubmit}
            disabled={disabled}
          >
            {mutation.isPending ? "Submitting…" : "Submit"}
          </button>
        </div>

        {mutation.isSuccess && mutation.data && (
          <div className="form-result form-result--success">
            Submitted! Rank #{mutation.data.rank ?? "—"} ·{" "}
            {mutation.data.percentile !== null
              ? `${mutation.data.percentile}%`
              : "—"}
          </div>
        )}

        {mutation.isError && (
          // ApiError.detail is the raw server message; .message is prefixed
          // with "API {status}: " which reads awkwardly in user-facing UI.
          <div className="form-result form-result--error">
            {mutation.error.detail}
          </div>
        )}
      </div>
    </div>
  );
}