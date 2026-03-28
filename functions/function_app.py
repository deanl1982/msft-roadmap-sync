"""Azure Functions entry point for msft-roadmap-sync.

Contains two HTTP-triggered functions:
  - fetch_roadmap    : Fetches and filters Microsoft roadmap RSS feeds
  - ado_operations   : Searches and creates work items in Azure DevOps
"""

import base64
import json
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# ============================================================
# fetch_roadmap — constants and helpers
# ============================================================

FEED_URLS = {
    "azure": "https://www.microsoft.com/releasecommunications/api/v2/azure/rss",
    "m365": "https://www.microsoft.com/releasecommunications/api/v2/m365/rss",
}

STATUSES = {"Launched", "In preview", "In development", "In review", "Rolling out"}

NON_PRODUCT_CATEGORIES = {
    "Launched", "In preview", "In development", "In review", "Rolling out",
    "General Availability", "Public Preview", "Private Preview", "Preview",
    "Current Channel", "Targeted Release", "Targeted Release (Entire Organization)",
    "Worldwide (Standard Multi-Tenant)", "GCC", "GCC High", "DoD",
    "Android", "iOS", "Desktop", "Mac", "Web", "Windows",
    "Developer", "Teams and Surface Devices",
    "Features", "Retirements", "Compliance", "Regions & Datacenters",
    "Operating System", "SDK and Tools", "Pricing & Offerings", "Services", "Open Source",
}

UPDATE_TYPES = {
    "Features", "Retirements", "Compliance", "Regions & Datacenters",
    "Operating System", "SDK and Tools", "Pricing & Offerings", "Services", "Open Source",
}


