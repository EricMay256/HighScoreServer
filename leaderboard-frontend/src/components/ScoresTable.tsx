// src/components/ScoresTable.tsx
import type { ScoreResponse, SortOrder } from "../api/types";

interface ScoresTableProps {
  scores: ScoreResponse[];
  sortOrder: SortOrder;
}

// Medal for top 3, plain rank number otherwise. Mirrors the Jinja template's
// {% if entry.rank == 1 %}🥇 ... {% else %}{{ entry.rank }} structure.
function rankCell(rank: number | null): string {
  if (rank === 1) return "🥇";
  if (rank === 2) return "🥈";
  if (rank === 3) return "🥉";
  return rank?.toString() ?? "—";
}

// Server sends ISO 8601 (see ScoreResponse.submitted_at in types.ts).
// Jinja template renders it raw; we slice to YYYY-MM-DD for the monospace
// date column. If the server ever pre-formats, drop this and render raw.
function formatDate(iso: string): string {
  return iso.slice(0, 10);
}

/**
 * Pure presentational table. All data comes from props — no queries, no
 * state. Parent (<Leaderboard />) owns fetching and passes sortOrder down
 * from the selected game mode's config so the score column header arrow
 * matches the actual query ordering.
 */
export default function ScoresTable({ scores, sortOrder }: ScoresTableProps) {
  const arrow = sortOrder === "ASC" ? "▲" : "▼";

  return (
    <table className="lb-table" aria-label="Leaderboard scores">
      <thead>
        <tr>
          <th className="col-rank">Rank</th>
          <th className="col-player">Player</th>
          <th className="col-score"> Score {arrow}</th>
          <th className="col-percentile">Percentile</th>
          <th className="col-date">Date</th>
        </tr>
      </thead>
      <tbody>
        {scores.map((entry) => {
          const isTop = entry.rank !== null && entry.rank <= 3;
          return (
            <tr
              key={entry.id}
              className={`lb-row ${isTop ? "lb-row--top" : ""}`}
            >
              <td className="col-rank">{rankCell(entry.rank)}</td>
              <td className="col-player">{entry.player}</td>
              <td className="col-score">{entry.score.toLocaleString()}</td>
              <td className="col-percentile">
                {entry.percentile !== null ? `${entry.percentile}%` : "—"}
              </td>
              <td className="col-date">{formatDate(entry.submitted_at)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}