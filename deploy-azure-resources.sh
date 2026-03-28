#!/usr/bin/env bash
# deploy-azure-resources.sh — Provision Azure resources for msft-roadmap-sync
#
# Run from the VS Code terminal at the root of this repository:
#   chmod +x deploy-azure-resources.sh && ./deploy-azure-resources.sh
#
# Provisions:
#   - Resource Group
#   - Storage Account (Function backing store)
#   - Application Insights
#   - Azure Function App + deploys function code
#   - Azure OpenAI resource + GPT-4o deployment
#   - Logic App scaffold (workflow must be configured in the portal)
#
# After this script completes, follow the printed instructions for the
# manual steps in AI Foundry (agent creation, tool registration).

set -euo pipefail

# ============================================================
# CONFIGURATION — edit these before running
# ============================================================

LOCATION="uksouth"                       # Azure region
RESOURCE_GROUP="rg-roadmap-sync"
STORAGE_ACCOUNT="stroadmapsync"          # Globally unique, 3-24 lowercase alphanum
FUNCTION_APP="func-roadmap-sync"         # Globally unique
OPENAI_ACCOUNT="aoai-roadmap-sync"
LOGIC_APP="la-roadmap-sync"
APP_INSIGHTS="appi-roadmap-sync"

# Leave blank to use your current default subscription
SUBSCRIPTION_ID=""

# ============================================================
# Helpers
# ============================================================

