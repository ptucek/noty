# Terraform IaC

Infra (Azure Container Apps + ACR + secrets + custom domain) jako kód
v `infra/`. Cíl: každá změna deploymentu jde přes git → PR → review → apply.

## Soubory

```
infra/
├── providers.tf            # azurerm provider + remote backend (Azure Storage)
├── variables.tf            # vstupní proměnné
├── main.tf                 # všechny resources (ACR, CA Env, Container App, hostname)
├── outputs.tf              # výstupy (URL, ASUID, ACR endpoint)
├── imports.tf              # import bloky pro existující resources (TF 1.5+)
└── terraform.tfvars.example  # vzor; kopíruj na terraform.tfvars (gitignored)
```

## První setup

### 1. Vytvoř Storage Account pro Terraform state

Stát nesmí být lokálně (lokální state se ztratí v CI nebo když přepneš stroj).
Použijeme Azure Storage Account.

```bash
az storage account create \
  --name ptucnotytfstate \
  --resource-group ptuc-foundry \
  --location swedencentral \
  --sku Standard_LRS \
  --encryption-services blob

az storage container create \
  --name tfstate \
  --account-name ptucnotytfstate \
  --auth-mode login
```

(Jméno `ptucnotytfstate` musí být **globálně unikátní** — pokud je zabrané, zvol jiné
a uprav `backend "azurerm"` v `infra/providers.tf`.)

### 2. Lokální Terraform CLI

```bash
brew install terraform        # nebo tfenv pro multi-version
cd infra/
terraform init                # stáhne providers + napojí backend
```

### 3. Připrav variables

```bash
cp terraform.tfvars.example terraform.tfvars
# uprav podle potřeby. NEpiš tam foundry_api_key, použij env:
export TF_VAR_foundry_api_key="<klíč z Foundry portálu>"
```

### 4. Plan + Apply

```bash
# 1. Plan ukáže, co Terraform udělá. Při prvním runu importuje existující
#    resources do state (díky imports.tf), takže výsledek by měl být "no changes".
terraform plan

# 2. Apply provede změny (NIC při prvním runu kromě importu).
terraform apply
```

Pokud apply funguje s "no changes", import proběhl. Existující resources jsou teď
spravované přes Terraform. **Smaž `infra/imports.tf`** (už nepotřebuješ) a commit.

### 5. Změny

Každá změna teď jde přes Terraform:
- Vyměnit image tag → `terraform apply -var=image_tag=v2`
- Přepnout na Managed Identity → `terraform apply -var=use_managed_identity_for_foundry=true`
- Přidat custom doménu → uprav `custom_hostname`, `terraform apply`

## GitHub Actions integrace

`.github/workflows/deploy.yml`:
- **Push do main** → build image, push do ACR (s tag = git SHA), `terraform apply`
- **PR proti main** → `terraform plan` + komentář na PR
- Auth do Azure: **OIDC federated identity** (žádné secrety v GitHubu)

### OIDC federated identity setup

Místo Service Principal credentials (které musíš rotovat) použijeme OIDC.
GitHub Actions runner si vyzvedne short-lived token z GitHubu a Azure ho přijme
jako důkaz identity.

```bash
# 1. Vytvoř App registration v Entra ID
APP_NAME="noty-gh-actions"
APP_ID=$(az ad app create --display-name $APP_NAME --query appId -o tsv)
echo "Client ID: $APP_ID"

# 2. Vytvoř Service Principal pro tu app
az ad sp create --id $APP_ID
SP_ID=$(az ad sp show --id $APP_ID --query id -o tsv)

# 3. Přiřaď roli "Contributor" na RG (nebo užší scope)
RG_ID=$(az group show --name ptuc-foundry --query id -o tsv)
az role assignment create \
  --assignee-object-id $SP_ID \
  --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope $RG_ID

# 4. Přidej Federated credential pro GitHub
az ad app federated-credential create \
  --id $APP_ID \
  --parameters '{
    "name": "github-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:ptucek/noty:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# 5. Pro PR runs (jiný subject)
az ad app federated-credential create \
  --id $APP_ID \
  --parameters '{
    "name": "github-pr",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:ptucek/noty:pull_request",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# 6. Přiřaď roli "Storage Blob Data Contributor" pro Terraform state
SA_ID=$(az storage account show --name ptucnotytfstate --resource-group ptuc-foundry --query id -o tsv)
az role assignment create \
  --assignee-object-id $SP_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope $SA_ID
```

V GitHub repo → Settings → Secrets and variables → Actions:
- `AZURE_CLIENT_ID` = $APP_ID (z kroku 1)
- `FOUNDRY_API_KEY` = klíč z Foundry portálu (pro `TF_VAR_foundry_api_key`)

`tenant-id` a `subscription-id` jsou public, dej je do workflow env.

## Tipy

### Co spravuje Terraform vs co ručně

| Resource | TF | Ručně |
|---|---|---|
| ACR | ✓ | — |
| Container Apps Environment | ✓ | — |
| Container App + secrets + env vars | ✓ | — |
| Custom hostname binding | ✓ | DNS records (CNAME + TXT) |
| Foundry resource (Cognitive Services) | ✗ (data source) | Ano (vznik manuálně) |
| Resource Group | ✗ (data source) | Existující, sdílená |
| Managed cert | ✗ (po-apply binding) | Azure automaticky vystaví |
| DNS records v davidtucek.cz | ✗ (WEDOS) | Ručně ve WEDOS panelu |

### Debug

```bash
terraform plan -refresh=true               # přečte aktuální stav z Azure
terraform state list                        # všechny spravované resources
terraform state show azurerm_container_app.main   # detail jednoho
terraform import <addr> <azure-id>         # ruční import (alt. k imports.tf)
terraform destroy                          # SMAŽE všechno spravované (pozor)
```

### Bezpečnost

- `terraform.tfvars` patří do `.gitignore` (obsahuje `foundry_api_key`)
- `terraform.tfstate` je v Azure Storage; access přes RBAC, ne přes credentials
- GitHub Actions secrets jsou encrypted at rest
- OIDC tokens jsou jen na 1h, žádný long-lived secret v repu

## Co se mohlo udělat jinak

- **Bicep místo TF**: Azure-native IaC, jednodušší pro pure-Azure stack. TF je multi-cloud.
- **Pulumi**: IaC v Pythonu/TypeScriptu. Lepší DX pokud máš velký kód.
- **`az containerapp up` skript v bashi**: nejjednodušší, ale nemá state — nevíš, co kde běží.
- **Helm + AKS**: kompletní Kubernetes setup. Mnohem víc complexity.

Pro hobby projekt s lehkým Azure je Terraform sweet spot.
