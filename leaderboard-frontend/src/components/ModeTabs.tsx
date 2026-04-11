// src/components/ModeTabs.tsx
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { getGameModes } from "../api/client";
import type { GameModeConfig, SortOrder } from "../api/types";

interface ModeTabsProps {
  selected: string;
  onChange: (mode: string) => void;
  onSortOrderChange: (order: SortOrder) => void;
}

export default function ModeTabs({
  selected,
  onChange,
  onSortOrderChange,
}: ModeTabsProps) {
  const { data: modes, isLoading, isError } = useQuery({
    queryKey: ["gameModes"],
    queryFn: getGameModes,
    // Game modes change rarely — cache for 5 minutes to avoid refetching
    // every time the component mounts.
    staleTime: 5 * 60_000,
  });

  // When the selected mode changes (or modes load for the first time),
  // report its sort_order up to the parent so the score column arrow
  // matches. Effect rather than inline call to avoid setState-during-render.
  useEffect(() => {
    if (!modes) return;
    const current = modes.find((m: GameModeConfig) => m.name === selected);
    if (current) onSortOrderChange(current.sort_order);
  }, [modes, selected, onSortOrderChange]);

  if (isLoading || isError || !modes) {
    // Tabs are non-critical chrome — fail silent rather than blocking the
    // page. The scores query will surface its own error if the API is down.
    return <nav className="mode-tabs" aria-label="Game mode selector" />;
  }

  return (
    <nav className="mode-tabs" aria-label="Game mode selector">
      {modes.map((mode) => (
        <button
          key={mode.name}
          type="button"
          className={`mode-tab ${
            mode.name === selected ? "mode-tab--active" : ""
          }`}
          onClick={() => onChange(mode.name)}
        >
          {mode.label ?? mode.name}
        </button>
      ))}
    </nav>
  );
}