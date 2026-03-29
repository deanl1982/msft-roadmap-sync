#!/usr/bin/env bash
# configure-logic-app.sh — Configure the Logic App workflow for the roadmap sync scheduler
#
# Run from the VS Code terminal at the root of this repository:
#   chmod +x configure-logic-app.sh && ./configure-logic-app.sh
#
# Prerequisites:
#   - deploy-azure-resources.sh must have been run first (creates la-roadmap-sync)
#   - create-foundry-agent.py must have been run (provides the Agent ID)
#   - .env must contain AZURE_FOUNDRY_ENDPOINT and AZURE_AGENT_ID
#
# What this script does:
#   1. Reads the Agent ID and Foundry endpoint from .env
#   2. Enables system-assigned Managed Identity on the Logic App
#   3. Grants the Logic App identity the AI Developer role on Azure OpenAI
#   4. Deploys the full workflow definition (recurrence trigger → agent run → poll loop)

set -euo pipefail

# ============================================================
# CONFIGURATION — must match deploy-azure-resources.sh
# ============================================================

RESOURCE_GROUP="rg-roadmap-sync"
LOGIC_APP="la-roadmap-sync"
OPENAI_ACCOUNT="aoai-roadmap-sync"
FOUNDRY_HUB="hub-roadmap-sync"   # AI Foundry Hub name (ML workspace)
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

[[ -f ".env" ]] || fail ".env file not found. Copy .env.example and fill in your values."

# Load .env
FOUNDRY_ENDPOINT=$(grep -E "^AZURE_FOUNDRY_ENDPOINT=" .env 2>/dev/null | cut -d'=' -f2-) || true
AGENT_ID=$(grep -E "^AZURE_AGENT_ID=" .env 2>/dev/null | cut -d'=' -f2-) || true
FOUNDRY_API_KEY=$(grep -E "^AZURE_FOUNDRY_API_KEY=" .env 2>/dev/null | cut -d'=' -f2-) || true

if [[ -z "$FOUNDRY_ENDPOINT" ]]; then
  fail "AZURE_FOUNDRY_ENDPOINT not found in .env. Add it and re-run."
fi

if [[ -z "$FOUNDRY_API_KEY" ]]; then
  echo ""
  warn "AZURE_FOUNDRY_API_KEY not found in .env"
  warn "Find it at: ai.azure.com → your project → Settings → API keys"
  echo -n "  Enter Foundry API key (input hidden): "
  read -r -s FOUNDRY_API_KEY
  echo ""
  if [[ -z "$FOUNDRY_API_KEY" ]]; then
    fail "Foundry API key is required."
  fi
fi

if [[ -z "$AGENT_ID" ]]; then
  echo ""
  warn "AZURE_AGENT_ID not found in .env"
  warn "Find it by running: python3 create-foundry-agent.py"
  warn "It is printed as 'Agent ID for Logic Apps: asst_...'"
  echo -n "  Enter Agent ID: "
  read -r AGENT_ID
  if [[ -z "$AGENT_ID" ]]; then
    fail "Agent ID is required."
  fi
fi

ok "AZURE_FOUNDRY_ENDPOINT: $FOUNDRY_ENDPOINT"
ok "Agent ID: $AGENT_ID"

if ! az account show &>/dev/null; then
  warn "Not logged in — opening browser login"
  az login
fi
ok "Logged in as: $(az account show --query 'user.name' -o tsv)"

if [[ -n "$SUBSCRIPTION_ID" ]]; then
  az account set --subscription "$SUBSCRIPTION_ID"
fi
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
ok "Subscription: $(az account show --query name -o tsv) (${SUBSCRIPTION_ID})"

# ============================================================
# 1. Enable Managed Identity on the Logic App
# ============================================================

log "Enabling system-assigned Managed Identity on Logic App"

az rest \
  --method PATCH \
  --url "https://management.azure.com/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/${LOGIC_APP}?api-version=2016-06-01" \
  --body '{"identity": {"type": "SystemAssigned"}}' \
  --output none

LOGIC_APP_PRINCIPAL=$(az resource show \
  --resource-group "$RESOURCE_GROUP" \
  --resource-type "Microsoft.Logic/workflows" \
  --name "$LOGIC_APP" \
  --query "identity.principalId" -o tsv)