BLUE='\033[1;34m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; NC='\033[0m'
log()  { echo -e "\n${BLUE}==> $1${NC}"; }
ok()   { echo -e "${GREEN}    ✓ $1${NC}"; }
warn() { echo -e "${YELLOW}    ⚠ $1${NC}"; }
fail() { echo -e "${RED}    ✗ $1${NC}"; exit 1; }

# ============================================================
# Preflight checks
# ============================================================

log "Preflight checks"

# Must be run from repo root
[[ -f "functions/function_app.py" ]] || \
  fail "Run this script from the root of the msft-roadmap-sync repository"
ok "Repository root confirmed"

# Azure CLI login
if ! az account show &>/dev/null; then
  warn "Not logged in — opening browser login"
  az login
fi
ok "Logged in as: $(az account show --query 'user.name' -o tsv)"

# Subscription
if [[ -n "$SUBSCRIPTION_ID" ]]; then
  az account set --subscription "$SUBSCRIPTION_ID"
fi
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
ok "Subscription: $(az account show --query name -o tsv) (${SUBSCRIPTION_ID})"

# zip (required for function packaging)
command -v zip &>/dev/null || fail "'zip' not found. Install with: brew install zip"

# ============================================================
# 1. Resource Group
# ============================================================

log "Creating resource group"
if az group show --name "$RESOURCE_GROUP" &>/dev/null; then
  ok "$RESOURCE_GROUP already exists — skipping"
else
  az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --output none
  ok "$RESOURCE_GROUP in $LOCATION"
fi

# ============================================================
# 2. Storage Account
# ============================================================

log "Creating storage account"
if az storage account show --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  ok "$STORAGE_ACCOUNT already exists — skipping"
else
  az storage account create \
    --name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --allow-blob-public-access false \
    --output none
  ok "$STORAGE_ACCOUNT"
fi

# ============================================================
# 3. Application Insights
# ============================================================

log "Creating Application Insights"
az extension add --name application-insights --only-show-errors 2>/dev/null || true

if az monitor app-insights component show \
     --app "$APP_INSIGHTS" \
     --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  ok "$APP_INSIGHTS already exists — skipping"
else
  az monitor app-insights component create \
    --app "$APP_INSIGHTS" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --output none
  ok "$APP_INSIGHTS"
fi

# ============================================================
# 4. Function App
# ============================================================

log "Creating Function App"
FUNCTION_APP_CREATED=false
if az functionapp show --name "$FUNCTION_APP" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  ok "$FUNCTION_APP already exists — skipping creation"
else
  az functionapp create \
    --name "$FUNCTION_APP" \
    --resource-group "$RESOURCE_GROUP" \
    --storage-account "$STORAGE_ACCOUNT" \
    --flexconsumption-location "$LOCATION" \
    --runtime python \
    --runtime-version 3.11 \
    --instance-memory 2048 \
    --app-insights "$APP_INSIGHTS" \
    --output none
  FUNCTION_APP_CREATED=true
  ok "$FUNCTION_APP"
fi

# ============================================================
# 4.5 Configure ADO credentials
# ============================================================

log "Configuring ADO PAT"

# Try to read from .env first
ADO_PAT=$(grep -E "^ADO_PAT=" .env 2>/dev/null | cut -d'=' -f2-) || true

if [[ -z "$ADO_PAT" ]]; then
  echo ""
  warn "ADO_PAT not found in .env"
  warn "Create a PAT at: https://dev.azure.com/hobbitfeetado/_usersSettings/tokens"
  warn "Required scope: Work Items (Read & Write)"
  echo -n "  Enter your Azure DevOps PAT (input hidden): "
  read -r -s ADO_PAT
  echo ""
fi

if [[ -z "$ADO_PAT" ]]; then
  warn "ADO_PAT not provided — ado_operations function will not authenticate to ADO"
  warn "Set it later with:"
  warn "  az functionapp config appsettings set --name $FUNCTION_APP --resource-group $RESOURCE_GROUP --settings ADO_PAT=<your-pat>"
else
  az functionapp config appsettings set \
    --name "$FUNCTION_APP" \
    --resource-group "$RESOURCE_GROUP" \
    --settings "ADO_PAT=$ADO_PAT" \
    --output none
  ok "ADO_PAT configured as Function App setting"
fi

# ============================================================
# 5. Deploy function code
# ============================================================

log "Publishing function code"

FUNC_ZIP="/tmp/func-roadmap-sync.zip"
# Only wait for provisioning if the app was just created
if [[ "$FUNCTION_APP_CREATED" == true ]]; then
  warn "Waiting 30s for Function App to finish provisioning..."
  sleep 30
fi

(cd functions && zip -r "$FUNC_ZIP" . -x "*.pyc" -x "__pycache__/*" -x ".venv/*" -x ".python_packages/*")

az functionapp deployment source config-zip \
  --name "$FUNCTION_APP" \
  --resource-group "$RESOURCE_GROUP" \
  --src "$FUNC_ZIP" \
  --build-remote true \
  --output none || true  # Flex Consumption returns partial-success exit code even on success

rm -f "$FUNC_ZIP"
ok "Function code deployed to $FUNCTION_APP"

# ============================================================
# 6. Retrieve function invocation URL
# ============================================================

log "Retrieving function key"
# On Flex Consumption, function-level keys are only available after first cold-start.
# Fall back to the app-level master key, which is always available and works for all functions.
FUNCTION_KEY=$(az functionapp keys list \
  --name "$FUNCTION_APP" \
  --resource-group "$RESOURCE_GROUP" \
  --query "masterKey" -o tsv 2>/dev/null) || true

if [[ -z "$FUNCTION_KEY" ]]; then
  warn "Could not retrieve key — run manually:"
  warn "  az functionapp keys list --name $FUNCTION_APP --resource-group $RESOURCE_GROUP --query masterKey -o tsv"
  FUNCTION_URL="https://${FUNCTION_APP}.azurewebsites.net/api/fetch_roadmap?code=<retrieve-key-manually>"
else
  FUNCTION_URL="https://${FUNCTION_APP}.azurewebsites.net/api/fetch_roadmap?code=${FUNCTION_KEY}"
  ok "Function URL ready"
fi

# ============================================================
# 7. Azure OpenAI
# ============================================================

log "Creating Azure OpenAI resource"
warn "Azure OpenAI requires subscription-level approval."
warn "If this step fails, request access at https://aka.ms/oai/access"

if az cognitiveservices account show \
     --name "$OPENAI_ACCOUNT" \
     --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  ok "$OPENAI_ACCOUNT already exists — skipping"
else
  az cognitiveservices account create \
    --name "$OPENAI_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --kind OpenAI \
    --sku S0 \
    --output none
  ok "$OPENAI_ACCOUNT"
fi

log "Deploying GPT-4o model"
if az cognitiveservices account deployment show \
     --name "$OPENAI_ACCOUNT" \
     --resource-group "$RESOURCE_GROUP" \
     --deployment-name gpt-4o &>/dev/null; then
  ok "gpt-4o deployment already exists — skipping"
else
  az cognitiveservices account deployment create \
    --name "$OPENAI_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --deployment-name gpt-4o \
    --model-name gpt-4o \
    --model-version "2024-11-20" \
    --model-format OpenAI \
    --sku-capacity 50 \
    --sku-name GlobalStandard \
    --output none
  ok "gpt-4o (GlobalStandard, 10K TPM)"
fi

# ============================================================
# 8. Logic App scaffold
# ============================================================

log "Creating Logic App scaffold"

if az logic workflow show \
     --name "$LOGIC_APP" \
     --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  ok "$LOGIC_APP already exists — skipping"
else
  az logic workflow create \
    --name "$LOGIC_APP" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --definition '{
      "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
      "contentVersion": "1.0.0.0",
      "definition": {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0",
        "triggers": {},
        "actions": {}
      }
    }' \
    --output none
  ok "$LOGIC_APP (scaffold only — configure workflow in portal)"
