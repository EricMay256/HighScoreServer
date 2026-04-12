// src/components/PeriodTabs.tsx
import type { Period } from "../api/types";

interface PeriodTabsProps {
  selected: Period;
  onChange: (period: Period) => void;
}

// Static list — periods are a fixed enum on the server (see Period in
// types.ts), so there's no point fetching them. Label is the display
// string; value is what the API expects.
// ReadonlyArray as mutations are not necessary
const PERIODS: ReadonlyArray<{ value: Period; label: string }> = [
  { value: "alltime", label: "All Time" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
];

export default function PeriodTabs({ selected, onChange }: PeriodTabsProps) {
  return (
    <nav className="period-tabs" aria-label="Time period selector">
      {PERIODS.map((p) => (
        <button
          key={p.value}
          type="button"
          className={`period-tab ${
            p.value === selected ? "period-tab--active" : ""
          }`}
          onClick={() => onChange(p.value)}
        >
          {p.label}
        </button>
      ))}
    </nav>
  );
}