def _fetch_feed(feed_url: str, feed_name: str) -> list[dict]:
    logging.info("Fetching %s feed from %s", feed_name, feed_url)
    req = urllib.request.Request(feed_url, headers={"User-Agent": "RoadmapSync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        xml_content = response.read()

    root = ET.fromstring(xml_content)
    items = []

    for item_el in root.findall(".//item"):
        categories = [cat.text for cat in item_el.findall("category") if cat.text]

        guid_el = item_el.find("guid")
        guid = guid_el.text if guid_el is not None else None

        title_el = item_el.find("title")
        raw_title = title_el.text if title_el is not None else ""
        title = re.sub(r"^\[.*?\]\s*", "", raw_title)

        link_el = item_el.find("link")
        link = link_el.text if link_el is not None else ""

        desc_el = item_el.find("description")
        description = desc_el.text if desc_el is not None else ""

        pub_date_el = item_el.find("pubDate")
        pub_date = None
        if pub_date_el is not None and pub_date_el.text:
            pub_date = _parse_rfc2822(pub_date_el.text)

        status = next((c for c in categories if c in STATUSES), None)
        products = [c for c in categories if c not in NON_PRODUCT_CATEGORIES]
        update_types = [c for c in categories if c in UPDATE_TYPES]

        items.append({
            "feed": feed_name,
            "guid": guid,
            "title": title,
            "rawTitle": raw_title,
            "link": link,
            "description": description,
            "pubDate": pub_date,
            "categories": categories,
            "status": status,
            "products": products,
            "updateTypes": update_types,
        })

    logging.info("Parsed %d items from %s", len(items), feed_name)
    return items


def _parse_rfc2822(date_str: str) -> str | None:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).isoformat()
    except (ValueError, TypeError):
        try:
            return datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z").isoformat()
        except (ValueError, TypeError):
            logging.warning("Could not parse date: %s", date_str)
            return None


def _matches_filter(item: dict, config: dict, cutoff: datetime) -> bool:
    global_filters = config.get("globalFilters", {})

    if item["pubDate"]:
        item_date = datetime.fromisoformat(item["pubDate"])
        if item_date.tzinfo is None:
            item_date = item_date.replace(tzinfo=timezone.utc)
        if item_date < cutoff:
            return False

    statuses = global_filters.get("statuses", [])
    if statuses and item["status"] not in statuses:
        return False

    all_products = set()
    for mapping in config.get("boardMappings", []):
        all_products.update(mapping.get("products", []))

    if all_products:
        item_cats = set(item["products"]) | set(item["categories"])
        if not item_cats & all_products:
            return False

    exclude_types = global_filters.get("excludeTypes", [])
    if exclude_types:
        for ut in item["updateTypes"]:
            if ut in exclude_types:
                return False

    return True


def _resolve_board(item: dict, config: dict) -> dict | None:
    item_cats = set(item["products"]) | set(item["categories"])

    for mapping in config.get("boardMappings", []):
        if item_cats & set(mapping.get("products", [])):
            return {"boardName": mapping["name"], "ado": mapping["ado"]}

    default = config.get("defaultBoard")
    if default:
        return {"boardName": "Default", "ado": default["ado"]}

    return None


# ============================================================
# ado_operations — helpers
# ============================================================

def _ado_request(method: str, url: str, body: dict | list | None, pat: str,
                 content_type: str = "application/json") -> dict:
    token = base64.b64encode(f":{pat}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": content_type,
        "Accept": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _normalize_org(organization: str) -> str:
    """Ensure org URL has no trailing slash for use in API paths."""
    return organization.rstrip("/")


# ============================================================
# HTTP Functions
# ============================================================

@app.route(route="fetch_roadmap", methods=["POST"])
def fetch_roadmap(req: func.HttpRequest) -> func.HttpResponse:
    """Fetch and filter Microsoft roadmap RSS items with board routing resolved.

    Request body (JSON):
        config   : contents of roadmap-sync-config.json
        daysBack : number of days to look back (optional, default 7)

    Returns JSON with totalFetched, totalFiltered, cutoffDate, feeds, errors, items[].
    Each item includes a board object with the target ADO project and area path.
    """
    logging.info("fetch_roadmap triggered")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Request body must be valid JSON"}),
            status_code=400, mimetype="application/json",
        )

    config = body.get("config")
    if not config:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'config' in request body"}),
            status_code=400, mimetype="application/json",
        )

    days_back = body.get("daysBack", config.get("globalFilters", {}).get("daysBack", 7))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    feeds = config.get("feeds", ["azure", "m365"])
    all_items = []
    errors = []

    for feed_name in feeds:
        feed_url = FEED_URLS.get(feed_name)
        if not feed_url:
            errors.append(f"Unknown feed: {feed_name}")
            continue
        try:
            all_items.extend(_fetch_feed(feed_url, feed_name))
        except Exception as e:
            logging.warning("Failed to fetch %s feed: %s", feed_name, e)
            errors.append(f"Failed to fetch {feed_name}: {str(e)}")

    filtered = [item for item in all_items if _matches_filter(item, config, cutoff)]

    results = []
    for item in filtered:
        board = _resolve_board(item, config)
        if board:
            item["board"] = board
            results.append(item)

    results.sort(key=lambda x: x.get("pubDate") or "", reverse=True)

    return func.HttpResponse(
        json.dumps({
            "totalFetched": len(all_items),
            "totalFiltered": len(results),
            "cutoffDate": cutoff.isoformat(),
            "feeds": feeds,
            "errors": errors,
            "items": results,
        }, default=str),
        mimetype="application/json",
    )


