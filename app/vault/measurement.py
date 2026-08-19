"""Statistics shared by the vault's measurement scripts.

Three scripts summarise a sample of observations and need the same summary to
mean the same thing: `measure_embedding_latency.py` times the embedding
provider, `measure_dedup_similarity.py` derives `Policy.flag_at` from a
similarity distribution, and `vault_load_probe.py` reports request latency under
concurrency.

They previously carried a copy each. Two were identical; the third was written
separately and reported **p50 of [10, 20, 90, 100] as 90** -- roughly a p75
under the name p50 -- so two files in one repository disagreed about what a
percentile was while sharing the name and the signature. That is the failure a
shared definition prevents, and it is worth more than the five lines it costs.

It lives in `app/vault/` rather than in `scripts/` because all three callers are
vault-owned and leave with the package. A helper in `scripts/` would be a fourth
thing for the extraction manifest to disentangle; here it travels with the code
that uses it and the manifest needs no new entry.

The cost, stated plainly: `vault_load_probe.py` previously imported nothing from
the application and could in principle run anywhere httpx was available. It now
needs the package on the path like its siblings. For a script invoked from the
repository or through `heroku run`, that is the cheaper half of the trade.
"""


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile.

    Deliberately not interpolated: at the sample sizes these scripts run, an
    interpolated p99 is a number invented between two observations rather than
    an observed one, and the decision it feeds deserves a real measurement.

    Raises on an empty sample rather than returning a placeholder. A zero
    reported as a latency is indistinguishable from a real measurement and would
    quietly flatter whatever it summarised; callers that can legitimately have
    no observations should say so themselves.
    """

    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return ordered[rank - 1]
