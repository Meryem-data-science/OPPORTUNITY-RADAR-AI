# Human relevance labelling protocol — `human-relevance-calibration-v0`

**Status: CALIBRATION. This protocol is not frozen.**
A `human-relevance-v1` can only be declared after a small lot has actually been
annotated on the operator's machine and its ambiguous cases have been reviewed.
Until then every artefact produced under it carries `v0`, and no measurement
computed from it may be presented as a settled benchmark.

| | |
|---|---|
| Label schema version | `human-label-v1` |
| Protocol version | `human-relevance-calibration-v0` |
| Blind view version | `blind-evidence-v1` |
| Selector version | `calibration-selector-v0` |
| Selection artefact schema | `evaluation-calibration-selection-v0` |

---

## 1. What is being judged

The frozen Phase 10.1 dataset (`evaluation-dataset-v3`) is a set of real
postings bound to one profile state. This protocol attaches, to some of those
postings, one number: **how relevant is this opportunity for the profile and
target being evaluated?**

The judgement is made **on the frozen evidence** — the title, the organization,
the location text, the posting's own description, the dates, the URLs and where
it was collected — and never by re-reading the live database or following a link
that may since have changed.

## 2. The rubric

| Grade | Name | Meaning |
|---|---|---|
| 3 | `VERY_RELEVANT` | Clearly a good opportunity for the profile and target. You would want it surfaced first. |
| 2 | `RELEVANT` | Relevant and reasonably actionable, with some reservations possible. You would want it surfaced. |
| 1 | `WEAKLY_RELEVANT` | Partial or weak link, borderline, or a fit too thin to justify a strong recommendation. |
| 0 | `OUT_OF_TARGET` | Not a relevant opportunity for this target. |

### The absolute rule

> **UNJUDGED ≠ 0.**

An opportunity nobody has judged has **no label row at all**. There is no
default, no implicit grade and no "assume out of target". If you cannot decide,
record nothing and move on — that is a valid outcome of a calibration round and
one of the things it exists to discover.

A label row that exists **must** carry an integer grade in `{0, 1, 2, 3}`. A
boolean is refused. A `-1` or a `4` is refused.

## 3. Optional diagnostics

Structured, versioned, optional, and **never used to recompute the grade**. They
exist so a later error analysis can ask *why* a human and the pipeline disagree.

| Field | Values |
|---|---|
| `geo_judgment` | `TARGET_GEOGRAPHY`, `NON_TARGET_GEOGRAPHY`, `UNKNOWN_GEOGRAPHY` |
| `data_ai_judgment` | `CORE_DATA_AI`, `ADJACENT_DATA_AI`, `NON_DATA_AI`, `UNKNOWN_DATA_AI` |
| `opportunity_type_judgment` | `TARGET_TYPE`, `NON_TARGET_TYPE`, `UNKNOWN_TYPE` |

Plus `reason_tags: list[str]` (open vocabulary, normalized to lowercase, sorted
and de-duplicated) and `note: str | None`.

Three states are kept apart and never collapsed:

* the diagnostic is **absent** (`null`) — the annotator said nothing about this axis;
* the diagnostic is **`..._UNKNOWN`** — the annotator looked and could not tell;
* the diagnostic is **`NON_...`** — the annotator decided against.

`UNKNOWN` is never converted to `false` or to a `NON_TARGET`.

## 4. Blindness

The annotator judges the evidence, not the pipeline's verdict about it.

**Shown before judgement** (the allowlist, `blind.VISIBLE_RECORD_FIELDS`):
`opportunity_id`, `canonical_title`, `organization`, `opportunity_type`,
`employment_type`, `location`, `country`, `remote_type`, `description`,
`source_url`, `application_url`, `canonical_url`, `published_at`, `deadline`,
`discovered_at`, `first_seen_at`, `last_seen_at`, `sources` (each limited to
`source_id`, `source_type`, `source_url`, `application_url`, `canonical_url`,
`discovered_at`).

The description is shown **verbatim**: nothing is trimmed, cleaned, summarised
or truncated.

**Withheld before judgement** (`blind.WITHHELD_RECORD_FIELDS`): `qualification`
(including the fine Data/AI classification), `geography_segments`,
`eligibility`, `matching` (lane, match quality, evidence coverage),
`recommendation` (rank, disposition, score, coverage), every assessment
fingerprint, plus `status`, `is_active` and `absorbed_duplicate_ids`.

