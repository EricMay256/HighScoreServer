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

# ── Prefix delete ──────────────────────────────────────────────────────────
#
# delete_prefix is what the leaderboard route invalidation depends on. Cache
# keys for /scores have shape `leaderboard:{mode}:{period}:{limit}:{offset}`,
# so a score submission deletes by the prefix `leaderboard:{mode}:`. The
# trailing-colon boundary case is the most important — without it, mode
# `classic` would invalidate `classic_endless` too.


def test_delete_prefix_removes_matching_keys():
    cache = make_cache()
    cache.setex("foo:1", 120, "a")
    cache.setex("foo:2", 120, "b")
    cache.setex("bar:1", 120, "c")
    deleted = cache.delete_prefix("foo:")
    assert deleted == 2
    assert cache.get("foo:1") is None
    assert cache.get("foo:2") is None
    assert cache.get("bar:1") == "c"


def test_delete_prefix_no_matches_returns_zero():
    cache = make_cache()
    cache.setex("bar:1", 120, "c")
    assert cache.delete_prefix("foo:") == 0
    assert cache.get("bar:1") == "c"


def test_delete_prefix_empty_prefix_clears_cache():
    """An empty prefix matches every key — useful as a flush operation."""
    cache = make_cache()
    cache.setex("a", 120, "1")
    cache.setex("b", 120, "2")
    deleted = cache.delete_prefix("")
    assert deleted == 2
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_delete_prefix_respects_trailing_colon_boundary():
    """
    `foo:` should not match keys starting with `foo` but not `foo:`.
    This is the boundary that protects /scores invalidation from accidentally
    nuking a sibling mode whose name shares a prefix with the submitted mode.
    """
    cache = make_cache()
    cache.setex("foo:1", 120, "a")
    cache.setex("foobar", 120, "b")
    deleted = cache.delete_prefix("foo:")
    assert deleted == 1
    assert cache.get("foo:1") is None
    assert cache.get("foobar") == "b"


def test_delete_prefix_concurrent_with_writes():
    """
    Smoke test: prefix delete running concurrently with writers should not
    corrupt the cache. The per-method lock guarantees consistency, but we
    exercise the contended path to catch lock-ordering regressions.
    """
    import threading
    cache = make_cache()
    errors: list[Exception] = []

    def writer(thread_id: int) -> None:
        try:
            for i in range(100):
                cache.setex(f"prefix:t{thread_id}:k{i}", 120, str(i))
                cache.setex(f"other:t{thread_id}:k{i}", 120, str(i))
        except Exception as e:
            errors.append(e)

    def deleter() -> None:
        try:
            for _ in range(50):
                cache.delete_prefix("prefix:")
        except Exception as e:
            errors.append(e)

    threads = (
        [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        + [threading.Thread(target=deleter) for _ in range(2)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    # `other:` keys should be untouched by any prefix delete.
    other_keys_present = any(
        cache.get(f"other:t{tid}:k{i}") is not None
        for tid in range(4)
        for i in range(100)
    )
    assert other_keys_present

