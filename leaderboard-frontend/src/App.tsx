// src/App.tsx
import { useState } from "react";
import { useAuth } from "./auth/store";
import { logout } from "./api/client";
import Leaderboard from "./components/Leaderboard";
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
  // Change "classic" if your seed data uses a different default mode name.
  const [gameMode, setGameMode] = useState<string>("classic");

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
          <nav>
            {auth.isAuthenticated && (
              <>
                <span style={{ marginRight: "1rem", color: "var(--text-muted)" }}>
                  {auth.username}{auth.isGuest ? " (guest)" : ""}
                </span>
                <button
                  type="button"
                  className="logout-btn"
                  onClick={handleLogout}
                >
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
          <Leaderboard gameMode={gameMode} onGameModeChange={setGameMode} />

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

      <footer className="site-footer">HIGHSCORESERVER · PORTFOLIO BUILD</footer>
    </>
  );
}