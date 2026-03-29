#!/usr/bin/env python3
"""Create the roadmap-sync Foundry Agent programmatically.

Creates the agent, sets the system prompt from agent-instructions.md, and
registers the fetch_roadmap Azure Function as an OpenAPI tool.

The ADO MCP Server tool cannot be connected programmatically — it requires
an OAuth consent flow in the Foundry portal. After running this script,
complete that one step manually (see output at the end).

Usage:
    pip3 install azure-ai-projects azure-ai-agents azure-identity python-dotenv
    python3 create-foundry-agent.py

Credentials are loaded from a .env file in the repo root (see .env.example).
Never commit .env — it is listed in .gitignore.

Authentication:
    Uses DefaultAzureCredential — run 'az login' first.
"""

import os
import sys
from pathlib import Path

# Load .env file if present (requires python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv not installed — fall back to environment variables

# ============================================================
# CONFIG — loaded from .env or environment variables
# ============================================================

FOUNDRY_ENDPOINT = os.environ.get("AZURE_FOUNDRY_ENDPOINT", "")
FUNCTION_URL = os.environ.get("AZURE_FUNCTION_URL", "")

AGENT_NAME = "roadmap-sync-agent"
MODEL_DEPLOYMENT = "gpt-4o"
INSTRUCTIONS_FILE = Path(__file__).parent / "agent-instructions.md"

# ============================================================

