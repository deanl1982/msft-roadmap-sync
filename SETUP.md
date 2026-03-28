# Setup Guide: Agentic Microsoft Roadmap Sync

Step-by-step instructions to deploy the agentic roadmap sync system using Azure AI Foundry, Azure Functions, and Logic Apps.

---

## Architecture Overview

```
+-------------------+       +---------------------------+       +-------------------------+
|   Logic Apps      |       |   Azure AI Foundry        |       |   Azure DevOps          |
|   (Scheduler)     |------>|   Agent Service           |------>|   (Multiple Boards)     |
|                   |       |                           |       |                         |
|   Weekday 07:00   |       |   1. Calls RSS Function   |       |   M365 Collaboration    |
|   UTC recurrence  |       |   2. Generates content    |       |   Security & Compliance |
+-------------------+       |   3. Routes to boards     |       |   Identity & Access     |
                            |   4. Creates work items   |       |   Endpoint Management   |
                            +-----|---------------------+       |   M365 Platform         |
                                  |                             +-------------------------+
                                  v
                            +---------------------------+
                            |   Azure Function          |
                            |   (RSS Fetch + Filter)    |
                            |                           |
                            |   Fetches Azure/M365 RSS  |
                            |   Filters by config       |
                            |   Resolves board mapping  |
                            +---------------------------+
```

**Data flow:**
1. Logic Apps triggers the Foundry Agent on a weekday schedule
2. The agent calls the Azure Function tool to fetch and filter RSS items
3. The Function returns filtered items, each tagged with its target ADO board
4. For each item, the agent checks ADO for duplicates, generates a rich work item (impact summary, recommended actions), and creates it on the correct board
5. The agent returns a sync summary to Logic Apps, which optionally sends a notification

---

## Azure Resources to Create

