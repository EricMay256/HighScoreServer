# gunicorn targets this module. `composed` returns the leaderboard alone, or the
# leaderboard and the vault behind one dispatcher when VAULT_ENABLED (host ADR 0016).
from composed import app  # noqa: F401
