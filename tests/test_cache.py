import threading
from app.cache import MemoryCache


# ── Helpers ────────────────────────────────────────────────────────────────

class FakeClock:
    """Monotonic fake clock for deterministic TTL tests."""
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_cache(clock: FakeClock | None = None, ttl: int = 120) -> MemoryCache:
    return MemoryCache(maxsize=128, default_ttl=ttl, timer=clock)


# ── Basic operations ───────────────────────────────────────────────────────

def test_get_missing_key_returns_none():
    cache = make_cache()
    assert cache.get("nope") is None


def test_setex_then_get_returns_value():
    cache = make_cache()
    cache.setex("k", 120, "v")
    assert cache.get("k") == "v"


def test_setex_overwrites_existing_value():
    cache = make_cache()
    cache.setex("k", 120, "first")
    cache.setex("k", 120, "second")
    assert cache.get("k") == "second"


def test_delete_removes_key():
    cache = make_cache()
    cache.setex("k", 120, "v")
    cache.delete("k")
    assert cache.get("k") is None


def test_delete_missing_key_is_noop():
    cache = make_cache()
    cache.delete("never-existed")  # should not raise


# ── TTL expiry ─────────────────────────────────────────────────────────────

def test_value_available_before_ttl():
    clock = FakeClock()
    cache = make_cache(clock=clock)
    cache.setex("k", 120, "v")
    clock.advance(119)
    assert cache.get("k") == "v"


def test_value_expired_after_ttl():
    clock = FakeClock()
    cache = make_cache(clock=clock)
    cache.setex("k", 120, "v")
    clock.advance(121)
    assert cache.get("k") is None


def test_reset_after_expiry():
    clock = FakeClock()
    cache = make_cache(clock=clock)
    cache.setex("k", 120, "v1")
    clock.advance(121)
    cache.setex("k", 120, "v2")
    assert cache.get("k") == "v2"


# ── close ──────────────────────────────────────────────────────────────────

def test_close_clears_entries():
    cache = make_cache()
    cache.setex("k", 120, "v")
    cache.close()
    assert cache.get("k") is None


# ── Thread safety ──────────────────────────────────────────────────────────

def test_concurrent_writes_do_not_corrupt_cache():
    """Smoke test: many threads writing/reading should not raise."""
    cache = make_cache()
    errors: list[Exception] = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(200):
                key = f"t{thread_id}:k{i}"
                cache.setex(key, 120, str(i))
                assert cache.get(key) == str(i)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
