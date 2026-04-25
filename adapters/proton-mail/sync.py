#!/usr/bin/env python3
"""
Proton Mail archive adapter for Spacedrive.

Fetches mail through Proton Mail Bridge or any IMAP-compatible endpoint using
Python stdlib only. The adapter emits thread, message, label, and attachment
records over the archive JSONL protocol.

Cursor strategy (v1):
- Tracks per-mailbox UIDVALIDITY + highest synced UID.
- Replays a small trailing UID window on incremental sync to refresh recent
  flags, labels, and metadata changes.
- Does not yet detect deletions or older label changes outside that window.
"""

import base64
import binascii
import email
import email.header
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import re
import ssl
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

BODY_LIMIT = 50000
SNIPPET_LIMIT = 500
REFRESH_WINDOW = 50
DEFAULT_MAILBOX = "All Mail"
CURSOR_VERSION = 1


def emit(operation: dict):
    print(json.dumps(operation, ensure_ascii=False), flush=True)


def log(level: str, message: str):
    emit({"log": level, "message": message})


def parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_int(value, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def sanitize_text(value: str, limit: Optional[int] = None) -> str:
    if not value:
        return ""
    value = value.replace("\x00", "")
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if limit and len(value) > limit:
        return value[:limit] + "..."
    return value


def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    parts = []
    for part, charset in email.header.decode_header(value):
        if isinstance(part, bytes):
            try:
                parts.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(part.decode("utf-8", errors="replace"))
        else:
            parts.append(part)
    return sanitize_text("".join(parts))


def header_value(message: email.message.Message, name: str) -> str:
    return decode_mime_header(message.get(name, ""))


def parse_address_list(value: str) -> str:
    if not value:
        return ""
    addresses = []
    for display_name, addr in email.utils.getaddresses([value]):
        display_name = decode_mime_header(display_name)
        if display_name and addr:
            addresses.append(f"{display_name} <{addr}>")
        elif addr:
            addresses.append(addr)
        elif display_name:
            addresses.append(display_name)
    return ", ".join(addresses)


def normalize_message_id(value: str) -> str:
    value = sanitize_text(value)
    if not value:
        return ""
    return value.strip().strip("<>").strip().lower()


def stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(part for part in parts if part is not None)
    digest = hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}:{digest}"


def canonical_subject(subject: str) -> str:
    subject = sanitize_text(subject).lower()
    while True:
        updated = re.sub(r"^(re|fwd?|aw|sv):\s*", "", subject, flags=re.IGNORECASE)
        if updated == subject:
            return subject
        subject = updated


def parse_date(value: str, fallback: Optional[str] = None) -> str:
    if value:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    if fallback:
        return fallback
    return datetime.now(timezone.utc).isoformat()


def parse_internaldate(value: str) -> Optional[str]:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%d-%b-%Y %H:%M:%S %z")
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def extract_text_and_attachments(message: email.message.Message) -> Tuple[str, List[dict]]:
    plain_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[dict] = []

    for part in message.walk():
        if part.is_multipart():
            continue

        disposition = (part.get_content_disposition() or "").lower()
        filename = decode_mime_header(part.get_filename() or "")
        content_type = part.get_content_type() or "application/octet-stream"
        payload = part.get_payload(decode=True) or b""
        content_id = sanitize_text(part.get("Content-ID", "")).strip("<>")

        is_inline_attachment = disposition == "inline" and content_type not in {"text/plain", "text/html"}
        if filename or disposition == "attachment" or is_inline_attachment:
            attachments.append(
                {
                    "filename": filename or "attachment",
                    "mime_type": content_type,
                    "size": len(payload),
                    "content_id": content_id,
                    "is_inline": disposition == "inline",
                }
            )
            continue

        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")

        if content_type == "text/plain":
            plain_parts.append(text)
        elif content_type == "text/html":
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            html_parts.append(text)

    body = "\n\n".join(part for part in plain_parts if part.strip())
    if not body:
        body = "\n\n".join(part for part in html_parts if part.strip())

    return sanitize_text(body, BODY_LIMIT), attachments


def message_snippet(body: str, fallback: str) -> str:
    return sanitize_text(body or fallback or "", SNIPPET_LIMIT)


