# Setup Guide: Agentic Microsoft Roadmap Sync

Step-by-step instructions to deploy the agentic roadmap sync system using Azure AI Foundry, Azure Functions, and Logic Apps.

---

## Architecture Overview

```text
+-------------------+       +---------------------------+       +-------------------------+
|   Logic Apps      |       |   Azure AI Foundry        |       |   Azure DevOps          |
|   (Scheduler)     |------>|   Agent Service           |------>|   (Multiple Boards)     |
|                   |       |                           |       |                         |
|   Weekday 07:00   |       |   1. Calls RSS Function   |       |   M365 Collaboration    |
|   UTC recurrence  |       |   2. Generates content    |       |   Security and          |
+-------------------+       |   3. Routes to boards     |       |     Compliance          |
                            |   4. Creates work items   |       |   Identity and Access   |
                            +-----|---------------------+       |   Endpoint Management   |
                                  |                             |   M365 Platform         |
                                  v                             +-------------------------+
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
| --- | --- | --- | --- |
| Resource Group | `Microsoft.Resources/resourceGroups` | `rg-roadmap-sync` | Contains all resources |
| Storage Account | `Microsoft.Storage/storageAccounts` | `stroadmapsync` | Required backing store for Azure Functions |
| Function App | `Microsoft.Web/sites` | `func-roadmap-sync` | Hosts the RSS fetch + filter Python function |
| Azure AI Foundry Hub | `Microsoft.MachineLearningServices/workspaces` | `hub-roadmap-sync` | AI Foundry hub (if you don't already have one) |
| Azure AI Foundry Project | (within hub) | `proj-roadmap-sync` | Contains the agent |
| Azure OpenAI Service | `Microsoft.CognitiveServices/accounts` | `aoai-roadmap-sync` | Hosts the GPT-4o model deployment |
| Logic App | `Microsoft.Logic/workflows` | `la-roadmap-sync` | Scheduled trigger for the agent |
| Application Insights | `Microsoft.Insights/components` | `appi-roadmap-sync` | Monitoring for the Function App |

---

## Prerequisites

- An Azure subscription with Contributor access
- An Azure DevOps organization with an existing project
- The following tools installed locally:
  - [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) (`az`)
  - `jq` — `brew install jq`
  - `zip` — `brew install zip`

---

## Step 1: Set Up ADO Boards

Run `setup-ado-boards.sh` to create teams and area paths in your ADO project. This script:

- Creates a `Roadmap` parent area node
- Creates a child area path and team for each board mapping in `roadmap-sync-config.json`
- Configures each team to own its area path (so items appear on the right board)
- Updates `roadmap-sync-config.json` in-place with the resolved `areaPath` values

```bash
chmod +x setup-ado-boards.sh && ./setup-ado-boards.sh
```

Before running, open `setup-ado-boards.sh` and confirm the top two variables match your environment:

```bash
ADO_ORG="https://dev.azure.com/yourorg"
ADO_PROJECT="Your-Project"
```

### 1.1 Adjust product-to-board groupings (optional)

Open `roadmap-sync-config.json`. The `boardMappings` array is **ordered** — items match the **first** mapping whose `products` list contains their product name. Add, remove, or reorder mappings to suit your org structure.

To add a new board mapping:

```json
{
  "name": "Your Board Name",
  "products": ["Product A", "Product B"],
  "ado": {
    "organization": "https://dev.azure.com/yourorg/",
    "project": "Your-ADO-Project",
    "workItemType": "Epic",
    "areaPath": ""
  }
}
```

Then re-run `setup-ado-boards.sh` — it is idempotent and will only create the missing team and area path.

> **Note:** ADO does not allow `&` in area path or team names. The script automatically replaces `&` with `and` when creating ADO resources.

### 1.2 Configure filters

In the `globalFilters` section of `roadmap-sync-config.json`:

- **`statuses`** -- Which roadmap statuses to include. Options: `Launched`, `In preview`, `In development`, `In review`, `Rolling out`
- **`excludeTypes`** -- Which update types to exclude. Options: `Features`, `Retirements`, `Compliance`, `Regions & Datacenters`, `Operating System`, `SDK and Tools`, `Pricing & Offerings`, `Services`, `Open Source`
- **`daysBack`** -- How many days back to look for new items (default: 7)

---

## Step 2: Deploy Azure Resources

Run `deploy-azure-resources.sh` to provision all required Azure infrastructure and deploy the function code:

```bash
chmod +x deploy-azure-resources.sh && ./deploy-azure-resources.sh
```

This script provisions (and skips any resources that already exist):

1. Resource Group
2. Storage Account
3. Application Insights
4. Function App (Flex Consumption plan, Python 3.11) + deploys function code via zip
5. Azure OpenAI resource + GPT-4o model deployment
6. Logic App scaffold

At the end it prints the **Function invocation URL** — save this for Step 3.

Before running, confirm the configuration block at the top of the script matches your environment. The only values likely to need changing are `LOCATION` (default: `uksouth`) and the resource names if they conflict with existing resources.

> **Note:** Azure OpenAI requires subscription-level approval. If the OpenAI step fails, request access at [aka.ms/oai/access](https://aka.ms/oai/access).

### 2.1 Test locally (optional but recommended)

Before deploying, verify the function works locally:

```bash
cd functions
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
func start
```

In another terminal:

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
            "workItemType": "Epic",
            "areaPath": ""
          }
        }
      ]
    }
  }'
```

