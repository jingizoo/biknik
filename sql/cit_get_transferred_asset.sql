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
edges AS (
    ----------------------------------------------------------------
    -- Edge A: physical book transfers.
    ----------------------------------------------------------------
    SELECT btd.src_asset_id  AS parent_asset_id,
           btd.dest_asset_id AS child_asset_id
    FROM   fa_book_transfer_dists btd
    WHERE  btd.src_asset_id  IS NOT NULL
    AND    btd.dest_asset_id IS NOT NULL
    AND    btd.src_asset_id <> btd.dest_asset_id

    UNION

    ----------------------------------------------------------------
    -- Edge B: DFF link - child.ATTRIBUTE3 holds the source asset_number.
    ----------------------------------------------------------------
    SELECT parent.asset_id AS parent_asset_id,
           child.asset_id  AS child_asset_id
    FROM   fa_additions_b child
    JOIN   fa_additions_b parent
           ON TRIM(parent.asset_number) = TRIM(child.attribute3)
    WHERE  child.attribute3 IS NOT NULL
    AND    parent.asset_id <> child.asset_id
),
chain_links AS (
    ----------------------------------------------------------------
    -- Ancestors: walk backwards from the current source node.
    ----------------------------------------------------------------
    SELECT e.parent_asset_id AS asset_id,
           -LEVEL            AS chain_pos,
           -1                AS direction
    FROM   edges e
    START WITH e.child_asset_id = (SELECT asset_id FROM source_node)
    CONNECT BY NOCYCLE PRIOR e.parent_asset_id = e.child_asset_id

    UNION ALL

    ----------------------------------------------------------------
    -- Descendants: walk forwards from the current source node.
    ----------------------------------------------------------------
    SELECT e.child_asset_id AS asset_id,
           LEVEL            AS chain_pos,
           1                AS direction
    FROM   edges e
    START WITH e.parent_asset_id = (SELECT asset_id FROM source_node)
    CONNECT BY NOCYCLE PRIOR e.child_asset_id = e.parent_asset_id

    UNION ALL

    ----------------------------------------------------------------
    -- Current source node itself.
    ----------------------------------------------------------------
    SELECT sn.asset_id AS asset_id,
           0           AS chain_pos,
           0           AS direction
    FROM   source_node sn
),
dedup AS (
    SELECT asset_id,
           chain_pos,
           direction
    FROM (
        SELECT cl.asset_id,
               cl.chain_pos,
               cl.direction,
               ROW_NUMBER() OVER (
                   PARTITION BY cl.asset_id
                   ORDER BY CASE WHEN cl.direction = 0 THEN 0 ELSE 1 END,
                            ABS(cl.chain_pos)
               ) AS rn
        FROM   chain_links cl
        WHERE  cl.asset_id IS NOT NULL
    )
    WHERE rn = 1
),
asset_book AS (
    SELECT d.asset_id,
           COALESCE(
               -- seed keeps the book resolved by source_node (honors the param)
               CASE WHEN d.asset_id = s.asset_id THEN s.book_type_code END,

               -- latest transfer-out book for this asset
               (SELECT MAX(btd.src_book_type_code)
                          KEEP (DENSE_RANK LAST ORDER BY btd.creation_date)
                FROM   fa_book_transfer_dists btd
                WHERE  btd.src_asset_id = d.asset_id),

               -- latest transfer-in book for this asset
               (SELECT MAX(btd.dest_book_type_code)
                          KEEP (DENSE_RANK LAST ORDER BY btd.creation_date)
                FROM   fa_book_transfer_dists btd
                WHERE  btd.dest_asset_id = d.asset_id),

               -- fallback for never-transferred assets
               (SELECT MAX(fb.book_type_code)
                FROM   fa_books fb
                WHERE  fb.asset_id = d.asset_id)
           ) AS book_type_code
    FROM   dedup d
           CROSS JOIN source_node s
),
ret AS (
    SELECT fr.asset_id,
           MAX(fr.date_retired) AS date_retired
    FROM   fa_retirements fr
    WHERE  fr.status = 'PROCESSED'
    GROUP  BY fr.asset_id
)
SELECT fa.asset_id        AS asset_id,
       fa.asset_number    AS asset_number,
       bk.book_type_code  AS book_type_code,

       ROW_NUMBER() OVER (
           ORDER BY d.chain_pos,
                    fa.creation_date,
                    fa.asset_id
       ) AS chain_order,

       CASE
           WHEN d.direction = 1
            AND d.asset_id <> s.asset_id
           THEN 'Y'
           ELSE 'N'
       END AS is_destination,

       ret.date_retired AS retirement_date,

       -- Back-compat aliases
       fa.asset_id       AS new_asset_id,
       fa.asset_number   AS new_asset_number,
       bk.book_type_code AS target_book_type_code
FROM   dedup d
JOIN   fa_additions_b fa  ON fa.asset_id = d.asset_id
JOIN   asset_book bk      ON bk.asset_id = d.asset_id
CROSS JOIN source_node s
LEFT JOIN ret             ON ret.asset_id = d.asset_id
ORDER BY chain_order