ok "Managed Identity enabled — principal: $LOGIC_APP_PRINCIPAL"

# ============================================================
# 2. Grant roles to Logic App Managed Identity
# ============================================================

log "Granting roles to Logic App Managed Identity"

# The Managed Identity service principal takes a moment to propagate in Entra ID.
# Retry role assignments up to 10 times with a 10s wait between attempts.
_assign_role() {
  local ROLE="$1"
  local SCOPE="$2"
  local LABEL="$3"

  EXISTING=$(az role assignment list \
    --assignee "$LOGIC_APP_PRINCIPAL" \
    --role "$ROLE" \
    --scope "$SCOPE" \
    --query "[0].id" -o tsv 2>/dev/null) || true

  if [[ -n "$EXISTING" ]]; then
    ok "$LABEL already assigned — skipping"
    return
  fi

  warn "Waiting for Managed Identity to propagate in Entra ID..."
  RETRIES=10
  for i in $(seq 1 $RETRIES); do
    if az role assignment create \
        --assignee "$LOGIC_APP_PRINCIPAL" \
        --role "$ROLE" \
        --scope "$SCOPE" \
        --output none 2>/dev/null; then
      ok "$LABEL granted"
      return
    fi
    if [[ $i -eq $RETRIES ]]; then
      fail "Failed to assign $LABEL after $RETRIES attempts. Try running the script again."
    fi
    warn "Attempt $i/$RETRIES failed — retrying in 10s..."
    sleep 10
  done
}

OPENAI_RESOURCE_ID=$(az cognitiveservices account show \
  --name "$OPENAI_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query id -o tsv)

# Get the Foundry Hub (ML workspace) resource ID — this is where agent permissions are enforced
FOUNDRY_HUB_ID=$(az ml workspace show \
  --name "$FOUNDRY_HUB" \
  --resource-group "$RESOURCE_GROUP" \
  --query id -o tsv 2>/dev/null) || true

if [[ -z "$FOUNDRY_HUB_ID" ]]; then
  warn "Could not find AI Foundry Hub '$FOUNDRY_HUB' — update FOUNDRY_HUB in this script if the name differs"
  warn "Falling back to resource group scope for Azure AI Developer role"
  FOUNDRY_HUB_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}"
fi

# Cognitive Services User — allows calling the Azure OpenAI endpoints
_assign_role "Cognitive Services User" "$OPENAI_RESOURCE_ID" "Cognitive Services User on Azure OpenAI"

# Azure AI Developer on Foundry Hub — required for Agent Service API (create thread, run, etc.)
_assign_role "Azure AI Developer" "$FOUNDRY_HUB_ID" "Azure AI Developer on Foundry Hub"

# ============================================================
# 3. Deploy workflow definition
# ============================================================

log "Deploying Logic App workflow definition"

# Build the workflow definition JSON.
# The workflow uses HTTP actions with Managed Identity auth to call the
# Azure AI Agent Service REST API directly (no managed connector required).
#
# Flow:
#   Recurrence (Mon-Fri 07:00 UTC)
#     → Create Thread
#     → Create Message (the sync prompt)
#     → Create Run (starts the agent)
#     → Poll Until Completed (checks every 30s, up to 1 hour)

# Get the Logic App's location for the PUT request
LOCATION=$(az resource show \
  --resource-group "$RESOURCE_GROUP" \
  --resource-type "Microsoft.Logic/workflows" \
  --name "$LOGIC_APP" \
  --query "location" -o tsv)

# Write the full workflow body to a temp file.
# az logic workflow update --definition requires an undocumented wrapper structure,
# so we use az rest PUT directly with the correct properties.definition nesting.
WORKFLOW_FILE=$(mktemp /tmp/logic-app-workflow.XXXXXX.json)

