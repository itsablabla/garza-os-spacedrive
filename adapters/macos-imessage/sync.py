#!/usr/bin/env python3
import json
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
TEXT_LIMIT = 50000
SNIPPET_LIMIT = 500


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
    return [part.strip().lower() for part in str(value).split(",") if part.strip()]


def parse_int(value, default):
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_iso(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_text(value, limit=None):
    if value in (None, ""):
        return ""
    text = str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if limit and len(text) > limit:
        return text[:limit] + "..."
    return text


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def copy_live_db(db_path):
    expanded = os.path.expanduser(db_path)
    if not os.path.exists(expanded):
        raise FileNotFoundError(f"Messages database not found: {expanded}")
    temp_dir = tempfile.mkdtemp(prefix="spacedrive-imessage-")
    target_db = os.path.join(temp_dir, "chat.db")
    shutil.copy2(expanded, target_db)
    for suffix in ("-wal", "-shm"):
        source = expanded + suffix
        if os.path.exists(source):
            shutil.copy2(source, target_db + suffix)
    return temp_dir, target_db


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def columns_for(conn, table_name):
    try:
        rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {row[1] for row in rows}


def pick_column(columns, *candidates):
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def apple_time_to_iso(value):
    if value in (None, ""):
        return ""
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return ""
    if raw == 0:
        return ""
    if raw > 10**15:
        raw = raw / 1_000_000_000.0
    elif raw > 10**12:
        raw = raw / 1_000_000.0
    elif raw > 10**9:
        raw = raw / 1_000_000_000.0
    dt = APPLE_EPOCH + timedelta(seconds=raw)
    return dt.astimezone(timezone.utc).isoformat()


def attributed_body_to_text(value):
    if value in (None, b"", ""):
        return ""
    try:
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, str):
            return normalize_text(value, TEXT_LIMIT)
        if not isinstance(value, (bytes, bytearray)):
            return ""
        text = value.decode("utf-8", errors="ignore")
        if "NSString" in text:
            pieces = []
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("$") and "NSDictionary" not in line and "NSNumber" not in line:
                    pieces.append(line)
            candidate = " ".join(pieces)
            if candidate:
                return normalize_text(candidate, TEXT_LIMIT)
        try:
            plist = plistlib.loads(bytes(value))
            if isinstance(plist, dict):
                collected = []
                def walk(item):
                    if isinstance(item, str):
                        collected.append(item)
                    elif isinstance(item, dict):
                        for nested in item.values():
                            walk(nested)
                    elif isinstance(item, (list, tuple)):
                        for nested in item:
                            walk(nested)
                walk(plist)
                if collected:
                    return normalize_text(" ".join(collected), TEXT_LIMIT)
        except Exception:
            pass
        filtered = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in text)
        return normalize_text(filtered, TEXT_LIMIT)
    except Exception:
        return ""


def json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:TEXT_LIMIT]
    except TypeError:
        return normalize_text(str(value), TEXT_LIMIT)


def build_queries(conn):
    queries = {}
    msg_cols = columns_for(conn, "message")
    chat_cols = columns_for(conn, "chat")
    handle_cols = columns_for(conn, "handle")
    att_cols = columns_for(conn, "attachment")

    queries["message_date"] = pick_column(msg_cols, "date", "date_created", "time")
    queries["message_text"] = pick_column(msg_cols, "text")
    queries["message_attributed"] = pick_column(msg_cols, "attributedBody", "attributedbody")
    queries["message_guid"] = pick_column(msg_cols, "guid", "message_guid")
    queries["message_service"] = pick_column(msg_cols, "service", "service_name")
    queries["message_edited"] = pick_column(msg_cols, "date_edited", "date_read")
    queries["message_assoc"] = pick_column(msg_cols, "associated_message_guid", "thread_originator_guid")
    queries["message_cache_room"] = pick_column(msg_cols, "cache_roomnames")
    queries["message_type"] = pick_column(msg_cols, "item_type", "message_action_type")
    queries["message_is_from_me"] = pick_column(msg_cols, "is_from_me")
    queries["message_is_read"] = pick_column(msg_cols, "is_read", "date_read")
    queries["message_handle_id"] = pick_column(msg_cols, "handle_id")

    queries["chat_guid"] = pick_column(chat_cols, "guid")
    queries["chat_name"] = pick_column(chat_cols, "display_name", "chat_identifier")
    queries["chat_service"] = pick_column(chat_cols, "service_name", "service")
    queries["chat_last_addressed"] = pick_column(chat_cols, "last_addressed_handle")

    queries["handle_id"] = pick_column(handle_cols, "id", "uncanonicalized_id")
    queries["handle_service"] = pick_column(handle_cols, "service")
    queries["handle_country"] = pick_column(handle_cols, "country")
    queries["handle_person"] = pick_column(handle_cols, "person_centric_id")

    queries["attachment_guid"] = pick_column(att_cols, "guid")
    queries["attachment_filename"] = pick_column(att_cols, "filename", "transfer_name")
    queries["attachment_mime"] = pick_column(att_cols, "mime_type")
    queries["attachment_bytes"] = pick_column(att_cols, "total_bytes")
    queries["attachment_created"] = pick_column(att_cols, "created_date", "start_date")

    return queries


