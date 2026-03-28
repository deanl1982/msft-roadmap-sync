# Microsoft Roadmap Sync

Automatically syncs Microsoft Azure and M365 roadmap updates from official RSS feeds into Azure DevOps Epics. An AI agent generates structured work items — including impact summaries and recommended actions — and routes them to the correct ADO boards based on product.

## How It Works

```text
Logic Apps (weekday 07:00 UTC)
    └─▶ Azure AI Foundry Agent (GPT-4o)
            ├─▶ fetch_roadmap Function  — fetches RSS, filters, resolves board routing
            └─▶ ado_operations Function — deduplicates and creates Epics in ADO
                    └─▶ Azure DevOps (multiple boards)
```

1. Logic Apps triggers the agent on a weekday schedule
2. The agent calls `fetch_roadmap` to get filtered roadmap items with board routing resolved
3. For each item, the agent checks ADO for an existing `RoadmapId:<guid>` tag (duplicate detection)
4. New items get a generated Epic with an Impact Summary, Recommended Actions, and the original description
5. The agent reports a summary of what was created, skipped, and any errors

## Repository Structure

| File | Purpose |
| --- | --- |
| `roadmap-sync-config.json` | Product-to-board mappings and RSS filter config |
| `agent-instructions.md` | Foundry Agent system prompt — paste into the Foundry portal |
| `functions/function_app.py` | Azure Functions entry point (`fetch_roadmap` + `ado_operations`) |
| `deploy-azure-resources.sh` | Provisions all Azure infrastructure and deploys function code |
| `setup-ado-boards.sh` | Creates ADO teams and area paths from the config |
| `create-foundry-agent.py` | Creates or updates the Foundry Agent and registers function tools |
| `playground-test-prompts.md` | Step-by-step test prompts for the Foundry Playground |
| `SETUP.md` | Full provisioning guide |
| `.env.example` | Template for local credentials |

## Boards

Items are routed to ADO boards based on product. All boards live in a single ADO project under a `Roadmap` area path hierarchy.

| Board | Products |
| --- | --- |
| M365 Collaboration | Teams, SharePoint, OneDrive, Outlook, Exchange, Planner, Viva, OneNote, PowerPoint |
| Security and Compliance | Microsoft Purview, Defender for Office 365, Information Protection |
| Identity and Access | Microsoft Entra |
| Endpoint Management | Microsoft Intune, Windows 365 |
| M365 Platform | Microsoft 365, M365 admin center, Microsoft Copilot (M365) |
| General | Fallback for all other products |

## Quick Start

### Prerequisites

- Azure subscription with Contributor access
- Azure DevOps project
- Azure CLI (`brew install azure-cli`)
- jq (`brew install jq`)
- Python 3.10+

### 1. Configure board mappings

Edit `roadmap-sync-config.json` to set your ADO organization and adjust product-to-board groupings if needed.

### 2. Set up ADO boards

```bash
chmod +x setup-ado-boards.sh && ./setup-ado-boards.sh
```

Creates a team and area path per board mapping. Updates `roadmap-sync-config.json` with resolved area paths.

### 3. Deploy Azure resources

Add your credentials to `.env` (copy from `.env.example`), then:

```bash
chmod +x deploy-azure-resources.sh && ./deploy-azure-resources.sh
```

Provisions: Resource Group, Storage Account, Application Insights, Function App, Azure OpenAI (GPT-4o), Logic App scaffold.

### 4. Create the Foundry Hub and Project

Create manually at [ai.azure.com](https://ai.azure.com) — link to the resource group and Azure OpenAI resource created in step 3. See `SETUP.md §3` for details.

### 5. Create the agent

```bash
pip3 install azure-ai-projects azure-ai-agents azure-identity python-dotenv
python3 create-foundry-agent.py
```

Registers the agent with both function tools. Save the printed Agent ID for the Logic App.

### 6. Configure Logic Apps

Open `la-roadmap-sync` in the Azure Portal and configure the recurrence trigger (Mon–Fri, 07:00 UTC) and the Foundry Agent action. See `SETUP.md §4`.

### 7. Test in Foundry Playground

Use the prompts in `playground-test-prompts.md` to validate each component before enabling the schedule.

## Work Item Format

Each Epic created in ADO includes:

- **Title** — cleaned roadmap item title (status prefix removed, max 120 chars)
- **Impact Summary** — AI-generated description of what the change means for an IT organisation
- **Recommended Actions** — status-dependent guidance (monitor / pilot / adopt)
- **Details table** — source feed, status, published date, products, link to Microsoft roadmap
- **Tags** — `Roadmap`, `RoadmapId:<guid>`, feed name, status, product names, `Needs-Review` (if In preview)

## Configuration

`roadmap-sync-config.json` controls filtering and routing:

```jsonc
{
  "feeds": ["azure", "m365"],           // which RSS feeds to fetch
  "globalFilters": {
    "statuses": ["In preview", "In development"],
    "excludeTypes": ["Retirements"],
    "daysBack": 7
  },
  "boardMappings": [                    // ordered — first match wins
    {
      "name": "M365 Collaboration",
      "products": ["Microsoft Teams", "SharePoint"],
      "ado": {
        "organization": "https://dev.azure.com/yourorg/",
        "project": "Your-Project",
        "workItemType": "Epic",
        "areaPath": "Your-Project\\Area\\Roadmap\\M365 Collaboration"
      }
    }
  ],
  "defaultBoard": { ... }               // fallback for unmatched products
}
```

## Local Development

Test the function locally before deploying:

```bash
cd functions
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start
```

```bash
curl -X POST http://localhost:7071/api/fetch_roadmap \
  -H "Content-Type: application/json" \
  -d '{"config": <paste roadmap-sync-config.json>, "daysBack": 14}'
```

## Cost

Under $5/month. The main costs are Azure OpenAI token usage (~$1–3/month at 50K tokens/day) and Logic Apps consumption (~$0.30/month). The Function App runs on a Flex Consumption plan and is effectively free at this invocation volume.
