# ExtFile service account IAM bindings for iValua extract buckets.
# Buckets are managed in their own terraform; this file only grants
# the ExtFile service accounts read/write (roles/storage.objectUser)
# so ExtFile can deliver iValua extracts into them.
#
# See: https://docs.citadelgroup.com/extfile/onboarding/connections/s3/#permissions

resource "google_storage_bucket_iam_member" "ivalua_extract_dev_extfile" {
  bucket = "cig-ivalua-extract"
  role   = "roles/storage.objectUser"
  member = "serviceAccount:sd-extfile@citadel-extfile-dev-1.iam.gserviceaccount.com"
}

resource "google_storage_bucket_iam_member" "ivalua_extract_prod_extfile" {
  bucket = "cig-ivalua-extract-prod"
  role   = "roles/storage.objectUser"
  member = "serviceAccount:sp-extfile@citadel-extfile-prod-1.iam.gserviceaccount.com"
}

# invoice_attachments/ path for ExtFile to monitor (PMTSUPPORT-42065).

resource "google_storage_bucket_object" "ivalua_extract_dev_invoice_attachments" {
  name    = "invoice_attachments/"
  content = "invoice_attachments"
  bucket  = "cig-ivalua-extract"
}

resource "google_storage_bucket_object" "ivalua_extract_prod_invoice_attachments" {
  name    = "invoice_attachments/"
  content = "invoice_attachments"
  bucket  = "cig-ivalua-extract-prod"
}
