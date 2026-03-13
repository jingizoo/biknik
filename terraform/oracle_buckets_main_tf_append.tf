# =============================================================================
# Oracle Fusion Reporting Bucket (Dev)
# Purpose: Tech-only bucket for storing intermediary files (errors, audit trails, CSVs)
#          surfaced to business users via Tableau. Organized by Oracle module.
# Retention: 365 days (1 year max per policy)
# =============================================================================

module "orc_fusion_reporting_storage_bucket" {
  source                     = "artifactory.citadelgroup.com/pe-terraform-local__platform-engineering/gcs_storage_bucket/google"
  version                    = "~> 2.3"
  name                       = "cig-orc-fusion-rpt-dev"
  project                    = local.project
  location                   = "NAM4"
  soft_delete_retention_days = 0

  data_type = "app-data"
  ttl       = 365

  versioning = {
    enabled            = true
    max_object_versions = 2
    non_current_ttl     = 7
  }

  extra_labels = {}
}

# Folder structure: organized by Oracle module, each with errors/ and audit_logs/ sub-folders

resource "google_storage_bucket_object" "orc_rpt_asset_mgmt" {
  name    = "asset_management/"
  content = "asset_management"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_asset_mgmt_errors" {
  name    = "asset_management/errors/"
  content = "errors"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_asset_mgmt_audit_logs" {
  name    = "asset_management/audit_logs/"
  content = "audit_logs"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_projects" {
  name    = "projects/"
  content = "projects"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_projects_errors" {
  name    = "projects/errors/"
  content = "errors"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_projects_audit_logs" {
  name    = "projects/audit_logs/"
  content = "audit_logs"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_accounting_hub" {
  name    = "accounting_hub/"
  content = "accounting_hub"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_accounting_hub_errors" {
  name    = "accounting_hub/errors/"
  content = "errors"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_accounting_hub_audit_logs" {
  name    = "accounting_hub/audit_logs/"
  content = "audit_logs"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_payables" {
  name    = "payables/"
  content = "payables"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_payables_errors" {
  name    = "payables/errors/"
  content = "errors"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

resource "google_storage_bucket_object" "orc_rpt_payables_audit_logs" {
  name    = "payables/audit_logs/"
  content = "audit_logs"
  bucket  = "cig-orc-fusion-rpt-dev"
  depends_on = [module.orc_fusion_reporting_storage_bucket]
}

# =============================================================================
# Oracle Staging Bucket (Dev)
# Purpose: Landing/staging zone for data going into Oracle.
#          Temporary storage organized by source system (FITS, accruals, etc.)
# Retention: 90 days (temporary data, shorter is better per policy)
# =============================================================================

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

resource "google_storage_bucket_object" "orc_staging_fits" {
  name    = "fits/"
  content = "fits"
  bucket  = "cig-orc-staging-dev"
  depends_on = [module.orc_staging_storage_bucket]
}

resource "google_storage_bucket_object" "orc_staging_accruals" {
  name    = "accruals/"
  content = "accruals"
  bucket  = "cig-orc-staging-dev"
  depends_on = [module.orc_staging_storage_bucket]
}

# =============================================================================
# IAM - Oracle Fusion Reporting & Staging (bare minimum, no k8s)
# =============================================================================

# Service account for Oracle bucket operations
resource "google_service_account" "sd-orc-fusion" {
  account_id   = "sd-orc-fusion"
  description  = "Used for Oracle Fusion reporting and staging bucket access in dev."
  disabled     = false
  display_name = "sd-orc-fusion"
  project      = data.google_project.project.project_id
}

# Project-level objectUser role (read/write objects)
resource "google_project_iam_member" "storage_user_role_orc_fusion" {
  project = data.google_project.project.project_id
  role    = "roles/storage.objectUser"
  member  = "serviceAccount:${google_service_account.sd-orc-fusion.email}"
}

# Project-level viewer roles (list buckets and view objects)
resource "google_project_iam_member" "storage_viewer_permissions_orc_fusion" {
  for_each = toset([
    "roles/storage.objectViewer",
    "roles/storage.bucketViewer"
  ])
  role    = each.key
  project = data.google_project.project.project_id
  member  = "serviceAccount:${google_service_account.sd-orc-fusion.email}"
}