def encode_mailbox(mailbox: str) -> str:
    result = []
    buffer = []

    def flush_buffer():
        if not buffer:
            return
        text = "".join(buffer)
        utf16 = text.encode("utf-16-be")
        encoded = base64.b64encode(utf16).decode("ascii").rstrip("=").replace("/", ",")
        result.append("&" + encoded + "-")
        buffer.clear()

    for char in mailbox:
        code = ord(char)
        if 0x20 <= code <= 0x7E and char != "&":
            flush_buffer()
            result.append(char)
        elif char == "&":
            flush_buffer()
            result.append("&-")
        else:
            buffer.append(char)

    flush_buffer()
    return "".join(result)


def parse_mailboxes(config: dict) -> List[str]:
    raw = sanitize_text(str(config.get("mailboxes", "") or ""))
    if not raw:
        return [DEFAULT_MAILBOX]
    mailboxes = [item.strip() for item in raw.split(",") if item.strip()]
    return mailboxes or [DEFAULT_MAILBOX]


def parse_cursor(raw_cursor) -> dict:
    if not raw_cursor:
        return {"version": CURSOR_VERSION, "mailboxes": {}}
    try:
        data = json.loads(raw_cursor)
    except (TypeError, ValueError):
        log("warn", "Ignoring legacy or invalid cursor state")
        return {"version": CURSOR_VERSION, "mailboxes": {}}

    if not isinstance(data, dict):
        return {"version": CURSOR_VERSION, "mailboxes": {}}

    version = data.get("version")
    mailboxes = data.get("mailboxes")
    if version != CURSOR_VERSION or not isinstance(mailboxes, dict):
        log("warn", "Ignoring cursor with incompatible schema version")
        return {"version": CURSOR_VERSION, "mailboxes": {}}

    return {"version": CURSOR_VERSION, "mailboxes": mailboxes}


def make_cursor(mailbox_state: Dict[str, dict]) -> str:
    return json.dumps({"version": CURSOR_VERSION, "mailboxes": mailbox_state}, sort_keys=True)


def create_ssl_context(verify: bool) -> ssl.SSLContext:
    return ssl.create_default_context() if verify else ssl._create_unverified_context()


def connect_imap(config: dict):
    host = sanitize_text(str(config.get("host", "") or ""))
    username = sanitize_text(str(config.get("username", "") or ""))
    password = str(config.get("password", "") or "")
    port = parse_int(config.get("port"), 1143)
    tls_mode = sanitize_text(str(config.get("tls_mode", "starttls") or "starttls")).lower()
    tls_verify = parse_bool(config.get("tls_verify"), False)

    if not host:
        raise ValueError("Missing required config: host")
    if not username:
        raise ValueError("Missing required config: username")
    if not password:
        raise ValueError("Missing required config: password")
    if tls_mode not in {"starttls", "ssl", "none"}:
        raise ValueError("tls_mode must be one of: starttls, ssl, none")

    context = create_ssl_context(tls_verify)

    if tls_mode == "ssl":
        client = imaplib.IMAP4_SSL(host, port, ssl_context=context)
    else:
        client = imaplib.IMAP4(host, port)
        if tls_mode == "starttls":
            client.starttls(context)

    status, _ = client.login(username, password)
    if status != "OK":
        raise RuntimeError("IMAP login failed")

    return client, {
        "host": host,
        "port": port,
        "username": username,
        "tls_mode": tls_mode,
        "tls_verify": tls_verify,
    }


def require_ok(status: str, data, action: str):
    if status == "OK":
        return
    detail = ""
    if data:
        detail = " ".join(
            item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
            for item in data
            if item is not None
        )
    raise RuntimeError(f"IMAP {action} failed: {detail or status}")


def select_mailbox(imap: imaplib.IMAP4, mailbox: str) -> Tuple[str, int]:
    status, data = imap.select(encode_mailbox(mailbox), readonly=True)
    require_ok(status, data, f"select {mailbox}")

    uidvalidity_response = imap.response("UIDVALIDITY")
    uidvalidity = ""
    if uidvalidity_response and uidvalidity_response[1]:
        uidvalidity = uidvalidity_response[1][0].decode("utf-8", errors="replace")

    exists = 0
    if data and data[0]:
        try:
            exists = int(data[0])
        except (TypeError, ValueError):
            exists = 0

    return uidvalidity, exists


def search_uids(imap: imaplib.IMAP4, query: str) -> List[int]:
    status, data = imap.uid("SEARCH", None, query)
    require_ok(status, data, f"search {query}")
    payload = data[0] if data and data[0] else b""
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    return [int(item) for item in text.split() if item.isdigit()]