def main():
    # Validate config
    if not FOUNDRY_ENDPOINT:
        print("ERROR: AZURE_FOUNDRY_ENDPOINT is not set.")
        print("  Add it to .env or export it as an environment variable.")
        print("  Find your endpoint in the Foundry portal: Settings > Project details > Endpoint")
        sys.exit(1)

    if not FUNCTION_URL:
        print("ERROR: AZURE_FUNCTION_URL is not set.")
        print("  Add it to .env or export it as an environment variable.")
        print("  Get key with: az functionapp keys list --name func-roadmap-sync --resource-group rg-roadmap-sync --query masterKey -o tsv")
        sys.exit(1)

    try:
        from azure.ai.projects import AIProjectClient
        from azure.ai.agents.models import OpenApiTool, OpenApiAnonymousAuthDetails
        from azure.identity import DefaultAzureCredential
    except ImportError:
        print("ERROR: Required packages not installed.")
        print("  Run: pip3 install azure-ai-projects azure-ai-agents azure-identity")
        sys.exit(1)

    # Load instructions
    if not INSTRUCTIONS_FILE.exists():
        print(f"ERROR: {INSTRUCTIONS_FILE} not found. Run from the repo root.")
        sys.exit(1)

    instructions = INSTRUCTIONS_FILE.read_text(encoding="utf-8")
    print(f"Loaded instructions from {INSTRUCTIONS_FILE} ({len(instructions)} chars)")

    # Parse the function key from the URL — it's the ?code= query parameter.
    # The key is embedded as a default parameter in each operation so the agent
    # always sends it without needing to know about it.
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(FUNCTION_URL)
    function_key = parse_qs(parsed.query).get("code", [""])[0]
    function_base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Reusable parameter definition for the function key
    code_param = {
        "name": "code",
        "in": "query",
        "required": True,
        "schema": {"type": "string", "default": function_key},
    }

    # Build OpenAPI spec for the fetch_roadmap function
    # Defines the tool schema the agent uses to call the HTTP-triggered function
    function_openapi_spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "Roadmap Fetch Function",
            "version": "1.0.0",
            "description": "Fetches and filters Microsoft Azure and M365 roadmap RSS items, returning structured JSON with board routing resolved.",
        },
        "servers": [{"url": function_base_url}],
        "paths": {
            "/api/fetch_roadmap": {
                "post": {
                    "operationId": "fetch_roadmap",
                    "summary": "Fetch and filter roadmap items",
                    "parameters": [code_param],
                    "description": (
                        "Fetches Azure and M365 RSS feeds, filters items by date/status/product, "
                        "and resolves the target ADO board for each item. "
                        "Returns filtered items ready for work item creation."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["config"],
                                    "properties": {
                                        "config": {
                                            "type": "object",
                                            "description": "Contents of roadmap-sync-config.json",
                                        },
                                        "daysBack": {
                                            "type": "integer",
                                            "description": "Number of days to look back (default: 7)",
                                            "default": 7,
                                        },
                                        "maxItems": {
                                            "type": "integer",
                                            "description": "Maximum number of items to return (newest first). Use to cap batch size per agent run.",
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Filtered roadmap items with board routing",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "totalFetched": {"type": "integer"},
                                            "totalFiltered": {"type": "integer"},
                                            "cutoffDate": {"type": "string"},
                                            "feeds": {"type": "array", "items": {"type": "string"}},
                                            "errors": {"type": "array", "items": {"type": "string"}},
                                            "items": {"type": "array", "items": {"type": "object"}},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    # Connect to Foundry project
    print(f"\nConnecting to Foundry project: {FOUNDRY_ENDPOINT}")
    credential = DefaultAzureCredential()
    client = AIProjectClient(endpoint=FOUNDRY_ENDPOINT, credential=credential)

    # Check if agent already exists
    print(f"Checking for existing agent '{AGENT_NAME}'...")
    existing_agent = None
    for agent in client.agents.list_agents():
        if agent.name == AGENT_NAME:
            existing_agent = agent
            break

    # Derive ado_operations URL from fetch_roadmap URL (same app, different route)
    ado_function_url = FUNCTION_URL.replace("/api/fetch_roadmap", "/api/ado_operations")
    function_base_url = FUNCTION_URL.split("/api/")[0]

    # Build ado_operations OpenAPI spec
    ado_openapi_spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "ADO Operations Function",
            "version": "1.0.0",
            "description": "Search for and create Azure DevOps work items. Auth is handled server-side via ADO_PAT.",
        },
        "servers": [{"url": function_base_url}],
        "paths": {
            "/api/ado_operations": {
                "post": {
                    "operationId": "ado_operations",
                    "summary": "Search for or create Azure DevOps work items",
                    "parameters": [code_param],
                    "description": (
                        "Two actions: 'search_work_items' checks for duplicates by tag "
                        "(e.g. RoadmapId:<guid>), 'create_work_item' creates a new Epic. "
                        "The ADO PAT is stored server-side — do not pass credentials."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["action", "organization", "project"],
                                    "properties": {
                                        "action": {
                                            "type": "string",
                                            "enum": ["search_work_items", "create_work_item"],
                                            "description": "Operation to perform",
                                        },
                                        "organization": {
                                            "type": "string",
                                            "description": "ADO organization URL, e.g. https://dev.azure.com/myorg/",
                                        },
                                        "project": {
                                            "type": "string",
                                            "description": "ADO project name",
                                        },
                                        "tag": {
                                            "type": "string",
                                            "description": "Tag to search for (search_work_items only), e.g. RoadmapId:12345",
                                        },
                                        "workItemType": {
                                            "type": "string",
                                            "description": "Work item type to create (create_work_item only), e.g. Epic",
                                        },
                                        "title": {
                                            "type": "string",
                                            "description": "Work item title (create_work_item only)",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "HTML description (create_work_item only)",
                                        },
                                        "areaPath": {
                                            "type": "string",
                                            "description": "Area path for the work item (create_work_item only)",
                                        },
                                        "tags": {
                                            "type": "string",
                                            "description": "Semicolon-separated tags (create_work_item only)",
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Operation result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "found": {"type": "boolean"},
                                            "count": {"type": "integer"},
                                            "workItems": {"type": "array", "items": {"type": "object"}},
                                            "created": {"type": "boolean"},
                                            "id": {"type": "integer"},
                                            "url": {"type": "string"},
                                            "title": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    # Build tools
    fetch_roadmap_tool = OpenApiTool(
        name="fetch_roadmap",
        spec=function_openapi_spec,
        description="Fetch and filter Microsoft roadmap RSS items with board routing resolved.",
        auth=OpenApiAnonymousAuthDetails(),
    )

    ado_operations_tool = OpenApiTool(
        name="ado_operations",
        spec=ado_openapi_spec,
        description="Search for existing ADO work items by tag (duplicate check) and create new Epics.",
        auth=OpenApiAnonymousAuthDetails(),
    )

    all_tool_definitions = fetch_roadmap_tool.definitions + ado_operations_tool.definitions

    if existing_agent:
        print(f"Agent '{AGENT_NAME}' already exists (id: {existing_agent.id}) — updating...")
        agent = client.agents.update_agent(
            agent_id=existing_agent.id,
            model=MODEL_DEPLOYMENT,
            instructions=instructions,
            tools=all_tool_definitions,
        )
        print(f"Updated agent: {agent.id}")
    else:
        print(f"Creating agent '{AGENT_NAME}'...")
        agent = client.agents.create_agent(
            model=MODEL_DEPLOYMENT,
            name=AGENT_NAME,
            instructions=instructions,
            tools=all_tool_definitions,
        )
        print(f"Created agent: {agent.id}")

    # Summary
    print()
    print("=" * 60)
    print("  AGENT READY")
    print("=" * 60)
    print(f"  Name:     {agent.name}")
    print(f"  ID:       {agent.id}")
    print(f"  Model:    {agent.model}")
    print(f"  Tools:    fetch_roadmap (OpenAPI)")
    print(f"            ado_operations (OpenAPI)")
    print()
    print(f"  fetch_roadmap URL:  {FUNCTION_URL}")
    print(f"  ado_operations URL: {ado_function_url}")
    print()
    print(f"  Agent ID for Logic Apps: {agent.id}")
    print()


if __name__ == "__main__":
    main()
