"""Assembling the text a document is embedded from, and hashing it.

Vault ADR 0013 fixes *what* gets embedded: title, aliases, tags, summary, body,
and nothing else. This module is the one place that renders it, because
``vault_document_embeddings.embedded_text_sha256`` is a hash *of this output* —
two implementations would mean a hash describing text nobody embedded.

The exclusions are the point, so they are restated here rather than left to the
ADR: timestamps and identifiers churn without changing meaning, and ``Type`` /
``Status`` are excluded because they are columns (``doc_type``, ``doc_status``)
and filtering exactly beats matching fuzzily. ``frontmatter`` is deliberately
absent — it is the bag for everything ADR 0013 decided was not worth embedding,
so including it would undo the decision.
"""

from collections.abc import Sequence
from hashlib import sha256
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddableDocument(Protocol):
    """The fields the embedding text is built from.

    A Protocol rather than a concrete type so both ``VaultDocument`` and
    ``NewVaultDocument`` satisfy it, and so a future importer record does not
    have to become a domain record first.
    """

    title: str
    body: str
    summary: str | None
    tags: tuple[str, ...]
    aliases: tuple[str, ...]


def _normalize_terms(values: Sequence[str]) -> list[str]:
    """Strip, drop blanks, de-duplicate, and sort.

    Sorted because reordering tags in frontmatter is not a change in meaning,
    and the hash exists to avoid paying an embedding call for changes that are
    not. De-duplicated for the same reason. The cost is that the model sees a
    canonical order rather than the author's, which is not a meaningful loss for
    a bag of keywords.
    """

    seen = {value.strip() for value in values if value.strip()}
    return sorted(seen)


def assemble_embedding_text(
    document: EmbeddableDocument,
    *,
    include_tags: bool = True,
) -> str:
    """Render the text to embed for one document.

    Title, aliases, and tags lead because they are the densest signal; the body
    follows after a blank line. Absent parts are omitted rather than emitted as
    empty lines, so a document that gains a summary does not merely shift
    whitespace around.

    ``include_tags`` exists for **measurement only** — the counterfactual in
    ``scripts/measure_dedup_similarity.py`` that asks whether removing tags from
    the embedding text would open a usable calibration margin. Nothing on the
    write path may pass it: this function's output is what
    ``vault_document_embeddings.embedded_text_sha256`` hashes, so a document
    embedded without tags and hashed as though it had them would make the stale
    check silently wrong. Keeping the default at True is what makes the
    parameter safe; changing the default is an ADR 0013 decision and a re-embed
    of the entire corpus.
    """

    head: list[str] = [document.title.strip()]

    aliases = _normalize_terms(document.aliases)
    if aliases:
        head.append(" ".join(aliases))

    tags = _normalize_terms(document.tags) if include_tags else []
    if tags:
        head.append(" ".join(tags))

    summary = (document.summary or "").strip()
    if summary:
        head.append(summary)

    body = document.body.strip()
    return "\n".join(head) + "\n\n" + body if body else "\n".join(head)


def embedding_text_digest(text: str) -> bytes:
    """SHA-256 of the assembled text, as the 32 bytes the column stores."""

    return sha256(text.encode("utf-8")).digest()


def digest_for(document: EmbeddableDocument) -> bytes:
    """Assemble and hash in one step.

    The pairing callers should reach for: it makes it impossible to hash text
    that was assembled differently from the text that gets embedded.
    """

    return embedding_text_digest(assemble_embedding_text(document))