You should get a JSON response with `totalFetched`, `totalFiltered`, and an `items` array. Each item should have a `board` object with the routing info.

### 2.2 Verify the deployed function

```bash
curl -X POST "<your-function-url>" \
  -H "Content-Type: application/json" \
  -d '{"config": <paste your roadmap-sync-config.json content>}'
```

---

## Step 3: Create the Azure AI Foundry Agent

This is the core agentic component. The agent uses GPT-4o to generate rich work item content and routes items to the correct ADO boards.

The hub and project must be created manually in the portal (once only). The agent itself can then be created — or updated — programmatically using `create-foundry-agent.py`.

### 3.1 Create an AI Foundry Hub (if you don't have one)

1. Go to [Azure AI Foundry portal](https://ai.azure.com)
2. Click **Create Hub** in the top-right
3. Configure:
   - **Name:** `hub-roadmap-sync`
   - **Resource Group:** `rg-roadmap-sync`
   - **Region:** `uksouth` (same as your other resources)
   - **Azure OpenAI resource:** Select `aoai-roadmap-sync` (created in Step 2)
4. Click **Create**

### 3.2 Create a Foundry Project

1. Within the hub, click **New Project**
2. **Name:** `proj-roadmap-sync`
3. Click **Create**

### 3.3 Create the Agent

1. In the project, navigate to **Agents** in the left sidebar
2. Click **New Agent**
3. Configure:
   - **Name:** `roadmap-sync-agent`
   - **Model deployment:** Select `gpt-4o` (from your Azure OpenAI resource)
   - **Instructions:** Copy and paste the entire contents of `agent-instructions.md` from this repository

### 3.4 Register the function tools (programmatic)

Run `create-foundry-agent.py` — this registers both `fetch_roadmap` and `ado_operations` as OpenAPI tools on the agent, and creates or updates the agent in one step:

```bash
python3 create-foundry-agent.py
```

Save the **Agent ID** printed at the end — you'll need it for the Logic Apps step.

> The script reads `AZURE_FOUNDRY_ENDPOINT` and `AZURE_FUNCTION_URL` from `.env`. See `.env.example` for the required values.

### 3.6 Test in the Foundry Playground

Before connecting the scheduler, test the agent interactively using the prompts in `playground-test-prompts.md`. Run Tests 1–6 in order.

1. Click **Playground** (or **Test** tab) in the agent view
2. Use the prompt from **Test 1** first (fetch only, no ADO writes), then work through the remaining tests

3. Verify the agent:
   - **Calls the `fetch_roadmap` function** -- you should see the tool call in the trace
   - **Gets filtered items back** -- check the function response has items
   - **Checks for duplicates** -- the agent should search ADO before creating
   - **Generates rich descriptions** -- work items should have Impact Summary and Recommended Actions sections
   - **Routes to correct boards** -- items should land in the correct area path per your board mappings
   - **Returns a summary** -- listing what was created, skipped, and any errors

4. **Run it again** with the same config to verify **duplicate detection** works -- all items should be skipped on the second run.

> **Tip:** Use `daysBack: 14` or `30` for testing to get more items. Switch to `7` for production.

---

## Step 4: Create the Logic Apps Scheduler

Logic Apps triggers the agent automatically on weekday mornings.

### 4.1 Create the Logic App

The Logic App scaffold was created by `deploy-azure-resources.sh`. Open it in the portal to configure the workflow:

1. Go to **Azure Portal** > **Resource Groups** > `rg-roadmap-sync` > `la-roadmap-sync`
2. Click **Logic App Designer**

### 4.2 Configure the Recurrence Trigger

1. In the Logic App Designer, click **Add a trigger**
2. Search for **Recurrence** and select it
3. Configure:
   - **Interval:** `1`
   - **Frequency:** `Week`
   - **On these days:** `Monday`, `Tuesday`, `Wednesday`, `Thursday`, `Friday`
   - **At these hours:** `7`
   - **At these minutes:** `0`
   - **Time zone:** `UTC`

### 4.3 Add the Foundry Agent Service Action

1. Click **+** > **Add an action**
2. Search for **Azure AI Foundry Agent Service** (or **Azure AI Agent Service**)
3. Add the **Create Thread** action
   - Connect to your Foundry project (`proj-roadmap-sync`)
   - No parameters needed
4. Add the **Create Message** action
   - **Thread ID:** Select the output from the Create Thread step
   - **Role:** `user`
   - **Content:**

     ```text
     Run the daily roadmap sync. Fetch items from the last 7 days, filter and route per config, create work items for new items, and report a summary.
     ```

5. Add the **Create Run** action
   - **Thread ID:** Same as above
   - **Agent ID:** Select `roadmap-sync-agent`
6. Add the **Get Run** action (with a loop/wait to poll for completion)
   - Or use the **Create Run and Wait** action if available, which polls automatically

### 4.4 Add a Notification Action (optional)

After the agent run completes, send a summary notification:

#### Option A: Microsoft Teams

1. Add action **Post message in a chat or channel** (Teams connector)
2. Post the agent's response message to a channel of your choice

#### Option B: Email

1. Add action **Send an email (V2)** (Office 365 Outlook connector)
2. Send the agent's summary to a distribution list

### 4.5 Save and Test

1. Click **Save** in the Logic App Designer
2. Click **Run Trigger** > **Run** to execute manually
3. Monitor the run in the **Runs history** tab
4. Verify:
   - The recurrence triggers correctly
   - The Foundry Agent is invoked
   - Epics appear in the correct ADO boards under the correct area paths
   - The notification is sent (if configured)

---

## Step 5: Grant Azure DevOps Permissions

The Foundry Agent's ADO MCP Server connection needs permission to create work items in the target project.

### 5.1 Verify the service connection identity

When you connected the ADO MCP Server in Step 3.4, it created a service connection using your identity (or a service principal). Check which identity is being used in the Foundry portal under the agent's tool configuration.

### 5.2 Grant permissions

1. Go to **Azure DevOps** > **Project Settings** > **Permissions**
2. Find the identity (your user account or the service principal)
3. Grant:
   - **Create work items** -- required to create new Epics
   - **Edit work items** -- required if you later want the agent to update items
   - **View work items** -- required for duplicate detection search

For area path-level permissions:

1. Go to **Project Settings** > **Project configuration** > **Areas**
2. Click the `Roadmap` node > **Security**
3. Grant the identity **Edit work items in this node** — this cascades to all child area paths

---

## Step 6: Verify End-to-End

Run through this checklist to confirm everything is working:

- [ ] **Function responds** -- POST to the function URL returns filtered items with board mappings
- [ ] **Agent calls function** -- In Foundry playground, the agent invokes `fetch_roadmap` as its first tool call
- [ ] **Content generation** -- Epics have an "Impact Summary" and "Recommended Actions" section (not just the raw RSS description)
- [ ] **Board routing** -- Items land under the correct area path per the config. Check at least 2 different boards
- [ ] **Duplicate detection** -- Running the agent twice with the same date range creates no duplicates. Items are tagged with `RoadmapId:<guid>`
- [ ] **Tags are correct** -- Each Epic has: `Roadmap`, `RoadmapId:<guid>`, feed name, status, and product tags
- [ ] **Needs-Review tag** -- Items with status "In preview" should have the `Needs-Review` tag
- [ ] **Logic Apps triggers** -- Manual trigger in Logic Apps successfully invokes the agent and completes
- [ ] **Notification arrives** -- If configured, the summary notification is sent to Teams/email

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

### Work items created in wrong area path

- Board mappings are matched in order. Check that the product isn't appearing earlier in the `boardMappings` array than intended
- Verify the product name in the RSS feed exactly matches what's in your config

### Duplicate items being created

- Ensure the agent is searching for `RoadmapId:<guid>` in the correct project. The WIQL search is scoped to `System.TeamProject`
- Check that the `guid` field is being extracted correctly from the RSS feed

### Logic Apps fails to invoke agent

- Check the Foundry Agent Service connector authentication is valid
- Verify the agent ID is correct in the Create Run action
- Check the Logic Apps run history for detailed error messages

---

## Cost Estimate

| Component | SKU / Plan | Monthly Cost |
| --- | --- | --- |
| Azure AI Foundry Agent Service | No infrastructure charge | Free |
| Azure OpenAI (GPT-4o) | ~50K tokens/day, 22 days/month | ~$1-3 |
| Azure Function App | Flex Consumption plan, 1 invocation/day | ~$0 |
| Storage Account | Standard LRS, minimal usage | ~$0.10 |
| Logic Apps | Consumption plan, 22 runs/month | ~$0.30 |
| Application Insights | Free tier (5GB/month) | Free |
| **Total** | | **Under $5/month** |

---

## File Reference

| File | What it does | When to edit |
| --- | --- | --- |
| `roadmap-sync-config.json` | Product-to-board mapping, filter config, and resolved area paths | When you add/remove products or ADO boards |
| `agent-instructions.md` | Foundry Agent system prompt with work item template | When you want to change the Epic format, tags, or agent behaviour |
| `deploy-azure-resources.sh` | Provisions all Azure infrastructure and deploys function code | When adding new Azure resources or redeploying after function changes |
| `setup-ado-boards.sh` | Creates ADO teams and area paths from the config | When you add new board mappings or set up a new ADO project |
| `functions/function_app.py` | RSS fetch, filter, board routing (`fetch_roadmap`) and ADO search/create (`ado_operations`) | When Microsoft changes the RSS feed format or you need new filter logic |
| `functions/host.json` | Azure Functions runtime configuration | Rarely |
| `functions/requirements.txt` | Python dependencies for the function | When adding new Python packages |
