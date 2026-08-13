# Embedding calibration — deriving `flag_at` per model

`Policy.flag_at` is a cosine similarity **on one specific embedding model**. It does not
transfer between models: swapping the provider or the model changes the distribution the
threshold sits in, so a value derived for `text-embedding-3-small` says nothing about
`text-embedding-3-large`, Voyage, or a local model.

This file is the durable record. `app/vault/calibration.py` holds the reference pairs and the
derivation; `scripts/measure_dedup_similarity.py` produces the numbers. Neither replaces the
register at the bottom of this page.

## Why the procedure is two-sided

The obvious measurement is the pairwise similarity of documents already in the corpus. It is
also, on its own, actively misleading.

Every pair of documents in the corpus is a pair somebody judged distinct enough that both
should exist. So the corpus distribution tells you where legitimate work sits, and its maximum
is a **floor**: a `flag_at` at or below it would have sent real notes to review. That is a
genuine and useful bound — but it constrains only the false-positive side.

It says nothing about where duplicates sit, and the temptation is to assume they sit higher.
On this corpus that assumption fails outright. Corpus similarity tops out at 0.7406 with a
wide apparently-empty stretch up to 1.0, which reads as ample room for a threshold. Measured,
genuine duplicates land at **0.75–0.81** — inside that stretch. The band was empty because
nothing had been measured in it, not because nothing belonged there. A threshold of 0.85
picked from the apparent gap would have caught **none** of the three reference duplicates.

So the procedure needs a second, opposing measurement: known duplicates, giving a **ceiling**.
A usable threshold sits strictly between floor and ceiling, with enough clear air that further
measurement will not close the gap.

## Both sides must be measured on the same text shape

This is the second way the procedure goes quietly wrong, and the first version of it did.

Corpus scores come from stored vectors, which `assemble_embedding_text` built over title +
aliases + tags + summary + body. If the reference pairs are embedded as bare prose, the floor
carries titles and tags and the ceiling does not, and the two are not comparable.

That is not a rounding error. Re-embedding fourteen corpus documents with tags removed moved
the **maximum** pair by −0.0513 while moving the mean only −0.0099: tags disproportionately
inflate the top of the distribution, because the pairs that share tags are the pairs already
topically close. One pair sharing `git`, `gotcha`, and `tooling` fell 0.0995.

So the reference pairs are full note shapes, and both sides run through
`assemble_embedding_text`. Correcting this widened the measured margin from 0.0072 to 0.0094 —
not enough to change the verdict, but enough that a borderline model could have been
misjudged.

## The reference pairs

`REFERENCE_DUPLICATE_PAIRS` in `app/vault/calibration.py`, as `ReferenceNote` records with a
title, body, and tags. Each pair states one insight twice: same claim, same operational
consequence, deliberately different vocabulary and sentence shape. They are not byte-equal and
not near string matches, so they defeat the Stage-A string deduper — which is precisely the
population semantic dedup is supposed to add.

Tags overlap without being identical, mirroring how the real corpus looks: two notes restating
one insight would be filed similarly but not by the same hand. Identical tags would inflate the
ceiling and flatter the result, so a test asserts they differ.

They are drawn from this corpus's own subject matter deliberately. A pair about an unrelated
domain would measure how well a model separates *topics*, and the gate's job is to recognize a
*restatement*.

Adding a pair can only lower the ceiling, so err toward including a borderline restatement.

## Running it

```bash
python -m scripts.measure_dedup_similarity
```

Needs `DATABASE_URL` pointing at a migrated vault with an embedded corpus, and
`VAULT_EMBEDDING_API_KEY`. `--corpus-only` skips the paid half and reports the distribution
without deriving anything. Cost is one request per reference pair — well under a cent.

The script filters the corpus to `status = active` and `readable_path_predicate()`, matching
what `find_similar` actually compares a contribution against. Calibrating against a population
the gate never sees would calibrate the wrong distribution.

