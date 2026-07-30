# 13. The embedding text carries semantic frontmatter, not bookkeeping

Date: 2026-07-29

## Status

Accepted

## Context

Nothing in `app/vault/` embedded a document. The only embedding call in the package is the query
side; the `f"{title}\n\n{body}"` in `scripts/seed_vault_demo.py` is a fixture loader's
convenience, and that file's docstring already disclaims being a backfill. So the question of what
text becomes a vector had never been answered — the seeder's answer was an accident of being the
only example.

It has to be answered before the importer, because the importer is what will embed at scale, and
answering it wrongly is expensive in two directions: too little text loses recall, and text that
churns buys embedding calls for vectors that did not need to change.

The tempting framing is "frontmatter or not". That is the wrong axis. Frontmatter holds two
completely different kinds of thing, and the split that matters is **semantic content versus
bookkeeping**.

## Decision

**Embed title, aliases, tags, summary, and body. Nothing else.**

```text
{title}
{aliases, space-joined}
{tags, space-joined}
{summary}

{body}
```

Included, and why:

- **`aliases`** — the strongest case in the whole schema. A note titled "PostgreSQL" aliased
  "Postgres" must be findable by someone who types the other name. This is precisely what an
  embedding is for.
- **`tags`** — genuine topical signal, and until now in *neither* retrieval arm: absent from
  `search_vector`, and served only by a GIN index for exact filtering.
- **`title`**, **`summary`**, **`body`** — already the lexical arm's weighted fields.

Excluded, and why:

- **`CreatedAt`, `LastUpdated`, `CompiledAt`** — no semantic content, and they change constantly.
  Embedding them means every timestamp bump is an API call for an identical vector.
- **`ID`, `SchemaVersion`, `ContributedBy`, `ClientRunID`, `CompileRunID`, `CompiledBy`** —
  identifiers.
- **`cssclasses`, `ReviewFreq`** — presentation and scheduling.
- **`Parent`, `DependsOn`, `SeeAlso`** — graph structure. `[[Some Note]]` would embed a title
  string; traversal serves this properly and fuzzy proximity does not.

**`Type` and `Status` are excluded, and this is the one that needs an argument.** The obvious
objection is dilution — a low-cardinality value repeated across hundreds of notes adds the same
tokens to every vector. That effect is real but scales inversely with note length, so it is weak
for a long note and only bites on stubs. It is not the reason.

The reason is that **they are columns now.** `doc_type` and `doc_status` (ADRs 0009 and 0011)
support `WHERE doc_type = 'Decision'`, which is exact. Embedding a closed vocabulary converts an
exact predicate into a fuzzy one. Do not embed what you can filter.

That rule is also why **tags are embedded but deliberately kept out of `search_vector`.** The two
arms are being asked different questions: the vector arm is asked for semantic proximity, where a
"postgres" tag legitimately pulls a query about databases closer; the lexical arm is asked for
term matching, which the existing GIN index on `tags` already answers exactly.

**Aliases do join `search_vector`, at weight A alongside the title**, because an alias is an
alternative title and is exactly the term a searcher types.

## Consequences

Migration `0004_reconciliation` adds `aliases TEXT[]`, adds `frontmatter JSONB`, and rebuilds
`search_vector`.

**`search_vector` needed an IMMUTABLE helper.** `array_to_string` is STABLE, and PostgreSQL
rejects a non-immutable expression in a generated column outright — verified, not assumed.
`array_to_tsvector` is IMMUTABLE and was rejected for a worse reason: it emits raw lexemes
(`'Postgres'`) that never match a stemmed query side (`'postgr'`), so it would have compiled and
then silently failed to match. `vault.text_array_to_string(text[], text)` is a wrapper pinned to
`text[]`, where the result genuinely depends only on the array contents and the separator. A test
asserts an alias is findable by a stemmed query, so a regression here fails loudly rather than
quietly returning nothing.

Rebuilding a persisted generated column rewrites the table and reindexes the GIN index. That is
why this landed with the reconciliation columns rather than after them — it is cheap on an empty
table and would not be later.

**`frontmatter JSONB` exists for faithful projection, not for retrieval.** The projector must
re-emit notes the validator accepts, and `global.yml`'s `known_extra_keys` (`Category`, `Purpose`,
`Owner/Collaborators`) makes a column-per-key impossible. It is deliberately *not* embedded: it is
the bag for everything this ADR decided was not worth embedding, so embedding it would undo the
decision. It is distinct from `provenance`, which records how a row got here rather than what the
note said.

**Re-import and re-embed are now separate events.**
`vault_document_embeddings.embedded_text_sha256` holds the hash of the text that produced *that*
vector, so a change to bookkeeping frontmatter does not buy an embedding call while a change to an
alias does. It lives on the embedding rather than the document because staleness is per profile —
one profile can be current while another is stale. NULL means unknown, which a re-embed job must
treat as stale rather than as current; that is the safe direction, and it is what every existing
row reads as.

**The assembly function is not written.** This ADR fixes the template and therefore fixes what
`embedded_text_sha256` hashes, but the code that renders a document into that text belongs with
the importer. Whatever writes it must be the single place the hash is computed, or the hash will
describe text nobody embedded.

**`VaultDocumentDetail` gains `aliases` and nothing else.** `frontmatter`, `source_sha256`, and
`embedded_text_sha256` are importer and projector internals; the read surface stays minimal until
a caller needs them, per ADR 0008's discipline.
