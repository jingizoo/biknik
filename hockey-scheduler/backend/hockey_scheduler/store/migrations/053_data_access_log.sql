-- 053_data_access_log: durable sensitive-read audit rows (#124).
--
-- NUMBERING NOTE (parallel lanes): versions 051 and 052 are now claimed by
-- PR #423 (context-epoch), merged to main ahead of this slice. This PR (#426)
-- lands before #424 (athlete identity)/#425 (SeasonRosterMembership), which
-- must renumber past 053 at their own merge time per the queue agreement.
--
-- One row per sensitive read — or refused read attempt — of one subject's
-- protected data (domain/privacy.py: DataAccessLog). Deliberately SEPARATE
-- from audit_logs/setup_audit_logs: read auditing has its own retention and
-- export story (#124 blocks 3-4, out of this slice), and mixing it into the
-- mutation trail would weld those decisions together.
--
-- STRUCTURALLY VALUE-FREE: there is no column for the value that was read,
-- and no free-form JSON column a value could be smuggled into. Recording
-- that a contact destination or birthdate was read must never copy it here.
-- ``subject_id``/``request_id`` are themselves bounded, validated opaque
-- tokens (#426 review finding 3) — the write-side boundary
-- (api/service.py's ``_canonical_subject_id``/``_safe_request_id``) refuses
-- to pass an unbounded, caller-shaped string through to these columns, so a
-- planted secret used AS an id cannot ride along disguised as one.
--
-- CLOSED VOCABULARY (#426 review finding 5): ``category`` and ``outcome``
-- are CHECK-constrained to the exact sets ``domain/privacy.py`` defines
-- (``SensitiveFieldCategory``/``VALID_OUTCOMES``) so an invalid value is
-- refused by the ENGINE, not merely by application convention — the same
-- invariant ``InMemoryStore._validate_data_access`` enforces in Python for
-- the backend with no engine to ask.
--
-- ORDERING (#426 review finding 5): ``seq`` is a real monotonic sequence —
-- assigned atomically via the SAME ``counters`` table ``next_id()`` already
-- uses (a dedicated ``__data_access_seq__`` prefix), never derived from the
-- textual ``id`` — because ``ORDER BY id`` sorts ``daccess_10`` before
-- ``daccess_2``. Mirrors PR #423's persisted-generation-counter pattern
-- (``ContextService``'s switch generation / ``epoch_fence``'s version
-- columns) rather than inventing a new ordering mechanism.
--
-- Portable additive DDL (TEXT only, CREATE IF NOT EXISTS, no rebuild, no
-- backfill): the table starts empty everywhere and existing rows are never
-- touched, so this is safe on a populated upgrade in either engine. This
-- table has never shipped to a released main (still on the #426 draft PR),
-- so the CREATE TABLE below is the complete, final shape rather than an
-- ALTER TABLE follow-up.
-- Column order matches ``fields(DataAccessLog)`` exactly (category ...
-- request_id, THEN id, THEN seq — #426 round-2 review finding 1 moved `id`
-- next to `seq` at the end of the dataclass, both now store-assigned at the
-- same moment rather than caller-precomputed; see domain/privacy.py's
-- DURABLE ID ALLOCATION section) — asserted by
-- ``test_table_columns_match_the_dataclass_exactly``. Functionally the
-- mapper always names columns explicitly (never ``SELECT *``), so physical
-- order cannot break a read/write either way; matching it anyway keeps the
-- schema legible next to the dataclass. `id` stays the PRIMARY KEY
-- regardless of its column position.
CREATE TABLE IF NOT EXISTS data_access_logs (
    category TEXT NOT NULL
        CHECK (category IN ('birthdate', 'registration_number',
                             'contact_destination', 'emergency_contact',
                             'medical_alert', 'discipline_note')),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    at TEXT NOT NULL,
    actor_user_id TEXT,
    actor_role TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('allowed', 'denied')),
    request_id TEXT,
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
);

-- The two queries the log exists to answer: "every access to THIS subject's
-- data" (export/data-rights, #124 block 3) and "everything THIS actor read"
-- (accountability review).
CREATE INDEX IF NOT EXISTS ix_data_access_subject
    ON data_access_logs (subject_type, subject_id);
CREATE INDEX IF NOT EXISTS ix_data_access_actor
    ON data_access_logs (actor_user_id);
-- The real chronological read order (#426 review finding 5) — see ORDERING
-- above. UNIQUE, not merely indexed: two rows can never legitimately share a
-- sequence position, so a collision is a bug the engine itself refuses.
CREATE UNIQUE INDEX IF NOT EXISTS ux_data_access_seq
    ON data_access_logs (seq);
