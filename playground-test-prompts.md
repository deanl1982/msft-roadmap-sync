# Foundry Playground Test Prompts

Use these prompts in the Foundry Playground to validate the agent end-to-end.
Run them in order — each test builds on the previous.

---

## Test 1: Fetch only (no ADO writes)

Validates that `fetch_roadmap` is called correctly and returns items.

```
Run a dry-run roadmap fetch. Call the fetch_roadmap function with the config below and daysBack set to 7.

IMPORTANT: Do NOT call ado_operations at all during this test. Do not search for duplicates. Do not create any work items. Only call fetch_roadmap.

Report:
- How many items were fetched from each feed
- How many passed the filters
- A list of the first 5 items with their title, status, products, and which board they would route to

Config:
{
  "feeds": ["m365"],
  "globalFilters": {
    "statuses": ["In preview", "In development"],
    "excludeTypes": ["Retirements"],
    "daysBack": 7
  },
  "boardMappings": [
    {
      "name": "M365 Collaboration",
      "products": ["Microsoft Teams", "SharePoint", "OneDrive", "Outlook", "Exchange", "Planner", "Microsoft To Do", "Microsoft Viva", "Microsoft 365 app", "OneNote", "PowerPoint"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\M365 Collaboration" }
    },
    {
      "name": "Security and Compliance",
      "products": ["Microsoft Purview", "Microsoft Defender for Office 365", "Microsoft Information Protection"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\Security and Compliance" }
    },
    {
      "name": "Identity and Access",
      "products": ["Microsoft Entra"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\Identity and Access" }
    },
    {
      "name": "Endpoint Management",
      "products": ["Microsoft Intune", "Windows 365"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\Endpoint Management" }
    },
    {
      "name": "M365 Platform",
      "products": ["Microsoft 365", "Microsoft 365 admin center", "Microsoft Copilot (Microsoft 365)"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\M365 Platform" }
    }
  ],
  "defaultBoard": {
    "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\General" }
  }
}
```

**What to check in the trace:**
- `fetch_roadmap` tool call appears
- Response contains `totalFetched` > 0 and `totalFiltered` > 0
- Items have a `board` object with the correct area path

---

## Test 2: Duplicate check only

Validates that `ado_operations` search works before any creates happen.

```
Check whether a work item tagged "RoadmapId:999999" already exists in the Hobbit-Dev project at https://dev.azure.com/hobbitfeetado/. Report whether it was found or not.
```

**What to check:**
- `ado_operations` is called with `action: "search_work_items"` and `tag: "RoadmapId:999999"`
- Response shows `found: false` (this GUID won't exist)
- No work item is created

---

## Test 3: Create a single test Epic

Validates that `ado_operations` can create a work item in ADO.

```
Create a single test Epic in Azure DevOps with the following details:
- Organization: https://dev.azure.com/hobbitfeetado/
- Project: Hobbit-Dev
- Work item type: Epic
- Title: [TEST] Playground validation item — safe to delete
- Description: <div><p>This is a test item created from the Foundry Playground to validate the ado_operations function. It can be safely deleted.</p></div>
- Area path: \Hobbit-Dev\Area\Roadmap\General
- Tags: Roadmap; Test; PlaygroundValidation

Report the created work item ID and URL.
```

**What to check:**
- `ado_operations` called with `action: "create_work_item"`
- Response contains a numeric `id` and a `url`
- Epic appears in ADO at `https://dev.azure.com/hobbitfeetado/Hobbit-Dev/_workitems`
- Area path is `Roadmap\General`
- Tags include `Roadmap`, `Test`, `PlaygroundValidation`

---

## Test 4: Full sync — single board, limited items

Validates the complete flow (fetch → dedup check → generate content → create) on a small scope.

```
Run a roadmap sync for Microsoft Teams items only from the last 14 days. For each item:
1. Check for duplicates using the RoadmapId tag
2. Generate a full work item (Impact Summary, Recommended Actions, original description)
3. Create the Epic in ADO
4. Report a summary

Limit to a maximum of 3 items to keep this test short. Use this config:

{
  "feeds": ["m365"],
  "globalFilters": {
    "statuses": ["In preview", "In development"],
    "excludeTypes": ["Retirements"],
    "daysBack": 14
  },
  "boardMappings": [
    {
      "name": "M365 Collaboration",
      "products": ["Microsoft Teams"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\M365 Collaboration" }
    }
  ],
  "defaultBoard": {
    "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\General" }
  }
}
```

**What to check:**
- `fetch_roadmap` called, returns Teams items
- `ado_operations` with `search_work_items` called for each item before creating
- `ado_operations` with `create_work_item` called for non-duplicates
- Each created Epic has an Impact Summary and Recommended Actions section in the description
- Tags include `RoadmapId:<guid>` and `Needs-Review` (if status is "In preview")
- Summary at end lists created count, skipped count, and any errors

---

## Test 5: Duplicate detection (re-run test)

Run Test 4 a second time with the same config. All items should be skipped.

```
Re-run the same roadmap sync as the previous test. All items should already exist in ADO from the last run. Confirm that no new Epics are created and report how many were skipped as duplicates.
```

**What to check:**
- `ado_operations` with `search_work_items` returns `found: true` for each item
- `ado_operations` with `create_work_item` is NOT called
- Summary shows 0 created, all items skipped

---

## Test 6: Full production sync

Only run this after Tests 1–5 pass. This is the same prompt used by Logic Apps on the daily schedule.

```
Run the daily roadmap sync. Fetch items from the last 7 days, check for duplicates, create Epics for new items, and report a summary. Use this config:

{
  "feeds": ["azure", "m365"],
  "globalFilters": {
    "statuses": ["In preview", "In development"],
    "excludeTypes": ["Retirements"],
    "daysBack": 7
  },
  "boardMappings": [
    {
      "name": "M365 Collaboration",
      "products": ["Microsoft Teams", "SharePoint", "OneDrive", "Outlook", "Exchange", "Planner", "Microsoft To Do", "Microsoft Viva", "Microsoft 365 app", "OneNote", "PowerPoint"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\M365 Collaboration" }
    },
    {
      "name": "Security and Compliance",
      "products": ["Microsoft Purview", "Microsoft Defender for Office 365", "Microsoft Information Protection"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\Security and Compliance" }
    },
    {
      "name": "Identity and Access",
      "products": ["Microsoft Entra"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\Identity and Access" }
    },
    {
      "name": "Endpoint Management",
      "products": ["Microsoft Intune", "Windows 365"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\Endpoint Management" }
    },
    {
      "name": "M365 Platform",
      "products": ["Microsoft 365", "Microsoft 365 admin center", "Microsoft Copilot (Microsoft 365)"],
      "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\M365 Platform" }
    }
  ],
  "defaultBoard": {
    "ado": { "organization": "https://dev.azure.com/hobbitfeetado/", "project": "Hobbit-Dev", "workItemType": "Epic", "areaPath": "\\Hobbit-Dev\\Area\\Roadmap\\General" }
  }
}
```
