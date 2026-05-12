-- Reference SQL for the destination-asset lookup BIP report used by
-- post_transfer_attribute_update.dest_lookup_bip in the OFAM asset
-- transfer pipeline.
--
-- The pipeline runs this report after a successful cross-book
-- bookTransfer to find the freshly-created destination asset, then
-- POSTs processTransaction-updateAssetDescriptiveDetails against the
-- new asset_id to bump ATTRIBUTE10 (or whichever DFF slot is configured).
--
-- Source-to-destination mapping for cross-book transfers lives in
-- FA_BOOK_TRANSFER_DISTS (not FA_ASSET_HISTORY).  See:
--   https://docs.oracle.com/en/cloud/saas/financials/25d/oedmf/fabooktransferdists-13170.html
--   https://docs.oracle.com/en/cloud/saas/financials/25d/oedmf/faassethistory-6188.html
--   https://docs.oracle.com/en/cloud/saas/financials/25d/oedmf/fatransactionheaders-6450.html
--
-- Bind variables expected from the runner (see ${...} substitutions in
-- post_transfer_attribute_update.dest_lookup_bip.params):
--   :P_SOURCE_ASSET_ID         - X_ASSET_ID of the source asset
--   :P_TARGET_BOOK_TYPE_CODE   - destination FA book code
--
-- Picks the most recent destination row when multiple book-transfers
-- exist for the same (source asset, target book) pair.

WITH x AS (
    SELECT btd.dest_asset_id,
           btd.dest_book_type_code,
           MAX(btd.creation_date) AS creation_date,
           MAX(btd.trx_reference_id)
               KEEP (DENSE_RANK LAST
                     ORDER BY btd.creation_date,
                              btd.book_transfer_dist_id)
               AS trx_reference_id,
           MAX(btd.dest_transaction_header_id)
               KEEP (DENSE_RANK LAST
                     ORDER BY btd.creation_date,
                              btd.book_transfer_dist_id)
               AS dest_transaction_header_id
    FROM   fa_book_transfer_dists btd
    WHERE  btd.src_asset_id        = :P_SOURCE_ASSET_ID
      AND  btd.dest_book_type_code = :P_TARGET_BOOK_TYPE_CODE
    GROUP  BY btd.dest_asset_id,
              btd.dest_book_type_code
),
r AS (
    SELECT x.*,
           ROW_NUMBER() OVER (
               ORDER BY x.creation_date            DESC,
                        x.dest_transaction_header_id DESC
           ) AS rn
    FROM   x
)
SELECT r.dest_asset_id        AS new_asset_id,
       fa.asset_number        AS new_asset_number,
       r.dest_book_type_code  AS target_book_type_code,
       r.trx_reference_id,
       r.dest_transaction_header_id,
       th.transaction_type_code,
       th.transaction_subtype,
       th.transaction_key
FROM   r
JOIN   fa_additions_b fa
       ON fa.asset_id = r.dest_asset_id
LEFT JOIN fa_transaction_headers th
       ON th.transaction_header_id = r.dest_transaction_header_id
      AND th.book_type_code        = r.dest_book_type_code
WHERE  r.rn = 1;

-- ---------------------------------------------------------------------
-- Schema validation queries
-- ---------------------------------------------------------------------
-- 1. Confirm the columns we DON'T use are absent from FA_ASSET_HISTORY
--    (sanity check against earlier incorrect docs).  Expected: 0 rows.
--
-- SELECT column_name
-- FROM   all_tab_columns
-- WHERE  table_name = 'FA_ASSET_HISTORY'
--   AND  column_name IN ('SOURCE_ASSET_ID', 'TRANSACTION_TYPE_CODE');
--
-- 2. Inspect the actual transfer mapping for a known source asset
--    (run before deploying to confirm rows show up).
--
-- SELECT btd.src_asset_id,
--        btd.src_book_type_code,
--        btd.dest_asset_id,
--        btd.dest_book_type_code,
--        btd.trx_reference_id,
--        btd.src_transaction_header_id,
--        btd.dest_transaction_header_id,
--        btd.creation_date
-- FROM   fa_book_transfer_dists btd
-- WHERE  btd.src_asset_id = :P_SOURCE_ASSET_ID
-- ORDER  BY btd.creation_date DESC;
--
-- 3. Discover the actual transaction_type_code / subtype / key values
--    your pod uses for book transfers — these vary by Oracle version.
--    Do NOT hard-code 'BOOK_TRANSFER' anywhere without verifying first.
--
-- SELECT th.transaction_type_code,
--        th.transaction_subtype,
--        th.transaction_key,
--        COUNT(*) AS cnt
-- FROM   fa_transaction_headers th
-- WHERE  th.asset_id IN (:P_SOURCE_ASSET_ID, :P_DEST_ASSET_ID)
-- GROUP  BY th.transaction_type_code,
--           th.transaction_subtype,
--           th.transaction_key
-- ORDER  BY cnt DESC;