cat > "$WORKFLOW_FILE" << WORKFLOW_EOF
{
  "location": "${LOCATION}",
  "properties": {
    "definition": {
      "\$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
      "contentVersion": "1.0.0.0",
      "parameters": {},
      "triggers": {
        "Recurrence": {
          "type": "Recurrence",
          "recurrence": {
            "frequency": "Week",
            "interval": 1,
            "schedule": {
              "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
              "hours": ["7"],
              "minutes": ["0"]
            },
            "timeZone": "UTC"
          }
        }
      },
      "actions": {
        "Create_Thread": {
          "type": "Http",
          "runAfter": {},
          "inputs": {
            "method": "POST",
            "uri": "${FOUNDRY_ENDPOINT}/threads?api-version=2025-05-01",
            "headers": { "Content-Type": "application/json" },
            "authentication": { "type": "ManagedServiceIdentity", "audience": "https://ai.azure.com" },
            "body": {}
          }
        },
        "Create_Message": {
          "type": "Http",
          "runAfter": { "Create_Thread": ["Succeeded"] },
          "inputs": {
            "method": "POST",
            "uri": "@{concat('${FOUNDRY_ENDPOINT}/threads/', body('Create_Thread')['id'], '/messages?api-version=2025-05-01')}",
            "headers": { "Content-Type": "application/json" },
            "authentication": { "type": "ManagedServiceIdentity", "audience": "https://ai.azure.com" },
            "body": {
              "role": "user",
              "content": "Run the daily roadmap sync. Fetch items from the last 7 days (limit to 25 items per run, newest first), check for duplicates, create Epics for new items, and report a summary. Use this config:\n\n{\"feeds\":[\"azure\",\"m365\"],\"globalFilters\":{\"statuses\":[\"In preview\",\"In development\"],\"excludeTypes\":[\"Retirements\"],\"daysBack\":7},\"maxItems\":25,\"boardMappings\":[{\"name\":\"M365 Collaboration\",\"products\":[\"Microsoft Teams\",\"SharePoint\",\"OneDrive\",\"Outlook\",\"Exchange\",\"Planner\",\"Microsoft To Do\",\"Microsoft Viva\",\"Microsoft 365 app\",\"OneNote\",\"PowerPoint\"],\"ado\":{\"organization\":\"https://dev.azure.com/hobbitfeetado/\",\"project\":\"Hobbit-Dev\",\"workItemType\":\"Epic\",\"areaPath\":\"\\\\Hobbit-Dev\\\\Area\\\\Roadmap\\\\M365 Collaboration\"}},{\"name\":\"Security and Compliance\",\"products\":[\"Microsoft Purview\",\"Microsoft Defender for Office 365\",\"Microsoft Information Protection\"],\"ado\":{\"organization\":\"https://dev.azure.com/hobbitfeetado/\",\"project\":\"Hobbit-Dev\",\"workItemType\":\"Epic\",\"areaPath\":\"\\\\Hobbit-Dev\\\\Area\\\\Roadmap\\\\Security and Compliance\"}},{\"name\":\"Identity and Access\",\"products\":[\"Microsoft Entra\"],\"ado\":{\"organization\":\"https://dev.azure.com/hobbitfeetado/\",\"project\":\"Hobbit-Dev\",\"workItemType\":\"Epic\",\"areaPath\":\"\\\\Hobbit-Dev\\\\Area\\\\Roadmap\\\\Identity and Access\"}},{\"name\":\"Endpoint Management\",\"products\":[\"Microsoft Intune\",\"Windows 365\"],\"ado\":{\"organization\":\"https://dev.azure.com/hobbitfeetado/\",\"project\":\"Hobbit-Dev\",\"workItemType\":\"Epic\",\"areaPath\":\"\\\\Hobbit-Dev\\\\Area\\\\Roadmap\\\\Endpoint Management\"}},{\"name\":\"M365 Platform\",\"products\":[\"Microsoft 365\",\"Microsoft 365 admin center\",\"Microsoft Copilot (Microsoft 365)\"],\"ado\":{\"organization\":\"https://dev.azure.com/hobbitfeetado/\",\"project\":\"Hobbit-Dev\",\"workItemType\":\"Epic\",\"areaPath\":\"\\\\Hobbit-Dev\\\\Area\\\\Roadmap\\\\M365 Platform\"}}],\"defaultBoard\":{\"ado\":{\"organization\":\"https://dev.azure.com/hobbitfeetado/\",\"project\":\"Hobbit-Dev\",\"workItemType\":\"Epic\",\"areaPath\":\"\\\\Hobbit-Dev\\\\Area\\\\Roadmap\\\\General\"}}}"
            }
          }
        },
        "Create_Run": {
          "type": "Http",
          "runAfter": { "Create_Message": ["Succeeded"] },
          "inputs": {
            "method": "POST",
            "uri": "@{concat('${FOUNDRY_ENDPOINT}/threads/', body('Create_Thread')['id'], '/runs?api-version=2025-05-01')}",
            "headers": { "Content-Type": "application/json" },
            "authentication": { "type": "ManagedServiceIdentity", "audience": "https://ai.azure.com" },
            "body": {
              "assistant_id": "${AGENT_ID}"
            }
          }
        },
        "Wait_For_Completion": {
          "type": "Until",
          "runAfter": { "Create_Run": ["Succeeded"] },
          "expression": "@or(equals(body('Poll_Run')['status'], 'completed'), equals(body('Poll_Run')['status'], 'failed'), equals(body('Poll_Run')['status'], 'cancelled'), equals(body('Poll_Run')['status'], 'expired'))",
          "limit": { "count": 120, "timeout": "PT1H" },
          "actions": {
            "Delay_30s": {
              "type": "Wait",
              "runAfter": {},
              "inputs": {
                "interval": { "count": 30, "unit": "Second" }
              }
            },
            "Poll_Run": {
              "type": "Http",
              "runAfter": { "Delay_30s": ["Succeeded"] },
              "inputs": {
                "method": "GET",
                "uri": "@{concat('${FOUNDRY_ENDPOINT}/threads/', body('Create_Thread')['id'], '/runs/', body('Create_Run')['id'], '?api-version=2025-05-01')}",
                "authentication": { "type": "ManagedServiceIdentity", "audience": "https://ai.azure.com" }
              }
            }
          }
        },
        "Check_Run_Status": {
          "type": "If",
          "runAfter": { "Wait_For_Completion": ["Succeeded"] },
          "expression": {
            "and": [{ "equals": ["@body('Poll_Run')['status']", "completed"] }]
          },
          "actions": {
            "Get_Final_Message": {
              "type": "Http",
              "inputs": {
                "method": "GET",
                "uri": "@{concat('${FOUNDRY_ENDPOINT}/threads/', body('Create_Thread')['id'], '/messages?api-version=2025-05-01&limit=1&order=desc')}",
                "authentication": { "type": "ManagedServiceIdentity", "audience": "https://ai.azure.com" }
              }
            }
          },
          "else": {
            "actions": {
              "Run_Failed": {
                "type": "Terminate",
                "inputs": {
                  "runStatus": "Failed",
                  "runError": {
                    "code": "@body('Poll_Run')['status']",
                    "message": "@{concat('Agent run ended with status: ', body('Poll_Run')['status'], '. Error: ', string(body('Poll_Run')['last_error']))}"
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
WORKFLOW_EOF

az rest \
  --method PUT \
  --url "https://management.azure.com/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/${LOGIC_APP}?api-version=2019-05-01" \
  --body "@${WORKFLOW_FILE}" \
  --output none

rm -f "$WORKFLOW_FILE"
ok "Workflow deployed"

# ============================================================
# Summary
# ============================================================

PORTAL_LA="https://portal.azure.com/#resource/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/${LOGIC_APP}/logicApp"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║           LOGIC APP CONFIGURED                           ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Logic App:  $LOGIC_APP"
echo "  Schedule:   Mon–Fri 07:00 UTC"
echo "  Agent ID:   $AGENT_ID"
echo ""
echo "  ┌─ Open in Azure Portal ─────────────────────────────────"
echo "  │  $PORTAL_LA"
echo "  └────────────────────────────────────────────────────────"
echo ""
echo "  Next steps:"
echo "  1. Open the Logic App in the portal and click 'Run Trigger' to test"
echo "  2. Check Runs history to verify the agent was invoked"
echo "  3. (Optional) Add a Teams/email notification action after the"
echo "     Check_Run_Status step using the Logic App Designer"
echo ""