def fetch_handles(conn, queries):
    if not table_exists(conn, "handle"):
        return {}
    handle_cols = columns_for(conn, "handle")
    select = ["ROWID as row_id"]
    for alias, column in [
        ("handle", queries["handle_id"]),
        ("service", queries["handle_service"]),
        ("country", queries["handle_country"]),
        ("person", queries["handle_person"]),
    ]:
        if column:
            select.append(f'"{column}" as "{alias}"')
        else:
            select.append(f"NULL as {alias}")
    sql = f"SELECT {', '.join(select)} FROM handle"
    rows = conn.execute(sql).fetchall()
    handles = {}
    for row in rows:
        handle = normalize_text(row["handle"], 500)
        handles[row["row_id"]] = {
            "external_id": handle or f"handle:{row['row_id']}",
            "handle": handle,
            "display_name": handle,
            "service": normalize_text(row["service"], 200),
            "country": normalize_text(row["country"], 200),
            "account": normalize_text(row["person"], 200),
            "metadata": json_text({"row_id": row["row_id"]}),
        }
    return handles


def fetch_chats(conn, queries):
    if not table_exists(conn, "chat"):
        return {}
    select = ["ROWID as row_id"]
    for alias, column in [
        ("guid", queries["chat_guid"]),
        ("name", queries["chat_name"]),
        ("service", queries["chat_service"]),
        ("last_addressed", queries["chat_last_addressed"]),
    ]:
        if column:
            select.append(f'"{column}" as "{alias}"')
        else:
            select.append(f"NULL as {alias}")
    sql = f"SELECT {', '.join(select)} FROM chat"
    rows = conn.execute(sql).fetchall()
    chats = {}
    for row in rows:
        guid = normalize_text(row["guid"], 500) or f"chat:{row['row_id']}"
        chats[row["row_id"]] = {
            "external_id": guid,
            "guid": guid,
            "display_name": normalize_text(row["name"], 1000),
            "service_name": normalize_text(row["service"], 200),
            "last_message_at": "",
            "participant_ids": set(),
            "participant_count": 0,
            "metadata": {"row_id": row["row_id"], "last_addressed_handle": row["last_addressed"]},
        }
    return chats


def fetch_chat_participants(conn, chats, handles):
    if not table_exists(conn, "chat_handle_join"):
        return
    rows = conn.execute("SELECT chat_id, handle_id FROM chat_handle_join").fetchall()
    for row in rows:
        chat = chats.get(row["chat_id"])
        handle = handles.get(row["handle_id"])
        if not chat or not handle:
            continue
        chat["participant_ids"].add(handle["external_id"])


def fetch_attachment_links(conn, queries):
    if not table_exists(conn, "message_attachment_join") or not table_exists(conn, "attachment"):
        return {}
    select = ["maj.message_id as message_id", "a.ROWID as attachment_rowid"]
    for alias, column in [
        ("guid", queries["attachment_guid"]),
        ("filename", queries["attachment_filename"]),
        ("mime_type", queries["attachment_mime"]),
        ("total_bytes", queries["attachment_bytes"]),
        ("created", queries["attachment_created"]),
    ]:
        if column:
            select.append(f'a."{column}" as "{alias}"')
        else:
            select.append(f"NULL as {alias}")
    sql = f"SELECT {', '.join(select)} FROM message_attachment_join maj JOIN attachment a ON a.ROWID = maj.attachment_id"
    rows = conn.execute(sql).fetchall()
    result = {}
    for row in rows:
        guid = normalize_text(row["guid"], 500) or f"attachment:{row['attachment_rowid']}"
        filename = normalize_text(row["filename"], 2000)
        if filename.startswith("~/"):
            filename = os.path.expanduser(filename)
        result.setdefault(row["message_id"], []).append({
            "external_id": guid,
            "filename": os.path.basename(filename) if filename else guid,
            "path": filename,
            "mime_type": normalize_text(row["mime_type"], 200),
            "transfer_name": os.path.basename(filename) if filename else "",
            "total_bytes": parse_int(row["total_bytes"], 0),
            "created_at": apple_time_to_iso(row["created"]),
            "metadata": json_text({"attachment_rowid": row["attachment_rowid"]}),
        })
    return result