Two mechanisms keep this true, and neither is a filter:

1. the view is built from an **allowlist**, so a field added to Phase 10.1
   tomorrow is invisible by default;
2. every contract field must appear in *either* the visible or the withheld
   list — a test refuses a field classified in neither, so a new score cannot be
   added without somebody deciding what to do with it.

The same rule binds any future UI or CLI: they render `blind_evidence_payload`,
they do not assemble evidence of their own.

## 5. Calibration selection

`calibration-selector-v0`. The size is **not** fixed by this protocol: the
operator passes `--sample-size N`. The Phase 10 benchmark size is not decided
here.

Selection **may** read the system's outputs; that is how a small lot ends up
varied. Each record is assigned a stratum:

* **recommendation band** — `RECOMMENDED_HIGH` / `RECOMMENDED_MID` /
  `RECOMMENDED_LOW`, by position among the ranked postings of this dataset
  (thirds), or `NOT_RECOMMENDED` for a posting the run never ranked. The order
  is **numeric**, on `(rank_position, opportunity_id)`: ordering ranks by any
  textual form of the number would give `1, 10, 100, 11, 2, …` and scramble the
  thirds on any run with more than nine postings. Every `rank_position` on a
  present recommendation block is validated against the Phase 9A contract
  (migration `0026`: `INTEGER NOT NULL CHECK (rank_position > 0)`,
  `UNIQUE (run_id, rank_position)`) — a boolean, a string, a null, a
  non-positive value or a repeated rank stops the draw with a `HumanLabelError`
  rather than being coerced into a band;
* **qualification bucket** — the persisted verdict verbatim (`CORE_TARGET`,
  `ADJACENT_TARGET`, `UNCERTAIN`, `OUT_OF_SCOPE`) or `QUALIFICATION_ABSENT`
  when the classifier never read the posting;
* **geography bucket** — `GEOGRAPHY_RESOLVED` (at least one segment resolved to
  a country), `GEOGRAPHY_UNRESOLVED`, `GEOGRAPHY_UNSEGMENTED`.

A secondary **diversity key** — `(source_type, opportunity_type)` — spreads the
draw across collection sources and kinds of posting without multiplying strata.

The draw:

1. strata are ordered by `SHA256(selector_version, dataset_content_fingerprint,
   stratum)` — deterministic, dataset-bound, and deliberately not alphabetical;
2. the lot is drawn **round-robin** over that order, one posting per stratum
   before any stratum gives a second;
3. within a stratum the pick is the candidate whose diversity key has been used
   least so far, ties broken by `SHA256(selector_version,
   dataset_content_fingerprint, opportunity_id)`;
4. the draw stops at `N` or when the dataset is exhausted.

**Same dataset + same selector version + same N → same ids in the same order.**
No tie is broken by file order, SQL order or set iteration order.

## 5b. Version policy — nothing is ever mixed

This build interprets **exactly one** protocol version
(`human-relevance-calibration-v0`) and **exactly one** selector version
(`calibration-selector-v0`). There is no compatibility policy, and that is a
decision rather than an omission.

The label *schema* version and the *protocol* version are checked separately and
are not interchangeable. A future `human-relevance-v1` label would almost
certainly parse under `human-label-v1` — same fields, same types, same JSON —
and differ only in the question the annotator was answering. So every stored
label and every stored selection is refused on read if its `protocol_version` is
not the one above, and every stored selection is refused if its
`selector_version` is not the one above.

Refused means refused: nothing is converted, nothing is migrated in place, and
no stored row is ever rewritten. When `human-relevance-v1` is declared, widening
these lists will be a deliberate act carrying a stated compatibility policy.

## 5c. Selection bindings — checked before anything is shown

`assert_selection_bindings(selection, dataset)` is the single primitive, called
on every path that uses a stored lot: resolving `--selection`, `show --next`,
`progress`, `fingerprint`, and `build_labelset_report`. It verifies:

* `dataset_id` and dataset content fingerprint;
* `profile_id` and profile context fingerprint;
* protocol version, selector version and selection schema version;
* `effective_sample_size == len(items)`, unique ids, positions `1..N` in order;
* every selected id is actually present in the frozen dataset.

A `--selection` pointing at another dataset's or another profile's lot is
refused **while it is still an argument** — before a single opportunity is
displayed — not later by whichever command happened to notice.

## 5d. The existing history is validated before anything is written

