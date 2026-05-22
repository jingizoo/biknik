-- Reference SQL for the lineage-lookup BIP report used by
-- post_transfer_attribute_update.dest_lookup_bip in the OFAM asset
-- transfer pipeline.
--
-- The pipeline runs this report after a successful cross-book
-- bookTransfer.  It serves TWO purposes:
--
--   1. Destination resolution — find the freshly-created destination
--      asset so processTransaction-updateAssetDescriptiveDetails can
--      target it.  The pipeline picks the row whose BOOK_TYPE_CODE
--      matches the in-flight transfer's target book.
--
--   2. Chain-order counting — return EVERY asset_id in the transferred
--      asset's lineage, oldest first, so the pipeline can read the
--      source link's CHAIN_ORDER and suffix the retired source's Tag
--      Number with the right _N ("not always _1": an asset transferred
--      three times lands the source at CHAIN_ORDER=3 -> _3).
--
-- ---------------------------------------------------------------------
-- WHY THE CHAIN IS WALKED TWO WAYS
-- ---------------------------------------------------------------------
-- A cross-book move shows up in the data in one of two shapes, and the
-- chain has to be reconstructed from BOTH or it falls flat:
--
--   (A) TRUE bookTransfer.  processTransaction-bookTransfer writes a
--       row to FA_BOOK_TRANSFER_DISTS linking the retired source asset
--       to the freshly-created destination asset.  Recent transfers
--       done by this pipeline look like this.
--
--   (B) HISTORICAL addition + retirement.  Before the pipeline (and
--       still, for some manual moves) a "transfer" was done by hand:
--       retire the asset in the old book, ADD a brand-new asset in the
--       new book.  Oracle writes NO FA_BOOK_TRANSFER_DISTS row for
--       that pair — so a walk over transfer rows alone loses every
--       hand-migrated link and the source comes back at CHAIN_ORDER=1
--       when it should be 2, 3, ...
--
--       The breadcrumb that survives a hand migration is the
--       "Source Asset Number" descriptive flexfield: whoever added the
--       new asset recorded the asset_number it was copied from.  We
--       recover the lost edge by joining that DFF back to the
--       predecessor's ASSET_NUMBER.
--
-- The ``edges`` CTE below UNIONs both edge shapes into one parent->child
-- relation; the CONNECT BY walks recover the full lineage regardless of
-- how any individual hop was recorded.  For pipeline-created transfers
-- both edges exist (the transfer row AND the Source Asset Number DFF
-- the pipeline populates) — UNION dedups, so the overlap is harmless
-- and gives the chain a redundant path.
--
-- Source-to-destination mapping for cross-book transfers lives in
-- FA_BOOK_TRANSFER_DISTS.  See:
--   https://docs.oracle.com/en/cloud/saas/financials/25d/oedmf/fabooktransferdists-13170.html
--   https://docs.oracle.com/en/cloud/saas/financials/25d/oedmf/faretirements-6428.html
--
-- Bind variables expected from the runner (see ${...} substitutions in
-- post_transfer_attribute_update.dest_lookup_bip.params):
--   :P_SOURCE_ASSET_ID    - X_ASSET_ID of the asset being transferred
--                           in *this* run (the link in the chain that
--                           was retired by the bookTransfer just
--                           completed).
--
-- The destination filter (book_type_code = target book) is applied in
-- the Python pipeline, not here — keeping the report reusable for both
-- destination lookup and full-chain ordering.
--
-- >>> CONFIRM THE DFF COLUMN BEFORE DEPLOYING <<<
-- The edges (B) branch assumes "Source Asset Number" lives in
-- FA_ADDITIONS_B.ATTRIBUTE3 (the physical column behind the
-- P_CAT_ATTRIBUTE3 parameter the pipeline POSTs).  If your tenant's
-- Asset Descriptive Flexfield maps that segment to a different
-- ATTRIBUTEn column, change it in ONE place — the edges (B) branch —
-- and nowhere else.  Validation query 3 at the bottom finds the real
-- column.

