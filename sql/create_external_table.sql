-- BigQuery External Table for FA Asset Transfer results.
--
-- Points directly at the GCS NDJSON files written by gcs_publisher.py.
-- The wildcard picks up all daily folders automatically — no LOAD DATA
-- or scheduled refresh needed.  Tableau / any BQ client always sees the
-- latest data as soon as the job writes to GCS.
--
-- Replace the placeholders before running:
--   ${PROJECT}  – GCP project          (e.g. cig-accounting-dev-1)
--   ${DATASET}  – BQ dataset           (e.g. peoplesoft_archive)
--   ${BUCKET}   – GCS bucket           (e.g. peoplesoft-cold-storage-archieve)
--   ${PREFIX}   – GCS prefix           (e.g. assetxfer/results)

CREATE OR REPLACE EXTERNAL TABLE `${PROJECT}.${DATASET}.v_json_flat_assetxfer`
OPTIONS (
  format       = 'JSON',
  uris         = ['gs://${BUCKET}/${PREFIX}/*/results.ndjson'],
  max_staleness = INTERVAL 5 MINUTE   -- cache results for 5 min to reduce GCS reads
);

-- Verify:
-- SELECT * FROM `${PROJECT}.${DATASET}.v_json_flat_assetxfer`
-- ORDER BY run_ts DESC
-- LIMIT 20;
