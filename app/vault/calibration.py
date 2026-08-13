"""Deriving ``Policy.flag_at`` for an embedding model, rather than choosing it.

``flag_at`` is a **cosine similarity on a specific embedding model**. It is not
portable: two models embedding the same corpus produce different distributions,
so a value derived for one is meaningless for another. ADR 0016 originally
carried an ungrounded claim about where that distribution sits; this module
exists so the number comes from measurement instead.

The derivation is two-sided, and both sides are necessary:

*Negative side* — the pairwise similarity of documents already in the corpus.
Every one of those pairs is, by construction, a pair of notes that a human or
an agent decided were distinct enough to both exist. The **largest** such score
is a floor: any ``flag_at`` at or below it would have sent legitimate notes to
review.

*Positive side* — the reference pairs below, which are genuine duplicates
expressed in different words. The **smallest** such score is a ceiling: any
``flag_at`` above it fails to catch a duplicate the gate exists to catch.

A usable threshold lives strictly between the two, *and far enough from both to
survive further measurement*. When the bands overlap — or merely graze — the
model cannot separate duplicates from distinct notes on this corpus, and the
honest answer is to leave ``flag_at`` at 1.0 rather than to pick the least bad
number. See ``derive_flag_at`` and ``MINIMUM_SEPARATION``.

The failure this guards against is subtle and was nearly shipped. Measuring only
the negative side shows corpus similarity topping out around 0.74 with nothing
above it, which reads as a wide empty band where a threshold could sit safely.
It is empty because nothing was measured in it, not because nothing belongs
there: real duplicates land above it, *inside* that band. A threshold picked
from the apparent gap would flag nothing the gate exists to flag.

**Both sides must be measured on the same text shape.** This is the second way
the procedure gets quietly wrong, and the first version of it did: the corpus
floor comes from stored vectors, which were built by ``assemble_embedding_text``
over title + aliases + tags + summary + body, while the reference pairs were
embedded as bare prose. Tags alone move the *maximum* corpus pair by roughly
0.05 — they barely shift the mean, because the pairs that share tags are the
pairs already topically close — so a floor measured with tags against a ceiling
measured without them understates the margin and biases the whole derivation
toward "not separable". The reference pairs are therefore full note shapes, and
both sides run through ``assemble_embedding_text``.

Nothing here does I/O. ``scripts/measure_dedup_similarity.py`` supplies the
scores; ``app/vault/docs/embedding-calibration.md`` records the procedure and
the per-model results.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ReferenceNote:
    """A note-shaped fixture, satisfying ``EmbeddableDocument``.

    Note-shaped rather than a bare body string on purpose. A real duplicate
    arrives through the contribution path with a title and tags, and
    ``assemble_embedding_text`` puts both *ahead* of the body as the densest
    signal. Measuring the ceiling on bodies alone would compare it against a
    corpus floor that had titles and tags folded in, which is the bias this
    module's docstring describes.
    """

    title: str
    body: str
    tags: tuple[str, ...] = ()
    summary: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)


# Pairs that MUST flag as duplicates under a correctly calibrated policy.
#
# Each pair states one insight twice: same claim, same operational consequence,
# deliberately different vocabulary and sentence shape. They are not byte-equal
# and not near-string-matches, so they defeat the Stage-A string deduper —
# which is exactly the population semantic dedup is supposed to add.
#
# They are drawn from this corpus's own subject matter on purpose. A pair about
# some unrelated domain would measure how well the model separates topics, not
# how well it recognizes a restatement, and the second is what the gate does.
#
# Tags overlap without being identical, mirroring the real corpus: two notes
# restating one insight would be filed similarly but not by the same hand. They
# are part of the fixture and they move the ceiling, so changing them changes
# the derived threshold — which is why they are chosen to look like what
# `vault_documents.tags` actually contains rather than to flatter the result.
#
# Adding a pair only ever tightens the derived ceiling, so err toward including
# a borderline restatement rather than excluding it.
REFERENCE_DUPLICATE_PAIRS: tuple[tuple[ReferenceNote, ReferenceNote], ...] = (
    (
        ReferenceNote(
            title="Embedded Unity packages do not refresh in an open editor",
            tags=("unity", "upm", "gotcha"),
            body=(
                "Unity keeps a package's imported state cached per editor "
                "instance, so an embedded package edited on disk will not "
                "refresh in an editor that was already open. Close every "
                "editor before re-importing, or the Package Manager serves "
                "the stale copy indefinitely."
            ),
        ),
        ReferenceNote(
            title="Close all Unity editors before re-importing a package",
            tags=("unity", "upm", "package-management"),
            body=(
                "If an editor was running when you changed an embedded "
                "package, its Package Manager will keep handing back the old "
                "import. The cached state is per instance, so quitting all "
                "open editors and reopening is what actually forces a fresh "
                "import."
            ),
        ),
    ),
    (
        ReferenceNote(
            title="A pipeline's exit status hides the failure you care about",
            tags=("bash", "gotcha", "tooling"),
            body=(
                "A pipeline's exit status in bash is the status of its last "
                "command, so piping a check into anything discards the "
                "check's own result. Read ${PIPESTATUS[0]} when the thing you "
                "care about is the first command."
            ),
        ),
        ReferenceNote(
            title="Check PIPESTATUS when gating on a piped command",
            tags=("bash", "gotcha", "scripting"),
            body=(
                "Never trust the exit code of a piped command to reflect the "
                "command on the left: bash reports only the rightmost one. If "
                "you are gating on the first process in the pipe, inspect "
                "${PIPESTATUS[0]} instead."
            ),
        ),
    ),
    (
        ReferenceNote(
            title="psycopg's async pool needs the selector loop on Windows",
            tags=("python", "windows", "postgres"),
            body=(
                "psycopg's async connection pool cannot run on the "
                "ProactorEventLoop that Windows selects by default. The policy "
                "has to be set to SelectorEventLoop before the loop is "
                "constructed, which means inside the launcher rather than in "
                "application startup."
            ),
        ),
        ReferenceNote(
            title="Set the event loop policy in the launcher, not at startup",
            tags=("python", "windows", "asyncio"),
            body=(
                "On Windows the default event loop breaks psycopg3's async "
                "pool. You have to install the selector loop policy up front, "
                "in whatever script starts the process — by the time the app's "
                "startup hook runs, the loop already exists and it is too late."
            ),
        ),
    ),
)


# How much clear air a threshold needs between the two populations before it is
# worth adopting.
#
# Both bounds are *observed extremes of small samples* — the floor from a few
# hundred corpus pairs, the ceiling from a handful of reference pairs — so each
# is a biased estimate of the true bound, and both biases point inward. A gap
# narrower than this is indistinguishable from sampling noise: one more note or
# one more reference pair would close it. Adopting a threshold from inside such
# a gap buys a gate that is wrong in both directions at once.
#
# 0.05 is itself a judgement, not a measurement, and is deliberately generous.
# The cost of demanding too much separation is that flag_at stays 1.0 and exact
# resubmission remains the only thing caught, which is the status quo and is
# safe. The cost of demanding too little is a gate that flags real work.
MINIMUM_SEPARATION = 0.05


@dataclass(frozen=True, slots=True)
class CalibrationBands:
    """What a measurement says about where ``flag_at`` may sit.

    ``floor`` is the highest similarity observed between documents known to be
    distinct; ``ceiling`` is the lowest observed between known duplicates. A
    threshold is usable only in ``(floor, ceiling]``.
    """

    floor: float
    ceiling: float
    recommended: float | None
    separated: bool
    reason: str


def derive_flag_at(
    distinct_scores: list[float],
    duplicate_scores: list[float],
) -> CalibrationBands:
    """Derive a ``flag_at`` from measured distinct and duplicate similarities.

    Returns ``recommended=None`` when the two populations overlap. That is a
    real answer, not a failure: it says this model does not separate them on
    this corpus, and a threshold picked from an overlapping range would
    misclassify in both directions at once. ``Policy`` then keeps its 1.0
    default, which flags only identical embeddings and is always safe.

    The midpoint is deliberate. The floor and the ceiling are both *observed
    extremes of small samples*, so the true bounds sit somewhere outside them
    on either side; the midpoint is the point that degrades most slowly as
    further measurement pushes either bound inward.
    """

    if not duplicate_scores:
        return CalibrationBands(
            floor=max(distinct_scores, default=0.0),
            ceiling=1.0,
            recommended=None,
            separated=False,
            reason=(
                "no duplicate reference scores: the true-positive side is "
                "unmeasured, so any threshold below 1.0 is a guess"
            ),
        )

    floor = max(distinct_scores, default=0.0)
    ceiling = min(duplicate_scores)

    if ceiling <= floor:
        return CalibrationBands(
            floor=floor,
            ceiling=ceiling,
            recommended=None,
            separated=False,
            reason=(
                f"bands overlap: a known-distinct pair scored {floor:.4f} while "
                f"a known-duplicate pair scored only {ceiling:.4f}. This model "
                "cannot separate the two populations on this corpus; leave "
                "flag_at at 1.0"
            ),
        )

    if ceiling - floor < MINIMUM_SEPARATION:
        return CalibrationBands(
            floor=floor,
            ceiling=ceiling,
            recommended=None,
            separated=False,
            reason=(
                f"bands are only {ceiling - floor:.4f} apart, under the "
                f"{MINIMUM_SEPARATION} minimum. Distinct pairs reach "
                f"{floor:.4f} and duplicate pairs start at {ceiling:.4f}, so "
                "the two populations touch and any threshold between them is "
                "within sampling noise; leave flag_at at 1.0"
            ),
        )

    # Two decimal places: the inputs are extremes of samples in the tens, and a
    # threshold quoted to four figures claims a precision the measurement does
    # not have. Rounding can move the value by at most 0.005, which the
    # MINIMUM_SEPARATION check above already guarantees is safe — the assertion
    # holds that guarantee in place if either constant is ever changed.
    recommended = round((floor + ceiling) / 2, 2)
    assert floor < recommended <= ceiling, (
        f"rounded recommendation {recommended} escaped the band "
        f"({floor:.4f}, {ceiling:.4f}]"
    )
    return CalibrationBands(
        floor=floor,
        ceiling=ceiling,
        recommended=recommended,
        separated=True,
        reason=(
            f"distinct pairs peak at {floor:.4f}, duplicate pairs bottom out at "
            f"{ceiling:.4f}; {recommended} sits in the gap"
        ),
    )
