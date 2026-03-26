/*
Loose asset-transfer candidate extraction for the uploaded Excel template.

Required binds:
  :p_source_book_type_code   -- source book, e.g. US CORP BOOK
  :p_target_book_type_code   -- target book, e.g. UK CORP BOOK

Optional binds:
  :p_transfer_to_entity      -- if null, derived from target book's legal entity
  :p_transfer_date           -- YYYY-MM-DD, defaults to sysdate
  :p_target_location_text    -- if null, defaults to current source location text
  :p_target_location_id      -- if null, defaults to current source location_id
  :p_target_expense_ccid     -- if null, best-effort target CCID is derived
  :p_raw_target_cost_center  -- if null, derived from source expense account segment
  :p_cost_center_segment_no  -- default 2
  :p_notes                   -- appended to NOTES
  :p_max_rows                -- optional row cap

Behavior:
- Loose selection: only source book + target book are required.
- Uses current FA_BOOKS row and picks the "best" current distribution row per asset
  (largest units, then latest effective date).
- Fills target fields from overrides first, otherwise from best available source values.
- Derives TRANSFER_TO_ENTITY from the target book's legal entity (via ledger relationships)
  when not explicitly provided.
- Does NOT enforce open-period validation, one-and-only-one distribution, or strict transfer eligibility.
*/
WITH
/* Resolve legal_entity_id from a book_type_code via:
   fa_book_controls → gl_ledgers → gl_ledger_relationships */
