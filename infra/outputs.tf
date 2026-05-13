output "container_app_fqdn" {
  description = "Default Azure FQDN (použij pro DNS CNAME)"
  value       = azurerm_container_app.main.ingress[0].fqdn
}

output "container_app_url" {
  description = "Plné HTTPS URL"
  value       = "https://${azurerm_container_app.main.ingress[0].fqdn}"
}

output "custom_hostname_verification_id" {
  description = "Hodnota pro asuid.<hostname> TXT record (Container Apps ownership verification)"
  value       = azurerm_container_app.main.custom_domain_verification_id
}

output "acr_login_server" {
  description = "Endpoint pro `docker login` / push"
  value       = azurerm_container_registry.main.login_server
}

output "managed_identity_principal_id" {
  description = "MI principal ID (pro role assignments)"
  value       = azurerm_container_app.main.identity[0].principal_id
}
