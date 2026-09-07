-- Phase 8B.1: the validated fine Data/AI category, stored beside the coarse
-- qualification it was derived from.
--
-- `0004` already stores **one current derived classification row per
-- opportunity**. The fine categories of Phase 8A are the second half of the
-- same derivation — same inputs, same eligible row set, same instant — so they
-- belong in that row and nowhere else. This migration therefore adds five
-- columns and creates no table:
--
--     opportunity_qualifications   is this opportunity Data/AI?      (0004)
--         + fine_*                 which Data/AI sub-domain?         (0025)
--
-- **No history table**, here or later: the approved design is one current row,
-- and `input_fingerprint` plus the two independent version strings already say
-- exactly when a row has to be recomputed.
--
-- **Two versions, not one.** `classifier_version` and `fine_classifier_version`
-- are separate rule systems that move independently (`qualification-rules-v2`
-- and `fine-data-ai-rules-v2` today). Reconciliation compares both, so a fine
-- recalibration alone recomputes the fine half of every row without pretending
-- the coarse rules changed.
--
-- **Every new column is nullable, and that is the point.** When this migration
-- is applied to a database that already holds coarse rows, those rows have not
-- been fine-classified: nothing has run yet. Backfilling
-- `fine_classifier_version = 'fine-data-ai-rules-v2'` here would record a
-- classification that never happened, and reconciliation would then skip
-- exactly the rows that need it. So this migration writes no value at all —
-- pre-existing rows keep their coarse data unchanged and read NULL in all five
-- new columns until persistence actually runs (Phase 8B.2 for operational
-- data).
--
-- **NULL therefore has two meanings, and the final CHECK keeps them apart:**
--
--   A. `fine_classifier_version IS NULL` — migrated, never fine-classified.
--      The other four columns are NULL too. Nothing is known.
--
--   B. `fine_classifier_version IS NOT NULL` with
--      `fine_primary_category IS NULL` — the fine classifier ran and
--      deliberately assigned no category, which is what it does for an
--      `OUT_OF_SCOPE` or `UNCERTAIN` opportunity: absence of evidence that an
--      opportunity is Data/AI is not evidence about its sub-domain. The three
--      JSON columns are written (`[]`, `[]` and the classifier's reason), so
--      the row states that it was classified.
--
-- `OTHER` is a third, distinct statement: a **proven** Data/AI opportunity for
-- which no supported sub-domain can be asserted. It is a value, never a NULL,
-- and a reader must not fold it together with either case above.
--
-- JSON stays TEXT, as everywhere else in this schema: no `json_valid()`, no
-- JSON1 dependency, and the columns hold the compact deterministic arrays the
-- persistence layer writes.

-- NULL, or exactly one member of the closed fine taxonomy. The list mirrors
-- `FineCategory` in `services/collector/qualification/fine_taxonomy.py` and is
-- closed for the same reason `0004` closes `primary_domain`: an invented
-- category must be refused by the database, not merely by the writer.
ALTER TABLE opportunity_qualifications ADD COLUMN fine_primary_category TEXT CHECK (
    fine_primary_category IS NULL OR fine_primary_category IN (
        'DATA_SCIENCE', 'DATA_ANALYTICS', 'DATA_ENGINEERING', 'MACHINE_LEARNING',
        'ARTIFICIAL_INTELLIGENCE', 'GENERATIVE_AI', 'NLP', 'COMPUTER_VISION',
        'BUSINESS_INTELLIGENCE', 'MLOPS', 'OTHER'
    )
);

-- The other matched categories, in classifier precedence order. `[]` means the
-- classifier found no secondary category; NULL means it never ran.
ALTER TABLE opportunity_qualifications ADD COLUMN fine_secondary_categories_json TEXT;

-- Why each category was considered, in the exact order the classifier produced
-- it: an array of `{"category","field","kind","signal"}` objects. Explainability
-- is the reason the fine classifier is a rule system at all, so it is persisted
-- rather than recomputed.
ALTER TABLE opportunity_qualifications ADD COLUMN fine_category_evidence_json TEXT;

-- The deciding fine rule, as text, in classifier order. For an unqualified
-- opportunity this is the explicit "not qualified as Data/AI" reason — the row
-- says why it carries no category.
ALTER TABLE opportunity_qualifications ADD COLUMN fine_reasons_json TEXT;

-- Added last so its CHECK can see the four columns above. It states case A
-- against case B: either nothing fine has been recorded, or the classification
-- ran and left its three JSON columns behind. `fine_primary_category` is
-- deliberately not in the second clause — a run that assigns no category is
-- exactly case B. Both branches are written NULL-safe on purpose: a CHECK whose
-- expression evaluates to NULL *passes* in SQLite, so the second branch tests
-- `IS NOT NULL` before it reads the version rather than letting a NULL version
-- turn the whole constraint into an unknown that lets a half-written row in.
ALTER TABLE opportunity_qualifications ADD COLUMN fine_classifier_version TEXT CHECK (
    (
        fine_classifier_version IS NULL
        AND fine_primary_category IS NULL
        AND fine_secondary_categories_json IS NULL
        AND fine_category_evidence_json IS NULL
        AND fine_reasons_json IS NULL
    )
    OR (
        fine_classifier_version IS NOT NULL
        AND length(trim(fine_classifier_version)) > 0
        AND fine_classifier_version = trim(fine_classifier_version)
        AND fine_secondary_categories_json IS NOT NULL
        AND fine_category_evidence_json IS NOT NULL
        AND fine_reasons_json IS NOT NULL
    )
);

-- The same shape `0004` gives every other categorical column it stores: one
-- index per closed vocabulary, so counting or filtering by category does not
-- scan the table. It stores no value and states no fact.
CREATE INDEX idx_opportunity_qualifications_fine_primary_category
    ON opportunity_qualifications(fine_primary_category);
