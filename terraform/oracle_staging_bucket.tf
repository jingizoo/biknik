# Oracle Staging Bucket (Dev)
# Purpose: Landing/staging zone for data going into Oracle.
#          Temporary storage organized by source system (FITS, accruals, etc.)
#          Similar pattern to how PeopleSoft staging was organized.
# Retention: 90 days (temporary data, shorter is better per policy)

module "orc_staging_storage_bucket" {
  source                     = "artifactory.citadelgroup.com/pe-terraform-local__platform-engineering/gcs_storage_bucket/google"
  version                    = "~> 2.3"
  name                       = "cig-orc-staging-dev"
  project                    = local.project
  location                   = "NAM4"
  soft_delete_retention_days = 0

  data_type = "temporary-files"
  ttl       = 90

  versioning = {
    enabled            = true
    max_object_versions = 2
    non_current_ttl     = 7
  }

  extra_labels = {}
}

# Folder structure: one folder per source system

# FITS (expense sub-ledger) staging folder
resource "google_storage_bucket_object" "orc_staging_fits" {
  name    = "fits/"
  content = "fits"
  bucket  = "cig-orc-staging-dev"

  depends_on = [module.orc_staging_storage_bucket]
}

# Accruals staging folder (for Steve's accounting hub process)
resource "google_storage_bucket_object" "orc_staging_accruals" {
  name    = "accruals/"
  content = "accruals"
  bucket  = "cig-orc-staging-dev"

  depends_on = [module.orc_staging_storage_bucket]
}
