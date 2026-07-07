-- 013_guardian_link_consent: consent record on guardian ↔ junior links (#35).
--
-- How an operator obtained/confirmed guardian authorization (e.g.
-- "signed_form", "verbal_confirmed", "email_reply") and when — the GDPR
-- Art. 8 consent record. Both nullable: existing/seed-created links (and any
-- internal verification that predates this column) simply have no recorded
-- consent method yet.

ALTER TABLE guardian_links ADD COLUMN consent_method TEXT;
ALTER TABLE guardian_links ADD COLUMN consented_at TEXT;
