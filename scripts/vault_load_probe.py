"""Drive concurrent vault traffic and record what the connection pool did.

Why this exists
---------------
The vault-enablement review leaves one condition open that cannot be closed by
reading code: observe capacity *after real vault traffic*. Its own words are
that the current sparse traffic "is not a saturation test". This produces that
traffic and collects both halves of the evidence at once.

What it can and cannot tell you
-------------------------------
The pools are hard-capped by construction -- the vault engine sets
``max_overflow=0`` and HSS's pool has a ``max_size`` -- and
``validate_connection_budget`` refuses to boot if the arithmetic does not fit.
So this is **not** proving you stay under the database's connection limit; you
cannot exceed it. It answers two narrower questions:

- **Was the budget big enough?** A pool that is full but never refuses anyone is
  correctly sized. A refusal is a caller that received a 503. Read that from the
  ``Vault pool`` log lines: peak and failures, per worker, cumulative.
- **Is the connection picture what we modelled?** Sampling ``pg_stat_activity``
  during load catches a consumer nobody counted, or pool settings that never
  took effect. Nothing logs HSS's own pool, so this is the only view of the
  total.

Reading the result
------------------
**A clean run is a valid answer, not a failed test.** The vault deliberately
releases its connection *during* the embedding call, so connections are held
only for database work and a well-sized pool will often show no failures at all.
What a clean run does not tell you is the margin. To learn that, raise
concurrency on the ``contribute`` tier until the first failure appears, then
back off -- rather than concluding "no failures at whatever load I happened to
generate".

The tiers, in ascending order of pressure
-----------------------------------------
- ``get_note`` -- one checkout, no embedding call. Pure connection churn, the
  highest rate per quota unit, and free.
- ``search`` -- two checkouts with a provider round trip *between* them. Closest
  to real read traffic. Costs one embedding call per request.
- ``contribute`` -- the worst case, and the reason this script bothers with
  writes at all. Contribution takes a corpus-wide ``pg_advisory_xact_lock`` and
  re-checks idempotency under it, so concurrent contributions serialise while
  each holds a connection. Sending one fixed idempotency key means the first
  request inserts and every later one **replays**: same lock contention, same
  connection holding, exactly one note created. The script prints its id.

Two things that will otherwise waste the run
--------------------------------------------
- **Quotas are per principal**, so one credential measures the rate limiter
  rather than the pool: ``search`` is 30/min burst 10, ``get_note`` 120/min
  burst 30. Pass several credentials issued under *different* ``--name`` values.
- **The pre-auth guard is IP-keyed at 600/min**, about ten requests a second
  from one address. ``--rate`` defaults below that on purpose. Raising it means
  relaxing a production safety control, so prefer more credentials over a
  higher rate.

The summary separates 429s from 503s for exactly this reason: a run full of
429s measured a limiter, not a pool.

Usage
-----
    python scripts/vault_load_probe.py \
        --base-url https://high-score-server-9db572197af4.herokuapp.com \
        --token hssv1_a_... --token hssv1_b_... \
        --tier get_note --concurrency 20 --duration 60

Add ``--sample-dsn`` with the deployment's database URL to sample the
database-wide connection count alongside the load. That sampler holds one
connection, which is one of the two the budget reserves for operators.
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx


# Well under the 600/min pre-auth IP guard, leaving room for the sampler and for
# whatever ordinary traffic the deployment is already serving.
_DEFAULT_RATE_PER_SECOND = 8.0
# The pre-auth guard is 600/minute, keyed on the client address.
_PREAUTH_REQUESTS_PER_SECOND = 10.0
_SAMPLE_INTERVAL_SECONDS = 2.0

# Unique per run, and both halves have to be. A fixed body flags against the
# note the *previous* run left behind -- identical content is exactly what the
# dedup gate exists to catch -- and a flagged note cannot be retired while its
# review case is open (ADR 0019), so the tier would poison itself after one
# use. A run id keeps each run's first request an insert and every later one a
# replay, leaving one retirable note per run.
_RUN_ID = f"{int(time.time())}-{os.getpid()}"
_CONTRIBUTION_KEY = f"load-probe-{_RUN_ID}"
_CONTRIBUTION_TITLE = f"Load probe: pool saturation rehearsal {_RUN_ID}"
_CONTRIBUTION_BODY = (
    "Written by scripts/vault_load_probe.py to exercise the contribution "
    "path's corpus-wide advisory lock under concurrency. One note is created "
    "per run however long it lasts, because the idempotency key is fixed for "
    f"the run and every later request replays. Run {_RUN_ID}. Retire it when "
    "the rehearsal is done."
)

_STATUS_LABELS = {
    0: "transport failure",
    200: "OK",
    401: "unauthenticated",
    403: "missing scope",
    409: "idempotency conflict",
    422: "rejected by governance",
    429: "RATE LIMITED (limiter, not pool)",
    503: "SATURATED (pool refused -- this is the number)",
}


@dataclass
class Outcome:
    """One request's result, in the terms the review cares about."""

    status: int
    seconds: float
    note_id: str = ""
    # Which transport error, when there was one. ReadTimeout and ConnectError
    # mean different things under load -- the first is the server taking too
    # long, the second is it refusing or dropping the connection outright.
    detail: str = ""


