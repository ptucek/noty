terraform {
  required_version = ">= 1.5.0" # 1.5+ pro `import` bloky

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }

  # Remote backend — state v Azure Storage Account. Důležité pro CI/CD
  # (lokální state by se ztratil mezi runs GitHub Actions).
  # PŘED prvním `terraform init` musíš storage account vytvořit (viz docs/TERRAFORM.md).
  backend "azurerm" {
    resource_group_name  = "ptuc-foundry"
    storage_account_name = "ptucnotytfstate"  # globally unique, lower-case
    container_name       = "tfstate"
    key                  = "noty.tfstate"
  }
}

provider "azurerm" {
  features {}
  # subscription_id se vezme z env var ARM_SUBSCRIPTION_ID nebo `az account show`
  subscription_id = var.subscription_id
}