## Reading the result

| Outcome | Meaning | Action |
| --- | --- | --- |
| Gap ≥ `MINIMUM_SEPARATION` | The model separates the populations | Adopt the recommended value, record it below, and change `DEFAULT_POLICY` |
| Gap < `MINIMUM_SEPARATION` | The populations touch | Leave `flag_at` at 1.0 |
| Ceiling ≤ floor | The populations overlap | Leave `flag_at` at 1.0 |

`MINIMUM_SEPARATION` is 0.05, and it is a judgement rather than a measurement. Both bounds are
observed extremes of small samples, so both are biased inward; a gap narrower than that is
indistinguishable from the noise of one more note or one more reference pair. Demanding too
much separation costs nothing but the status quo — `flag_at = 1.0` still catches exact
resubmission. Demanding too little costs real contributions.

**1.0 is the correct default for any unmeasured model.** It is not dedup switched off:
byte-identical text produces an identical vector and a cosine of 1.0, so exact resubmission is
still caught. It is dedup narrowed to the one band that needs no calibration.

Scores drift by ~0.0001 between runs — embedding endpoints are not bit-deterministic. Quote
thresholds to two decimals; the fourth figure is not real.

## Model register

Every model evaluated, with the measurement that produced its verdict. A row here is what
justifies a `DEFAULT_POLICY` value — do not change the constant without adding one.

### `openai/text-embedding-3-small:1536`

| | |
| --- | --- |
| Measured | 2026-08-12 |
| Corpus | 39 active readable Agent notes, 741 pairs |
| Adopted `flag_at` | **1.0** (unchanged) |

Corpus pairwise distribution:

| min | p50 | p90 | p95 | p99 | max | mean |
| --- | --- | --- | --- | --- | --- | --- |
| 0.0417 | 0.2542 | 0.4067 | 0.4889 | 0.6265 | 0.7406 | 0.2723 |

Reference duplicate pairs: 0.8431, 0.7664, 0.7500.

Floor 0.7406, ceiling 0.7500, **gap 0.0094** — well under the 0.05 minimum. The populations
touch: the closest legitimate pair in the corpus (two notes about Unity package import
staleness, genuinely distinct) scores within 0.01 of the weakest deliberate restatement.

Verdict: **this model does not separate restatement from adjacency on this corpus.** Keep
`flag_at = 1.0`. This is a property of the model and the corpus together, and it is the
substantive reason the vault's semantic dedup currently catches only exact resubmission.

Worth noting for anyone evaluating an alternative: the failure is not that duplicates score
low — 0.75–0.81 is a decent signal — but that closely-related-but-distinct notes score almost
as high. A model with better *discrimination* in the 0.7–0.9 range would help here even if its
absolute duplicate scores were lower.

## What to measure next

- **Whether `tags` should be in the embedding text at all** — the largest open question, and
  the one that most constrains whether `flag_at` can ever be calibrated. Tags move the corpus
  maximum by ~0.05 while barely moving the mean, and the margin they are competing against is
  0.0094. ADR 0017 moved *classification* out of `tags`, but topical tags like `gotcha` and
  `tooling` remain and will never become facets. Removing tags from the embedding would drop
  the floor and could plausibly open a real gap. It is an ADR 0013 decision and needs the
  counterfactual measured on the full corpus, not fourteen documents.
- **More reference pairs.** Three is thin, and the ceiling is a minimum over them, so it is the
  least stable number in the procedure. Every pair added tightens it.
- **A second model**, to find out whether the narrow gap is `text-embedding-3-small`'s
  limitation or an intrinsic property of a corpus of short operational notes.
- **Live contribution scores.** Every contribution records its top similarity in
  `vault_write_requests.response.top_similarity`, so the corpus half of this measurement
  accumulates from real traffic rather than only from re-running the script. Note that write
  requests are prunable (`scripts/prune_idempotency_keys.py`), so that is a rolling window —
  harvest into this register before pruning.