WITH
-- =====================================================================
-- edges: every parent -> child hop in an asset's lineage, regardless
-- of HOW the move was recorded.  Two columns only, so UNION collapses
-- the (A)/(B) overlap for pipeline transfers down to one hop.
-- =====================================================================
edges AS (
    --------------------------------------------------------------------
    -- (A) TRUE bookTransfer edges — one row per transfer in
    --     FA_BOOK_TRANSFER_DISTS (a transfer may have several
    --     distribution rows; UNION dedups them to one hop).
    --------------------------------------------------------------------
    SELECT btd.src_asset_id  AS parent_asset_id,
           btd.dest_asset_id AS child_asset_id
    FROM   fa_book_transfer_dists btd
    WHERE  btd.src_asset_id  IS NOT NULL
      AND  btd.dest_asset_id IS NOT NULL
      AND  btd.src_asset_id != btd.dest_asset_id

    UNION

    --------------------------------------------------------------------
    -- (B) HISTORICAL addition + retirement edges, recovered from the
    --     "Source Asset Number" DFF the hand-added asset carried.
    --
    --     child  = the asset that was ADDED in the new book.
    --     parent = the predecessor it was copied from, located by
    --              matching the DFF to the predecessor's ASSET_NUMBER.
    --
    --     >>> ATTRIBUTE3 = "Source Asset Number" DFF — see file header
    --     and validation query 3.  Change here only if it differs. <<<
    --------------------------------------------------------------------
    SELECT parent.asset_id AS parent_asset_id,
           child.asset_id  AS child_asset_id
    FROM   fa_additions_b child
    JOIN   fa_additions_b parent
           ON TRIM(parent.asset_number) = TRIM(child.attribute3)
    WHERE  child.attribute3 IS NOT NULL
      AND  parent.asset_id  != child.asset_id
),
-- =====================================================================
-- chain_links: every asset_id in the seed asset's lineage — ancestors
-- (walk UP the edges), descendants (walk DOWN), and the seed itself.
-- NOCYCLE guards against a malformed DFF pointing into a loop; the
-- LEVEL cap guards against a runaway false chain from bad DFF data.
-- =====================================================================
chain_links AS (
    -- Ancestors: every asset the seed ultimately descends from.
    SELECT e.parent_asset_id AS asset_id
    FROM   edges e
    START  WITH e.child_asset_id = :P_SOURCE_ASSET_ID
    CONNECT BY NOCYCLE PRIOR e.parent_asset_id = e.child_asset_id
           AND LEVEL <= 50

    UNION

    -- Descendants: the destination just created, plus any onward hops.
    SELECT e.child_asset_id
    FROM   edges e
    START  WITH e.parent_asset_id = :P_SOURCE_ASSET_ID
    CONNECT BY NOCYCLE PRIOR e.child_asset_id = e.parent_asset_id
           AND LEVEL <= 50

    UNION

    -- The seed asset itself (the link retired by THIS transfer) — kept
    -- even when it has no ancestors and no descendants.
    SELECT :P_SOURCE_ASSET_ID
    FROM   dual
),
-- =====================================================================
-- this_destination: the asset created by the bookTransfer that just
-- completed — the most-recent TRUE-transfer child of the seed.  Only
-- this row is flagged IS_DESTINATION=Y; a DFF-only descendant from some
-- older hand-migration must not be mistaken for the new asset.
-- =====================================================================
this_destination AS (
    SELECT MAX(btd.dest_asset_id)
             KEEP (DENSE_RANK LAST ORDER BY btd.creation_date) AS dest_asset_id
    FROM   fa_book_transfer_dists btd
    WHERE  btd.src_asset_id = :P_SOURCE_ASSET_ID
),
-- =====================================================================
-- resolved: one row per distinct chain asset, with its book resolved.
-- Book lookup is a COALESCE waterfall so it works for never-transferred,
-- just-retired, and previously-transferred assets alike:
--   1. src_book_type_code from the most recent transfer-OUT
--   2. dest_book_type_code from the most recent transfer-IN
--   3. fa_books (any row — handles hand-migrated assets that have no
--      transfer row at all, and survives the date_ineffective flip a
--      bookTransfer / retirement applies to the source's fa_books row).
-- =====================================================================
resolved AS (
    SELECT fa.asset_id      AS asset_id,
           fa.asset_number  AS asset_number,
           fa.creation_date AS creation_date,
           COALESCE(
             (SELECT MAX(btd.src_book_type_code)
                       KEEP (DENSE_RANK LAST ORDER BY btd.creation_date)
                FROM   fa_book_transfer_dists btd
                WHERE  btd.src_asset_id = fa.asset_id),
             (SELECT MAX(btd.dest_book_type_code)
                       KEEP (DENSE_RANK LAST ORDER BY btd.creation_date)
                FROM   fa_book_transfer_dists btd
                WHERE  btd.dest_asset_id = fa.asset_id),
             (SELECT MAX(fb.book_type_code)
                FROM   fa_books fb
                WHERE  fb.asset_id = fa.asset_id)
           )                AS book_type_code
    FROM   (SELECT DISTINCT asset_id
            FROM   chain_links
            WHERE  asset_id IS NOT NULL) cl
    JOIN   fa_additions_b fa ON fa.asset_id = cl.asset_id
)
SELECT  r.asset_id       AS asset_id,
        r.asset_number   AS asset_number,
        r.book_type_code AS book_type_code,
        -- CHAIN_ORDER: oldest link = 1, newest = N.  fa_additions_b.
        -- creation_date is the row-insert timestamp of EACH asset and
        -- rises strictly along the chain whether a hop was a transfer
        -- (new dest row) or a hand addition (new added row), so it
        -- orders both edge types consistently.  The source's
        -- CHAIN_ORDER is the _N the pipeline suffixes it with.
        ROW_NUMBER() OVER (
            ORDER BY r.creation_date, r.asset_id
        )                AS chain_order,
        CASE
            WHEN r.asset_id = (SELECT dest_asset_id FROM this_destination)
            THEN 'Y' ELSE 'N'
        END              AS is_destination,
        -- One row per asset: scalar subquery, not a join, so an asset
        -- with several PROCESSED retirements can't multiply chain rows.
        (SELECT MAX(fr.date_retired)
           FROM   fa_retirements fr
           WHERE  fr.asset_id = r.asset_id
             AND  fr.status   = 'PROCESSED')  AS retirement_date,
        -- Back-compat aliases: existing destination-resolution code
        -- reads NEW_ASSET_ID / NEW_ASSET_NUMBER, so every row exposes
        -- them.  The pipeline filters by BOOK_TYPE_CODE to pick the
        -- right destination row.
        r.asset_id       AS new_asset_id,
        r.asset_number   AS new_asset_number,
        r.book_type_code AS target_book_type_code
FROM    resolved r
ORDER BY chain_order;

-- ---------------------------------------------------------------------
-- Schema validation queries
-- ---------------------------------------------------------------------
-- 1. Inspect the actual transfer mapping for a known source asset.
--    Run before deploying to confirm the transfer walk works for an
--    asset that's been through a TRUE bookTransfer.
--
-- SELECT btd.src_asset_id,
--        btd.src_book_type_code,
--        btd.dest_asset_id,
--        btd.dest_book_type_code,
--        btd.creation_date
-- FROM   fa_book_transfer_dists btd
-- START  WITH btd.dest_asset_id = :P_SOURCE_ASSET_ID
-- CONNECT BY PRIOR btd.src_asset_id = btd.dest_asset_id;
--
-- 2. Confirm CONNECT BY is permitted in your pod (some hardened
--    environments restrict recursive queries).  Expected: returns at
--    least one row when the source has ancestors.
--
-- SELECT COUNT(*)
-- FROM   fa_book_transfer_dists btd
-- START  WITH btd.dest_asset_id = :P_SOURCE_ASSET_ID
-- CONNECT BY PRIOR btd.src_asset_id = btd.dest_asset_id;
--
-- 3. >>> FIND THE "Source Asset Number" DFF COLUMN <<<
--    The edges (B) branch joins on FA_ADDITIONS_B.ATTRIBUTE3.  Confirm
--    that is the column behind P_CAT_ATTRIBUTE3 "Source Asset Number".
--    Pick an asset you KNOW was hand-migrated and whose predecessor's
--    asset_number you know, then scan every attribute column for it:
--
-- SELECT fa.asset_id, fa.asset_number, fa.attribute_category_code,
--        fa.attribute1,  fa.attribute2,  fa.attribute3,  fa.attribute4,
--        fa.attribute5,  fa.attribute6,  fa.attribute7,  fa.attribute8,
--        fa.attribute9,  fa.attribute10, fa.attribute11, fa.attribute12,
--        fa.attribute13, fa.attribute14, fa.attribute15
-- FROM   fa_additions_b fa
-- WHERE  fa.asset_id = :P_SOURCE_ASSET_ID;
--    -- (attribute16..attribute30 also exist if the DFF runs long.)
--    The column showing the predecessor's asset_number is the one the
--    edges (B) branch must join on — update it there if not ATTRIBUTE3.
--
-- 4. Preview the addition+retirement edges recovered from the DFF for
--    one asset — confirms the breadcrumb resolves to a real
--    predecessor before you trust the merged chain.
--
-- SELECT child.asset_id      AS child_asset_id,
--        child.asset_number  AS child_asset_number,
--        child.attribute3    AS source_asset_number_dff,
--        parent.asset_id     AS parent_asset_id,
--        parent.asset_number AS parent_asset_number
-- FROM   fa_additions_b child
-- JOIN   fa_additions_b parent
--        ON TRIM(parent.asset_number) = TRIM(child.attribute3)
-- WHERE  child.asset_id = :P_SOURCE_ASSET_ID;
--
-- 5. Pre-flight: count the merged chain size for a known transferred
--    asset before relying on it in production — guards against a
--    runaway false chain from bad DFF data (CONNECT BY is LEVEL-capped
--    at 50 above; a healthy lineage is single digits).
