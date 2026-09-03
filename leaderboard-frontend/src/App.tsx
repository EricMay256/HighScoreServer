// src/App.tsx
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "./auth/store";
import { getGameModes, logout } from "./api/client";
import Leaderboard from "./components/Leaderboard";
import type { ModeAvailability } from "./components/Leaderboard";
import AuthPanel from "./components/AuthPanel";
import SubmitPanel from "./components/SubmitPanel";
import RenamePanel from "./components/RenamePanel";
import ClaimPanel from "./components/ClaimPanel";

export default function App() {
  // useAuth subscribes to the auth store via useSyncExternalStore. Any
  // setTokens/clearTokens call (login, logout, refresh, claim) triggers a
  // re-render here automatically — no context provider, no prop drilling.
  const auth = useAuth();

  // Single source of truth for the active game mode. Lifted here so both
  // Leaderboard (which displays scores for it) and SubmitPanel (which
  // submits to it) stay in sync — submitting a score always targets the
  // mode the user is currently viewing, which is what players expect.
  //
  // Null until the user picks one, then the server's first mode. Nothing here
  // names a mode: the initial state used to be the literal "blitz", so any
  // database without a mode of that name opened on an empty board. Shares
  // ModeTabs' query key, so deriving it costs no extra request.
  const { data: modes, isError: modesFailed } = useQuery({
    queryKey: ["gameModes"],
    queryFn: getGameModes,
    staleTime: 5 * 60_000,
  });
  const [selectedMode, setSelectedMode] = useState<string | null>(null);
  const gameMode = selectedMode ?? modes?.[0]?.name ?? "";

  // An empty gameMode has three causes and they are not interchangeable, so
  // Leaderboard is told which rather than left to assume the hopeful one.
  // "none" is a real reachable state, not a defensive branch: the baseline
  // migration creates `game_modes` empty and seeding is a separate step, so a
  // freshly migrated database resolves this query successfully to [].
  const modeAvailability: ModeAvailability =
    gameMode !== ""
      ? "ready"
      : modesFailed
        ? "failed"
        : modes !== undefined
          ? "none"
          : "loading";

  const handleLogout = async () => {
    // logout() clears tokens in its finally block even if the network call
    // fails, so the UI will flip to logged-out regardless.
    await logout();
  };

  return (
    <>
      <header className="site-header">
        <div className="header-inner">
          <span className="site-title">HIGHSCORESERVER</span>
          <nav className="site-nav">
          <a href="/" className="site-nav-link">
            Home
          </a>
          <a href="/leaderboard" className="site-nav-link">
            Leaderboard
          </a>
          {auth.isAuthenticated && (
            <>
              <span className="site-user-chip">
                {auth.username}{auth.isGuest ? " (guest)" : ""}
              </span>
              <button type="button" className="logout-btn" onClick={handleLogout}>
                Logout
              </button>
            </>
          )}
        </nav>
        </div>
      </header>

      <main className="site-main">
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr)",
            gap: "2rem",
            maxWidth: "1200px",
            margin: "0 auto",
          }}
        >
          <Leaderboard
            gameMode={gameMode}
            onGameModeChange={setSelectedMode}
            modeAvailability={modeAvailability}
          />

          <aside style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
            {auth.isAuthenticated ? (
              <>
                <SubmitPanel gameMode={gameMode} />
                <RenamePanel />
                {auth.isGuest && <ClaimPanel />}
              </>
            ) : (
              <AuthPanel />
            )}
          </aside>
        </div>
      </main>

      <footer className="site-footer">
        HIGHSCORESERVER · PORTFOLIO BUILD · <a href="/docs">API Reference</a>
      </footer>
    </>
  );
}