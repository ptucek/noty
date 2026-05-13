# Deployment do Azure

Aktuální deploy: **Azure Container Apps**, region `swedencentral`, resource group `ptuc-foundry`.

## Architektura deployu

```
GitHub: ptucek/noty.git
        │
        │ az containerapp up --source .
        ▼
┌───────────────────────────────────────┐
│  Azure Container Registry (ACR)       │
│  cloud build z Dockerfile             │
│  → noty-app:latest (~4 GB image)      │
└───────────────────────────────────────┘
        │
        │ pull
        ▼
┌───────────────────────────────────────┐
│  Container Apps Environment           │
│   ├─ scale-to-zero                    │
│   └─ public ingress                   │
│        └─ Container: noty-app         │
│             ├─ port 7860              │
│             ├─ secrets: foundry-key   │
│             └─ env: FOUNDRY_RESOURCE  │
└───────────┬───────────────────────────┘
            │
            ▼
   noty-app.<random>.swedencentral.azurecontainerapps.io
            │
            │  (CNAME)
            ▼
   noty.davidtucek.cz
```

## Předpoklady

```bash
# Azure CLI
brew install azure-cli
az login --tenant 47e2c60d-2942-4060-a899-7e3a71c2a67d
az account set --subscription d96e1291-0b9a-4f15-a392-a63136aabcae

# Docker (jen pro lokální test, ne pro deploy — ACR cloud build to dělá)
brew install docker
```

## První deploy

```bash
cd noty/

az containerapp up \
  --name noty-app \
  --resource-group ptuc-foundry \
  --location swedencentral \
  --source . \
  --ingress external \
  --target-port 7860 \
  --env-vars \
    "GRADIO_SERVER_NAME=0.0.0.0" \
    "GRADIO_SERVER_PORT=7860" \
    "ANTHROPIC_FOUNDRY_RESOURCE=ptuc-foundry-test" \
    "LLM_CLEANUP=0"
```

Trvá **15-30 min** poprvé:
- Source upload (~1 min)
- ACR build z Dockerfile (~10-20 min — 4 GB image s ML deps)
- Container Apps Environment create (~3 min, jen poprvé)
- Deploy + ingress (~2 min)

Po úspěchu: dostaneš public URL `https://noty-app.<random>.swedencentral.azurecontainerapps.io`.

## Doplnit API key pro LLM cleanup

Po základním deployi:

```bash
# Uložit klíč jako Container Apps secret
az containerapp secret set \
  --name noty-app \
  --resource-group ptuc-foundry \
  --secrets foundry-key=<TVŮJ_API_KEY>

# Přiřadit secret jako env var + zapnout LLM cleanup
az containerapp update \
  --name noty-app \
  --resource-group ptuc-foundry \
  --set-env-vars \
    ANTHROPIC_FOUNDRY_API_KEY=secretref:foundry-key \
    LLM_CLEANUP=1
```

## Lepší auth — Managed Identity (production-ready)

Místo API key (který musíš rotovat) přidej **System-Assigned Managed Identity** + roli na Foundry:

```bash
# 1. Zapnout System-Assigned MI
az containerapp identity assign \
  --name noty-app \
  --resource-group ptuc-foundry \
  --system-assigned

# 2. Získat principal ID
PRINCIPAL=$(az containerapp identity show \
  --name noty-app --resource-group ptuc-foundry \
  --query principalId -o tsv)

# 3. Přiřadit roli "Cognitive Services User" na Foundry resource
FOUNDRY_ID=$(az resource show \
  --resource-group ptuc-foundry \
  --name ptuc-foundry-test \
  --resource-type "Microsoft.CognitiveServices/accounts" \
  --query id -o tsv)

az role assignment create \
  --assignee $PRINCIPAL \
  --role "Cognitive Services User" \
  --scope $FOUNDRY_ID

# 4. Odebrat API key, zapnout Entra ID path (jen FOUNDRY_RESOURCE, žádný API_KEY)
az containerapp update \
  --name noty-app \
  --resource-group ptuc-foundry \
  --remove-env-vars ANTHROPIC_FOUNDRY_API_KEY \
  --set-env-vars LLM_CLEANUP=1

# (volitelně smazat secret)
az containerapp secret remove \
  --name noty-app \
  --resource-group ptuc-foundry \
  --secret-names foundry-key
```

`DefaultAzureCredential` v `llm_cleanup.py::_build_client()` automaticky použije MI token,
když `ANTHROPIC_FOUNDRY_RESOURCE` je set ale `_API_KEY` chybí.

## Custom doména (`noty.davidtucek.cz`)

```bash
# 1. Získat default FQDN
FQDN=$(az containerapp show \
  --name noty-app --resource-group ptuc-foundry \
  --query properties.configuration.ingress.fqdn -o tsv)
echo "Default URL: https://$FQDN"

# 2. Získat ASUID pro DNS validaci
ASUID=$(az containerapp show \
  --name noty-app --resource-group ptuc-foundry \
  --query properties.customDomainVerificationId -o tsv)
echo "ASUID pro TXT record: $ASUID"

# 3. Na davidtucek.cz DNS přidej:
#    CNAME: noty.davidtucek.cz   →   $FQDN
#    TXT:   asuid.noty.davidtucek.cz   →   $ASUID
# (počkej 5-15 min na DNS propagaci)

# 4. Přidat custom domain + free managed cert
az containerapp hostname add \
  --hostname noty.davidtucek.cz \
  --name noty-app \
  --resource-group ptuc-foundry

az containerapp hostname bind \
  --hostname noty.davidtucek.cz \
  --name noty-app \
  --resource-group ptuc-foundry \
  --environment <env-name>
```

Po úspěchu: `https://noty.davidtucek.cz` ✓ (Azure free cert, auto-renew).

## Update deployu

```bash
# Re-build + re-deploy z aktuálního adresáře
az containerapp up \
  --name noty-app \
  --resource-group ptuc-foundry \
  --source .
```

Container Apps udělá **rolling update** — žádný downtime.

## Sledování logů + scaling

```bash
# Live logy
az containerapp logs show \
  --name noty-app --resource-group ptuc-foundry --follow

# Scaling: scale-to-zero (default) — pokud nikdo nepřipojuje, container se vypne
# Aktivace: cca 30s cold start při prvním requestu
# Custom scaling rules:
az containerapp update \
  --name noty-app --resource-group ptuc-foundry \
  --min-replicas 0 --max-replicas 3
```

## Cost

| Komponenta | Cena (cca, Sweden Central) |
|---|---|
| Container Apps Consumption plan | $0 v klidu (scale-to-zero), $0.02-0.10/h při běhu |
| ACR Basic SKU | ~$5/měs (storage pro image) |
| Foundry Claude API | per-token, $1-25/1M (modelově) |
| Egress (data out) | první 100 GB/měs free |

Hobby provoz (občasné použití sestrou): **<$20/měs**.

## Troubleshooting

- **Cold start příliš pomalý?** Zvyš `--min-replicas 1` → ~$30/měs ale instant response.
- **MuseScore render selže?** Zkontroluj logy — pravděpodobně chybí `QT_QPA_PLATFORM=offscreen` env.
- **LLM cleanup nefunguje?** Ověř secret + env var: `az containerapp show --query properties.template.containers[0].env`
- **Out of memory?** Zvyš resources: `--cpu 2 --memory 4Gi`