def main():
    config, cursor_text = parse_input()
    db_path = str(config.get("chat_db_path") or "").strip()
    if not db_path:
        log("error", "Missing required config: chat_db_path")
        sys.exit(2)
    attachments_root = os.path.expanduser(str(config.get("attachments_root") or "~/Library/Messages/Attachments"))
    convo_filter = set(parse_csv(config.get("conversation_filter")))
    participant_filter = set(parse_csv(config.get("participant_filter")))
    date_from = parse_iso(config.get("date_from"))
    date_to = parse_iso(config.get("date_to"))
    max_messages = parse_int(config.get("max_messages"), 5000)

    try:
        cursor = json.loads(cursor_text) if cursor_text else {}
    except json.JSONDecodeError:
        cursor = {}
    if not isinstance(cursor, dict):
        cursor = {}
    last_synced = parse_iso(cursor.get("last_timestamp"))

    temp_dir = None
    try:
        temp_dir, snapshot_path = copy_live_db(db_path)
        conn = sqlite3.connect(snapshot_path)
        conn.row_factory = sqlite3.Row
        queries = build_queries(conn)
        handles = fetch_handles(conn, queries)
        chats = fetch_chats(conn, queries)
        fetch_chat_participants(conn, chats, handles)
        attachment_links = fetch_attachment_links(conn, queries)

        if not table_exists(conn, "message"):
            log("error", "Messages schema missing message table")
            sys.exit(2)

        select = ["m.ROWID as row_id"]
        for alias, column in [
            ("guid", queries["message_guid"]),
            ("text", queries["message_text"]),
            ("attributed_body", queries["message_attributed"]),
            ("date_value", queries["message_date"]),
            ("edited_value", queries["message_edited"]),
            ("service", queries["message_service"]),
            ("assoc_guid", queries["message_assoc"]),
            ("cache_roomnames", queries["message_cache_room"]),
            ("item_type", queries["message_type"]),
            ("is_from_me", queries["message_is_from_me"]),
            ("is_read", queries["message_is_read"]),
            ("handle_id", queries["message_handle_id"]),
        ]:
            if column:
                select.append(f'm."{column}" as "{alias}"')
            else:
                select.append(f"NULL as {alias}")
        join_sql = ""
        if table_exists(conn, "chat_message_join"):
            select.append("cmj.chat_id as chat_id")
            join_sql += " LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID"
        else:
            select.append("NULL as chat_id")
        sql = f"SELECT {', '.join(select)} FROM message m{join_sql}"
        rows = conn.execute(sql).fetchall()

        filtered = []
        for row in rows:
            ts = apple_time_to_iso(row["date_value"])
            dt = parse_iso(ts)
            if dt and date_from and dt < date_from:
                continue
            if dt and date_to and dt > date_to:
                continue
            if dt and last_synced and dt < last_synced and max_messages <= 0:
                continue
            filtered.append((dt or APPLE_EPOCH, row, ts))
        filtered.sort(key=lambda item: item[0])
        if max_messages > 0:
            filtered = filtered[-max_messages:]

        seen_participants = set()
        seen_chats = set()
        seen_messages = []
        latest_timestamp = cursor.get("last_timestamp") or ""

        for _dt, row, ts in filtered:
            chat = chats.get(row["chat_id"])
            if not chat:
                fallback_guid = normalize_text(row["cache_roomnames"], 500)
                if fallback_guid:
                    chat = {
                        "external_id": fallback_guid,
                        "guid": fallback_guid,
                        "display_name": fallback_guid,
                        "service_name": normalize_text(row["service"], 200),
                        "last_message_at": "",
                        "participant_ids": set(),
                        "participant_count": 0,
                        "metadata": {"synthetic": True},
                    }
                    chats[row["chat_id"]] = chat
            if not chat:
                continue

            participant = handles.get(row["handle_id"])
            if not participant and not row["is_from_me"]:
                handle_text = normalize_text(row["handle_id"], 500)
                if handle_text:
                    participant = {
                        "external_id": handle_text,
                        "handle": handle_text,
                        "display_name": handle_text,
                        "service": normalize_text(row["service"], 200),
                        "country": "",
                        "account": "",
                        "metadata": json_text({"synthetic": True}),
                    }
                    handles[row["handle_id"]] = participant
            if participant:
                chat["participant_ids"].add(participant["external_id"])

            chat_name = chat["display_name"] or chat["guid"]
            message_guid = normalize_text(row["guid"], 500) or f"message:{row['row_id']}"
            sender = "Me" if row["is_from_me"] else (participant["display_name"] if participant else "Unknown")

            if convo_filter:
                convo_values = {chat_name.lower(), chat["guid"].lower()}
                if not convo_values.intersection(convo_filter):
                    continue
            if participant_filter and not row["is_from_me"]:
                participant_values = set()
                if participant:
                    participant_values = {participant["handle"].lower(), participant["display_name"].lower()}
                if not participant_values.intersection(participant_filter):
                    continue

            text = normalize_text(row["text"], TEXT_LIMIT)
            if not text:
                text = attributed_body_to_text(row["attributed_body"])
            attachments = attachment_links.get(row["row_id"], [])
            reply_to = normalize_text(row["assoc_guid"], 500)
            if reply_to and not reply_to.startswith("p:"):
                reply_to = reply_to
            elif reply_to.startswith("p:"):
                reply_to = reply_to[2:]

            emit({
                "upsert": "chat",
                "external_id": chat["external_id"],
                "fields": {
                    "name": chat_name[:1000],
                    "display_name": chat_name[:1000],
                    "guid": chat["guid"][:500],
                    "service_name": chat["service_name"][:200],
                    "is_group": len(chat["participant_ids"]) > 1,
                    "last_message_at": ts,
                    "participant_count": len(chat["participant_ids"]),
                    "metadata": json_text(chat["metadata"]),
                },
            })
            seen_chats.add(chat["external_id"])

            if participant:
                emit({
                    "upsert": "participant",
                    "external_id": participant["external_id"],
                    "fields": {
                        "handle": participant["handle"][:500],
                        "display_name": participant["display_name"][:500],
                        "service": participant["service"][:200],
                        "country": participant["country"][:200],
                        "account": participant["account"][:200],
                        "metadata": participant["metadata"],
                    },
                })
                emit({"link": "chat", "id": chat["external_id"], "to": "participant", "to_id": participant["external_id"]})
                seen_participants.add(participant["external_id"])

            emit({
                "upsert": "message",
                "external_id": message_guid,
                "fields": {
                    "text": text,
                    "snippet": normalize_text(text, SNIPPET_LIMIT),
                    "sender": sender[:500],
                    "chat_name": chat_name[:1000],
                    "service": normalize_text(row["service"], 200),
                    "timestamp": ts,
                    "edited_at": apple_time_to_iso(row["edited_value"]),
                    "is_from_me": bool(row["is_from_me"]),
                    "is_read": bool(row["is_read"]),
                    "item_type": normalize_text(row["item_type"], 200) or "message",
                    "associated_guid": normalize_text(row["assoc_guid"], 500),
                    "reply_to": reply_to[:500],
                    "attachments": json_text([att["filename"] for att in attachments]),
                    "metadata": json_text({
                        "row_id": row["row_id"],
                        "cache_roomnames": row["cache_roomnames"],
                    }),
                    "chat_id": chat["external_id"],
                    "participant_id": participant["external_id"] if participant else None,
                },
            })
            seen_messages.append(message_guid)
            if ts and ts > latest_timestamp:
                latest_timestamp = ts

            for attachment in attachments:
                path = attachment["path"]
                if path and path.startswith("~/"):
                    path = os.path.expanduser(path)
                if path and attachments_root and path.startswith(os.path.expanduser("~/Library/Messages/Attachments")):
                    path = path
                emit({
                    "upsert": "attachment",
                    "external_id": attachment["external_id"],
                    "fields": {
                        "filename": attachment["filename"],
                        "path": path[:4000] if path else "",
                        "mime_type": attachment["mime_type"],
                        "transfer_name": attachment["transfer_name"],
                        "total_bytes": attachment["total_bytes"],
                        "created_at": attachment["created_at"],
                        "metadata": attachment["metadata"],
                        "message_id": message_guid,
                    },
                })

        next_cursor = {
            "version": 1,
            "synced_at": iso_now(),
            "last_timestamp": latest_timestamp,
            "message_count": len(seen_messages),
        }
        emit({"cursor": json.dumps(next_cursor, ensure_ascii=False, sort_keys=True)})
        log("info", f"Synced {len(seen_chats)} chats, {len(seen_participants)} participants, {len(seen_messages)} messages")
    except Exception as exc:
        log("error", str(exc))
        sys.exit(2)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
