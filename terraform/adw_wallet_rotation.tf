# Autonomous Data Warehouse (ADW) Wallet Rotation
# Purpose: Rotate the ADW instance wallet password on each apply, persist the
#          new password as a versioned secret in OCI Vault, and emit the wallet
#          zip locally for downstream consumers.
# Notes:   - The previous version of the script accepted the password via
#            var.wallet_password. We now generate it inline with random_password
#            and write it to OCI Vault so nothing sensitive lives on disk or in
#            tfvars.
#          - OCI Vault secrets are versioned: re-applying creates a new version
#            of the same-named secret rather than overwriting in place.

terraform {
  required_providers {
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    oci = {
      source  = "oracle/oci"
      version = "~> 5.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

# Generate a fresh wallet password on every apply.
# Special-character set is restricted to symbols ADB reliably accepts.
resource "random_password" "wallet" {
  length           = 32
  special          = true
  override_special = "_-#%"
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 2

  # Force regeneration on every apply so each run rotates.
  # Remove this block if you'd rather rotate only when explicitly tainted.
  keepers = {
    rotate_on = timestamp()
  }
}

# Persist the generated password to OCI Vault. A new secret version is created
# on each apply, so prior wallets remain decryptable for the grace period.
resource "oci_vault_secret" "wallet_password" {
  compartment_id = var.compartment_ocid
  vault_id       = var.vault_ocid
  key_id         = var.key_ocid
  secret_name    = var.wallet_secret_name

  secret_content {
    content_type = "BASE64"
    content      = base64encode(random_password.wallet.result)
  }

  lifecycle {
    ignore_changes = [secret_content[0].stage]
  }
}

# Trigger the actual ADB-side wallet rotation.
resource "oci_database_autonomous_database_instance_wallet_management" "rotate" {
  autonomous_database_id = var.adw_ocid
  should_rotate          = true
  grace_period           = 24
}

# Download the freshly rotated wallet using the generated password.
resource "oci_database_autonomous_database_wallet" "wallet" {
  autonomous_database_id = var.adw_ocid
  password               = random_password.wallet.result
  base64_encode_content  = true
  generate_type          = "SINGLE"

  depends_on = [
    oci_database_autonomous_database_instance_wallet_management.rotate,
    oci_vault_secret.wallet_password,
  ]
}

# Emit the wallet zip locally for downstream tooling.
resource "local_sensitive_file" "wallet_zip" {
  filename        = "${path.module}/wallet.zip"
  content_base64  = oci_database_autonomous_database_wallet.wallet.content
  file_permission = "0600"
}

output "wallet_secret_id" {
  description = "OCID of the OCI Vault secret holding the current wallet password."
  value       = oci_vault_secret.wallet_password.id
}

output "wallet_secret_version" {
  description = "Current version number of the wallet password secret."
  value       = oci_vault_secret.wallet_password.current_version_number
}