@app.route(route="ado_operations", methods=["POST"])
def ado_operations(req: func.HttpRequest) -> func.HttpResponse:
    """Search for or create Azure DevOps work items.

    The ADO PAT is read from the ADO_PAT environment variable (Function App setting).
    Never pass credentials in the request body.

    Request body (JSON):

      Search for duplicate by tag:
        {
          "action": "search_work_items",
          "organization": "https://dev.azure.com/myorg/",
          "project": "My-Project",
          "tag": "RoadmapId:12345"
        }

      Create a work item:
        {
          "action": "create_work_item",
          "organization": "https://dev.azure.com/myorg/",
          "project": "My-Project",
          "workItemType": "Epic",
          "title": "Work item title",
          "description": "<div>HTML description</div>",
          "areaPath": "My-Project\\\\Area\\\\Roadmap\\\\Board",
          "tags": "Roadmap; RoadmapId:12345; M365; In preview"
        }
    """
    logging.info("ado_operations triggered")

    pat = os.environ.get("ADO_PAT")
    if not pat:
        return func.HttpResponse(
            json.dumps({"error": "ADO_PAT environment variable is not configured"}),
            status_code=500, mimetype="application/json",
        )

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Request body must be valid JSON"}),
            status_code=400, mimetype="application/json",
        )

    action = body.get("action")
    organization = body.get("organization", "")
    project = body.get("project", "")

    if not action or not organization or not project:
        return func.HttpResponse(
            json.dumps({"error": "Missing required fields: action, organization, project"}),
            status_code=400, mimetype="application/json",
        )

    org = _normalize_org(organization)

    # ------------------------------------------------------------------
    # search_work_items — WIQL query to detect duplicates by tag
    # ------------------------------------------------------------------
    if action == "search_work_items":
        tag = body.get("tag")
        if not tag:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: tag"}),
                status_code=400, mimetype="application/json",
            )

        wiql_url = f"{org}/{project}/_apis/wit/wiql?api-version=7.1"
        wiql_query = {
            "query": (
                f"SELECT [System.Id], [System.Title], [System.Tags] "
                f"FROM WorkItems "
                f"WHERE [System.TeamProject] = '{project}' "
                f"AND [System.Tags] CONTAINS '{tag}'"
            )
        }

        try:
            result = _ado_request("POST", wiql_url, wiql_query, pat)
            work_items = result.get("workItems", [])
            return func.HttpResponse(
                json.dumps({
                    "found": len(work_items) > 0,
                    "count": len(work_items),
                    "workItems": work_items,
                }),
                mimetype="application/json",
            )
        except Exception as e:
            logging.error("ADO search failed: %s", e)
            return func.HttpResponse(
                json.dumps({"error": f"ADO search failed: {str(e)}"}),
                status_code=502, mimetype="application/json",
            )

    # ------------------------------------------------------------------
    # create_work_item — JSON Patch to create a new work item
    # ------------------------------------------------------------------
    elif action == "create_work_item":
        work_item_type = body.get("workItemType", "Epic")
        title = body.get("title")
        description = body.get("description", "")
        area_path = body.get("areaPath", "")
        tags = body.get("tags", "")

        if not title:
            return func.HttpResponse(
                json.dumps({"error": "Missing required field: title"}),
                status_code=400, mimetype="application/json",
            )

        patch = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": description},
            {"op": "add", "path": "/fields/System.Tags", "value": tags},
        ]
        if area_path:
            patch.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})

        create_url = (
            f"{org}/{project}/_apis/wit/workitems/"
            f"${work_item_type}?api-version=7.1"
        )

        try:
            result = _ado_request(
                "POST", create_url, patch, pat,
                content_type="application/json-patch+json",
            )
            work_item_id = result.get("id")
            work_item_url = result.get("_links", {}).get("html", {}).get("href", "")
            logging.info("Created work item %s: %s", work_item_id, title)
            return func.HttpResponse(
                json.dumps({
                    "created": True,
                    "id": work_item_id,
                    "url": work_item_url,
                    "title": title,
                }),
                mimetype="application/json",
            )
        except Exception as e:
            logging.error("ADO create failed: %s", e)
            return func.HttpResponse(
                json.dumps({"error": f"ADO create failed: {str(e)}"}),
                status_code=502, mimetype="application/json",
            )

    else:
        return func.HttpResponse(
            json.dumps({"error": f"Unknown action: '{action}'. Valid actions: search_work_items, create_work_item"}),
            status_code=400, mimetype="application/json",
        )
