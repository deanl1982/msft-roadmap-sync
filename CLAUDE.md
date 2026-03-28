# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Syncs Microsoft Azure and M365 roadmap items from official RSS feeds into Azure DevOps work items. Two surfaces:

1. **Agentic (Foundry Agent)** — LLM-powered agent that generates rich work item content (impact summaries, recommended actions) and routes items to multiple ADO boards based on product-to-board mappings. Scheduled via Logic Apps. This is the primary path.
2. **Interactive (GitHub Copilot Agent)** — On-demand agent for manual triage sessions. User selects which items to push; agent handles ADO creation interactively.

## Key Files

### Agentic system (primary)

- **`functions/fetch_roadmap/function_app.py`** — Python Azure Function. Fetches RSS, parses XML, filters by config, resolves board mapping for each item. HTTP-triggered, returns JSON.
- **`roadmap-sync-config.json`** — Config with `globalFilters` and `boardMappings` array mapping products to ADO projects/boards. First match wins (ordered list).
- **`agent-instructions.md`** — Foundry Agent system prompt. Contains work item template, routing rules, and behavioral constraints. Paste into Foundry portal.
- **`SETUP.md`** — Step-by-step provisioning guide for Function, Foundry Agent, and Logic Apps.
- **`deploy.sh`** — Azure Cloud Shell script that provisions all required Azure resources.

### Interactive (GitHub Copilot)

- **`.github/agents/roadmap-sync.agent.md`** — Copilot agent definition for interactive triage. `prompts.txt` has example invocations.

## Running Locally

### Azure Function

Requires Python 3.10+ (uses `str | None` union syntax) and the Azure Functions Core Tools (`func`).

```bash
cd functions
pip install -r requirements.txt
func start

# Test with curl
curl -X POST http://localhost:7071/api/fetch_roadmap \
  -H "Content-Type: application/json" \
  -d '{"config": <contents of roadmap-sync-config.json>, "daysBack": 7}'
```

## Testing

There are no automated tests. Local validation is done via the curl command above against a running `func start` instance.

## Architecture

### Agentic flow

```text
Logic Apps (weekday schedule) → Foundry Agent → fetch_roadmap Function (RSS + filter)
                                             → ADO MCP Server (dedup + create work items)
                                             → Multiple ADO projects/boards
```

The **Function** handles deterministic work (RSS parsing, filtering, board resolution). The **Agent** handles generative work (impact summaries, recommended actions, title cleanup).

### Config structure

`roadmap-sync-config.json` has four sections:

- `feeds` — which RSS feeds to fetch (`azure`, `m365`)
- `globalFilters` — statuses, excludeTypes, daysBack
- `boardMappings[]` — **ordered** array of `{name, products[], ado{org, project, workItemType, areaPath}}`. A product listed in multiple mappings only routes to the first match.
- `defaultBoard` — fallback for items not matching any mapping

### RSS Feed URLs

- Azure: `https://www.microsoft.com/releasecommunications/api/v2/azure/rss`
- M365: `https://www.microsoft.com/releasecommunications/api/v2/m365/rss`

### Duplicate Detection

Work items are tagged with `RoadmapId:<guid>`. The Foundry agent searches the target ADO project for this tag before creating.

## Category Extraction Logic

RSS items have flat `<category>` elements. Products are extracted by **exclusion** — anything not in the known status/platform/type/channel sets is treated as a product name. The exclusion sets are defined in `NON_PRODUCT_CATEGORIES` in `function_app.py`.
