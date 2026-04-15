# Oracle Fusion Reporting Bucket (Dev)
# Purpose: Tech-only bucket for storing intermediary files (errors, audit trails, CSVs)
#          surfaced to business users via Tableau. Organized by Oracle module.
# Retention: 365 days (1 year max per policy)

locals {
  project     = "cig-acctgateway-dev-1"
  vpc_project = "citadel-pte-vpc"
}

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

# Asset Management module folders
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

# Projects module folders
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

# Accounting Hub module folders
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

# Payables module folders
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
