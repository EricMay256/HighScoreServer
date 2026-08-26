"""A labelled query set, for measuring retrieval rather than asserting it.

Vault ADR 0034 defers chunk-level embeddings against four triggers. Three are
about size and fire on their own as the corpus grows. The fourth is the only
one that could justify chunking on retrieval *quality* — "a labelled query set
demonstrates that whole-page vectors lose narrow section-level questions that
chunk vectors would find" — and it is the only one that has to be built rather
than waited for. This is that set.

It is not only for the chunking question. The same cases measure any change to
retrieval: fusion constants, the lexical arm's disjunction (ADR 0007), a second
embedding profile, or the search-compaction work that reshaped what a hit
carries. Retrieval currently has no regression signal at all, which is the
larger gap.

**The labels are judgements, and they are recorded as provisional.** Each case
carries ``validated``, false until a human has confirmed its relevant set
against the corpus. That flag is not ceremony: these labels were authored by
the same agent that changed the search response, and an answer key written by
the party being examined is worth exactly as much scrutiny as that suggests.
``scripts/measure_retrieval_quality.py`` reports validated and provisional
cases separately for the same reason.

**Relevance is by title, not by id.** Note ids are stable only as long as the
row survives — ``scripts/remap_vault_reference_ids.py`` exists because they
churned across import generations — so a set keyed by id would decay silently
into a set that measures nothing. Titles are resolved at evaluation time, and a
title that no longer resolves is reported rather than skipped.

**A case names what should be found, not what is ranked first.** Several
queries have more than one right answer, because the corpus genuinely covers a
subject in a note and again in the wiki page compiled from it. Scoring those as
one-correct-answer questions would punish the corpus for being organised.
"""

from dataclasses import dataclass
from typing import Literal


# What each case is probing. The categories are the ones the efficiency
# assessment names, because the chunking question is asked in these terms and
# an evaluation that reported one aggregate number could not answer it: the
# whole hypothesis is that ONE category (narrow section questions inside long
# pages) is where document-level vectors lose.
CaseCategory = Literal[
    # A question answered by one section of a long wiki page. If chunking ever
    # helps, it helps here and nowhere else -- so these are the cases that
    # decide ADR 0034's fourth trigger.
    "narrow_section",
    # A question about a whole document's subject. Document-level vectors
    # should be at their best here, and chunking risks making them worse by
    # splitting the thing being asked about.
    "broad_document",
    # An exact identifier -- error code, flag, symbol. The lexical arm should
    # carry these outright; a failure means the vector arm is drowning it.
    "exact_term",
    # The insight, restated in words the note does not use. The vector arm's
    # whole reason to exist; a failure here means semantic retrieval is not
    # earning its cost.
    "paraphrase",
    # A short question against a short atomic note. The common case, and the
    # one chunking should never touch.
    "atomic_note",
]


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    """One query and the documents that should come back for it."""

    query: str
    category: CaseCategory
    # Titles that genuinely answer the query. Order is not significance --
    # scoring uses set membership and the rank of the best hit.
    relevant_titles: tuple[str, ...]
    # Why these and not others, written for the human validating the label.
    # A rationale that cannot be checked against the corpus is a label that
    # should not be trusted.
    rationale: str
    # False until a human has confirmed the relevant set. See the module
    # docstring: the labels were authored by an interested party.
    validated: bool = False


