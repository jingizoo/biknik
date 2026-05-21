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
--   2. Chain-wide tag bump (opt-in via post_transfer_attribute_update.
--      bump_chain=true) — return EVERY asset_id in the transferred
--      asset's lineage so the pipeline can POST the same bumped Tag
--      Number against all of them.  Keeps the historical chain
--      (original + every retired intermediate + the new destination)
--      in lockstep on the underscore-suffix scheme.
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
-- destination lookup and full-chain bumping.

WITH chain_links AS (
    -- Walk backwards: every ancestor of the source asset
    SELECT btd.src_asset_id       AS asset_id,
           btd.src_book_type_code AS book_type_code,
           btd.creation_date      AS creation_date,
           -1                     AS direction
    FROM   fa_book_transfer_dists btd
    START  WITH btd.dest_asset_id = :P_SOURCE_ASSET_ID
    CONNECT BY PRIOR btd.src_asset_id = btd.dest_asset_id

    UNION

    -- Walk forwards: every descendant (the destination just created,
    -- plus any onward transfers if the chain branches).
    SELECT btd.dest_asset_id,
           btd.dest_book_type_code,
           btd.creation_date,
           +1
    FROM   fa_book_transfer_dists btd
    START  WITH btd.src_asset_id = :P_SOURCE_ASSET_ID
    CONNECT BY PRIOR btd.dest_asset_id = btd.src_asset_id

    UNION

    -- The source asset itself (the link that was retired by THIS
    -- transfer) — included with direction=0 so it always appears
    -- exactly once in the deduplicated chain.  Pull its active book
    -- from fa_books so BOOK_TYPE_CODE is populated even when the
    -- asset has never been cross-book transferred (and thus has no
    -- rows in fa_book_transfer_dists).
    SELECT :P_SOURCE_ASSET_ID,
           fb.book_type_code,
           SYSDATE,
           0
    FROM   fa_books fb
    WHERE  fb.asset_id            = :P_SOURCE_ASSET_ID
      AND  fb.date_ineffective IS NULL
),
dedup AS (
    SELECT asset_id,
           MAX(book_type_code) KEEP (DENSE_RANK LAST ORDER BY creation_date)
               AS book_type_code,
           MAX(creation_date) AS creation_date,
           MAX(direction)     AS direction
    FROM   chain_links
    WHERE  asset_id IS NOT NULL
    GROUP  BY asset_id
)
SELECT  fa.asset_id        AS asset_id,
        fa.asset_number    AS asset_number,
        d.book_type_code   AS book_type_code,
        ROW_NUMBER() OVER (
            ORDER BY d.creation_date, fa.asset_id
        )                  AS chain_order,
        CASE
            WHEN d.direction = 1 AND d.asset_id != :P_SOURCE_ASSET_ID
            THEN 'Y' ELSE 'N'
        END                AS is_destination,
        fr.date_retired    AS retirement_date,
        -- Back-compat aliases: existing destination-resolution code
        -- reads NEW_ASSET_ID / NEW_ASSET_NUMBER, so every row exposes
        -- them.  The pipeline filters by BOOK_TYPE_CODE to pick the
        -- right destination row.
        fa.asset_id        AS new_asset_id,
        fa.asset_number    AS new_asset_number,
        d.book_type_code   AS target_book_type_code
FROM    dedup d
JOIN    fa_additions_b fa ON fa.asset_id = d.asset_id
LEFT JOIN fa_retirements fr
       ON fr.asset_id = fa.asset_id
      AND fr.status   = 'PROCESSED'
ORDER BY chain_order;

-- ---------------------------------------------------------------------
-- Schema validation queries
-- ---------------------------------------------------------------------
-- 1. Inspect the actual transfer mapping for a known source asset.
--    Run before deploying to confirm the chain walks correctly for an
--    asset that's been transferred multiple times.
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
-- 2. Confirm CONNECT BY is permitted on FA_BOOK_TRANSFER_DISTS in your
--    pod (some hardened environments restrict recursive queries).
--    Expected: returns at least one row when the source has ancestors.
--
-- SELECT COUNT(*)
-- FROM   fa_book_transfer_dists btd
-- START  WITH btd.dest_asset_id = :P_SOURCE_ASSET_ID
-- CONNECT BY PRIOR btd.src_asset_id = btd.dest_asset_id;
--
-- 3. Pre-flight: count the chain size for a known transferred asset
--    before enabling bump_chain in production — guards against
--    runaway chains (cap defaults to 20 in the pipeline).
