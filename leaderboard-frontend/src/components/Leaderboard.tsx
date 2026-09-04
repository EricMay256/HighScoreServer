// src/components/Leaderboard.tsx
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getScores, ApiError } from "../api/client";
import type { Period, SortOrder } from "../api/types";
import ModeTabs from "./ModeTabs";
import PeriodTabs from "./PeriodTabs";
import ScoresTable from "./ScoresTable";

/**
 * Why there is no active game mode, when there isn't one.
 *
 * `loading` may still resolve; `failed` and `none` will not, and they are
 * different problems — one is the server being unreachable, the other is a
 * database nobody has seeded.
 */
export type ModeAvailability = "loading" | "ready" | "failed" | "none";

interface LeaderboardProps {
  gameMode: string;
  onGameModeChange: (mode: string) => void;
  modeAvailability: ModeAvailability;
}

export default function Leaderboard({
  gameMode,
  onGameModeChange,
  modeAvailability,
}: LeaderboardProps) {
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
    // Empty until the mode list resolves, and /scores requires a mode.
    enabled: gameMode !== "",
  });

  return (
    <div className="lb-container">
      <div className="lb-header">
        <h1 className="lb-title">
          {gameMode ? `${gameMode.toUpperCase()} MODE` : "LEADERBOARD"}
        </h1>
        <p className="lb-subtitle">Top scores</p>
      </div>

      <ModeTabs
        selected={gameMode}
        onChange={onGameModeChange}
        onSortOrderChange={setSortOrder}
      />

      <PeriodTabs selected={period} onChange={setPeriod} />

      {/* Neither of these will resolve on their own, so a spinner would sit
          there indefinitely claiming otherwise. They are told apart because
          the reader can act on one of them and not the other. */}
      {modeAvailability === "failed" && (
        <div className="lb-error" role="alert">
          ⚠ Could not load game modes. Reload to try again.
        </div>
      )}

      {modeAvailability === "none" && (
        <div className="lb-error" role="alert">
          No game modes are configured on this server yet.
        </div>
      )}

      {(isLoading || modeAvailability === "loading") && (
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