Before a judgement is appended or a labelset reported, the whole of
`labels.jsonl` is read and checked. Per row: label schema, protocol version, and
the four bindings (dataset id, content fingerprint, profile id, profile context
fingerprint), plus the requirement that the opportunity is in the frozen
dataset. Per opportunity, in file order: revisions run `1, 2, 3, …` with no gap
and no repeat, revision 1 claims no relabel reason, and every later revision
states one.

One bad row stops the write. A valid judgement is never appended behind an
invalid one — that would make the corruption permanent and give it company —
and nothing is dropped, repaired, renumbered or quarantined.

## 6. Traceability

Every label row carries: label schema version, protocol version, `dataset_id`,
dataset content fingerprint, `profile_id`, profile context fingerprint,
`opportunity_id`, `relevance_grade`, diagnostics, `reason_tags`, `note`,
`labeled_at`, `revision`, `relabel_reason`.

No personal data is copied into a label. The dataset already carries the profile
binding, and the label reuses it.

## 7. Fingerprints

**Selection fingerprint** = SHA-256 over: selector version, `dataset_id`,
dataset content fingerprint, `parameters.sample_size` (the *effective* size),
and the ordered selected ids. Outside it: `generated_at`, the requested size
when it exceeded the dataset, and the per-item strata.

**Labelset fingerprint** = SHA-256 over: label schema version, protocol version,
`dataset_id`, dataset content fingerprint, `profile_id`, profile context
fingerprint, selector version, selection fingerprint, and the labels of the
selected postings in `opportunity_id` order — each contributing its grade, its
diagnostics, its reason tags and its note.

Outside it, deliberately:

* **`labeled_at`** — the same judgements recorded at two different moments
  produce the **same** labelset fingerprint;
* `revision` and `relabel_reason` — a grade corrected to 2 and a grade given as
  2 first time are the same opinion;
* the unjudged postings — they have no row, and inventing one would be the exact
  mistake this protocol forbids.

The note **is** inside: a note qualifies the grade it accompanies, and a
labelset whose notes changed is a labelset whose judgements changed.

## 8. Writing behaviour

* Labels live in `data/evaluation/labels/<dataset_id>/labels.jsonl`, beside the
  frozen dataset rather than inside it — Phase 10.1's directory stays frozen.
* The file is **append-only**. A first judgement is revision 1.
* A second judgement of an already-labelled opportunity is **refused by
  default**. It is accepted only with an explicit relabel **and** a stated
  reason, and is then written as revision *n+1* with the earlier row left in
  place. Nothing is ever edited or deleted; a correction is an event in the
  file.
* Writes are atomic (temporary file + `os.replace`). There is no lock: this is
  one operator on one machine.
* Nothing under `data/` is ever committed.

## 9. Commands

```
python -m evaluation.labeling.cli verify      --dataset-dir DIR
python -m evaluation.labeling.cli select      --dataset-dir DIR --sample-size N
python -m evaluation.labeling.cli show        --dataset-dir DIR --next
python -m evaluation.labeling.cli label       --dataset-dir DIR \
    --opportunity-id ID --grade 0..3 \
    [--geo-judgment X] [--data-ai-judgment X] [--opportunity-type-judgment X] \
    [--reason-tag T]... [--note TEXT] [--relabel --relabel-reason TEXT]
python -m evaluation.labeling.cli progress    --dataset-dir DIR
python -m evaluation.labeling.cli fingerprint --dataset-dir DIR [--write-manifest]
```

Every command verifies the frozen dataset first. There is no `--force`.
`show` never prints a system verdict, a stratum or a rank — not even for a
posting already judged.

## 10. Out of scope

No Precision@K, no NDCG, no recall, no business metric, no URL health metric, no
baseline, no experiment, no error analysis, no dashboard, no migration, no
evaluation table in the database, and no change to Recommendation, Matching,
Qualification, Eligibility or Geography. Phase 10.3 is where metrics start, and
it starts from a labelset produced here.

## 11. What must happen before `human-relevance-v1`

1. run `verify` against the real frozen dataset on the operator's machine;
2. draw a small calibration lot;
3. actually annotate it — a person, reading postings, applying the rubric above;
4. review the ambiguous cases and the disagreements the rubric could not settle;
5. only then decide the wording of `human-relevance-v1`, the diagnostics
   vocabulary, and the benchmark size.

None of those five steps has been performed. Anything claiming otherwise is
wrong.
