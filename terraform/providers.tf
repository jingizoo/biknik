# Provider configuration shared by Terraform resources in this directory.
# - google: existing GCS bucket modules (oracle_*_bucket.tf).
# - oci:    ADW wallet rotation + OCI Vault secret storage.
#
# OCI authentication: defaults to "InstancePrincipal" so this can run from a
# GKE workload identity / OCI compute instance / pipeline runner without
# checked-in keys. Override auth = "ApiKey" + private_key_path locally if
# needed.

provider "google" {
  project = local.project
}

provider "oci" {
  auth   = var.oci_auth
  region = var.oci_region

  # Only consulted when auth = "ApiKey". Safe to leave unset for
  # InstancePrincipal / ResourcePrincipal auth.
  tenancy_ocid     = var.oci_tenancy_ocid
  user_ocid        = var.oci_user_ocid
  fingerprint      = var.oci_fingerprint
  private_key_path = var.oci_private_key_path
}

variable "oci_auth" {
  description = "OCI provider auth method. One of: InstancePrincipal, ResourcePrincipal, SecurityToken, ApiKey."
  type        = string
  default     = "InstancePrincipal"
}

variable "oci_region" {
  description = "OCI region the ADW and Vault live in (e.g. us-ashburn-1)."
  type        = string
}

variable "oci_tenancy_ocid" {
  description = "Tenancy OCID. Required only when oci_auth = \"ApiKey\"."
  type        = string
  default     = ""
}

variable "oci_user_ocid" {
  description = "User OCID. Required only when oci_auth = \"ApiKey\"."
  type        = string
  default     = ""
}

variable "oci_fingerprint" {
  description = "API key fingerprint. Required only when oci_auth = \"ApiKey\"."
  type        = string
  default     = ""
}

variable "oci_private_key_path" {
  description = "Path to the API private key PEM. Required only when oci_auth = \"ApiKey\"."
  type        = string
  default     = ""
}
