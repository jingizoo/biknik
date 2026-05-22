WITH p AS (
    SELECT TO_NUMBER(:P_SOURCE_ASSET_ID)                  AS source_asset_id,
           CAST(:P_TARGET_BOOK_TYPE_CODE AS VARCHAR2(30)) AS source_book_type_code
    FROM   dual
),
source_node AS (
    SELECT p.source_asset_id AS asset_id,
           COALESCE(
               p.source_book_type_code,

               -- latest transfer-out book for this source asset
               (SELECT MAX(btd.src_book_type_code)
                          KEEP (DENSE_RANK LAST ORDER BY btd.creation_date)
                FROM   fa_book_transfer_dists btd
                WHERE  btd.src_asset_id = p.source_asset_id),

               -- latest transfer-in book for this source asset
               (SELECT MAX(btd.dest_book_type_code)
                          KEEP (DENSE_RANK LAST ORDER BY btd.creation_date)
                FROM   fa_book_transfer_dists btd
                WHERE  btd.dest_asset_id = p.source_asset_id),

               -- fallback for never-transferred assets
               (SELECT MAX(fb.book_type_code)
                FROM   fa_books fb
                WHERE  fb.asset_id = p.source_asset_id)
           ) AS book_type_code
    FROM   p
),
chain_links AS (
    ----------------------------------------------------------------
    -- Ancestors: walk backwards from the current source node.
    -- Example: US -> UK, when current node is 89472 / UK CORP BOOK.
    ----------------------------------------------------------------
    SELECT btd.src_asset_id                              AS asset_id,
           CAST(btd.src_book_type_code AS VARCHAR2(30))  AS book_type_code,
           CAST(btd.creation_date AS TIMESTAMP)          AS creation_date,
           -LEVEL                                        AS chain_pos,
           -1                                            AS direction
    FROM   fa_book_transfer_dists btd
           CROSS JOIN source_node s
    START WITH btd.dest_asset_id = s.asset_id
           AND btd.dest_book_type_code = s.book_type_code
    CONNECT BY NOCYCLE
           PRIOR btd.src_asset_id = btd.dest_asset_id
       AND PRIOR btd.src_book_type_code = btd.dest_book_type_code

    UNION ALL

    ----------------------------------------------------------------
    -- Descendants: walk forwards from the current source node.
    -- Example: UK -> CH, when current node is 89472 / UK CORP BOOK.
    ----------------------------------------------------------------
    SELECT btd.dest_asset_id                             AS asset_id,
           CAST(btd.dest_book_type_code AS VARCHAR2(30)) AS book_type_code,
           CAST(btd.creation_date AS TIMESTAMP)          AS creation_date,
           LEVEL                                         AS chain_pos,
           1                                             AS direction
    FROM   fa_book_transfer_dists btd
           CROSS JOIN source_node s
    START WITH btd.src_asset_id = s.asset_id
           AND btd.src_book_type_code = s.book_type_code
    CONNECT BY NOCYCLE
           PRIOR btd.dest_asset_id = btd.src_asset_id
       AND PRIOR btd.dest_book_type_code = btd.src_book_type_code

    UNION ALL

    ----------------------------------------------------------------
    -- Current source node itself.
    ----------------------------------------------------------------
    SELECT s.asset_id                              AS asset_id,
           CAST(s.book_type_code AS VARCHAR2(30))  AS book_type_code,
           TIMESTAMP '1900-01-01 00:00:00'         AS creation_date,
           0                                       AS chain_pos,
           0                                       AS direction
    FROM   source_node s
),
dedup AS (
    SELECT asset_id,
           book_type_code,
           creation_date,
           chain_pos,
           direction
    FROM (
        SELECT cl.*,
               ROW_NUMBER() OVER (
                   PARTITION BY cl.asset_id, cl.book_type_code
                   ORDER BY CASE WHEN cl.direction = 0 THEN 0 ELSE 1 END,
                            ABS(cl.chain_pos),
                            cl.creation_date DESC
               ) AS rn
        FROM   chain_links cl
        WHERE  cl.asset_id IS NOT NULL
        AND    cl.book_type_code IS NOT NULL
    )
    WHERE rn = 1
),
ret AS (
    SELECT fr.asset_id,
           fr.book_type_code,
           MAX(fr.date_retired) AS date_retired
    FROM   fa_retirements fr
    WHERE  fr.status = 'PROCESSED'
    GROUP  BY fr.asset_id,
              fr.book_type_code
)
SELECT fa.asset_id      AS asset_id,
       fa.asset_number  AS asset_number,
       d.book_type_code AS book_type_code,

       ROW_NUMBER() OVER (
           ORDER BY d.chain_pos,
                    d.creation_date,
                    fa.asset_id,
                    d.book_type_code
       ) AS chain_order,

       CASE
           WHEN d.direction = 1
            AND (
                    d.asset_id <> s.asset_id
                 OR d.book_type_code <> s.book_type_code
                )
           THEN 'Y'
           ELSE 'N'
       END AS is_destination,

       ret.date_retired AS retirement_date,

       -- Back-compat aliases
       fa.asset_id      AS new_asset_id,
       fa.asset_number  AS new_asset_number,
       d.book_type_code AS target_book_type_code
FROM   dedup d
JOIN   fa_additions_b fa
       ON fa.asset_id = d.asset_id
CROSS JOIN source_node s
LEFT JOIN ret
       ON ret.asset_id = d.asset_id
      AND ret.book_type_code = d.book_type_code
ORDER BY chain_order
