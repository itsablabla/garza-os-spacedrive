#!/usr/bin/env python3
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PAGE_SIZE = 100
BLOCK_DEPTH_LIMIT = 2


def emit(operation):
    print(json.dumps(operation, ensure_ascii=False), flush=True)


def log(level, message):
    emit({"log": level, "message": message})


def parse_input():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        log("error", f"Invalid input JSON: {exc}")
        sys.exit(2)
    return payload.get("config", {}) or {}, payload.get("cursor")


def parse_csv(value):
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value, default):
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def notion_request(method, path, token, body=None):
    url = API_BASE + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Notion API {exc.code} {path}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Notion API request failed for {path}: {exc}") from exc


def paginate(method, path, token, body=None, result_key="results"):
    next_cursor = None
    while True:
        payload = dict(body or {})
        payload["page_size"] = PAGE_SIZE
        if next_cursor:
            payload["start_cursor"] = next_cursor
        if method == "POST":
            response = notion_request(method, path, token, payload)
        else:
            suffix = ("&" if "?" in path else "?") + f"page_size={PAGE_SIZE}"
            if next_cursor:
                suffix += f"&start_cursor={next_cursor}"
            response = notion_request(method, path + suffix, token)
        for item in response.get(result_key, []):
            yield item
        if not response.get("has_more"):
            break
        next_cursor = response.get("next_cursor")
        if not next_cursor:
            break


def rich_text_to_plain(items):
    parts = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = item.get("plain_text")
        if text:
            parts.append(text)
            continue
        if isinstance(item.get("text"), dict):
            content = item["text"].get("content")
            if content:
                parts.append(content)
    return "".join(parts).strip()


def normalize_text(text, limit=None):
    if not text:
        return ""
    text = str(text).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if limit and len(text) > limit:
        return text[:limit] + "..."
    return text


def serialize_for_text(value):
    if value in (None, ""):
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def extract_title(page):
    properties = page.get("properties") or {}
    for prop in properties.values():
        if not isinstance(prop, dict):
            continue
        if prop.get("type") == "title":
            return rich_text_to_plain(prop.get("title"))
    if page.get("title"):
        return rich_text_to_plain(page.get("title"))
    return "Untitled"


def property_text(prop):
    if not isinstance(prop, dict):
        return ""
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in {"title", "rich_text"}:
        return rich_text_to_plain(value)
    if kind == "select":
        return value.get("name", "") if isinstance(value, dict) else ""
    if kind == "multi_select":
        return ", ".join(v.get("name", "") for v in value or [] if isinstance(v, dict))
    if kind == "status":
        return value.get("name", "") if isinstance(value, dict) else ""
    if kind == "people":
        return ", ".join(v.get("name", v.get("id", "")) for v in value or [] if isinstance(v, dict))
    if kind == "relation":
        return ", ".join(v.get("id", "") for v in value or [] if isinstance(v, dict))
    if kind == "formula" and isinstance(value, dict):
        inner_type = value.get("type")
        return property_text({"type": inner_type, inner_type: value.get(inner_type)})
    if kind == "rollup":
        return serialize_for_text(value)
    if kind == "files":
        names = []
        for item in value or []:
            if isinstance(item, dict):
                names.append(item.get("name") or item.get("type") or "file")
        return ", ".join(names)
    if kind in {"checkbox"}:
        return "true" if value else "false"
    if kind in {"number"}:
        return "" if value is None else str(value)
    if kind in {"url", "email", "phone_number"}:
        return value or ""
    if kind in {"created_time", "last_edited_time", "date"}:
        if isinstance(value, dict):
            return value.get("start", "")
        return value or ""
    if kind in {"created_by", "last_edited_by"} and isinstance(value, dict):
        return value.get("name") or value.get("id", "")
    return serialize_for_text(value)


def properties_text(properties):
    lines = []
    for name, prop in (properties or {}).items():
        text = property_text(prop)
        if text:
            lines.append(f"{name}: {text}")
    return "\n".join(lines)