@dataclass
class Results:
    outcomes: list[Outcome] = field(default_factory=list)
    samples: list[tuple[int, int]] = field(default_factory=list)
    created_note_id: str | None = None


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


async def discover_note_ids(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    wanted: int,
) -> list[str]:
    """Find real note ids, so the read tier exercises a hit rather than a 404."""

    response = await client.get(
        f"{base_url}/api/v1/vault/search",
        params={"q": "the", "limit": min(wanted, 50)},
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return [hit["note_id"] for hit in response.json().get("hits", [])]


async def one_request(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    tier: str,
    note_ids: list[str],
    index: int,
) -> Outcome:
    headers = {"Authorization": f"Bearer {token}"}
    started = time.perf_counter()
    try:
        if tier == "get_note":
            note_id = note_ids[index % len(note_ids)]
            response = await client.get(
                f"{base_url}/api/v1/vault/notes/{note_id}",
                headers=headers,
            )
        elif tier == "search":
            response = await client.get(
                f"{base_url}/api/v1/vault/search",
                params={"q": "connection pool saturation", "limit": 5},
                headers=headers,
            )
        else:
            response = await client.post(
                f"{base_url}/api/v1/vault/contributions",
                headers=headers,
                json={
                    "title": _CONTRIBUTION_TITLE,
                    "body": _CONTRIBUTION_BODY,
                    "idempotency_key": _CONTRIBUTION_KEY,
                    "tags": ["load-probe"],
                },
            )
    except httpx.HTTPError as error:
        # A transport failure is evidence too: a dropped connection under load is
        # what an H12 looks like from the client side.
        return Outcome(
            status=0,
            seconds=time.perf_counter() - started,
            detail=type(error).__name__,
        )

    elapsed = time.perf_counter() - started
    note_id = ""
    if tier == "contribute" and response.status_code == 200:
        body = response.json()
        note_id = body.get("note_id") or ""
        detail = body.get("status") or ""
        return Outcome(
            status=response.status_code,
            seconds=elapsed,
            note_id=note_id,
            detail=detail,
        )
    return Outcome(status=response.status_code, seconds=elapsed, note_id=note_id)


async def sample_connections(dsn: str, results: Results, stop: asyncio.Event) -> None:
    """Record the database-wide connection count while the load runs.

    Separate from the vault's own counters on purpose: those are per worker and
    count only vault checkouts, so the total -- HSS's pool, the release dyno,
    this sampler itself -- is invisible to them.

    Never allowed to end the run: this is instrumentation, and losing the sample
    is much cheaper than losing the load.
    """

    import psycopg

    query = (
        "SELECT count(*), count(*) FILTER (WHERE state = 'active') "
        "FROM pg_stat_activity WHERE datname = current_database()"
    )
    try:
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            while not stop.is_set():
                async with connection.cursor() as cursor:
                    await cursor.execute(query)
                    row = await cursor.fetchone()
                    if row is not None:
                        results.samples.append((int(row[0]), int(row[1])))
                try:
                    await asyncio.wait_for(stop.wait(), _SAMPLE_INTERVAL_SECONDS)
                except TimeoutError:
                    pass
    except Exception as error:  # noqa: BLE001 - sampling must not kill the run
        print(
            f"connection sampling stopped: {type(error).__name__}",
            file=sys.stderr,
        )


async def run_probe(arguments: argparse.Namespace) -> Results:
    results = Results()
    tokens: list[str] = arguments.token
    base_url: str = arguments.base_url.rstrip("/")

    limits = httpx.Limits(max_connections=arguments.concurrency * 2)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        note_ids: list[str] = []
        if arguments.tier == "get_note":
            note_ids = await discover_note_ids(client, base_url, tokens[0], 20)
            if not note_ids:
                raise SystemExit(
                    "No notes found to fetch. Use --tier search on an empty corpus."
                )

        stop = asyncio.Event()
        sampler = (
            asyncio.create_task(sample_connections(arguments.sample_dsn, results, stop))
            if arguments.sample_dsn
            else None
        )

        counter = 0
        deadline = time.monotonic() + arguments.duration
        interval = 1.0 / arguments.rate if arguments.rate > 0 else 0.0
        in_flight: set[asyncio.Task[Outcome]] = set()

        while time.monotonic() < deadline:
            while len(in_flight) >= arguments.concurrency:
                done, in_flight = await asyncio.wait(
                    in_flight,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                results.outcomes.extend(task.result() for task in done)

            # The contribute tier deliberately uses one credential. The
            # idempotency ledger is keyed (principal_id, idempotency_key), so a
            # second principal finds no prior key, runs the whole write path,
            # meets an identical embedding and is *flagged* -- which opens a
            # review case, and ADR 0019 refuses to retire a document while one
            # is pending. Spreading writes across credentials therefore leaves
            # behind exactly the mess this tier promises not to make.
            token = (
                tokens[0]
                if arguments.tier == "contribute"
                else tokens[counter % len(tokens)]
            )
            in_flight.add(
                asyncio.create_task(
                    one_request(
                        client,
                        base_url,
                        token,
                        arguments.tier,
                        note_ids,
                        counter,
                    )
                )
            )
            counter += 1
            if interval:
                await asyncio.sleep(interval)

        if in_flight:
            done, _ = await asyncio.wait(in_flight)
            results.outcomes.extend(task.result() for task in done)

        stop.set()
        if sampler is not None:
            await sampler

    for outcome in results.outcomes:
        if outcome.note_id:
            results.created_note_id = outcome.note_id
            break
    return results


def report(arguments: argparse.Namespace, results: Results) -> None:
    statuses = Counter(outcome.status for outcome in results.outcomes)
    latencies = [outcome.seconds for outcome in results.outcomes]
    total = len(results.outcomes)

    print()
    print(f"tier         : {arguments.tier}")
    print(f"credentials  : {len(arguments.token)}")
    print(f"concurrency  : {arguments.concurrency}   rate cap {arguments.rate}/s")
    print(f"requests     : {total} over {arguments.duration:.0f}s")
    print()
    for status, count in sorted(statuses.items()):
        label = _STATUS_LABELS.get(status, "")
        shown = status if status else "ERR"
        print(f"  {shown:>4} x{count:<6} {label}")
    print()
    details = Counter(o.detail for o in results.outcomes if o.detail)
    if details:
        kinds = ", ".join(f"{name} x{count}" for name, count in details.items())
        # For contribute these are governed outcomes (inserted / flagged /
        # rejected); elsewhere they are transport error types.
        label = "outcomes" if arguments.tier == "contribute" else "transport"
        print(f"{label:<13}: {kinds}")
    if latencies:
        print(
            f"latency      : p50 {percentile(latencies, 0.5) * 1000:.0f}ms   "
            f"p95 {percentile(latencies, 0.95) * 1000:.0f}ms   "
            f"max {max(latencies) * 1000:.0f}ms   "
            f"mean {statistics.mean(latencies) * 1000:.0f}ms"
        )
    if results.samples:
        totals = [sample[0] for sample in results.samples]
        actives = [sample[1] for sample in results.samples]
        print(
            f"db conns     : max total {max(totals)}, max active {max(actives)}, "
            f"over {len(results.samples)} samples"
        )
    print()

    if statuses.get(429):
        achieved = total / arguments.duration if arguments.duration else 0.0
        print(
            f"NOTE: {statuses[429]} of {total} requests were rate limited, so "
            "they never reached" \
            f"\n      the pool. This run measured a limiter, not a budget."
        )
        # Two limiters, both answering 429, with opposite remedies. Which one
        # bound is inferable from the achieved rate: the pre-auth guard is
        # IP-keyed at 600/min, so anything near ten a second hits that first
        # and no number of credentials will change it.
        if achieved > _PREAUTH_REQUESTS_PER_SECOND:
            print(
                f"      At {achieved:.0f} requests/second you are past the "
                f"IP-keyed pre-auth guard\n      (~{_PREAUTH_REQUESTS_PER_SECOND:.0f}/s). "
                "Lower --rate. More credentials will not help:\n"
                "      that limiter is keyed on your address, not your principal."
            )
        else:
            print(
                "      The rate stayed within the IP guard, so this is the "
                "per-principal quota.\n      Add credentials issued under "
                "different --name values."
            )
    if statuses.get(503):
        print(
            "RESULT: the pool refused work. It is too small for this concurrency.\n"
            "        Raise VAULT_DB_POOL_SIZE and revisit the connection budget --\n"
            "        remembering HSS_PROCESS_COUNT has to move with it."
        )
    elif total and not statuses.get(429):
        print(
            "RESULT: no refusals at this concurrency. A valid answer, but it bounds\n"
            "        nothing. Raise --concurrency on the contribute tier until the\n"
            "        first 503 if you want to know the margin."
        )
    print()
    print("Now read the half this script cannot see, which is per worker:")
    print('  heroku logs --app <app> | grep "Vault pool"')
    if results.created_note_id:
        print()
        print(f"Created one note: {results.created_note_id}")
        print("Retire it with a vault:delete credential when the rehearsal is done.")
        print(
            "If any contribution came back flagged, retirement is refused"
            "\nuntil the review case is settled -- see ADR 0019."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive concurrent vault traffic and report what the pool did.",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help=(
            "Target root, for example https://high-score-server-x.herokuapp.com. "
            "Required and never defaulted: this points load at a live deployment."
        ),
    )
    parser.add_argument(
        "--token",
        action="append",
        required=True,
        help=(
            "Vault bearer token. Repeat for several credentials -- quotas are per "
            "principal, so one token measures the limiter rather than the pool."
        ),
    )
    parser.add_argument(
        "--tier",
        choices=("get_note", "search", "contribute"),
        default="get_note",
        help=(
            "get_note is free churn; search costs an embedding call each; "
            "contribute serialises on the advisory lock and is the worst case."
        ),
    )
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=float, default=30.0, help="Seconds.")
    parser.add_argument(
        "--rate",
        type=float,
        default=_DEFAULT_RATE_PER_SECOND,
        help=(
            "Requests per second ceiling; 0 disables. The default stays under the "
            "IP-keyed pre-auth guard of 600/min."
        ),
    )
    parser.add_argument(
        "--sample-dsn",
        default=os.environ.get("VAULT_LOAD_PROBE_DSN"),
        help=(
            "Optional database URL. Samples pg_stat_activity during the run, "
            "which is the only view of the database-wide total."
        ),
    )
    arguments = parser.parse_args()

    if arguments.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if arguments.duration <= 0:
        parser.error("--duration must be positive")

    # psycopg3's async connection drives sockets with loop.add_reader/add_writer,
    # which Windows' default ProactorEventLoop does not implement. The same guard
    # as run_dev.py, tests/conftest.py, and the other scripts; a no-op on Linux,
    # which is where Heroku runs it.
    if sys.platform == "win32":
        import selectors

        results = asyncio.run(
            run_probe(arguments),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    else:
        results = asyncio.run(run_probe(arguments))

    report(arguments, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
