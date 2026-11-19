terraform {
  required_version = "~> 1.17.0"
  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = "= 0.6.88"
    }
  }
  backend "s3" {
    key                         = "prod/mk8s/terraform.tfstate"
    use_lockfile                = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
  }
}

provider "nebius" {
  service_account = {
    account_id_env       = "NEBIUS_SA_ID"
    public_key_id_env    = "NEBIUS_AUTHORIZED_KEY_ID"
    private_key_file_env = "NEBIUS_PRIVATE_KEY_FILE"
  }
}