def fetch_message(imap: imaplib.IMAP4, uid: int):
    status, data = imap.uid("FETCH", str(uid), "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])")
    require_ok(status, data, f"fetch uid {uid}")

    meta = b""
    body = b""
    for item in data or []:
        if isinstance(item, tuple):
            meta = item[0] or b""
            body = item[1] or b""
            break

    if not body:
        raise RuntimeError(f"No RFC822 payload returned for UID {uid}")

    meta_text = meta.decode("utf-8", errors="replace")
    flags_match = re.search(r"FLAGS \((.*?)\)", meta_text)
    internaldate_match = re.search(r'INTERNALDATE "([^"]+)"', meta_text)
    size_match = re.search(r"RFC822\.SIZE (\d+)", meta_text)

    flags = [flag for flag in flags_match.group(1).split() if flag] if flags_match else []
    internaldate = internaldate_match.group(1) if internaldate_match else ""
    size = int(size_match.group(1)) if size_match else len(body)

    return body, flags, internaldate, size


def ensure_label(cache: set, label_id: str, name: str, label_type: str):
    if label_id in cache:
        return
    emit(
        {
            "upsert": "label",
            "external_id": label_id,
            "fields": {
                "name": sanitize_text(name, 500),
                "color": "",
                "type": label_type,
            },
        }
    )
    cache.add(label_id)


def label_identity(mailbox: str, flag: Optional[str] = None):
    if flag is None:
        return f"mailbox:{mailbox}", mailbox, "mailbox"

    normalized = sanitize_text(flag)
    if normalized.startswith("\\"):
        display = normalized[1:] or normalized
        return f"flag:{normalized.lower()}", display, "system-flag"

    return f"keyword:{normalized.lower()}", normalized, "keyword"


def thread_identity(subject: str, message_id: str, references: List[str], in_reply_to: str) -> str:
    if references:
        return stable_id("thread", references[0])
    if in_reply_to:
        return stable_id("thread", in_reply_to)
    if message_id:
        return stable_id("thread", message_id)
    canonical = canonical_subject(subject)
    if canonical:
        return stable_id("thread-subject", canonical)
    return stable_id("thread-fallback", subject or "(no-subject)")


def message_identity(username: str, mailbox: str, uidvalidity: str, uid: int, message_id: str) -> str:
    if message_id:
        return stable_id("message", message_id)
    return stable_id("message-fallback", username.lower(), mailbox, uidvalidity, str(uid))


def parse_references(header: str) -> List[str]:
    values = []
    for item in re.findall(r"<[^>]+>", header or ""):
        normalized = normalize_message_id(item)
        if normalized:
            values.append(normalized)
    return values


