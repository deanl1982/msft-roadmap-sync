# Standard Operating Procedures — Microsoft Roadmap Sync

Common operator tasks for maintaining and adjusting the roadmap sync pipeline.

---

## How do I change how far back the sync looks?

Edit the `daysBack` value in the `content` string inside `configure-logic-app.sh`:

```json
"daysBack": 7
```

Change it to `14`, `30`, etc. Then redeploy:

```bash
./configure-logic-app.sh
```

> The daily production sync uses `7`. Increase temporarily if you suspect items were missed due to a gap in the schedule. Do not set this above `30` without also reducing `maxItems` — large windows return hundreds of items which will exceed the Azure OpenAI S0 token rate limit and cause the agent run to fail with `rate_limit_exceeded`.

---

## How do I add a new product to an existing board?

Edit `roadmap-sync-config.json` — add the product name to the relevant `boardMappings[].products` array:

```json
{
  "name": "M365 Collaboration",
  "products": ["Microsoft Teams", "SharePoint", "YourNewProduct"]
}
```

Then update the same config in `configure-logic-app.sh` (the inline JSON in the `content` string) and redeploy:

```bash
./configure-logic-app.sh
```

> Product names must **exactly** match the category names in the Microsoft RSS feed (case-sensitive). To find valid names, run `fetch_roadmap` with an empty `boardMappings` array and inspect the `products` field on returned items.

---

## How do I add a new board?

1. Add a new entry to `roadmap-sync-config.json`:

```json
{
  "name": "Your Board Name",
  "products": ["Product A", "Product B"],
  "ado": {
    "organization": "https://dev.azure.com/hobbitfeetado/",
    "project": "Hobbit-Dev",
    "workItemType": "Epic",
    "areaPath": ""
  }
}
```

2. Run `setup-ado-boards.sh` — it will create the ADO team and area path and fill in `areaPath` automatically:

```bash
./setup-ado-boards.sh
```

3. Update the inline config in `configure-logic-app.sh` to include the new board mapping, then redeploy:

```bash
./configure-logic-app.sh
```

---

## How do I change the work item type (Epic → Feature)?

Change `workItemType` in the relevant board mapping in both `roadmap-sync-config.json` and the inline config in `configure-logic-app.sh`:

```json
"workItemType": "Feature"
```

Then redeploy:

```bash
./configure-logic-app.sh
```

---

## How do I change the sync schedule?

Edit the `Recurrence` trigger in `configure-logic-app.sh`:

```json
"triggers": {
  "Recurrence": {
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
}
```

Adjust `weekDays`, `hours`, or `minutes` as needed. Then redeploy:

```bash
./configure-logic-app.sh
```

---

## How do I change which statuses are synced?

Edit `globalFilters.statuses` in the inline config in `configure-logic-app.sh`:

```json
"statuses": ["In preview", "In development"]
```

Valid values: `Launched`, `In preview`, `In development`, `In review`, `Rolling out`

Add `"Launched"` if you want items that have already shipped. Then redeploy:

```bash
./configure-logic-app.sh
```

---

## How do I rotate the Azure DevOps PAT?

1. Create a new PAT at your ADO organisation → User Settings → Personal Access Tokens
   - Required scope: **Work Items (Read & Write)**
2. Update the Function App setting:

```bash
az functionapp config appsettings set \
  --name func-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --settings "ADO_PAT=<new-pat>"
```

3. Update `.env` with the new PAT value for future script runs.

> Set a calendar reminder before the PAT expires — the `ado_operations` function will silently fail with 401 errors if the PAT is expired.

---

## How do I trigger the sync manually?

**Option A — Logic App (full pipeline):**

Portal → `rg-roadmap-sync` → `la-roadmap-sync` → **Run Trigger** → **Run**

**Option B — Direct curl (fetch only, no ADO writes):**

```bash
FUNC_KEY=$(az functionapp keys list \
  --name func-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --query "masterKey" -o tsv)

curl -s -X POST \
  "https://func-roadmap-sync.azurewebsites.net/api/fetch_roadmap?code=${FUNC_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"config": <paste roadmap-sync-config.json>, "daysBack": 7}' | jq '.totalFiltered'
```

---

## How do I check what the last Logic App run created?

Portal → `la-roadmap-sync` → **Runs history** → click the latest run → expand **Get_Final_Message** → **Outputs** → look at:

```
body.data[0].content[0].text.value
```

This contains the agent's summary: items fetched, created, skipped, and any errors.

---

## How do I check if the function is working?

```bash
FUNC_KEY=$(az functionapp keys list \
  --name func-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --query "masterKey" -o tsv)

# Search for a tag that won't exist — should return found: false
curl -s -X POST \
  "https://func-roadmap-sync.azurewebsites.net/api/ado_operations?code=${FUNC_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"action":"search_work_items","organization":"https://dev.azure.com/hobbitfeetado/","project":"Hobbit-Dev","tag":"RoadmapId:test-healthcheck"}'
```

Expected response: `{"found": false, "count": 0, "workItems": []}`

---

## How do I update the agent instructions?

1. Edit `agent-instructions.md`
2. Run `create-foundry-agent.py` to push the updated instructions to the agent:

```bash
python3 create-foundry-agent.py
```

The script updates the existing agent — it does not create a duplicate.

---

## How do I redeploy the function code after changes?

```bash
./deploy-azure-resources.sh
```

The script is idempotent — it skips all already-provisioned resources and only re-zips and redeploys the function code.

---

## How do I view function logs?

```bash
az monitor app-insights query \
  --app appi-roadmap-sync \
  --resource-group rg-roadmap-sync \
  --analytics-query "traces | where timestamp > ago(1h) | order by timestamp desc | take 50" \
  --output table
```

Or open Application Insights in the portal: `rg-roadmap-sync` → `appi-roadmap-sync` → **Logs**.

---

## What do I do if items are not being created in ADO?

Work through this checklist in order:

1. **Check `fetch_roadmap` returns items** — run the curl health check above with `daysBack: 30`. If `totalFiltered` is 0, the filter is too narrow or there are no new items in the feed.
2. **Check all items are duplicates** — if `fetch_roadmap` returns items but nothing is created, the agent may be finding all of them already in ADO. Verify by searching ADO for `RoadmapId:` tagged items.
3. **Check the ADO PAT** — verify it hasn't expired and has Work Items (Read & Write) scope.
4. **Check the Logic App run** — expand the run steps in Runs history to find the first step that failed or returned unexpected output.
5. **Check Application Insights** — look for errors in the function logs around the time of the Logic App run.
