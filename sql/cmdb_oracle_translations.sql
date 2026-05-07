/*
CMDB-to-Oracle FA ID Translations Report
=========================================
Run via BI Publisher (ExternalReportWSSService).
Output feeds oracle_translations in the mass-additions config:
  { "location_id": { "LC000030": 300000004974106 },
    "expense_ccid": { "30755":   682610           } }

Report parameters (bind variables):
  P_BOOK_TYPE_CODE  -- e.g. 'US CORP BOOK'

Expected DATA_DS structure:
  <DATA_DS>
    <G_1>
      <TRANSLATION_FIELD>location_id</TRANSLATION_FIELD>
      <CMDB_CODE>LC000030</CMDB_CODE>
      <ORACLE_FA_ID>300000004974106</ORACLE_FA_ID>
    </G_1>
    ...
  </DATA_DS>

Notes:
  LOCATION - The u_cit_location_code stored in ServiceNow maps to
    FA_LOCATIONS via a descriptive flexfield.  ATTRIBUTE1 is the
    most common DFF column for the Citadel CIT location code; change
    if your tenant uses a different attribute (ATTRIBUTE2 … ATTRIBUTE30).

  EXPENSE_CCID - 'cost_center.name' returns '30755 - Network Hardware';
    the pipeline strips the ' - ...' suffix to get '30755' which is the
    cost centre segment value (SEGMENT2 by default in Citadel's COA;
    adjust the segment alias below if your chart differs).
    The query returns the lowest CODE_COMBINATION_ID active in FA
    distributions for the pilot book and that cost centre.  If multiple
    CCIDs share the same cost centre segment, the DBA should confirm
    which account represents the depreciation-expense natural account
    and refine the WHERE clause (e.g. AND gcc.segment3 = '70000').
*/

WITH
book_coa AS (
    SELECT gl.chart_of_accounts_id
    FROM   fa_book_controls fbc
    JOIN   gl_ledgers       gl  ON gl.ledger_id = fbc.set_of_books_id
    WHERE  fbc.book_type_code   = :P_BOOK_TYPE_CODE
      AND  fbc.date_ineffective IS NULL
    FETCH FIRST 1 ROW ONLY
)

-- ── LOCATION: u_cit_location_code → FA location_id ──────────────────────────
SELECT
    'location_id'                AS TRANSLATION_FIELD,
    fl.attribute1                AS CMDB_CODE,       -- e.g. LC000030
    TO_CHAR(fl.location_id)      AS ORACLE_FA_ID
FROM fa_locations fl
WHERE fl.enabled_flag  = 'Y'
  AND fl.attribute1    IS NOT NULL
  AND fl.attribute1    LIKE 'LC%'           -- narrow to CIT location codes

UNION ALL

-- ── EXPENSE CCID: cost-centre segment → code_combination_id ─────────────────
SELECT
    'expense_ccid'                    AS TRANSLATION_FIELD,
    gcc.segment2                      AS CMDB_CODE,   -- cost centre number, e.g. 30755
    TO_CHAR(MIN(dh.code_combination_id)) AS ORACLE_FA_ID
FROM   fa_distribution_history dh
JOIN   gl_code_combinations    gcc ON gcc.code_combination_id   = dh.code_combination_id
JOIN   fa_book_controls        fbc ON fbc.book_type_code        = :P_BOOK_TYPE_CODE
                                   AND fbc.date_ineffective      IS NULL
JOIN   book_coa                bc  ON gcc.chart_of_accounts_id  = bc.chart_of_accounts_id
WHERE  (dh.transaction_header_id_out IS NULL OR dh.date_ineffective IS NULL)
  AND  gcc.enabled_flag   = 'Y'
  AND  gcc.summary_flag   = 'N'
  AND  gcc.segment2       IS NOT NULL       -- cost centre segment must be populated
  -- Uncomment to restrict to the depreciation-expense natural account range:
  -- AND  gcc.segment3    LIKE '6%'
GROUP BY gcc.segment2

ORDER BY TRANSLATION_FIELD, CMDB_CODE