book_legal_entity AS (
    SELECT
        fbc.book_type_code,
        glr.legal_entity_id,
        ROW_NUMBER() OVER (
            PARTITION BY fbc.book_type_code
            ORDER BY glr.last_update_date DESC, glr.legal_entity_id
        ) AS rn
    FROM fa_book_controls fbc
    JOIN gl_ledgers gl
      ON gl.ledger_id = fbc.set_of_books_id
    JOIN gl_ledger_relationships glr
      ON glr.target_ledger_id = gl.ledger_id
     AND glr.relationship_type_code IN ('SUBLEDGER', 'JOURNAL')
    WHERE fbc.date_ineffective IS NULL
),
target_entity_lookup AS (
    SELECT book_type_code, legal_entity_id
    FROM book_legal_entity
    WHERE rn = 1
),
params AS (
    SELECT
        :p_source_book_type_code                              AS source_book_type_code,
        :p_target_book_type_code                              AS target_book_type_code,
        NVL(:p_transfer_to_entity,
            (SELECT TO_CHAR(tel.legal_entity_id)
             FROM target_entity_lookup tel
             WHERE tel.book_type_code = :p_target_book_type_code)
        )                                                     AS transfer_to_entity,
        NVL(TO_DATE(:p_transfer_date, 'YYYY-MM-DD'), TRUNC(SYSDATE)) AS transfer_date,
        :p_target_location_text                               AS target_location_text,
        :p_target_location_id                                 AS target_location_id,
        :p_target_expense_ccid                                AS target_expense_ccid,
        :p_raw_target_cost_center                             AS raw_target_cost_center,
        NVL(:p_cost_center_segment_no, 2)                     AS cost_center_segment_no,
        :p_notes                                              AS notes,
        NVL(:p_max_rows, 5000)                                AS max_rows
    FROM dual
),
book_controls_latest AS (
    SELECT
        fbc.book_type_code,
        MAX(fbc.set_of_books_id) KEEP (DENSE_RANK LAST ORDER BY fbc.last_update_date) AS ledger_id
    FROM fa_book_controls fbc
    GROUP BY fbc.book_type_code
),
source_book_ctx AS (
    SELECT
        p.source_book_type_code AS book_type_code,
        bcl.ledger_id,
        gl.chart_of_accounts_id AS coa_id
    FROM params p
    LEFT JOIN book_controls_latest bcl
      ON bcl.book_type_code = p.source_book_type_code
    LEFT JOIN gl_ledgers gl
      ON gl.ledger_id = bcl.ledger_id
),
target_book_ctx AS (
    SELECT
        p.target_book_type_code AS book_type_code,
        bcl.ledger_id,
        gl.chart_of_accounts_id AS coa_id
    FROM params p
    LEFT JOIN book_controls_latest bcl
      ON bcl.book_type_code = p.target_book_type_code
    LEFT JOIN gl_ledgers gl
      ON gl.ledger_id = bcl.ledger_id
),
base_assets AS (
    SELECT
        fb.asset_id,
        adb.asset_number,
        fb.book_type_code,
        adb.asset_type,
        fb.date_placed_in_service,
        fb.cost,
        fb.original_cost,
        fb.adjusted_cost,
        adb.tag_number,
        adb.serial_number,
        adb.model_number
    FROM fa_books fb
    JOIN fa_additions_b adb
      ON adb.asset_id = fb.asset_id
    JOIN params p
      ON p.source_book_type_code = fb.book_type_code
    WHERE fb.date_ineffective IS NULL
      AND NVL(fb.cost, 0) <> 0
      AND UPPER(NVL(adb.asset_type, 'CAPITALIZED')) NOT LIKE 'GROUP%'
      AND UPPER(NVL(adb.asset_type, 'CAPITALIZED')) NOT LIKE 'EXPENS%'
      AND UPPER(NVL(adb.asset_type, 'CAPITALIZED')) NOT LIKE 'CONSTRUCTION%'
),
ranked_dists AS (
    SELECT
        dh.asset_id,
        dh.book_type_code,
        dh.distribution_id,
        dh.units_assigned,
        dh.location_id,
        dh.code_combination_id,
        dh.date_effective,
        ROW_NUMBER() OVER (
            PARTITION BY dh.asset_id, dh.book_type_code
            ORDER BY NVL(dh.units_assigned, 0) DESC,
                     dh.date_effective DESC,
                     dh.distribution_id DESC
        ) AS rn
    FROM fa_distribution_history dh
    JOIN params p
      ON p.source_book_type_code = dh.book_type_code
    WHERE dh.transaction_header_id_out IS NULL
       OR dh.date_ineffective IS NULL
),
best_dist AS (
    SELECT
        rd.asset_id,
        rd.book_type_code,
        rd.distribution_id,
        rd.units_assigned,
        rd.location_id,
        rd.code_combination_id,
        rd.date_effective
    FROM ranked_dists rd
    WHERE rd.rn = 1
),
source_location AS (
    SELECT
        bd.asset_id,
        bd.book_type_code,
        bd.location_id,
        TRIM(BOTH '-' FROM
            NVL(fl.segment1, '') ||
            CASE WHEN fl.segment2 IS NOT NULL THEN '-' || fl.segment2 ELSE '' END ||
            CASE WHEN fl.segment3 IS NOT NULL THEN '-' || fl.segment3 ELSE '' END ||
            CASE WHEN fl.segment4 IS NOT NULL THEN '-' || fl.segment4 ELSE '' END
        ) AS location_text
    FROM best_dist bd
    LEFT JOIN fa_locations fl
      ON fl.location_id = bd.location_id
),
source_ccid AS (
    SELECT
        bd.asset_id,
        bd.book_type_code,
        bd.code_combination_id AS source_expense_ccid,
        gcc.chart_of_accounts_id,
        gcc.enabled_flag,
        gcc.summary_flag,
        gcc.detail_posting_allowed_flag,
        gcc.segment1,
        gcc.segment2,
        gcc.segment3,
        gcc.segment4,
        gcc.segment5,
        gcc.segment6,
        gcc.segment7,
        gcc.segment8,
        gcc.segment9,
        gcc.segment10
    FROM best_dist bd
    LEFT JOIN gl_code_combinations gcc
      ON gcc.code_combination_id = bd.code_combination_id
),
matched_target_ccid AS (
    SELECT
        s.asset_id,
        g2.code_combination_id,
        ROW_NUMBER() OVER (
            PARTITION BY s.asset_id
            ORDER BY g2.code_combination_id
        ) AS rn
    FROM source_ccid s
    CROSS JOIN target_book_ctx tb
    JOIN gl_code_combinations g2
      ON g2.chart_of_accounts_id = tb.coa_id
     AND NVL(g2.enabled_flag, 'Y') = 'Y'
     AND NVL(g2.summary_flag, 'N') = 'N'
     AND NVL(g2.detail_posting_allowed_flag, 'Y') = 'Y'
     AND NVL(g2.segment1,  '~') = NVL(s.segment1,  '~')
     AND NVL(g2.segment2,  '~') = NVL(s.segment2,  '~')
     AND NVL(g2.segment3,  '~') = NVL(s.segment3,  '~')
     AND NVL(g2.segment4,  '~') = NVL(s.segment4,  '~')
     AND NVL(g2.segment5,  '~') = NVL(s.segment5,  '~')
     AND NVL(g2.segment6,  '~') = NVL(s.segment6,  '~')
     AND NVL(g2.segment7,  '~') = NVL(s.segment7,  '~')
     AND NVL(g2.segment8,  '~') = NVL(s.segment8,  '~')
     AND NVL(g2.segment9,  '~') = NVL(s.segment9,  '~')
     AND NVL(g2.segment10, '~') = NVL(s.segment10, '~')
),
selected_target_ccid AS (
    SELECT
        asset_id,
        code_combination_id
    FROM matched_target_ccid
    WHERE rn = 1
),
assembled AS (
    SELECT
        ba.asset_id,
        ba.asset_number,
        ba.book_type_code,
        ba.asset_type,
        ba.date_placed_in_service,
        ba.cost,
        ba.original_cost,
        ba.adjusted_cost,
        ba.tag_number,
        ba.serial_number,
        ba.model_number,
        bd.units_assigned,
        bd.location_id AS source_location_id,
        sl.location_text AS source_location_text,
        scc.source_expense_ccid,
        sbc.coa_id AS source_coa_id,
        tbc.coa_id AS target_coa_id,
        stc.code_combination_id AS matched_target_expense_ccid,
        CASE (SELECT cost_center_segment_no FROM params)
            WHEN 1  THEN scc.segment1
            WHEN 2  THEN scc.segment2
            WHEN 3  THEN scc.segment3
            WHEN 4  THEN scc.segment4
            WHEN 5  THEN scc.segment5
            WHEN 6  THEN scc.segment6
            WHEN 7  THEN scc.segment7
            WHEN 8  THEN scc.segment8
            WHEN 9  THEN scc.segment9
            WHEN 10 THEN scc.segment10
            ELSE scc.segment2
        END AS derived_cost_center
    FROM base_assets ba
    LEFT JOIN best_dist bd
      ON bd.asset_id = ba.asset_id
     AND bd.book_type_code = ba.book_type_code
    LEFT JOIN source_location sl
      ON sl.asset_id = ba.asset_id
     AND sl.book_type_code = ba.book_type_code
    LEFT JOIN source_ccid scc
      ON scc.asset_id = ba.asset_id
     AND scc.book_type_code = ba.book_type_code
    LEFT JOIN source_book_ctx sbc
      ON sbc.book_type_code = ba.book_type_code
    LEFT JOIN target_book_ctx tbc
      ON 1 = 1
    LEFT JOIN selected_target_ccid stc
      ON stc.asset_id = ba.asset_id
),
final_ranked AS (
    SELECT
        ROW_NUMBER() OVER (
            ORDER BY NVL(a.adjusted_cost, a.cost) DESC,
                     a.cost DESC,
                     a.date_placed_in_service ASC,
                     a.asset_number
        ) AS seq_no,
        a.*
    FROM assembled a
)
SELECT *
FROM (
    SELECT
        'XFER_' || TO_CHAR(p.transfer_date, 'YYYYMMDD') || '_' || LPAD(fr.seq_no, 6, '0') AS REQUEST_ID,
        fr.asset_number                                                                     AS ASSET_NUMBER,
        fr.asset_id                                                                         AS ASSET_ID,
        fr.book_type_code                                                                   AS BOOK_TYPE_CODE,
        p.transfer_to_entity                                                                AS TRANSFER_TO_ENTITY,
        TO_CHAR(p.transfer_date, 'YYYY-MM-DD')                                              AS TRANSFER_DATE,
        NVL(p.target_location_text, fr.source_location_text)                                AS TRANSFER_TO_LOCATION,
        NVL(p.target_location_id, fr.source_location_id)                                    AS TARGET_LOCATION_ID,
        COALESCE(
            p.target_expense_ccid,
            fr.matched_target_expense_ccid,
            CASE
                WHEN fr.source_coa_id = fr.target_coa_id THEN fr.source_expense_ccid
                ELSE NULL
            END,
            fr.source_expense_ccid
        )                                                                                   AS TARGET_EXPENSE_CCID,
        p.target_book_type_code                                                             AS TARGET_BOOK_TYPE_CODE,
        NVL(p.raw_target_cost_center, fr.derived_cost_center)                               AS RAW_TARGET_COST_CENTER,
        RTRIM(
            CASE WHEN p.notes IS NOT NULL THEN p.notes || ' | ' END ||
            'LOOSE_PICK' ||
            ' | ASSET_TYPE=' || NVL(fr.asset_type, 'UNKNOWN') ||
            ' | SRC_LOC_ID=' || NVL(TO_CHAR(fr.source_location_id), 'NULL') ||
            ' | SRC_CCID=' || NVL(TO_CHAR(fr.source_expense_ccid), 'NULL') ||
            CASE WHEN fr.matched_target_expense_ccid IS NOT NULL THEN ' | TGT_CCID_MATCHED=Y' ELSE ' | TGT_CCID_MATCHED=N' END ||
            CASE WHEN fr.source_coa_id = fr.target_coa_id THEN ' | SAME_COA=Y' ELSE ' | SAME_COA=N' END ||
            ' | COST=' || TO_CHAR(fr.cost) ||
            ' | ADJ_COST=' || TO_CHAR(fr.adjusted_cost) ||
            ' | UNITS=' || NVL(TO_CHAR(fr.units_assigned), 'NULL')
        )                                                                                   AS NOTES
    FROM final_ranked fr
    CROSS JOIN params p
)
WHERE ROWNUM <= (SELECT max_rows FROM params)
ORDER BY REQUEST_ID;
