# Hlavní Terraform definice. Resources jsou ve stejné RG jako Foundry, takže RG nespravujeme.

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

data "azurerm_cognitive_account" "foundry" {
  name                = var.foundry_resource_name
  resource_group_name = var.resource_group_name
}

# -----------------------------------------------------------------------------
# Azure Container Registry — privátní registr pro náš Docker image.
# -----------------------------------------------------------------------------
resource "azurerm_container_registry" "main" {
  name                = "ca9a82ed6179acr" # existující název (importujeme)
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = true # zjednodušuje pull pro Container Apps
}

# -----------------------------------------------------------------------------
# Container Apps Environment — sdílený "host" pro Container Apps v dané RG.
# Drží Log Analytics workspace, networking, scale rules.
# -----------------------------------------------------------------------------
resource "azurerm_container_app_environment" "main" {
  name                = "${var.app_name}-env"
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location

  # Consumption profile (scale-to-zero). Bez log_analytics_workspace_id by se založil default.
}

# -----------------------------------------------------------------------------
# Container App — náš Gradio kontejner.
# -----------------------------------------------------------------------------
resource "azurerm_container_app" "main" {
  name                         = var.app_name
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = data.azurerm_resource_group.main.name
  revision_mode                = "Single"

  identity {
    # System-Assigned Managed Identity — slouží pro Entra ID auth do Foundry
    # (alternativa k API key, pokud var.use_managed_identity_for_foundry == true).
    type = "SystemAssigned"
  }

  # ACR credentials pro pull. Při admin_enabled = true použijeme username/password.
  registry {
    server               = azurerm_container_registry.main.login_server
    username             = azurerm_container_registry.main.admin_username
    password_secret_name = "acr-password"
  }

  secret {
    name  = "acr-password"
    value = azurerm_container_registry.main.admin_password
  }

  # Foundry API key — pouze pokud nepoužíváme Managed Identity.
  dynamic "secret" {
    for_each = var.use_managed_identity_for_foundry ? [] : [1]
    content {
      name  = "foundry-key"
      value = var.foundry_api_key
    }
  }

  template {
    container {
      name   = var.app_name
      image  = "${azurerm_container_registry.main.login_server}/${var.app_name}:${var.image_tag}"
      cpu    = 1.0
      memory = "2Gi"

      env {
        name  = "GRADIO_SERVER_NAME"
        value = "0.0.0.0"
      }
      env {
        name  = "GRADIO_SERVER_PORT"
        value = "7860"
      }
      env {
        name  = "ANTHROPIC_FOUNDRY_RESOURCE"
        value = var.foundry_resource_name
      }
      env {
        name  = "LLM_CLEANUP"
        value = var.llm_cleanup_enabled ? "1" : "0"
      }
      env {
        name  = "CLEANUP_MODEL"
        value = var.cleanup_model
      }

      # API key přes secret reference, jen pokud nepoužíváme MI
      dynamic "env" {
        for_each = var.use_managed_identity_for_foundry ? [] : [1]
        content {
          name        = "ANTHROPIC_FOUNDRY_API_KEY"
          secret_name = "foundry-key"
        }
      }
    }

    min_replicas = 0 # scale-to-zero — šetří peníze
    max_replicas = 3
  }

  ingress {
    external_enabled = true
    target_port      = 7860
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

# -----------------------------------------------------------------------------
# Managed Identity → Foundry role assignment (pokud zvoleno místo API key).
# -----------------------------------------------------------------------------
resource "azurerm_role_assignment" "foundry_mi" {
  count                = var.use_managed_identity_for_foundry ? 1 : 0
  scope                = data.azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_container_app.main.identity[0].principal_id
}

# -----------------------------------------------------------------------------
# Custom hostname (volitelný — vyžaduje CNAME + asuid.TXT v DNS PŘED apply).
# Pokud chybí DNS records, apply selže s "InvalidCustomHostNameValidation".
# -----------------------------------------------------------------------------
resource "azurerm_container_app_custom_domain" "main" {
  count            = var.custom_hostname != "" ? 1 : 0
  name             = var.custom_hostname
  container_app_id = azurerm_container_app.main.id
  # certificate_binding_type a container_app_environment_managed_certificate_id
  # se nastaví po prvním apply, kdy Azure vystaví managed cert.
  lifecycle {
    ignore_changes = [
      certificate_binding_type,
      container_app_environment_certificate_id,
    ]
  }
}