RETRIEVAL_CASES: tuple[RetrievalCase, ...] = (
    # ---------------------------------------------------- narrow section ----
    RetrievalCase(
        query="why does reciprocal rank fusion throw away the similarity scores",
        category="narrow_section",
        relevant_titles=(
            "RRF discards scores to fuse incomparable arms, and k tunes agreement against rank",
            "RAG and Retrieval Design for the B2 Engine",
        ),
        rationale=(
            "The note states it outright. The wiki page covers it in one "
            "section among five, so this is exactly the shape where a "
            "whole-page vector is diluted by the sections about embedding "
            "column limits and HNSW post-filtering."
        ),
    ),
    RetrievalCase(
        query="how deep should each retrieval arm fetch before fusing",
        category="narrow_section",
        relevant_titles=(
            "RAG and Retrieval Design for the B2 Engine",
            "pgvector HNSW post-filters, so a filtered vector search silently returns too few rows",
        ),
        rationale=(
            "Over-fetch depth is one subsection of the wiki page and the "
            "subject of the pgvector note. A page-level vector has to carry "
            "this against four unrelated sections."
        ),
    ),
    RetrievalCase(
        query="what makes a corpus-derived similarity floor misleading",
        category="narrow_section",
        relevant_titles=(
            "A dedup threshold needs both a floor and a ceiling; the corpus alone gives an illusory empty band",
            "Calibrating a Semantic Dedup Threshold",
        ),
        rationale=(
            "The illusory empty band is one part of a six-section wiki page "
            "whose other parts cover tag inflation and review-queue "
            "calibration."
        ),
    ),
    RetrievalCase(
        query="which line endings break a patch that was generated minutes earlier",
        category="narrow_section",
        relevant_titles=(
            "A patch file CRLF-mangled in transit fails every hunk",
            "Windows: Byte-Exact Artifacts and Toolchains",
        ),
        rationale=(
            "The wiki page collects several byte-exactness failures; CRLF "
            "mangling is one of them. The note is the primary answer."
        ),
    ),
    # --------------------------------------------------- broad document ----
    RetrievalCase(
        query="how should retrieval be designed for a knowledge corpus in postgres",
        category="broad_document",
        relevant_titles=(
            "RAG and Retrieval Design for the B2 Engine",
            "B2 retrieval should be hybrid: Postgres tsvector plus pgvector fused with RRF",
        ),
        rationale=(
            "A whole-page question. Document-level retrieval should win here, "
            "and chunking could plausibly make it worse by splitting the "
            "subject being asked about."
        ),
    ),
    RetrievalCase(
        query="what goes wrong when tests run against a real database",
        category="broad_document",
        relevant_titles=(
            "Testing Against a Real Database and Environment",
            "Two pytest processes against one database deadlock on the autouse TRUNCATE fixture",
            "Alembic test DB upgrades must target TEST_DATABASE_URL explicitly",
        ),
        rationale=(
            "The page's whole subject, plus the two notes it was compiled "
            "from that state specific failures."
        ),
    ),
    RetrievalCase(
        query="working with git worktrees",
        category="broad_document",
        relevant_titles=(
            "Working Inside a Git Worktree",
            "In a git worktree, absolute paths naming the main checkout edit the wrong tree",
            "A .env loaded from a checkout-relative path silently disappears in a git worktree",
        ),
        rationale=(
            "Deliberately a bare topic rather than a question -- the shape a "
            "caller uses when orienting rather than solving."
        ),
    ),
    # ------------------------------------------------------- exact term ----
    RetrievalCase(
        query="FN0007",
        category="exact_term",
        relevant_titles=(
            "FishNet FN0007: IsOwner is disallowed inside OnStartNetwork",
        ),
        rationale=(
            "A compiler diagnostic code appearing in exactly one title. The "
            "lexical arm should return this outright; if it does not, the "
            "vector arm is drowning an exact match."
        ),
    ),
    RetrievalCase(
        query="CS0012",
        category="exact_term",
        relevant_titles=(
            "Extracting a base class to a new assembly breaks callers' asmdefs with CS0012",
        ),
        rationale="Same shape as FN0007, in a different technology.",
    ),
    RetrievalCase(
        query="jsonb_path_ops",
        category="exact_term",
        relevant_titles=(
            "GIN on JSONB: jsonb_path_ops is smaller and faster for containment but drops the existence operators",
            "JSONB Facet Columns: Constraints and Indexes",
        ),
        rationale=(
            "An identifier rather than a word, so stemming cannot help and "
            "the lexical arm must match it literally."
        ),
    ),
    RetrievalCase(
        query="TEST_DATABASE_URL",
        category="exact_term",
        relevant_titles=(
            "Alembic test DB upgrades must target TEST_DATABASE_URL explicitly",
        ),
        rationale=(
            "An environment variable name. Underscored identifiers are where "
            "a text-search configuration's tokenizer most often surprises."
        ),
    ),
    # -------------------------------------------------------- paraphrase ----
    RetrievalCase(
        query="stopping a network timeout from writing the same record twice",
        category="paraphrase",
        relevant_titles=(
            "Never ask a model for an idempotency key; derive it from the content",
            "Idempotency and Identity in a Write Path",
        ),
        rationale=(
            "Neither title contains 'timeout' or 'twice'. Pure vector-arm "
            "work; a lexical-only deployment should be expected to miss this, "
            "which is itself worth measuring."
        ),
    ),
    RetrievalCase(
        query="my rate limiter runs too late to protect the expensive part",
        category="paraphrase",
        relevant_titles=(
            "A route decorator cannot guard work done in a FastAPI dependency",
        ),
        rationale=(
            "The note's subject in words it never uses -- it says 'decorator' "
            "and 'dependency', the query says 'too late' and 'expensive'."
        ),
    ),
    RetrievalCase(
        query="an agent could read a secret out of the conversation history",
        category="paraphrase",
        relevant_titles=(
            "A CLI that prints a secret to stdout leaks it into any agent transcript",
        ),
        rationale=(
            "'conversation history' for 'transcript', 'read out of' for "
            "'leaks into'. No shared content word except 'secret'."
        ),
    ),
    RetrievalCase(
        query="restricting what a tool can do is not the same as hiding it",
        category="paraphrase",
        relevant_titles=(
            "Restricting the tool list is a prompt-injection boundary that authorization is not",
        ),
        rationale=(
            "Close in meaning and deliberately inverted in phrasing, to test "
            "that the vector arm is matching the claim rather than the "
            "wording."
        ),
    ),
    # ------------------------------------------------------- atomic note ----
    RetrievalCase(
        query="does piping a command hide whether it failed",
        category="atomic_note",
        relevant_titles=("Piping a check discards its exit status and fakes a pass",),
        rationale="A short question against a short single-claim note.",
    ),
    RetrievalCase(
        query="can a CHECK constraint contain a subquery",
        category="atomic_note",
        relevant_titles=(
            "PostgreSQL rejects subqueries in CHECK constraints; wrap the predicate in an IMMUTABLE function",
        ),
        rationale="A closed question the note answers in its title.",
    ),
    RetrievalCase(
        query="why did my timestamps change time zone",
        category="atomic_note",
        relevant_titles=(
            "timestamptz comes back in the session's time zone, so anything byte-stable must convert",
        ),
        rationale="Symptom-first phrasing, which is how this is actually hit.",
    ),
    RetrievalCase(
        query="should the vault trust a steam id sent by the client",
        category="atomic_note",
        relevant_titles=("Steam auth tickets must be validated server-side",),
        rationale=(
            "A yes/no question whose answer is the note's whole content. "
            "'ticket' does not appear in the query."
        ),
    ),
    RetrievalCase(
        query="what happens if an empty search result set is actually a broken embedding provider",
        category="atomic_note",
        relevant_titles=(
            "A vault search caller must branch on vector_status, because a degraded result set looks healthy",
        ),
        rationale=(
            "Directly about the field the search contract asks callers to "
            "branch on, so a regression here is a regression in the thing "
            "the tool description promises."
        ),
    ),
)
