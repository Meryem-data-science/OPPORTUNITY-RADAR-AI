# Human relevance labelling protocol

Two versions live in this document, and keeping them apart is the point.

| | `human-relevance-calibration-v0` | `human-relevance-v1` |
|---|---|---|
| What it is | the protocol the **writer records** and the reader interprets | the **frozen semantic contract** |
| Status | calibration round **completed locally**, AI-assisted with human validation | semantics **frozen** after that round |
| Labels carrying it | every label that exists | **none** — no judgement has been made under it |
| Accepted by the label reader | yes | **no**, deliberately |
| Where it lives | `schema.HUMAN_LABEL_PROTOCOL_VERSION` | `rubric.FROZEN_HUMAN_RELEVANCE_PROTOCOL_VERSION` |

| | |
|---|---|
| Label schema version | `human-label-v1` |
| Blind view version | `blind-evidence-v1` |
| Selector version | `calibration-selector-v0` |
| Selection artefact schema | `evaluation-calibration-selection-v0` |

## 0. Calibration — `human-relevance-calibration-v0`

A real calibration round has been run, on the operator's machine, against the
frozen dataset and the labels under `data/`. Neither exists in any development
container, and nothing in this repository has read those labels: the digests and
counts recorded in `rubric.CALIBRATION_V0_PROVENANCE` are what that round
reported, carried here as provenance rather than as a verification performed
here.

* 12 opportunities judged, 0 unjudged;
* 13 audit rows — one opportunity was explicitly relabelled at revision 2 after
  the rubric was clarified, and both rows remain in the trail. That relabel is
  the calibration doing its job;
* distribution: OUT_OF_TARGET 7, WEAKLY_RELEVANT 2, RELEVANT 3,
  VERY_RELEVANT 0.

**The round was AI-assisted with final human validation.** A model proposed a
reading of each posting and the operator decided every grade. That is a
legitimate way to find out whether a rubric is usable — it is what surfaced the
actionability rule and the hard-constraint rule below — and it is **not**:

* an independent human benchmark;
* an inter-annotator agreement study;
* an independent gold-standard holdout.

A holdout intended to *evaluate* the pipeline must not let a model propose the
grade before the person forms one, or the two are no longer independent and what
gets measured is the model agreeing with itself.

## 0b. Final frozen rubric — `human-relevance-v1`

Frozen after that round. Its semantics are in `rubric.py` and in §2 below.

**No label carries `human-relevance-v1`, and none can yet.** The writer still
records `calibration-v0`, `SUPPORTED_PROTOCOL_VERSIONS` still contains only
`calibration-v0`, and a row claiming v1 is refused on read. That is deliberate:
switching the writer would either rewrite what the existing labels say they
answered, or append v1 rows behind v0 rows and leave one history holding
judgements of two different questions. A real v1 benchmark will need a labelset
explicitly separated from the calibration history — a storage question, and not
this slice's.

What the freeze changed: the **question**, not the scale. Same four integers,
same four names. Under v0 the rubric effectively asked how close a posting
looked to Data/AI. v1 asks whether the opportunity is **actually actionable for
the profile and preferences the dataset is frozen against**, with lexical
proximity to Data/AI as evidence toward that and never a substitute for it.

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

## 2. The rubric — frozen as `human-relevance-v1`

An opportunity is judged on its **actually actionable relevance for the frozen
profile and preferences**, not on its lexical proximity to Data/AI.

### 2.1 Hard constraints

Some requirements are not tradeable: a posting that **explicitly** contradicts
one is out of target however well it matches on everything else. Which
requirements are hard is declared by the profile and preferences behind
`profile_context_fingerprint` — the protocol names only *kinds* of constraint
and never a country, a city, a level or a language, because a protocol that
hard-coded one profile's geography would be a protocol for one person.

`rubric.HardConstraintKind`:

| Kind | What the profile declares as non-tradeable |
|---|---|
| `TARGET_GEOGRAPHY` | where the work is: country, region, on-site city, whether remote from elsewhere is acceptable |
| `TARGET_LEVEL` | seniority and kind of position — a strictly PFE / internship / junior target declares this |
| `WORK_AUTHORIZATION` | right to work, visa, clearance, residency |
| `TARGET_DOMAIN` | the subject of the work, for a target defined by a domain such as Data/AI |
| `OTHER_PROFILE_CONSTRAINT` | anything else declared non-tradeable — a language, a start date, a contract type |

`rubric.ConstraintEvidence` is three-valued — `SATISFIED`, `CONTRADICTED`,
`UNKNOWN` — and only `CONTRADICTED` establishes the grade-0 condition.

**For the profile currently evaluated**, the operator has declared geography a
hard constraint: the opportunities sought are in Morocco, so a posting
explicitly located elsewhere is `OUT_OF_TARGET` **unless** the applicable
profile or preferences explicitly accept another country or international
remote. That instantiation belongs to the profile, not to the protocol; a second
profile declares its own.

### 2.2 The four grades