def user_name(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("id", "")
    return ""


def icon_text(value):
    if not isinstance(value, dict):
        return ""
    if value.get("type") == "emoji":
        return value.get("emoji", "")
    if value.get("type") in {"external", "file"}:
        inner = value.get(value.get("type"), {})
        if isinstance(inner, dict):
            return inner.get("url", "")
    return serialize_for_text(value)


def cover_text(value):
    if not isinstance(value, dict):
        return ""
    inner = value.get(value.get("type", ""), {})
    if isinstance(inner, dict):
        return inner.get("url", "")
    return serialize_for_text(value)


def block_plain_text(block):
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    payload = block.get(kind, {}) if kind else {}
    if not isinstance(payload, dict):
        return ""
    pieces = []
    if "rich_text" in payload:
        pieces.append(rich_text_to_plain(payload.get("rich_text")))
    if "caption" in payload:
        pieces.append(rich_text_to_plain(payload.get("caption")))
    for key in ("text", "title", "language", "expression", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            pieces.append(value)
    if kind == "child_page":
        pieces.append(payload.get("title", ""))
    if kind == "to_do" and payload.get("checked"):
        pieces.append("checked")
    return normalize_text("\n".join(part for part in pieces if part))


def fetch_blocks(token, block_id, depth=0):
    if depth > BLOCK_DEPTH_LIMIT:
        return []
    texts = []
    for block in paginate("GET", f"/blocks/{block_id}/children", token):
        text = block_plain_text(block)
        if text:
            texts.append(text)
        if block.get("has_children"):
            texts.extend(fetch_blocks(token, block.get("id"), depth + 1))
    return texts


def parent_info(page):
    parent = page.get("parent") or {}
    parent_type = parent.get("type", "")
    if not parent_type:
        return "", ""
    key = parent_type
    if key.endswith("_id"):
        return parent_type[:-3], parent.get(key, "")
    return parent_type, parent.get(parent_type, "")


def normalize_page_id(raw_id):
    return str(raw_id or "").strip()


def data_source_name(cache, page):
    data_source_id = cache.get(page.get("id"), "")
    if not data_source_id:
        parent = page.get("parent") or {}
        if parent.get("type") in {"database_id", "data_source_id"}:
            data_source_id = parent.get(parent["type"], "")
    return data_source_id


def fetch_data_source_items(token, data_source_id):
    path = f"/databases/{data_source_id}/query"
    try:
        return list(paginate("POST", path, token, body={}))
    except RuntimeError:
        path = f"/data-sources/{data_source_id}/query"
        return list(paginate("POST", path, token, body={}))


def retrieve_page(token, page_id):
    return notion_request("GET", f"/pages/{page_id}", token)


def main():
    config, cursor_text = parse_input()
    token = str(config.get("integration_token") or "").strip()
    if not token:
        log("error", "Missing required config: integration_token")
        sys.exit(2)

    include_archived = parse_bool(config.get("include_archived"), False)
    page_limit = parse_int(config.get("page_limit"), 250)
    title_query = str(config.get("title_query") or "").strip()
    page_ids = [normalize_page_id(item) for item in parse_csv(config.get("page_ids")) if normalize_page_id(item)]
    data_source_ids = [normalize_page_id(item) for item in parse_csv(config.get("data_source_ids")) if normalize_page_id(item)]

    try:
        cursor = json.loads(cursor_text) if cursor_text else {}
    except json.JSONDecodeError:
        cursor = {}
    if not isinstance(cursor, dict):
        cursor = {}
    prev_pages = cursor.get("pages") if isinstance(cursor.get("pages"), dict) else {}
    last_cursor_time = parse_iso(cursor.get("last_edited_time"))

    pages = {}
    data_source_names = {}

    try:
        for data_source_id in data_source_ids:
            items = fetch_data_source_items(token, data_source_id)
            data_source_names[data_source_id] = data_source_id
            for item in items:
                if isinstance(item, dict) and item.get("object") == "page":
                    pages[item.get("id")] = item
        if page_ids:
            for page_id in page_ids:
                page = retrieve_page(token, page_id)
                if isinstance(page, dict) and page.get("object") == "page":
                    pages[page.get("id")] = page
        search_body = {"filter": {"property": "object", "value": "page"}}
        if title_query:
            search_body["query"] = title_query
        for item in paginate("POST", "/search", token, body=search_body):
            if isinstance(item, dict) and item.get("object") == "page":
                pages[item.get("id")] = item
                if page_limit > 0 and len(pages) >= page_limit:
                    break
    except Exception as exc:
        log("error", str(exc))
        sys.exit(2)

    ordered_pages = sorted(
        [page for page in pages.values() if isinstance(page, dict)],
        key=lambda page: page.get("last_edited_time") or page.get("created_time") or "",
    )

    if page_limit > 0:
        ordered_pages = ordered_pages[:page_limit]

    synced = 0
    seen_ids = set()
    max_edited = cursor.get("last_edited_time") or ""
    for page in ordered_pages:
        page_id = page.get("id")
        if not page_id:
            continue
        last_edited = page.get("last_edited_time") or ""
        edited_dt = parse_iso(last_edited)
        if last_cursor_time and edited_dt and edited_dt < last_cursor_time and page_id not in page_ids:
            if title_query or data_source_ids:
                continue
        title = extract_title(page)
        body_parts = [properties_text(page.get("properties"))]
        try:
            body_parts.extend(fetch_blocks(token, page_id))
        except Exception as exc:
            log("warn", f"Failed to fetch blocks for page {page_id}: {exc}")
        body = normalize_text("\n\n".join(part for part in body_parts if part), 50000)
        snippet = normalize_text(body, 500)
        parent_type, parent_id = parent_info(page)
        parent = page.get("parent") or {}
        ds_id = ""
        if parent.get("type") in {"database_id", "data_source_id"}:
            ds_id = parent.get(parent["type"], "")
        archived = bool(page.get("archived") or False)
        trashed = bool(page.get("in_trash") or page.get("trashed") or False)
        locked = bool(page.get("is_locked") or False)

        if archived or trashed:
            if include_archived:
                pass
            else:
                emit({"delete": "page", "external_id": page_id})
                seen_ids.add(page_id)
                continue

        emit({
            "upsert": "page",
            "external_id": page_id,
            "fields": {
                "title": normalize_text(title, 1000),
                "body": body,
                "snippet": snippet,
                "url": str(page.get("url") or "")[:4000],
                "public_url": str(page.get("public_url") or "")[:4000],
                "parent_type": parent_type[:200],
                "parent_id": str(parent_id)[:500],
                "data_source_id": str(ds_id)[:500],
                "data_source_name": str(data_source_names.get(ds_id, ds_id))[:500],
                "created_by": user_name(page.get("created_by"))[:500],
                "last_edited_by": user_name(page.get("last_edited_by"))[:500],
                "created_time": page.get("created_time") or "",
                "last_edited_time": last_edited,
                "archived": archived,
                "trashed": trashed,
                "locked": locked,
                "icon": icon_text(page.get("icon"))[:4000],
                "cover": cover_text(page.get("cover"))[:4000],
                "properties": serialize_for_text(page.get("properties"))[:50000],
                "metadata": serialize_for_text({
                    "id": page.get("id"),
                    "object": page.get("object"),
                    "parent": page.get("parent"),
                    "object_type": page.get("object"),
                })[:50000],
            },
        })
        seen_ids.add(page_id)
        synced += 1
        if last_edited and last_edited > max_edited:
            max_edited = last_edited

    if not include_archived:
        for page_id in prev_pages.keys():
            if page_id not in seen_ids:
                emit({"delete": "page", "external_id": page_id})

    next_cursor = {
        "version": 1,
        "synced_at": iso_now(),
        "last_edited_time": max_edited,
        "pages": {page_id: {"last_edited_time": pages.get(page_id, {}).get("last_edited_time", "")} for page_id in seen_ids},
    }
    emit({"cursor": json.dumps(next_cursor, ensure_ascii=False, sort_keys=True)})
    log("info", f"Synced {synced} Notion pages")


if __name__ == "__main__":
    main()
