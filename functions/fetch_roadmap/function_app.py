"""Azure Function: Fetch and filter Microsoft roadmap RSS feeds.

HTTP-triggered function that fetches Azure and M365 roadmap RSS feeds,
parses items, and filters by product/status/type based on configuration.
Returns filtered items as JSON for the Foundry Agent to process.
"""

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

FEED_URLS = {
    "azure": "https://www.microsoft.com/releasecommunications/api/v2/azure/rss",
    "m365": "https://www.microsoft.com/releasecommunications/api/v2/m365/rss",
}

STATUSES = {"Launched", "In preview", "In development", "In review", "Rolling out"}

# Categories that are NOT product names — used to extract products by exclusion
NON_PRODUCT_CATEGORIES = {
    # Statuses
    "Launched", "In preview", "In development", "In review", "Rolling out",
    # Release channels
    "General Availability", "Public Preview", "Private Preview", "Preview",
    "Current Channel", "Targeted Release", "Targeted Release (Entire Organization)",
    # Environments
    "Worldwide (Standard Multi-Tenant)", "GCC", "GCC High", "DoD",
    # Platforms
    "Android", "iOS", "Desktop", "Mac", "Web", "Windows",
    # Audiences
    "Developer", "Teams and Surface Devices",
    # Update types
    "Features", "Retirements", "Compliance", "Regions & Datacenters",
    "Operating System", "SDK and Tools", "Pricing & Offerings", "Services", "Open Source",
}

UPDATE_TYPES = {
    "Features", "Retirements", "Compliance", "Regions & Datacenters",
    "Operating System", "SDK and Tools", "Pricing & Offerings", "Services", "Open Source",
}


def fetch_feed(feed_url: str, feed_name: str) -> list[dict]:
    """Fetch and parse a single RSS feed into structured items."""
    import urllib.request

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
    """Parse RFC 2822 date string to ISO 8601 format."""
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(date_str)
        return dt.isoformat()
    except (ValueError, TypeError):
        try:
            dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z")
            return dt.isoformat()
        except (ValueError, TypeError):
            logging.warning("Could not parse date: %s", date_str)
            return None


def matches_filter(item: dict, config: dict, cutoff: datetime) -> bool:
    """Check if an item passes the global filters."""
    global_filters = config.get("globalFilters", {})

    # Date filter
    if item["pubDate"]:
        item_date = datetime.fromisoformat(item["pubDate"])
        if item_date.tzinfo is None:
            item_date = item_date.replace(tzinfo=timezone.utc)
        if item_date < cutoff:
            return False

    # Status filter
    statuses = global_filters.get("statuses", [])
    if statuses and item["status"] not in statuses:
        return False

    # Product filter — item must match at least one product from any board mapping
    all_products = set()
    for mapping in config.get("boardMappings", []):
        all_products.update(mapping.get("products", []))

    if all_products:
        item_cats = set(item["products"]) | set(item["categories"])
        if not item_cats & all_products:
            return False

    # Exclude types
    exclude_types = global_filters.get("excludeTypes", [])
    if exclude_types:
        for ut in item["updateTypes"]:
            if ut in exclude_types:
                return False

    return True


def resolve_board(item: dict, config: dict) -> dict | None:
    """Find the first matching board mapping for an item. Returns the board config or defaultBoard."""
    item_cats = set(item["products"]) | set(item["categories"])

    for mapping in config.get("boardMappings", []):
        mapping_products = set(mapping.get("products", []))
        if item_cats & mapping_products:
            return {
                "boardName": mapping["name"],
                "ado": mapping["ado"],
            }

    default = config.get("defaultBoard")
    if default:
        return {
            "boardName": "Default",
            "ado": default["ado"],
        }

    return None


@app.route(route="fetch_roadmap", methods=["POST"])
def fetch_roadmap(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP-triggered function to fetch and filter roadmap items.

    Request body (JSON):
        config: The roadmap-sync-config.json content (object)
        daysBack: Number of days to look back (optional, default 7)

    Returns:
        JSON array of filtered items, each with a "board" field indicating
        the target ADO project/board.
    """
    logging.info("fetch_roadmap function triggered")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Request body must be valid JSON"}),
            status_code=400,
            mimetype="application/json",
        )

    config = body.get("config")
    if not config:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'config' in request body"}),
            status_code=400,
            mimetype="application/json",
        )

    days_back = body.get("daysBack", config.get("globalFilters", {}).get("daysBack", 7))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Fetch from configured feeds
    feeds = config.get("feeds", ["azure", "m365"])
    all_items = []
    errors = []

    for feed_name in feeds:
        feed_url = FEED_URLS.get(feed_name)
        if not feed_url:
            errors.append(f"Unknown feed: {feed_name}")
            continue
        try:
            items = fetch_feed(feed_url, feed_name)
            all_items.extend(items)
        except Exception as e:
            logging.warning("Failed to fetch %s feed: %s", feed_name, e)
            errors.append(f"Failed to fetch {feed_name}: {str(e)}")

    # Filter
    filtered = [item for item in all_items if matches_filter(item, config, cutoff)]

    # Resolve board mapping for each item
    results = []
    for item in filtered:
        board = resolve_board(item, config)
        if board:
            item["board"] = board
            results.append(item)

    # Sort by pubDate descending
    results.sort(key=lambda x: x.get("pubDate") or "", reverse=True)

    response = {
        "totalFetched": len(all_items),
        "totalFiltered": len(results),
        "cutoffDate": cutoff.isoformat(),
        "feeds": feeds,
        "errors": errors,
        "items": results,
    }

    return func.HttpResponse(
        json.dumps(response, default=str),
        mimetype="application/json",
    )