def process_message(
    raw_message: bytes,
    *,
    username: str,
    mailbox: str,
    uidvalidity: str,
    uid: int,
    flags: List[str],
    internaldate: str,
    size: int,
    label_cache: set,
    thread_cache: Dict[str, dict],
):
    parsed = email.message_from_bytes(raw_message, policy=email.policy.default)

    subject = header_value(parsed, "Subject") or "(no subject)"
    from_addr = parse_address_list(parsed.get("From", ""))
    to_addr = parse_address_list(parsed.get("To", ""))
    cc_addr = parse_address_list(parsed.get("Cc", ""))
    bcc_addr = parse_address_list(parsed.get("Bcc", ""))
    message_id_raw = header_value(parsed, "Message-ID")
    message_id = normalize_message_id(message_id_raw)
    in_reply_to_raw = header_value(parsed, "In-Reply-To")
    in_reply_to = normalize_message_id(in_reply_to_raw)
    references_header = header_value(parsed, "References")
    references = parse_references(references_header)

    received_at = parse_date(
        header_value(parsed, "Date"),
        fallback=parse_internaldate(internaldate),
    )
    body, attachments = extract_text_and_attachments(parsed)
    snippet = message_snippet(body, parsed.get("Subject", ""))

    message_external_id = message_identity(username, mailbox, uidvalidity, uid, message_id)
    thread_external_id = thread_identity(subject, message_id, references, in_reply_to)

    label_names = [mailbox]
    all_label_ids = []

    mailbox_label_id, mailbox_label_name, mailbox_label_type = label_identity(mailbox)
    ensure_label(label_cache, mailbox_label_id, mailbox_label_name, mailbox_label_type)
    all_label_ids.append(mailbox_label_id)

    for flag in flags:
        label_id, label_name, label_type = label_identity(mailbox, flag)
        ensure_label(label_cache, label_id, label_name, label_type)
        all_label_ids.append(label_id)
        if label_name not in label_names:
            label_names.append(label_name)

    if thread_external_id not in thread_cache:
        emit(
            {
                "upsert": "thread",
                "external_id": thread_external_id,
                "fields": {
                    "subject": sanitize_text(subject, 1000),
                    "last_date": received_at,
                    "snippet": snippet,
                },
            }
        )

    emit(
        {
            "upsert": "message",
            "external_id": message_external_id,
            "fields": {
                "subject": sanitize_text(subject, 1000),
                "snippet": snippet,
                "body": body,
                "from": sanitize_text(from_addr, 2000),
                "to": sanitize_text(to_addr, 4000),
                "cc": sanitize_text(cc_addr, 4000),
                "bcc": sanitize_text(bcc_addr, 4000),
                "date": received_at,
                "mailbox": sanitize_text(mailbox, 500),
                "labels": sanitize_text(", ".join(dict.fromkeys(label_names)), 2000),
                "message_id": sanitize_text(message_id_raw, 1000),
                "in_reply_to": sanitize_text(in_reply_to_raw, 1000),
                "references": sanitize_text(references_header, 4000),
                "is_read": "\\Seen" in flags,
                "is_flagged": "\\Flagged" in flags,
                "has_attachments": bool(attachments),
                "size": size,
                "thread_id": thread_external_id,
            },
        }
    )

    for label_id in all_label_ids:
        emit({"link": "message", "id": message_external_id, "to": "label", "to_id": label_id})

    for index, attachment in enumerate(attachments):
        attachment_id = stable_id(
            "attachment",
            message_external_id,
            str(index),
            attachment["filename"],
            attachment["content_id"],
        )
        emit(
            {
                "upsert": "attachment",
                "external_id": attachment_id,
                "fields": {
                    "filename": sanitize_text(attachment["filename"], 1000),
                    "mime_type": sanitize_text(attachment["mime_type"], 500),
                    "size": attachment["size"],
                    "content_id": sanitize_text(attachment["content_id"], 1000),
                    "is_inline": attachment["is_inline"],
                    "message_id": message_external_id,
                },
            }
        )

    thread_state = thread_cache.setdefault(
        thread_external_id,
        {
            "subject": sanitize_text(subject, 1000),
            "last_date": received_at,
            "last_sort_key": received_at,
            "message_ids": set(),
            "snippet": snippet,
        },
    )
    thread_state["message_ids"].add(message_external_id)
    if received_at >= thread_state["last_sort_key"]:
        thread_state["last_sort_key"] = received_at
        thread_state["last_date"] = received_at
        if snippet:
            thread_state["snippet"] = snippet


def emit_threads(thread_cache: Dict[str, dict]):
    for thread_id, state in thread_cache.items():
        emit(
            {
                "upsert": "thread",
                "external_id": thread_id,
                "fields": {
                    "subject": state["subject"],
                    "last_date": state["last_date"],
                    "message_count": len(state["message_ids"]),
                    "snippet": state["snippet"],
                },
            }
        )


