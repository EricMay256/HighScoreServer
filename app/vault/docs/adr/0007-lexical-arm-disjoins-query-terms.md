# 7. The lexical arm disjoins query terms

Date: 2026-07-29

## Status

Accepted

Amends [ADR 0006](0006-hybrid-retrieval-fused-by-reciprocal-rank.md), which remains accepted.
Fusion, `k`, oversampling, and the degradation policy are unchanged; only how the lexical arm
builds its `tsquery` changes.

## Context

ADR 0006 established two retrieval arms and left the lexical one as
`websearch_to_tsquery(config, query)`. That function conjoins every term it parses, so
`how do I make vector similarity search faster?` becomes

```
'make' & 'vector' & 'similar' & 'search' & 'faster'
```

and a document matches only if it contains **all five** lexemes. Measured against a two-document
fixture — one containing every term, one relevant but phrased differently — the conjunctive form
returned only the first:

| Document | AND | OR |
| -------- | --- | -- |
| contains every content word | matched | matched, `ts_rank_cd` 3.80 |
| relevant, phrased differently | missed | matched, `ts_rank_cd` 0.60 |

Two things follow. Recall decays as the query lengthens, because each additional word is another
term the document is required to contain; and the vault's intended consumer is an agent asking
questions, which are long. The lexical arm was therefore contributing little precisely where the
corpus is meant to be useful.

It is worth being exact about the failure, because the first diagnosis was not. Conjunction does
not make question-shaped queries match *nothing* — a document holding every term still matches
fine. The defect is that recall falls off sharply with query length, and questions are long.

The counter-argument to disjoining is precision: OR admits weakly related documents. That
argument is weaker here than it looks. `ts_rank_cd` separated the two fixtures by roughly six
times, and RRF consumes **positions, not scores**, so a marginal disjunction-only hit enters
fusion near the bottom of the candidate list and contributes about `1/260` against a leading
hit's `1/61`. The fusion rule already damps what disjunction lets in.

## Decision

The lexical arm disjoins the terms of the parsed query.

The rewrite is applied to `websearch_to_tsquery`'s **output**, replacing the ` & ` separators in
its text form with ` | `, rather than re-lexing the raw query string. That choice is what
preserves the rest of websearch's syntax:

- **Quoted phrases survive.** websearch renders `"nearest neighbours"` as `'nearest' <->
  'neighbour'`, and the phrase operator is left untouched, so word order still matters. Re-lexing
  the raw string with `to_tsvector` would have flattened the phrase into two independent lexemes.
- **Negation opts out entirely.** `'index' | !'gin'` would match every document *lacking* "gin",
  inverting the caller's intent, so a query whose parsed form contains `!` keeps websearch's
  conjunctive reading unchanged.

Rewriting parsed text is a pragmatic move rather than an elegant one — PostgreSQL exposes no
function to disjoin a `tsquery` — so its safety rests on a specific property: the text-search
parser never places a space inside a lexeme, therefore the literal separator ` & ` cannot occur
within one and the replacement cannot corrupt a term. This was verified against inputs designed
to break it, including `AT&T`, `"AT & T"`, `R&D pipeline`, and `don't`.

The `text_search_config` remains a bound parameter, never interpolated, exactly as ADR 0006
requires.

## Consequences

The lexical arm now answers "which documents share vocabulary with this query" instead of "which
documents contain all of it". Long, natural-language queries retrieve lexically instead of
returning nothing, and hybrid retrieval is genuinely hybrid for the queries the vault exists to
serve.

**`lexical_rank` weakens as an agreement signal.** The arm returns a fuller candidate set on most
queries, so "both arms ranked this" no longer implies the same level of corroboration it did
under conjunction. The `lexical_rank` and `vector_rank` fields stay in the response — they remain
the honest record of which arm proposed a document, and at what depth — but a consumer should not
read co-occurrence as strong evidence. Nothing in the ranking depends on this; it is a caveat for
whoever reads the fields.

Keyword and identifier queries are unaffected in practice. A single-term query has no conjunction
to rewrite, and for short queries the conjunctive and disjunctive forms usually select the same
documents, with `ts_rank_cd` ordering them.

Query plans are unchanged. PostgreSQL constant-folds the rewrite before planning — the plan shows
an ordinary `tsquery` literal — and the GIN index on `search_vector` is used exactly as before,
confirmed by comparing forced index plans for both forms.

Negating queries keep the old behaviour, so the arm has two modes rather than one. This is a real
cost in explicability, accepted because the alternative is a negation that silently means close
to its opposite.

A relevance floor is still absent, as ADR 0006 recorded, and disjunction widens the candidate set
this applies to. RRF continues to damp weak single-arm hits rather than exclude them. Should a
floor become necessary, a `ts_rank_cd` minimum on the lexical arm is now the more likely lever,
and it should be set against measurements rather than invented.