| Resource | Type | Name (suggested) | Purpose |
|---|---|---|---|
| Resource Group | `Microsoft.Resources/resourceGroups` | `rg-roadmap-sync` | Contains all resources |
| Storage Account | `Microsoft.Storage/storageAccounts` | `stroadmapsync` | Required backing store for Azure Functions |
| Function App | `Microsoft.Web/sites` | `func-roadmap-sync` | Hosts the RSS fetch + filter Python function |
| Azure AI Foundry Hub | `Microsoft.MachineLearningServices/workspaces` | `hub-roadmap-sync` | AI Foundry hub (if you don't already have one) |
| Azure AI Foundry Project | (within hub) | `proj-roadmap-sync` | Contains the agent |
| Azure OpenAI Service | `Microsoft.CognitiveServices/accounts` | `aoai-roadmap-sync` | Hosts the GPT-4o model deployment |
| Logic App | `Microsoft.Logic/workflows` | `la-roadmap-sync` | Scheduled trigger for the agent |
| Application Insights | `Microsoft.Insights/components` | `appi-roadmap-sync` | (Optional) Monitoring for the Function App |

---

## Prerequisites

- An Azure subscription with Contributor access
- An Azure DevOps organization with existing projects and boards you want to route items to
- The following tools installed locally:
  - [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) (`az` command)
  - [Azure Functions Core Tools v4](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local) (`func` command)
  - Python 3.11+
  - Git

---

## Step 1: Configure Your Board Mappings

Before creating any Azure resources, configure how products map to your ADO boards.

### 1.1 Open `roadmap-sync-config.json`

This file controls which RSS feeds to fetch, what to filter, and where to route each item.

### 1.2 Replace placeholder values

Find and replace these placeholders with your real values:

| Placeholder | Replace with | Example |
|---|---|---|
| `YOUR_ORG` | Your ADO organization name | `mycompany` |
| `YOUR_M365_PROJECT` | ADO project for M365 collaboration items | `M365-Collaboration` |
| `YOUR_SECURITY_PROJECT` | ADO project for security/compliance items | `InfoSec` |
| `YOUR_IDENTITY_PROJECT` | ADO project for identity/access items | `Identity-Platform` |
| `YOUR_ENDPOINT_PROJECT` | ADO project for endpoint management items | `Endpoint-Management` |
| `YOUR_PLATFORM_PROJECT` | ADO project for M365 platform items | `M365-Platform` |
| `YOUR_DEFAULT_PROJECT` | Fallback project for unmatched items | `IT-Roadmap` |

### 1.3 Adjust product-to-board groupings

The `boardMappings` array is **ordered** -- items match the **first** mapping that contains their product. Review the product lists and move products between groups if your org structure is different.

To add a new board mapping, append an entry:

```json
{
  "name": "Your Board Name",
  "products": ["Product A", "Product B"],
  "ado": {
    "organization": "https://dev.azure.com/yourorg",
    "project": "Your-ADO-Project",
    "workItemType": "Feature",
    "areaPath": ""
  }
}
```

### 1.4 Configure filters

In the `globalFilters` section:

- **`statuses`** -- Which roadmap statuses to include. Options: `Launched`, `In preview`, `In development`, `In review`, `Rolling out`
- **`excludeTypes`** -- Which update types to exclude. Options: `Features`, `Retirements`, `Compliance`, `Regions & Datacenters`, `Operating System`, `SDK and Tools`, `Pricing & Offerings`, `Services`, `Open Source`
- **`daysBack`** -- How many days back to look for new items (default: 7)

### 1.5 Set area paths (optional)

If you want items created under a specific area path within a project, set the `areaPath` field. Use double backslashes for path separators:

```json
"areaPath": "M365-Collaboration\\Vendor Roadmap\\Incoming"
```

Leave as `""` to use the project's default area path.

---

## Step 2: Create the Resource Group

All resources will live in a single resource group.

```bash
az login
az group create --name rg-roadmap-sync --location uksouth
```

Change `uksouth` to your preferred Azure region.

---

## Step 3: Deploy the Azure Function

The Azure Function fetches RSS feeds, parses the XML, applies filters from the config, and returns structured JSON with board routing resolved. This is the deterministic part of the pipeline -- no LLM involved.

### 3.1 Create the Storage Account

Azure Functions requires a Storage Account for internal state management.

```bash
az storage account create \
  --name stroadmapsync \
  --resource-group rg-roadmap-sync \
  --location uksouth \
  --sku Standard_LRS
```

> **Note:** Storage account names must be globally unique, lowercase, 3-24 characters. If `stroadmapsync` is taken, choose a different name and use it in subsequent commands.

### 3.2 Create the Function App

```bash
az functionapp create \
  --name func-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --storage-account stroadmapsync \
  --consumption-plan-location uksouth \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --os-type Linux
```

> **Note:** Function App names must be globally unique. If `func-roadmap-sync` is taken, choose a different name.

### 3.3 Test locally (optional but recommended)

Before deploying, verify the function works locally:

```bash
cd functions
python -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
func start
```

In another terminal, test with a POST request:

```bash
curl -X POST http://localhost:7071/api/fetch_roadmap \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "feeds": ["m365"],
      "globalFilters": {
        "statuses": ["In preview"],
        "excludeTypes": ["Retirements"],
        "daysBack": 14
      },
      "boardMappings": [
        {
          "name": "Test Board",
          "products": ["Microsoft Teams"],
          "ado": {
            "organization": "https://dev.azure.com/test",
            "project": "Test",
            "workItemType": "Feature",
            "areaPath": ""
          }
        }
      ]
    }
  }'
```

You should get a JSON response with `totalFetched`, `totalFiltered`, and an `items` array. Each item should have a `board` object with the routing info.

### 3.4 Deploy to Azure

```bash
cd functions
func azure functionapp publish func-roadmap-sync
```

### 3.5 Get the Function URL and key

After deployment, retrieve the function URL:

```bash
az functionapp function show \
  --name func-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --function-name fetch_roadmap \
  --query "invokeUrlTemplate" -o tsv
```

Get the function key:

```bash
az functionapp function keys list \
  --name func-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --function-name fetch_roadmap \
  --query "default" -o tsv
```

Save both values -- you'll need them when registering the function as a tool in Foundry.

The full invocation URL is: `<invokeUrlTemplate>?code=<function-key>`

### 3.6 Verify the deployed function

```bash
curl -X POST "<your-function-url>?code=<your-function-key>" \
  -H "Content-Type: application/json" \
  -d '{"config": <paste your roadmap-sync-config.json content>}'
```

---

## Step 4: Deploy Azure OpenAI

The Foundry Agent needs an Azure OpenAI model deployment for content generation.

### 4.1 Create the Azure OpenAI resource

```bash
az cognitiveservices account create \
  --name aoai-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --location uksouth \
  --kind OpenAI \
  --sku S0
```

> **Note:** Azure OpenAI requires a subscription that has been approved for access. If you haven't already, you may need to request access at https://aka.ms/oai/access.

### 4.2 Deploy the GPT-4o model

```bash
az cognitiveservices account deployment create \
  --name aoai-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --deployment-name gpt-4o \
  --model-name gpt-4o \
  --model-version "2024-11-20" \
  --model-format OpenAI \
  --sku-capacity 10 \
  --sku-name GlobalStandard
```

> **Capacity:** 10K tokens-per-minute is sufficient for this workload (~50K tokens per daily run). Increase if you plan higher volume.

---

## Step 5: Create the Azure AI Foundry Agent

This is the core agentic component. The agent uses GPT-4o to generate rich work item content and routes items to the correct ADO boards.

### 5.1 Create an AI Foundry Hub (if you don't have one)

1. Go to [Azure AI Foundry portal](https://ai.azure.com)
2. Click **Create Hub** in the top-right
3. Configure:
   - **Name:** `hub-roadmap-sync`
   - **Resource Group:** `rg-roadmap-sync`
   - **Region:** `uksouth` (same as your other resources)
   - **Azure OpenAI resource:** Select `aoai-roadmap-sync` (created in Step 4)
4. Click **Create**

### 5.2 Create a Foundry Project

1. Within the hub, click **New Project**
2. **Name:** `proj-roadmap-sync`
3. Click **Create**

### 5.3 Create the Agent

1. In the project, navigate to **Agents** in the left sidebar
2. Click **New Agent**
3. Configure:
   - **Name:** `roadmap-sync-agent`
   - **Model deployment:** Select `gpt-4o` (from your Azure OpenAI resource)
   - **Instructions:** Copy and paste the entire contents of `agent-instructions.md` from this repository

### 5.4 Add the Azure DevOps MCP Server tool

This gives the agent the ability to search for and create work items in Azure DevOps.

1. In the agent's **Tools** section, click **Add tool**
2. Click **MCP Servers** tab
3. Select **Azure DevOps** from the tool catalog
4. Click **Connect** and authenticate with your Azure DevOps organization
5. The following tool operations will be available:
   - **Search work items** -- used for duplicate detection (searching by `RoadmapId:<guid>` tag)
   - **Create work item** -- used to create new work items on the target board
   - **Get work item** -- used to verify created items
   - **List projects** -- used to validate project names from config
6. Click **Save**

### 5.5 Add the Azure Function as a custom tool

This gives the agent the ability to call your RSS fetch + filter function.

1. In the agent's **Tools** section, click **Add tool**
2. Select **Azure Function**
3. Select your Function App: `func-roadmap-sync`
4. Select the function: `fetch_roadmap`
5. The function's input/output schema will be auto-detected from the code
6. Click **Save**

> **Alternative (OpenAPI):** If auto-detection doesn't work, you can manually define the tool by providing an OpenAPI spec. The function accepts a POST with `{"config": {...}, "daysBack": 7}` and returns `{"totalFetched": N, "totalFiltered": N, "items": [...]}`.

### 5.6 Test in the Foundry Playground

Before connecting the scheduler, test the agent interactively.

1. Click **Playground** (or **Test** tab) in the agent view
2. Send this message:

   > Run the daily roadmap sync. Use this config: `<paste the contents of your roadmap-sync-config.json>`. Fetch items from the last 14 days, filter and route per config, create work items for new items, and report a summary.

3. Verify the agent:
   - **Calls the `fetch_roadmap` function** -- you should see the tool call in the trace
   - **Gets filtered items back** -- check the function response has items
   - **Checks for duplicates** -- the agent should search ADO before creating
   - **Generates rich descriptions** -- work items should have Impact Summary and Recommended Actions sections
   - **Routes to correct boards** -- items should be created in the correct ADO project per your board mappings
   - **Returns a summary** -- listing what was created, skipped, and any errors

4. **Run it again** with the same config to verify **duplicate detection** works -- all items should be skipped on the second run.

> **Tip:** Use `daysBack: 14` or `30` for testing to get more items. Switch to `7` for production.

---

## Step 6: Create the Logic Apps Scheduler

Logic Apps triggers the agent automatically on weekday mornings.

### 6.1 Create the Logic App

**Via Azure Portal:**

1. Go to **Azure Portal** > search for **Logic Apps** > click **Create**
2. Configure:
   - **Type:** Consumption
   - **Name:** `la-roadmap-sync`
   - **Resource Group:** `rg-roadmap-sync`
   - **Region:** `uksouth`
3. Click **Review + Create** > **Create**
4. Once deployed, click **Go to resource** > **Logic App Designer**

**Via Azure CLI:**

```bash
az logic workflow create \
  --name la-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --location uksouth \
  --definition '{}'
```

Then open the Logic App in the Azure Portal to configure the workflow visually.

### 6.2 Configure the Recurrence Trigger

1. In the Logic App Designer, click **Add a trigger**
2. Search for **Recurrence** and select it
3. Configure:
   - **Interval:** `1`
   - **Frequency:** `Week`
   - **On these days:** `Monday`, `Tuesday`, `Wednesday`, `Thursday`, `Friday`
   - **At these hours:** `7`
   - **At these minutes:** `0`
   - **Time zone:** `UTC`

### 6.3 Add the Foundry Agent Service Action

1. Click **+** > **Add an action**
2. Search for **Azure AI Foundry Agent Service** (or **Azure AI Agent Service**)
3. Add the **Create Thread** action
   - Connect to your Foundry project (`proj-roadmap-sync`)
   - No parameters needed
4. Add the **Create Message** action
   - **Thread ID:** Select the output from the Create Thread step
   - **Role:** `user`
   - **Content:**
     ```
     Run the daily roadmap sync. Fetch items from the last 7 days, filter and route per config, create work items for new items, and report a summary.
     ```
5. Add the **Create Run** action
   - **Thread ID:** Same as above
   - **Agent ID:** Select `roadmap-sync-agent`
6. Add the **Get Run** action (with a loop/wait to poll for completion)
   - Or use the **Create Run and Wait** action if available, which polls automatically

### 6.4 Add a Notification Action (optional)

After the agent run completes, send a summary notification:

**Option A: Microsoft Teams**
1. Add action **Post message in a chat or channel** (Teams connector)
2. Post the agent's response message to a channel of your choice

**Option B: Email**
1. Add action **Send an email (V2)** (Office 365 Outlook connector)
2. Send the agent's summary to a distribution list

**Option C: ADO Comment**
1. Add action **Add a comment** (Azure DevOps connector)
2. Post a summary comment on a tracking work item

### 6.5 Save and Test

1. Click **Save** in the Logic App Designer
2. Click **Run Trigger** > **Run** to execute manually
3. Monitor the run in the **Runs history** tab
4. Verify:
   - The recurrence triggers correctly
   - The Foundry Agent is invoked
   - Work items appear in the correct ADO boards
   - The notification is sent (if configured)

---

## Step 7: Grant Azure DevOps Permissions

The Foundry Agent's ADO MCP Server connection needs permission to create work items in each target project.

### 7.1 Verify the service connection identity

When you connected the ADO MCP Server in Step 5.4, it created a service connection using your identity (or a service principal). Check which identity is being used in the Foundry portal under the agent's tool configuration.

### 7.2 Grant permissions per project

For each ADO project in your board mappings, ensure the identity has:

1. Go to **Azure DevOps** > **Project Settings** > **Permissions**
2. Find the identity (your user account or the service principal)
3. Grant:
   - **Create work items** -- required to create new items
   - **Edit work items** -- required if you later want the agent to update items
   - **View work items** -- required for duplicate detection search

If using area paths, ensure the identity also has **Edit work items in this node** on the relevant area paths:

1. Go to **Project Settings** > **Project configuration** > **Areas**
2. Click the area path node > **Security**
3. Grant the identity the required permissions

---

## Step 8: Verify End-to-End

Run through this checklist to confirm everything is working:

- [ ] **Function responds** -- POST to the function URL returns filtered items with board mappings
- [ ] **Agent calls function** -- In Foundry playground, the agent invokes `fetch_roadmap` as its first tool call
- [ ] **Content generation** -- Work items have an "Impact Summary" and "Recommended Actions" section (not just the raw RSS description)
- [ ] **Board routing** -- Items land in the correct ADO project per the config. Check at least 2 different boards
- [ ] **Duplicate detection** -- Running the agent twice with the same date range creates no duplicates. Items are tagged with `RoadmapId:<guid>`
- [ ] **Tags are correct** -- Each work item has: `Roadmap`, `RoadmapId:<guid>`, feed name, status, and product tags
- [ ] **Needs-Review tag** -- Items with status "In preview" should have the `Needs-Review` tag
- [ ] **Logic Apps triggers** -- Manual trigger in Logic Apps successfully invokes the agent and completes
- [ ] **Notification arrives** -- If configured, the summary notification is sent to Teams/email

---

## Step 9: Retire the Legacy Pipeline

Once the agentic system is verified and has run successfully for a few days:

1. **Disable the ADO pipeline schedule** -- Go to your Azure DevOps pipeline > Edit > Triggers > remove or disable the cron schedule
2. **Keep `Sync-RoadmapItems.ps1`** as a fallback -- it can still be run locally with `-DryRun` to preview items without the LLM layer

---

## Troubleshooting

### Function returns 0 items

- Check `globalFilters.daysBack` -- increase to 14 or 30 to widen the window
- Check `globalFilters.statuses` -- ensure the statuses you're filtering for actually exist in the feed
- Check `boardMappings[].products` -- product names must exactly match the RSS feed categories (case-sensitive). Run the function with an empty `boardMappings` to see all products available in the feed

### Agent doesn't call the function

- Verify the function is registered as a tool in the agent's configuration
- Check the function URL and key are correct
- In the Foundry playground, look at the agent trace to see if it attempted the tool call and received an error

### Work items created in wrong project

- Board mappings are matched in order. Check that the product isn't appearing earlier in the `boardMappings` array than intended
- Verify the product name in the RSS feed exactly matches what's in your config

### Duplicate items being created

- Ensure the agent is searching for `RoadmapId:<guid>` in the correct project. The WIQL search is scoped to `System.TeamProject`
- Check that the `guid` field is being extracted correctly from the RSS feed (some items use `guid` text content, others use an attribute)

### Logic Apps fails to invoke agent

- Check the Foundry Agent Service connector authentication is valid
- Verify the agent ID is correct in the Create Run action
- Check the Logic Apps run history for detailed error messages

---

## Cost Estimate

| Component | SKU / Plan | Monthly Cost |
|---|---|---|
| Azure AI Foundry Agent Service | No infrastructure charge | Free |
| Azure OpenAI (GPT-4o) | ~50K tokens/day, 22 days/month | ~$1-3 |
| Azure Function App | Consumption plan, 1 invocation/day | ~$0 |
| Storage Account | Standard LRS, minimal usage | ~$0.10 |
| Logic Apps | Consumption plan, 22 runs/month | ~$0.30 |
| Application Insights (optional) | Free tier (5GB/month) | Free |
| **Total** | | **Under $5/month** |

---

## File Reference

| File | What it does | When to edit |
|---|---|---|
| `roadmap-sync-config.json` | Product-to-board mapping and filter config | When you add/remove products or ADO boards |
| `agent-instructions.md` | Foundry Agent system prompt with work item template | When you want to change the work item format, tags, or agent behaviour |
| `functions/fetch_roadmap/function_app.py` | RSS fetch, parse, filter, and board routing logic | When Microsoft changes the RSS feed format or you need new filter logic |
| `functions/host.json` | Azure Functions runtime configuration | Rarely |
| `functions/requirements.txt` | Python dependencies for the function | When adding new Python packages |