def sync_mailbox(
    imap: imaplib.IMAP4,
    mailbox: str,
    username: str,
    mailbox_cursor: dict,
    remaining_budget: Optional[int],
    thread_cache: Dict[str, dict],
) -> Tuple[int, dict]:
    uidvalidity, exists = select_mailbox(imap, mailbox)
    log("info", f"Syncing mailbox '{mailbox}' ({exists} messages visible)")

    previous_uidvalidity = str(mailbox_cursor.get("uidvalidity", "") or "")
    previous_last_uid = parse_int(mailbox_cursor.get("last_uid"), 0)

    backfill_incomplete = bool(mailbox_cursor.get("backfill_incomplete"))
    if previous_uidvalidity and previous_uidvalidity == uidvalidity and previous_last_uid > 0:
        if backfill_incomplete:
            uids = search_uids(imap, f"UID {previous_last_uid + 1}:*")
        else:
            search_start = max(1, previous_last_uid - REFRESH_WINDOW + 1)
            uids = search_uids(imap, f"UID {search_start}:*")
    else:
        if previous_uidvalidity and previous_uidvalidity != uidvalidity:
            log("warn", f"UIDVALIDITY changed for '{mailbox}', restarting mailbox sync from the beginning")
        uids = search_uids(imap, "ALL")

    all_uids = sorted(dict.fromkeys(uids))
    uids = list(all_uids)
    truncated = False
    if remaining_budget is not None and len(uids) > remaining_budget:
        uids = uids[:remaining_budget]
        truncated = True

    label_cache = set()
    processed = 0
    highest_seen_uid = previous_last_uid if previous_uidvalidity == uidvalidity else 0

    for uid in uids:
        raw_message, flags, internaldate, size = fetch_message(imap, uid)
        process_message(
            raw_message,
            username=username,
            mailbox=mailbox,
            uidvalidity=uidvalidity,
            uid=uid,
            flags=flags,
            internaldate=internaldate,
            size=size,
            label_cache=label_cache,
            thread_cache=thread_cache,
        )
        highest_seen_uid = max(highest_seen_uid, uid)
        processed += 1

    if processed == 0:
        log("info", f"No new or refreshable messages found in '{mailbox}'")
    else:
        log("info", f"Processed {processed} messages from '{mailbox}'")

    return processed, {
        "uidvalidity": uidvalidity,
        "last_uid": highest_seen_uid,
        "backfill_incomplete": truncated or (bool(all_uids) and highest_seen_uid < all_uids[-1]),
    }


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        log("error", f"Invalid adapter input JSON: {exc}")
        sys.exit(2)

    config = payload.get("config", {}) if isinstance(payload, dict) else {}
    cursor = parse_cursor(payload.get("cursor") if isinstance(payload, dict) else None)
    mailboxes = parse_mailboxes(config)
    max_results = parse_int(config.get("max_results"), 500)
    budget = None if max_results == 0 else max(max_results, 0)

    log(
        "info",
        "Deletion detection is not implemented in Proton Mail adapter v1; future syncs will add and refresh mail safely, but removed messages may persist until a reset or future cursor upgrade",
    )

    try:
        imap, connection_info = connect_imap(config)
    except (imaplib.IMAP4.error, ssl.SSLError, OSError, ValueError) as exc:
        log("error", f"IMAP connection failed: {exc}")
        sys.exit(2)

    next_mailbox_state = {}
    thread_cache: Dict[str, dict] = {}
    try:
        log(
            "info",
            f"Connected to {connection_info['host']}:{connection_info['port']} using {connection_info['tls_mode']}",
        )
        total_processed = 0
        fallback_used = False

        for mailbox in mailboxes:
            if budget is not None and budget <= 0:
                next_mailbox_state[mailbox] = cursor["mailboxes"].get(
                    mailbox,
                    {"uidvalidity": "", "last_uid": 0},
                )
                continue

            mailbox_cursor = cursor["mailboxes"].get(mailbox, {})
            try:
                processed, mailbox_state = sync_mailbox(
                    imap,
                    mailbox,
                    connection_info["username"],
                    mailbox_cursor,
                    budget,
                    thread_cache,
                )
            except RuntimeError as exc:
                if mailbox == DEFAULT_MAILBOX and len(mailboxes) == 1 and not fallback_used:
                    log("warn", f"Mailbox '{DEFAULT_MAILBOX}' unavailable, falling back to INBOX: {exc}")
                    fallback_used = True
                    processed, mailbox_state = sync_mailbox(
                        imap,
                        "INBOX",
                        connection_info["username"],
                        cursor["mailboxes"].get("INBOX", {}),
                        budget,
                        thread_cache,
                    )
                    next_mailbox_state["INBOX"] = mailbox_state
                    total_processed += processed
                    if budget is not None:
                        budget -= processed
                    continue
                raise

            next_mailbox_state[mailbox] = mailbox_state
            total_processed += processed
            if budget is not None:
                budget -= processed

        emit_threads(thread_cache)
        emit({"cursor": make_cursor(next_mailbox_state)})
        log(
            "info",
            f"Sync complete: {total_processed} messages processed across {len(next_mailbox_state)} mailboxes",
        )

    except (imaplib.IMAP4.error, RuntimeError, ssl.SSLError, OSError, binascii.Error) as exc:
        log("error", f"IMAP sync failed: {exc}")
        sys.exit(1)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
