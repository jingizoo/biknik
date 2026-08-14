-- 054_league_ranking_policies: per-League substitute-matching configuration
-- (#287 slice 1, stacked on #205's substitute-eligibility cutover).
--
-- (Numbered 054: 051 is reserved by the #273 athlete-identity slice, 052 by
-- the #205 SeasonRosterMembership slice this branch stacks on, and 053 by
-- the #124 privacy-foundation slice — all in flight on the owner-directed
-- merge order on #212, renumbered if needed at merge-time rebase. The
-- loader applies versions in numeric order and tolerates gaps.)
--
-- One row per permanent League holding what issue #287 decided a League may
-- configure about substitute matching:
--   * rules — the ORDERED rule list as JSON:
--         [{"kind": "...", "enabled": true|false}, ...]
--     order IS priority; every RankingRuleKind appears exactly once
--     (service-validated), so "disabled" is an explicit flag and an absent
--     kind is unrepresentable rather than ambiguous. Kinds: fairness
--     (fewest completed sub games), skill_proximity (1-7 scale),
--     position_preference (skaters: exact -> plays-both -> alternate),
--     random (seeded; default tiebreaker in last place). The goalie/skater
--     separation is deliberately NOT a row here: it is a hard gate, not
--     League-configurable data.
--   * notice_window_enabled — the decided EXCLUSION filter (drop candidates
--     needing more advance notice than remains before the game); a filter,
--     not an ordering rule, hence a flag rather than a rules entry.
--   * random_seed — makes the random rule/tiebreaker reproducible.
--   * offer_response_deadline_minutes — the configurable response window of
--     the offer -> accept/decline/timeout workflow.
--
-- PURE PERSISTENCE. Nothing reads this table for ranking yet: the ranking
-- engine is a separate in-flight PR and wiring it to the candidate list is
-- the NEXT #287 slice. Absence of a row is well-defined (the in-code
-- default policy — issue order, all enabled, notice window on, seed 0,
-- 24 h deadline), so this table starts empty, needs no backfill, and
-- existing installs change behavior not at all. #287's open questions
-- (fairness reset, which games count, skill-rating ownership,
-- cross-boundary substitution, late-game offer validity) are NOT encoded —
-- they remain owner decisions.
--
-- Portable additive DDL (TEXT/INTEGER + JSON-as-TEXT only, no rebuild;
-- booleans are INTEGER 0/1 everywhere in this schema — 001/021/029
-- precedent — which is also what lets both engines compare them uniformly);
-- FK to the permanent League, one policy per League by unique index
-- (matches 046's scheduling_policies precedent).

CREATE TABLE IF NOT EXISTS league_ranking_policies (
    id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    rules TEXT NOT NULL,
    notice_window_enabled INTEGER NOT NULL,
    random_seed INTEGER NOT NULL,
    offer_response_deadline_minutes INTEGER NOT NULL,
    FOREIGN KEY (league_id) REFERENCES leagues(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_league_ranking_policies_league
    ON league_ranking_policies (league_id);
