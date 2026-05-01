/*
Diagnostic SQL: locate which Oracle FA columns hold the CMDB-side
identifiers Citadel uses.

Run each block in OTBI > Reports & Analytics > Data Model > Query mode
(or directly against the FA schema if you have read access).  Use the
output to update sql/cmdb_oracle_translations.sql so the production
BIP report returns rows.
*/

-- ============================================================================
-- 1. LOCATION: which FA_LOCATIONS column holds 'LC000030'?
-- ============================================================================
-- The ServiceNow `u_cit_location_code` returned 'LC000030' for asset
-- C42105000676.  It must live in one of FA_LOCATIONS' attributes or
-- segments — this scans all of them at once.

SELECT
    fl.location_id,
    fl.segment1,  fl.segment2,  fl.segment3,  fl.segment4,
    fl.attribute1, fl.attribute2, fl.attribute3, fl.attribute4, fl.attribute5,
    fl.attribute6, fl.attribute7, fl.attribute8, fl.attribute9, fl.attribute10
FROM   fa_locations fl
WHERE  fl.enabled_flag = 'Y'
  AND  'LC000030' IN (
        fl.segment1,  fl.segment2,  fl.segment3,  fl.segment4,
        fl.attribute1, fl.attribute2, fl.attribute3, fl.attribute4, fl.attribute5,
        fl.attribute6, fl.attribute7, fl.attribute8, fl.attribute9, fl.attribute10,
        fl.attribute11, fl.attribute12, fl.attribute13, fl.attribute14, fl.attribute15
       );

-- Once you know the column (say it's ATTRIBUTE3), update the
-- "LOCATION" UNION branch in cmdb_oracle_translations.sql:
--   fl.attribute3  AS CMDB_CODE,
--   fl.attribute3  LIKE 'LC%'

-- ============================================================================
-- 2. COST CENTRE: which GL_CODE_COMBINATIONS segment holds '30755'?
-- ============================================================================
-- Cost centre prefix from cost_center.name = '30755 - Network Hardware'.
-- Most Citadel charts of accounts put cost centre in segment2, but verify.

SELECT
    gcc.code_combination_id,
    gcc.segment1, gcc.segment2, gcc.segment3, gcc.segment4,
    gcc.segment5, gcc.segment6, gcc.segment7, gcc.segment8
FROM   gl_code_combinations gcc
WHERE  gcc.enabled_flag = 'Y'
  AND  '30755' IN (
        gcc.segment1, gcc.segment2, gcc.segment3, gcc.segment4,
        gcc.segment5, gcc.segment6, gcc.segment7, gcc.segment8
       )
  AND  ROWNUM <= 5;

-- The segment that contains '30755' is your cost-centre segment.
-- If it's not segment2, change "gcc.segment2" everywhere in the
-- "EXPENSE_CCID" branch of cmdb_oracle_translations.sql.

-- ============================================================================
-- 3. WHICH BOOK + COA does the pilot use?
-- ============================================================================
-- Confirms the join in cmdb_oracle_translations.sql is hitting the
-- right ledger.  US CORP BOOK should resolve to one chart_of_accounts_id.

SELECT
    fbc.book_type_code,
    gl.ledger_id,
    gl.name              AS ledger_name,
    gl.chart_of_accounts_id
FROM   fa_book_controls fbc
JOIN   gl_ledgers       gl  ON gl.ledger_id = fbc.set_of_books_id
WHERE  fbc.book_type_code   = 'US CORP BOOK'
  AND  fbc.date_ineffective IS NULL;

-- ============================================================================
-- 4. SAMPLE: a single FA distribution for cost centre '30755' + its CCID
-- ============================================================================
-- Once you know which segment is cost centre (substitute below), this
-- gives you the depreciation-expense CCID currently used for cost
-- centre 30755 in the pilot book — exactly what Oracle FA expects in
-- P_DEPRN_EXPENSE_CCID_TBL.

SELECT
    dh.code_combination_id,
    gcc.segment1, gcc.segment2, gcc.segment3, gcc.segment4,
    COUNT(*) AS asset_count
FROM   fa_distribution_history dh
JOIN   gl_code_combinations    gcc ON gcc.code_combination_id = dh.code_combination_id
JOIN   fa_book_controls        fbc ON fbc.book_type_code      = 'US CORP BOOK'
                                   AND fbc.date_ineffective    IS NULL
WHERE  gcc.segment2          = '30755'   -- adjust segment if different
  AND  gcc.enabled_flag       = 'Y'
  AND  gcc.summary_flag       = 'N'
  AND  dh.transaction_header_id_out IS NULL
GROUP BY
    dh.code_combination_id,
    gcc.segment1, gcc.segment2, gcc.segment3, gcc.segment4
ORDER BY asset_count DESC
FETCH FIRST 5 ROWS ONLY;