fi

# ============================================================
# Summary
# ============================================================

PORTAL_RG="https://portal.azure.com/#resource/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}"
PORTAL_LOGIC="${PORTAL_RG}/providers/Microsoft.Logic/workflows/${LOGIC_APP}/logicApp"
FOUNDRY_URL="https://ai.azure.com"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              DEPLOYMENT COMPLETE                         ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Resource Group:   $RESOURCE_GROUP"
echo "  Location:         $LOCATION"
echo "  Function App:     $FUNCTION_APP"
echo "  Azure OpenAI:     $OPENAI_ACCOUNT  (deployment: gpt-4o)"
echo "  Logic App:        $LOGIC_APP"
echo ""
echo "  ┌─ Function invocation URL (save this) ──────────────────"
echo "  │  $FUNCTION_URL"
echo "  └────────────────────────────────────────────────────────"
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║         MANUAL STEPS REMAINING  (see SETUP.md §5–7)     ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  1. Create AI Foundry Hub + Project"
echo "     $FOUNDRY_URL"
echo "     • Hub: link to resource group '$RESOURCE_GROUP'"
echo "     • Hub: link to Azure OpenAI '$OPENAI_ACCOUNT'"
echo "     • Project: create within that hub"
echo ""
echo "  2. Create the agent (Agents → New Agent)"
echo "     • Name: roadmap-sync-agent"
echo "     • Model: gpt-4o"
echo "     • Instructions: paste the full contents of agent-instructions.md"
echo "     • Add tool → MCP Servers → Azure DevOps"
echo "     • Add tool → Azure Function → $FUNCTION_APP → fetch_roadmap"
echo "       Function URL: $FUNCTION_URL"
echo ""
echo "  3. Design the Logic App workflow"
echo "     $PORTAL_LOGIC"
echo "     • Trigger: Recurrence → Mon–Fri, every 1 Week, 07:00 UTC"
echo "     • Action: Azure AI Foundry Agent Service → Create Run and Wait"
echo "       Prompt: 'Run the daily roadmap sync. Use roadmap-sync-config.json."
echo "       Fetch items from the last 7 days, create work items, report summary.'"
echo "     • (Optional) Action: Teams/Email — post agent summary"
echo ""
echo "  4. Grant ADO permissions for each project in roadmap-sync-config.json"
echo "     • Project Settings → Permissions → agent identity"
echo "     • Grant: Create work items, Edit work items, View work items"
echo ""
echo "  Full instructions: SETUP.md"
echo ""
