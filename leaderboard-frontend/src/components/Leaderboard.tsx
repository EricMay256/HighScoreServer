// src/components/Leaderboard.tsx
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getScores, ApiError } from "../api/client";
import type { Period, SortOrder } from "../api/types";
import ModeTabs from "./ModeTabs";
import PeriodTabs from "./PeriodTabs";
import ScoresTable from "./ScoresTable";

interface LeaderboardProps {
  gameMode: string;
  onGameModeChange: (mode: string) => void;
}

export default function Leaderboard({ gameMode, onGameModeChange }: LeaderboardProps) {
  // Period stays local — only Leaderboard cares about it. If SubmitPanel
  // ever needs to know which period the user is viewing (it doesn't, since
  // submissions go to all periods server-side via the snapshot upsert),
  // lift this to App the same way gameMode was lifted.
  const [period, setPeriod] = useState<Period>("alltime");

  // ModeTabs reports the selected mode's sort_order up via
  // onSortOrderChange so the score column arrow in ScoresTable matches
  // the server's actual ORDER BY direction. Lifted to this component
  // (rather than owned inside ModeTabs) so ScoresTable can read it as
  // a sibling prop without prop-drilling through the tabs.
  const [sortOrder, setSortOrder] = useState<SortOrder>("DESC");

  const {
    data,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["scores", gameMode, period],
    queryFn: () => getScores(gameMode, period),
  });

  return (
    <div className="lb-container">
      <div className="lb-header">
        <h1 className="lb-title">{gameMode.toUpperCase()} MODE</h1>
        <p className="lb-subtitle">Top scores</p>
      </div>

      <ModeTabs
        selected={gameMode}
        onChange={onGameModeChange}
        onSortOrderChange={setSortOrder}
      />

      <PeriodTabs selected={period} onChange={setPeriod} />

      {isLoading && (
        <div className="lb-loading">
          <div className="lb-spinner" />
          Loading scores…
        </div>
      )}

      {isError && (
        <div className="lb-error" role="alert">
          ⚠ {error instanceof ApiError ? error.detail : "Failed to load scores"}
        </div>
      )}

      {!isLoading && !isError && data && data.scores.length === 0 && (
        <div className="lb-empty">
          <p>No scores yet for <strong>{gameMode}</strong> mode.</p>
          <p>Be the first to submit one.</p>
        </div>
      )}

      {!isLoading && !isError && data && data.scores.length > 0 && (
        <ScoresTable scores={data.scores} sortOrder={sortOrder} />
      )}
    </div>
  );
}