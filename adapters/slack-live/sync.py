#!/usr/bin/env python3
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://slack.com/api"
MAX_RETRIES = 4
PAGE_LIMIT = 200
THREAD_STATE_LIMIT = 1000
THREAD_RETENTION_DAYS = 180
HISTORY_OVERLAP_SECONDS = 120

MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)(?:\|([^>]+))?>")
SUBTEAM_RE = re.compile(r"<!subteam\^[^|>]+\|([^>]+)>")
LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")
MAILTO_RE = re.compile(r"<mailto:([^|>]+)(?:\|([^>]+))?>")
ENTITY_REPLACEMENTS = {
    "<!here>": "@here",
    "<!channel>": "@channel",
    "<!everyone>": "@everyone",
}


def log(level, message):
    print(json.dumps({"log": level, "message": message}), flush=True)


def emit(operation):
    print(json.dumps(operation), flush=True)


def parse_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def parse_int(value, default):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_csv(value):
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def now_utc():
    return datetime.now(timezone.utc)


def ts_to_iso(ts_value):
    try:
        if not ts_value:
            return ""
        return datetime.fromtimestamp(float(ts_value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def iso_to_ts(iso_value):
    if not iso_value:
        return None
    try:
        return datetime.fromisoformat(iso_value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def slack_ts_to_float(ts_value):
    try:
        return float(ts_value)
    except (TypeError, ValueError):
        return 0.0


def normalize_ts_string(ts_value):
    try:
        return f"{float(ts_value):.6f}"
    except (TypeError, ValueError):
        return ""


def slack_permalink(base_url, channel_id, ts_value):
    if not base_url or not channel_id or not ts_value:
        return ""
    ts_digits = str(ts_value).replace(".", "")
    return f"{base_url.rstrip('/')}/archives/{channel_id}/p{ts_digits}"


def truncate(value, limit):
    if not value:
        return ""
    value = str(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def reaction_summary(reactions):
    parts = []
    for reaction in reactions or []:
        name = reaction.get("name", "")
        count = reaction.get("count", 0)
        users = reaction.get("users") or []
        if name:
            if users:
                parts.append(f":{name}: {count} ({', '.join(users[:10])})")
            else:
                parts.append(f":{name}: {count}")
    return truncate(", ".join(parts), 4000)


def files_summary(files):
    parts = []
    for file_info in files or []:
        if not isinstance(file_info, dict):
            continue
        name = file_info.get("name") or file_info.get("title") or file_info.get("id") or "file"
        mime_type = file_info.get("mimetype") or file_info.get("filetype") or ""
        size = file_info.get("size") or 0
        bits = [name]
        if mime_type:
            bits.append(mime_type)
        if size:
            bits.append(f"{size}b")
        parts.append(" ".join(bits))
    return truncate("; ".join(parts), 4000)


def compact_metadata(message):
    keys = [
        "type",
        "subtype",
        "client_msg_id",
        "app_id",
        "team",
        "reply_users_count",
        "is_locked",
        "subscribed",
        "pinned_to",
    ]
    data = {}
    for key in keys:
        value = message.get(key)
        if value not in (None, "", [], {}):
            data[key] = value

    edited = message.get("edited") or {}
    if edited.get("user") or edited.get("ts"):
        data["edited"] = {
            "user": edited.get("user"),
            "ts": edited.get("ts"),
        }

    blocks = message.get("blocks") or []
    if blocks:
        data["block_types"] = [block.get("type") for block in blocks[:20] if block.get("type")]

    attachments = message.get("attachments") or []
    if attachments:
        previews = []
        for attachment in attachments[:10]:
            preview_bits = []
            for key in ("title", "text", "fallback", "service_name"):
                value = attachment.get(key)
                if value:
                    preview_bits.append(truncate(value, 200))
            if preview_bits:
                previews.append(" | ".join(preview_bits))
        if previews:
            data["attachments"] = previews

    metadata = message.get("metadata")
    if metadata:
        data["event_metadata"] = metadata

    if not data:
        return ""

    return truncate(json.dumps(data, ensure_ascii=False, sort_keys=True), 12000)


def normalize_text(text, users_map, channel_names):
    if not text:
        return ""

    text = html.unescape(str(text))
    for raw, replacement in ENTITY_REPLACEMENTS.items():
        text = text.replace(raw, replacement)

    def replace_user(match):
        user_id = match.group(1)
        return "@" + users_map.get(user_id, user_id)

    def replace_channel(match):
        channel_id = match.group(1)
        fallback = match.group(2) or channel_names.get(channel_id, channel_id)
        return "#" + fallback

    def replace_subteam(match):
        return match.group(1) or "@group"

    def replace_link(match):
        url = match.group(1)
        label = match.group(2)
        return label or url

    def replace_mailto(match):
        email_value = match.group(1)
        label = match.group(2)
        return label or email_value

    text = MENTION_RE.sub(replace_user, text)
    text = CHANNEL_RE.sub(replace_channel, text)
    text = SUBTEAM_RE.sub(replace_subteam, text)
    text = MAILTO_RE.sub(replace_mailto, text)
    text = LINK_RE.sub(replace_link, text)

    return truncate(text.strip(), 50000)


class SlackClient:
    def __init__(self, token):
        self.token = token

    def api_get(self, method, params=None):
        params = params or {}
        encoded = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{API_BASE}/{method}"
        if encoded:
            url += "?" + encoded

        for attempt in range(MAX_RETRIES):
            request = urllib.request.Request(url)
            request.add_header("Authorization", f"Bearer {self.token}")
            request.add_header("Accept", "application/json; charset=utf-8")
            request.add_header("User-Agent", "spacedrive-slack-live/0.1")
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    retry_after = parse_int(error.headers.get("Retry-After"), 1)
                    log("warn", f"Slack rate limited {method}, retrying in {retry_after}s")
                    time.sleep(retry_after)
                    continue
                if error.code >= 500 and attempt < MAX_RETRIES - 1:
                    delay = 2 ** attempt
                    log("warn", f"Slack API {method} returned {error.code}, retrying in {delay}s")
                    time.sleep(delay)
                    continue
                body = error.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Slack API {method} failed with HTTP {error.code}: {body}")
            except urllib.error.URLError as error:
                if attempt < MAX_RETRIES - 1:
                    delay = 2 ** attempt
                    log("warn", f"Slack API {method} network error {error}, retrying in {delay}s")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Slack API {method} network error: {error}")

            if payload.get("ok"):
                return payload

            error_code = payload.get("error", "unknown_error")
            if error_code == "ratelimited" and attempt < MAX_RETRIES - 1:
                delay = 2 ** attempt
                log("warn", f"Slack API {method} rate limited, retrying in {delay}s")
                time.sleep(delay)
                continue
            raise RuntimeError(f"Slack API {method} returned error: {error_code}")

        raise RuntimeError(f"Slack API {method} exceeded retry budget")

    def paginate(self, method, collection_key, params=None):
        params = dict(params or {})
        while True:
            payload = self.api_get(method, params)
            for item in payload.get(collection_key, []) or []:
                yield item
            cursor = ((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
            params["cursor"] = cursor


class SlackArchiver:
    def __init__(self, config, cursor_state):
        self.config = config
        self.client = SlackClient(config["token"])
        self.cursor_state = cursor_state if isinstance(cursor_state, dict) else {}
        self.cursor_state.setdefault("version", 1)
        self.cursor_state.setdefault("channels", {})
        self.workspace_id = "workspace"
        self.workspace_url = ""
        self.users_map = {}
        self.channels_map = {}
        self.synthetic_users = set()

    def run(self):
        workspace = self.sync_workspace()
        self.workspace_id = workspace["id"]
        self.workspace_url = workspace.get("url", "")

        self.sync_users()
        channels = self.sync_channels()

        synced = 0
        for channel in channels:
            self.sync_channel(channel)
            synced += 1

        self.prune_removed_channels({channel["id"] for channel in channels})
        emit({"cursor": json.dumps(self.cursor_state, separators=(",", ":"), sort_keys=True)})
        log("info", f"Synced {synced} Slack conversations")

    def sync_workspace(self):
        auth_info = self.client.api_get("auth.test")
        team_id = auth_info.get("team_id") or self.cursor_state.get("workspace_id") or "workspace"
        team_name = auth_info.get("team") or "Slack Workspace"
        team_url = auth_info.get("url") or ""
        domain = ""
        enterprise_id = ""
        enterprise_name = ""

        try:
            team_info = self.client.api_get("team.info")
            team = team_info.get("team") or {}
            team_name = team.get("name") or team_name
            team_url = team.get("url") or team_url
            domain = team.get("domain") or ""
            enterprise_id = team.get("enterprise_id") or ""
            enterprise_name = team.get("enterprise_name") or ""
        except Exception as error:
            log("warn", f"team.info unavailable: {error}")

        emit({
            "upsert": "workspace",
            "external_id": team_id,
            "fields": {
                "name": team_name,
                "team_id": team_id,
                "url": team_url,
                "domain": domain,
                "enterprise_id": enterprise_id,
                "enterprise_name": enterprise_name,
            },
        })

        self.cursor_state["workspace_id"] = team_id
        return {
            "id": team_id,
            "url": team_url,
            "name": team_name,
        }

    def sync_users(self):
        count = 0
        for member in self.client.paginate("users.list", "members", {"limit": PAGE_LIMIT}):
            if not isinstance(member, dict):
                continue
            user_id = member.get("id")
            if not user_id:
                continue
            profile = member.get("profile") or {}
            display_name = profile.get("display_name") or profile.get("real_name") or member.get("real_name") or member.get("name") or user_id
            self.users_map[user_id] = display_name
            emit({
                "upsert": "user",
                "external_id": user_id,
                "fields": {
                    "username": member.get("name") or "",
                    "display_name": display_name,
                    "real_name": member.get("real_name") or profile.get("real_name") or "",
                    "email": profile.get("email") or "",
                    "title": profile.get("title") or "",
                    "avatar_url": profile.get("image_512") or profile.get("image_192") or profile.get("image_72") or "",
                    "tz": member.get("tz") or "",
                    "is_bot": bool(member.get("is_bot")),
                    "is_admin": bool(member.get("is_admin")),
                    "is_owner": bool(member.get("is_owner")),
                    "is_deleted": bool(member.get("deleted")),
                    "workspace_id": self.workspace_id,
                },
            })
            count += 1
        log("info", f"Synced {count} Slack users")

    def sync_channels(self):
        types = ["public_channel"]
        if self.config["include_private"]:
            types.append("private_channel")
        if self.config["include_direct_messages"]:
            types.append("im")
        if self.config["include_group_dms"]:
            types.append("mpim")

        desired_names = {name.lower() for name in self.config["channel_names"]}
        desired_ids = set(self.config["channel_ids"])
        conversations = []

        params = {
            "types": ",".join(types),
            "limit": PAGE_LIMIT,
            "exclude_archived": "false" if self.config["include_archived"] else "true",
        }

        for conversation in self.client.paginate("conversations.list", "channels", params):
            conversation_id = conversation.get("id")
            if not conversation_id:
                continue
            channel_name = self.channel_display_name(conversation)
            self.channels_map[conversation_id] = channel_name
            if desired_ids and conversation_id not in desired_ids:
                continue
            if desired_names and channel_name.lower() not in desired_names and (conversation.get("name") or "").lower() not in desired_names:
                continue
            emit({
                "upsert": "channel",
                "external_id": conversation_id,
                "fields": {
                    "name": conversation.get("name") or conversation_id,
                    "display_name": channel_name,
                    "kind": self.channel_kind(conversation),
                    "topic": truncate(((conversation.get("topic") or {}).get("value") or ""), 5000),
                    "purpose": truncate(((conversation.get("purpose") or {}).get("value") or ""), 5000),
                    "is_archived": bool(conversation.get("is_archived")),
                    "is_private": bool(conversation.get("is_private")),
                    "is_shared": bool(conversation.get("is_shared") or conversation.get("is_ext_shared") or conversation.get("is_org_shared")),
                    "is_member": bool(conversation.get("is_member")),
                    "member_count": parse_int(conversation.get("num_members"), 0),
                    "created_at": ts_to_iso(conversation.get("created")),
                    "workspace_id": self.workspace_id,
                },
            })
            conversations.append(conversation)

        conversations.sort(key=lambda item: (self.channel_display_name(item).lower(), item.get("id", "")))
        log("info", f"Selected {len(conversations)} Slack conversations")
        return conversations

    def sync_channel(self, conversation):
        channel_id = conversation.get("id")
        if not channel_id:
            return

        state = self.cursor_state["channels"].get(channel_id) or {}
        state.setdefault("threads", {})

        max_history_ts, thread_updates = self.sync_channel_history(conversation, state)
        thread_state = dict(state.get("threads") or {})
        thread_state.update(thread_updates)
        thread_state = self.sync_threads(conversation, thread_state)

        if max_history_ts:
            state["last_history_ts"] = max_history_ts
        state["threads"] = self.prune_threads(thread_state)
        self.cursor_state["channels"][channel_id] = state
        log("info", f"Synced {self.channel_display_name(conversation)}")

    def sync_channel_history(self, conversation, state):
        channel_id = conversation["id"]
        last_history_ts = normalize_ts_string(state.get("last_history_ts"))
        oldest = None

        if last_history_ts:
            oldest = max(slack_ts_to_float(last_history_ts) - HISTORY_OVERLAP_SECONDS, 0.0)
        elif self.config["full_history_days"] > 0:
            oldest = (now_utc() - timedelta(days=self.config["full_history_days"])).timestamp()

        history_params = {
            "channel": channel_id,
            "limit": PAGE_LIMIT,
            "inclusive": "true",
            "include_all_metadata": "true",
        }
        if oldest is not None and oldest > 0:
            history_params["oldest"] = f"{oldest:.6f}"

        thread_updates = {}
        max_history_ts = last_history_ts
        fetched = 0
        page_params = dict(history_params)

        while True:
            payload = self.client.api_get("conversations.history", page_params)
            messages = payload.get("messages") or []
            messages.sort(key=lambda message: slack_ts_to_float(message.get("ts")))

            for message in messages:
                ts_value = normalize_ts_string(message.get("ts"))
                if not ts_value:
                    continue
                self.emit_message(conversation, message)
                fetched += 1
                if not max_history_ts or slack_ts_to_float(ts_value) > slack_ts_to_float(max_history_ts):
                    max_history_ts = ts_value
                thread_ts = normalize_ts_string(message.get("thread_ts"))
                latest_reply = normalize_ts_string(message.get("latest_reply"))
                if thread_ts and thread_ts == ts_value:
                    thread_updates[thread_ts] = max(thread_updates.get(thread_ts, ts_value), latest_reply or ts_value, key=slack_ts_to_float)
                elif thread_ts:
                    thread_updates[thread_ts] = max(thread_updates.get(thread_ts, thread_ts), ts_value, key=slack_ts_to_float)

                if self.config["history_limit"] > 0 and fetched >= self.config["history_limit"]:
                    break

            if self.config["history_limit"] > 0 and fetched >= self.config["history_limit"]:
                break

            cursor = ((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
            page_params["cursor"] = cursor

        return max_history_ts, thread_updates

    def sync_threads(self, conversation, thread_state):
        channel_id = conversation["id"]
        channel_name = self.channel_display_name(conversation)
        updated_state = dict(thread_state)
        for parent_ts, latest_reply_ts in sorted(thread_state.items(), key=lambda item: slack_ts_to_float(item[0])):
            oldest = max(slack_ts_to_float(latest_reply_ts) - HISTORY_OVERLAP_SECONDS, slack_ts_to_float(parent_ts))
            page_params = {
                "channel": channel_id,
                "ts": parent_ts,
                "limit": PAGE_LIMIT,
                "inclusive": "true",
                "oldest": f"{oldest:.6f}",
            }
            latest_seen = normalize_ts_string(latest_reply_ts) or normalize_ts_string(parent_ts)
            while True:
                try:
                    payload = self.client.api_get("conversations.replies", page_params)
                except Exception as error:
                    log("warn", f"Failed to sync thread {channel_name} {parent_ts}: {error}")
                    break
                replies = payload.get("messages") or []
                replies.sort(key=lambda message: slack_ts_to_float(message.get("ts")))
                for reply in replies:
                    ts_value = normalize_ts_string(reply.get("ts"))
                    if not ts_value:
                        continue
                    self.emit_message(conversation, reply)
                    if not latest_seen or slack_ts_to_float(ts_value) > slack_ts_to_float(latest_seen):
                        latest_seen = ts_value
                cursor = ((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
                if not cursor:
                    break
                page_params["cursor"] = cursor
            updated_state[parent_ts] = latest_seen or latest_reply_ts or parent_ts
        return updated_state

    def emit_message(self, conversation, message):
        ts_value = normalize_ts_string(message.get("ts"))
        if not ts_value:
            return

        subtype = message.get("subtype") or ""
        if subtype == "message_deleted":
            return

        author_id, author_name = self.resolve_author(message)
        channel_id = conversation.get("id") or ""
        channel_name = self.channel_display_name(conversation)
        thread_ts = normalize_ts_string(message.get("thread_ts"))
        is_thread_reply = bool(thread_ts and thread_ts != ts_value)
        mentions = sorted({match.group(1) for match in MENTION_RE.finditer(message.get("text") or "")})
        mention_names = [self.users_map.get(user_id, user_id) for user_id in mentions]
        normalized_text = normalize_text(message.get("text") or "", self.users_map, self.channels_map)
        fields = {
            "text": normalized_text,
            "author": truncate(author_name, 500),
            "channel_name": channel_name,
            "channel_kind": self.channel_kind(conversation),
            "subtype": subtype,
            "timestamp": ts_to_iso(ts_value),
            "edited_at": ts_to_iso((message.get("edited") or {}).get("ts")),
            "thread_ts": thread_ts,
            "reply_count": parse_int(message.get("reply_count"), 0),
            "is_thread_reply": is_thread_reply,
            "reactions": reaction_summary(message.get("reactions") or []),
            "mentions": truncate(", ".join(mention_names), 2000),
            "files": files_summary(message.get("files") or []),
            "metadata": compact_metadata(message),
            "permalink": slack_permalink(self.workspace_url, channel_id, ts_value),
            "channel_id": channel_id,
        }
        if author_id:
            fields["user_id"] = author_id
        if is_thread_reply:
            fields["parent_id"] = f"{channel_id}:{thread_ts}"

        emit({
            "upsert": "message",
            "external_id": f"{channel_id}:{ts_value}",
            "fields": fields,
        })

    def resolve_author(self, message):
        user_id = message.get("user")
        if user_id:
            return user_id, self.users_map.get(user_id, user_id)

        bot_id = message.get("bot_id")
        bot_profile = message.get("bot_profile") or {}
        username = message.get("username") or bot_profile.get("name") or bot_id or "Slack Bot"
        if bot_id:
            synthetic_id = f"bot:{bot_id}"
            self.ensure_virtual_user(
                synthetic_id,
                username,
                real_name=username,
                avatar_url=bot_profile.get("image_72") or bot_profile.get("icons", {}).get("image_48") or "",
                is_bot=True,
            )
            return synthetic_id, self.users_map.get(synthetic_id, username)

        return "", username

    def ensure_virtual_user(self, external_id, display_name, real_name="", avatar_url="", is_bot=False):
        if external_id in self.synthetic_users:
            return
        self.synthetic_users.add(external_id)
        self.users_map[external_id] = display_name
        emit({
            "upsert": "user",
            "external_id": external_id,
            "fields": {
                "username": display_name,
                "display_name": display_name,
                "real_name": real_name or display_name,
                "email": "",
                "title": "",
                "avatar_url": avatar_url,
                "tz": "",
                "is_bot": bool(is_bot),
                "is_admin": False,
                "is_owner": False,
                "is_deleted": False,
                "workspace_id": self.workspace_id,
            },
        })

    def channel_display_name(self, conversation):
        if conversation.get("is_im"):
            user_id = conversation.get("user")
            if user_id:
                return self.users_map.get(user_id, conversation.get("name") or user_id)
            return conversation.get("name") or conversation.get("id") or "direct-message"
        if conversation.get("is_mpim"):
            return conversation.get("name") or conversation.get("id") or "group-dm"
        return conversation.get("name") or conversation.get("id") or "channel"

    def channel_kind(self, conversation):
        if conversation.get("is_im"):
            return "direct_message"
        if conversation.get("is_mpim"):
            return "group_direct_message"
        if conversation.get("is_private"):
            return "private_channel"
        return "public_channel"

    def prune_threads(self, thread_state):
        if not thread_state:
            return {}
        cutoff = (now_utc() - timedelta(days=THREAD_RETENTION_DAYS)).timestamp()
        rows = sorted(thread_state.items(), key=lambda item: slack_ts_to_float(item[1]), reverse=True)
        pruned = {}
        for parent_ts, latest_reply_ts in rows:
            latest_value = slack_ts_to_float(latest_reply_ts)
            if len(pruned) >= THREAD_STATE_LIMIT:
                break
            if latest_value and latest_value < cutoff and len(pruned) >= min(200, THREAD_STATE_LIMIT):
                continue
            pruned[parent_ts] = latest_reply_ts
        return pruned

    def prune_removed_channels(self, active_ids):
        active_ids = set(active_ids)
        existing = dict(self.cursor_state.get("channels") or {})
        for channel_id in existing.keys():
            if channel_id not in active_ids:
                self.cursor_state["channels"].pop(channel_id, None)


def load_cursor(raw_cursor):
    if not raw_cursor:
        return {"version": 1, "channels": {}}
    try:
        parsed = json.loads(raw_cursor)
        if isinstance(parsed, dict):
            parsed.setdefault("version", 1)
            parsed.setdefault("channels", {})
            return parsed
    except json.JSONDecodeError:
        log("warn", "Ignoring invalid Slack cursor state")
    return {"version": 1, "channels": {}}


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as error:
        log("error", f"Invalid input JSON: {error}")
        sys.exit(2)

    config = input_data.get("config") or {}
    token = config.get("token") or ""
    if not token:
        log("error", "Missing required config: token")
        sys.exit(2)

    runtime_config = {
        "token": token,
        "channel_names": parse_csv(config.get("channel_names", "")),
        "channel_ids": parse_csv(config.get("channel_ids", "")),
        "history_limit": max(parse_int(config.get("history_limit"), 1000), 0),
        "full_history_days": max(parse_int(config.get("full_history_days"), 0), 0),
        "include_archived": parse_bool(config.get("include_archived"), False),
        "include_private": parse_bool(config.get("include_private"), True),
        "include_direct_messages": parse_bool(config.get("include_direct_messages"), True),
        "include_group_dms": parse_bool(config.get("include_group_dms"), True),
    }

    cursor_state = load_cursor(input_data.get("cursor"))

    try:
        SlackArchiver(runtime_config, cursor_state).run()
    except Exception as error:
        log("error", f"Slack sync failed: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