| Grade | Name | Meaning |
|---|---|---|
| 3 | `VERY_RELEVANT` | Clearly very well suited to the profile and target. The critical dimensions are positively established: a relevant Data/AI domain; a level or kind of position compatible with the target (PFE / internship / junior) where that constraint applies; geography and actionability compatible with the declared constraints; no known hard contradiction. You would want it surfaced first. |
| 2 | `RELEVANT` | Relevant and reasonably actionable. Some unknowns, non-blocking reservations or incomplete evidence on a secondary dimension may remain, but there is enough positive evidence to want it surfaced. No known hard contradiction. |
| 1 | `WEAKLY_RELEVANT` | A real but weak, partial or borderline link: an adjacent domain, a level that may be too high without being stated, Data/AI only partly demonstrated, too much uncertainty for a strong recommendation. No explicitly established hard contradiction, which would impose 0. |
| 0 | `OUT_OF_TARGET` | A human determined the opportunity is outside the actionable target — typically an explicit contradiction with a hard constraint: a location incompatible with the declared geography, an explicitly senior position against a strictly PFE/internship/junior target, work authorisation explicitly out of reach, work clearly outside Data/AI when Data/AI is a target constraint, or any other declared hard constraint explicitly contradicted. |

Grades 1, 2 and 3 are **positive judgements requiring positive evidence**. The
absence of a contradiction is not relevance.

### The absolute rules

> **UNJUDGED ≠ 0.**

An opportunity nobody has judged has **no label row at all**. There is no
default, no implicit grade and no "assume out of target". `0` is a negative
human judgement, never a fallback. If the evidence is too thin to defend any
grade, record nothing — that is a valid outcome and one of the things a
calibration round exists to discover.

> **UNKNOWN ≠ FALSE.**

Stated case by case in `rubric.UNKNOWN_IS_NOT_FALSE`, because this is exactly
the rule that erodes when somebody implements against the prose:

* a posting with no description is **not** an out-of-target posting;
* a posting stating no level is **not** a senior posting;
* a posting stating no country is **not** a posting outside the target geography;
* a posting stating no opportunity type is **not** a posting of the wrong type;
* evidence that is absent is **not** evidence that contradicts;
* an opportunity nobody judged is **not** an opportunity graded 0.

An unknown can lower confidence and lead to a 1 or a 2 depending on the rest of
the evidence, and a pile of unknowns can lead to UNJUDGED. What it cannot do, on
its own or by accretion, is become a hard contradiction.

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

## 5e. Stored rows are read strictly and never repaired

Reading a stored label is fail-closed. Nothing is coerced: no `int(...)` over a
string, no `str(...)` over a number, no normalization of a tag list or a note
that was not already canonical. `bool` is refused everywhere an integer is
expected, because `isinstance(True, int)` is true and `"revision": true` would
otherwise read as revision 1.

Two layers, in order:

1. every field is checked against the raw JSON type the contract states —
   `profile_id`, `opportunity_id` and `revision` must already be positive JSON
   integers; `labeled_at` an ISO-8601 string; `diagnostics` an object with
   exactly the three contract keys; `reason_tags` a list of strings already in
   canonical form; `note` and `relabel_reason` a string in canonical form or
   `null`; the key set exactly the contract's;
2. the parsed object is re-serialized through `human_label_payload` and compared
   as **canonical JSON** (structures, not bytes — indentation and key order in
   the file are irrelevant) against the row that was read. Anything layer 1 did
   not anticipate surfaces here as a mismatch.

A `relevance_grade_name` that contradicts its `relevance_grade` — grade 2 with
`VERY_RELEVANT` — is **refused, never reconciled**: the row says two things
about one judgement and picking one would be this code deciding what somebody
meant.

Appending preserves the earlier rows **verbatim**. The stored lines are written
back as the exact strings they were, with one new canonical line added; they are
not rebuilt from the objects they parsed into. So recording a new judgement
cannot alter a character of an older one, and a non-canonical file can never be
silently "repaired" by an unrelated append — it stops the append instead, byte
for byte unchanged. The write still goes through a temporary file and
`os.replace`: append-only here is an audit guarantee about content, not a claim
about syscalls.

## 5f. A stored lot must be what its selector actually draws

Bindings and a self-declared fingerprint are not enough. A digest computed
*from* a selection file only says the file is internally consistent; a
hand-assembled file with plausible ids and a recomputed digest passes every such
check while naming postings the algorithm never drew.

So `assert_selection_bindings` redraws the lot —
`select_calibration_sample(dataset, selection.requested_sample_size)`, the real
selector, not a second implementation — and compares the effective size, the
ordered ids, the selection fingerprint, and each item's `stratum` and
`diversity_key`. This is only possible because `calibration-selector-v0` is
deterministic, and it is why the strata are compared rather than believed: an
explanation nobody checks is decoration, and a wrong one misleads exactly the
person auditing a lot after annotation.

`select_calibration_sample` does not call the guard, so drawing a fresh lot
cannot re-enter it.

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
* The file is **append-only**, and the earlier rows are preserved verbatim on
  every append (see §5e). A first judgement is revision 1.
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

## 11. What is done, and what is not

Done:

1. `verify` run against the real frozen dataset on the operator's machine;
2. a calibration lot drawn;
3. the lot annotated — 12 judgements, AI-assisted with human validation of every
   grade, one explicit relabel;
4. the ambiguous cases reviewed, which produced the actionability rule and the
   hard-constraint rule;
5. the semantics of `human-relevance-v1` frozen, in `rubric.py` and in §2 above.

Not done, and not claimed:

* **no label carries `human-relevance-v1`.** The definition is frozen; no
  judgement has been made under it;
* **no independent human holdout exists.** The calibration round is not one, for
  the reason given in §0;
* **no benchmark size is decided**, and no metric is implemented. Precision@K,
  NDCG, recall, baselines and error analysis all belong to Phase 10.3 and after;
* **no storage separation between a v0 and a v1 labelset has been built.** A real
  v1 benchmark needs one before its first label is written.

Anything claiming otherwise is wrong.
