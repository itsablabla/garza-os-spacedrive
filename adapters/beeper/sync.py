#!/usr/bin/env python3
import json
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone


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


def normalize_time(value):
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        if value > 10**12:
            value = value / 1000.0
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    text = str(value).strip()
    parsed = parse_iso(text)
    if parsed:
        return parsed.isoformat()
    try:
        num = float(text)
        if num > 10**12:
            num = num / 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def json_text(value):
    if value in (None, ""):
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:50000]
    except TypeError:
        return str(value)[:50000]


def request_json(base_url, path, token, timeout, params=None):
    url = base_url.rstrip("/") + path
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if query:
            url = f"{url}?{query}"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {path}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed for {path}: {exc}") from exc


def as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "data", "results", "chats", "messages", "accounts"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return []


def lower_texts(value):
    return {item.lower() for item in parse_csv(value)}


def pick(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def chat_external_id(chat):
    return str(pick(chat.get("id"), chat.get("chat_id"), chat.get("guid"), chat.get("mxid")))


def account_external_id(account):
    return str(pick(account.get("id"), account.get("account_id"), account.get("bridge_id"), account.get("user_id")))


def participant_external_id(raw):
    return str(pick(raw.get("id"), raw.get("user_id"), raw.get("mxid"), raw.get("handle"), raw.get("address"), raw.get("identifier")))


def message_external_id(chat_id, raw):
    base = pick(raw.get("id"), raw.get("message_id"), raw.get("guid"), raw.get("event_id"), raw.get("ts"), raw.get("timestamp"))
    return f"{chat_id}:{base}"


def normalize_text(value):
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value.replace("\x00", "").strip()[:50000]
    return json_text(value)


def account_matches(account, account_filter, network_filter):
    if account_filter:
        values = {
            str(pick(account.get("id"), "")).lower(),
            str(pick(account.get("account_id"), "")).lower(),
            str(pick(account.get("bridge_id"), "")).lower(),
            str(pick(account.get("service_name"), account.get("network"), "")).lower(),
        }
        if not values.intersection(account_filter):
            return False
    if network_filter:
        values = {
            str(pick(account.get("network"), "")).lower(),
            str(pick(account.get("service_name"), "")).lower(),
            str(pick(account.get("bridge_id"), "")).lower(),
        }
        if not values.intersection(network_filter):
            return False
    return True


def chat_matches(chat, chat_filter, type_filter, network_filter, account_ids):
    if account_ids:
        acct = str(pick(chat.get("account_id"), chat.get("account", {}).get("id"), ""))
        if acct and acct not in account_ids:
            return False
    if chat_filter:
        values = {
            str(pick(chat.get("id"), "")).lower(),
            str(pick(chat.get("name"), "")).lower(),
            str(pick(chat.get("display_name"), "")).lower(),
            str(pick(chat.get("normalized_name"), "")).lower(),
        }
        if not values.intersection(chat_filter):
            return False
    if type_filter:
        kind = str(pick(chat.get("type"), chat.get("chat_type"), "")).lower()
        if kind and kind not in type_filter:
            return False
    if network_filter:
        values = {
            str(pick(chat.get("network"), "")).lower(),
            str(pick(chat.get("service_name"), "")).lower(),
        }
        if values.intersection(network_filter):
            return True
        return not any(values)
    return True


def participant_record(raw, chat, account):
    pid = participant_external_id(raw)
    if not pid:
        return None
    metadata = dict(raw)
    return pid, {
        "display_name": str(pick(raw.get("display_name"), raw.get("name"), raw.get("full_name"), pid))[:500],
        "handle": str(pick(raw.get("handle"), raw.get("username"), raw.get("mxid"), raw.get("address"), pid))[:500],
        "network": str(pick(raw.get("network"), chat.get("network"), account.get("network"), ""))[:200],
        "avatar_url": str(pick(raw.get("avatar_url"), raw.get("avatar"), raw.get("photo_url"), ""))[:2000],
        "is_self": bool(pick(raw.get("is_self"), raw.get("self"), False)),
        "metadata": json_text(metadata),
    }


def main():
    config, cursor_text = parse_input()
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        log("error", "Missing required config: base_url")
        sys.exit(2)

    timeout = parse_int(config.get("timeout_seconds"), 30)
    token = str(config.get("bearer_token") or "").strip()
    account_filter = lower_texts(config.get("account_filter"))
    network_filter = lower_texts(config.get("network_filter"))
    chat_filter = lower_texts(config.get("chat_filter"))
    type_filter = lower_texts(config.get("chat_type_filter"))
    date_from = parse_iso(config.get("date_from"))
    date_to = parse_iso(config.get("date_to"))
    chat_limit = parse_int(config.get("chat_limit"), 0)
    message_limit = parse_int(config.get("message_limit"), 500)

    try:
        cursor = json.loads(cursor_text) if cursor_text else {}
    except json.JSONDecodeError:
        cursor = {}
    if not isinstance(cursor, dict):
        cursor = {}
    chat_state = cursor.get("chats") if isinstance(cursor.get("chats"), dict) else {}

    try:
        accounts_raw = request_json(base_url, "/v1/accounts", token, timeout)
        accounts = as_list(accounts_raw)
    except Exception as exc:
        log("error", str(exc))
        sys.exit(2)

    accounts_by_id = {}
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if not account_matches(account, account_filter, network_filter):
            continue
        account_id = account_external_id(account)
        if not account_id:
            continue
        accounts_by_id[account_id] = account
        emit({
            "upsert": "account",
            "external_id": account_id,
            "fields": {
                "name": str(pick(account.get("name"), account.get("display_name"), account.get("user_id"), account_id))[:500],
                "network": str(pick(account.get("network"), account.get("service_name"), account.get("bridge_id"), ""))[:200],
                "account_id": str(pick(account.get("account_id"), account.get("id"), account_id))[:500],
                "bridge_id": str(pick(account.get("bridge_id"), account.get("bridge"), ""))[:500],
                "status": str(pick(account.get("status"), account.get("state"), ""))[:200],
                "user_id": str(pick(account.get("user_id"), account.get("mxid"), account.get("remote_id"), ""))[:500],
                "metadata": json_text(account),
            },
        })

    try:
        chats_raw = request_json(base_url, "/v1/chats", token, timeout)
        chats = as_list(chats_raw)
    except Exception as exc:
        log("error", str(exc))
        sys.exit(2)

    selected_chats = []
    account_ids = set(accounts_by_id.keys()) if accounts_by_id else set()
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        if not chat_matches(chat, chat_filter, type_filter, network_filter, account_ids):
            continue
        selected_chats.append(chat)
        if chat_limit > 0 and len(selected_chats) >= chat_limit:
            break

    total_messages = 0
    new_state = {"version": 1, "synced_at": iso_now(), "chats": {}}

    for chat in selected_chats:
        chat_id = chat_external_id(chat)
        if not chat_id:
            continue
        chat_detail = chat
        try:
            detail = request_json(base_url, f"/v1/chats/{urllib.parse.quote(chat_id, safe='')}", token, timeout)
            if isinstance(detail, dict):
                chat_detail = detail
        except Exception as exc:
            log("warn", f"Failed to fetch chat {chat_id}: {exc}")

        account_id = str(pick(chat_detail.get("account_id"), chat_detail.get("account", {}).get("id"), chat.get("account_id"), ""))
        account = accounts_by_id.get(account_id, {}) if account_id else {}
        last_activity = normalize_time(pick(chat_detail.get("last_activity_at"), chat_detail.get("updated_at"), chat_detail.get("timestamp"), chat_detail.get("last_message_ts")))

        emit({
            "upsert": "chat",
            "external_id": chat_id,
            "fields": {
                "name": str(pick(chat_detail.get("name"), chat_detail.get("display_name"), chat_id))[:1000],
                "normalized_name": str(pick(chat_detail.get("normalized_name"), chat_detail.get("slug"), "")).lower()[:1000],
                "chat_type": str(pick(chat_detail.get("type"), chat_detail.get("chat_type"), "unknown"))[:200],
                "network": str(pick(chat_detail.get("network"), account.get("network"), chat_detail.get("service_name"), ""))[:200],
                "topic": normalize_text(pick(chat_detail.get("topic"), chat_detail.get("description"), ""))[:5000],
                "account_id": account_id,
                "last_activity_at": last_activity,
                "unread_count": parse_int(pick(chat_detail.get("unread_count"), chat_detail.get("unread"), 0), 0),
                "is_archived": bool(pick(chat_detail.get("archived"), chat_detail.get("is_archived"), False)),
                "metadata": json_text(chat_detail),
            },
        })

        participants = []
        for key in ("participants", "members", "users"):
            value = chat_detail.get(key)
            if isinstance(value, list):
                participants = value
                break
        linked_participants = set()
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            record = participant_record(participant, chat_detail, account)
            if not record:
                continue
            participant_id, fields = record
            emit({"upsert": "participant", "external_id": participant_id, "fields": fields})
            if participant_id not in linked_participants:
                emit({"link": "chat", "id": chat_id, "to": "participant", "to_id": participant_id})
                linked_participants.add(participant_id)

        state = chat_state.get(chat_id, {}) if isinstance(chat_state.get(chat_id), dict) else {}
        watermark = normalize_time(state.get("watermark"))
        params = {}
        if message_limit > 0:
            params["limit"] = message_limit
        if watermark:
            params["since"] = watermark
        try:
            messages_raw = request_json(base_url, f"/v1/chats/{urllib.parse.quote(chat_id, safe='')}/messages", token, timeout, params=params)
            messages = as_list(messages_raw)
        except Exception as exc:
            log("warn", f"Failed to fetch messages for {chat_id}: {exc}")
            new_state["chats"][chat_id] = {
                "watermark": last_activity or watermark,
                "last_chat_activity": last_activity,
            }
            continue

        known_message_ids = set()
        latest_seen = watermark or last_activity
        sorted_messages = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            msg_time = normalize_time(pick(message.get("created_at"), message.get("timestamp"), message.get("ts"), message.get("sent_at")))
            dt = parse_iso(msg_time) if msg_time else None
            if date_from and dt and dt < date_from:
                continue
            if date_to and dt and dt > date_to:
                continue
            sorted_messages.append((dt or datetime.min.replace(tzinfo=timezone.utc), message, msg_time))
        sorted_messages.sort(key=lambda item: item[0])

        for _dt, message, msg_time in sorted_messages:
            msg_id = message_external_id(chat_id, message)
            if not msg_id:
                continue
            known_message_ids.add(msg_id)
            latest_seen = msg_time or latest_seen

            sender_info = pick(message.get("sender"), message.get("author"), message.get("user"), {})
            sender_name = ""
            participant_id = ""
            if isinstance(sender_info, dict):
                record = participant_record(sender_info, chat_detail, account)
                if record:
                    participant_id, participant_fields = record
                    emit({"upsert": "participant", "external_id": participant_id, "fields": participant_fields})
                    if participant_id not in linked_participants:
                        emit({"link": "chat", "id": chat_id, "to": "participant", "to_id": participant_id})
                        linked_participants.add(participant_id)
                    sender_name = participant_fields.get("display_name") or participant_fields.get("handle") or participant_id
            else:
                participant_id = str(sender_info or "")
                sender_name = participant_id

            text = normalize_text(pick(message.get("text"), message.get("body"), message.get("content"), ""))
            snippet = text[:500]
            attachments = pick(message.get("attachments"), message.get("files"), [])
            reply_to_raw = pick(message.get("reply_to"), message.get("parent_id"), message.get("thread_parent_id"), "")
            reply_to = f"{chat_id}:{reply_to_raw}" if reply_to_raw else ""
            if reply_to and reply_to not in known_message_ids:
                reply_to = ""
            permalink = str(pick(message.get("permalink"), message.get("url"), ""))[:4000]

            emit({
                "upsert": "message",
                "external_id": msg_id,
                "fields": {
                    "text": text,
                    "snippet": snippet,
                    "sender": str(sender_name)[:500],
                    "chat_name": str(pick(chat_detail.get("name"), chat_detail.get("display_name"), chat_id))[:1000],
                    "network": str(pick(chat_detail.get("network"), account.get("network"), ""))[:200],
                    "timestamp": msg_time,
                    "edited_at": normalize_time(pick(message.get("edited_at"), message.get("updated_at"), "")),
                    "status": str(pick(message.get("status"), message.get("delivery_status"), ""))[:200],
                    "message_type": str(pick(message.get("type"), message.get("msgtype"), "message"))[:200],
                    "reply_to": reply_to,
                    "attachments": json_text(attachments),
                    "metadata": json_text(message),
                    "permalink": permalink,
                    "chat_id": chat_id,
                    "participant_id": participant_id,
                },
            })
            total_messages += 1

        new_state["chats"][chat_id] = {
            "watermark": latest_seen or last_activity or watermark,
            "last_chat_activity": last_activity,
        }

    emit({"cursor": json.dumps(new_state, ensure_ascii=False, sort_keys=True)})
    log("info", f"Synced {len(accounts_by_id)} accounts, {len(selected_chats)} chats, {total_messages} messages")


if __name__ == "__main__":
    main()
