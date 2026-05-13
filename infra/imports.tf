# Deklarativní `import` bloky (Terraform 1.5+). Při `terraform plan` se TF pokusí
# importovat existující resources do state. Po prvním úspěšném `terraform apply`
# (kdy je import dokončen) tyto bloky můžeme odstranit.
#
# Pokud `terraform apply` selže s "already exists", buď je import úspěšný (state má
# resource) nebo neúspěšný (lze odstranit s `terraform state rm <addr>` a zkusit znovu).

import {
  to = azurerm_container_registry.main
  id = "/subscriptions/d96e1291-0b9a-4f15-a392-a63136aabcae/resourceGroups/ptuc-foundry/providers/Microsoft.ContainerRegistry/registries/ca9a82ed6179acr"
}

import {
  to = azurerm_container_app_environment.main
  id = "/subscriptions/d96e1291-0b9a-4f15-a392-a63136aabcae/resourceGroups/ptuc-foundry/providers/Microsoft.App/managedEnvironments/noty-app-env"
}

import {
  to = azurerm_container_app.main
  id = "/subscriptions/d96e1291-0b9a-4f15-a392-a63136aabcae/resourceGroups/ptuc-foundry/providers/Microsoft.App/containerApps/noty-app"
}
