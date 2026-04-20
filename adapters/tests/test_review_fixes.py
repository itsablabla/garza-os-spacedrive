import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def run_adapter(script_path, payload):
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=script_path.parent,
    )
    lines = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            lines.append(json.loads(line))
    return proc, lines


class ReviewFixTests(unittest.TestCase):
    def test_beeper_account_filter_zero_matches_blocks_chat_sync(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/v1/accounts":
                    body = {"accounts": [{"id": "acct-1", "network": "imessage"}]}
                elif self.path == "/v1/chats":
                    body = {"chats": [{"id": "chat-1", "account_id": "acct-1", "name": "Chat"}]}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            proc, lines = run_adapter(
                REPO_ROOT / "adapters/beeper/sync.py",
                {
                    "config": {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "account_filter": "missing-account",
                    }
                },
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        upserts = [line for line in lines if line.get("upsert")]
        self.assertEqual(upserts, [])

    def test_beeper_paginates_and_emits_parent_relation(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path, _, query = self.path.partition("?")
                if path == "/v1/accounts":
                    body = {"accounts": [{"id": "acct-1", "network": "imessage"}]}
                elif path == "/v1/chats":
                    body = {"chats": [{"id": "chat-1", "account_id": "acct-1", "name": "Chat"}]}
                elif path == "/v1/chats/chat-1":
                    body = {"id": "chat-1", "account_id": "acct-1", "name": "Chat"}
                elif path == "/v1/chats/chat-1/messages" and "cursor=cursor-1" not in query:
                    body = {
                        "messages": [
                            {
                                "id": "parent",
                                "text": "hello",
                                "created_at": "2026-01-01T00:00:00+00:00",
                                "sortKey": "cursor-1",
                            }
                        ],
                        "hasMore": True,
                        "cursor": "cursor-1",
                    }
                elif path == "/v1/chats/chat-1/messages":
                    body = {
                        "messages": [
                            {
                                "id": "reply",
                                "text": "world",
                                "created_at": "2026-01-01T00:01:00+00:00",
                                "linkedMessageID": "parent",
                                "sortKey": "cursor-2",
                            }
                        ],
                        "hasMore": False,
                        "cursor": "cursor-2",
                    }
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            proc, lines = run_adapter(
                REPO_ROOT / "adapters/beeper/sync.py",
                {"config": {"base_url": f"http://127.0.0.1:{server.server_port}", "message_limit": 0}},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        messages = [line for line in lines if line.get("upsert") == "message"]
        self.assertEqual([line["external_id"] for line in messages], ["chat-1:parent", "chat-1:reply"])
        self.assertEqual(messages[1]["fields"]["reply_to"], "chat-1:parent")
        self.assertEqual(messages[1]["fields"]["parent_id"], "chat-1:parent")

    def test_macos_imessage_uses_stable_synthetic_chat_ids_and_respects_last_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE message (
                    ROWID INTEGER PRIMARY KEY,
                    text TEXT,
                    date INTEGER,
                    guid TEXT,
                    service TEXT,
                    cache_roomnames TEXT,
                    is_from_me INTEGER,
                    is_read INTEGER,
                    handle_id TEXT
                );
                INSERT INTO message (ROWID, text, date, guid, service, cache_roomnames, is_from_me, is_read, handle_id)
                VALUES
                    (1, 'old', 1000, 'guid-old', 'iMessage', 'room-a', 0, 1, 'alice'),
                    (2, 'new', 2000, 'guid-new', 'iMessage', 'room-b', 0, 1, 'bob');
                """
            )
            conn.commit()
            conn.close()

            proc, lines = run_adapter(
                REPO_ROOT / "adapters/macos-imessage/sync.py",
                {
                    "config": {"chat_db_path": str(db_path), "max_messages": 5000},
                    "cursor": json.dumps({"last_timestamp": "2001-01-01T00:25:00+00:00"}),
                },
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        messages = [line for line in lines if line.get("upsert") == "message"]
        chats = [line for line in lines if line.get("upsert") == "chat"]
        self.assertEqual([m["external_id"] for m in messages], ["guid-new"])
        self.assertEqual([c["external_id"] for c in chats], ["synthetic:room-b"])

    def test_notion_page_limit_does_not_delete_previous_pages_on_truncated_run(self):
        script = REPO_ROOT / "adapters/notion/sync.py"
        with tempfile.TemporaryDirectory() as tmp:
            stub_dir = Path(tmp)
            stub_path = stub_dir / "sitecustomize.py"
            stub_path.write_text(
                """
import json
import urllib.request

class _Resp:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def read(self):
        return self.payload
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False

def _payload(url, data):
    if url.endswith("/search"):
        return {
            "results": [
                {
                    "object": "page",
                    "id": "page-1",
                    "url": "https://example/page-1",
                    "public_url": "",
                    "properties": {"Name": {"type": "title", "title": [{"plain_text": "One"}]}},
                    "parent": {"type": "workspace", "workspace": True},
                    "created_by": {"id": "u1"},
                    "last_edited_by": {"id": "u1"},
                    "created_time": "2026-01-01T00:00:00+00:00",
                    "last_edited_time": "2026-01-02T00:00:00+00:00",
                    "archived": False,
                    "in_trash": False,
                    "is_locked": False,
                }
            ],
            "has_more": True,
            "next_cursor": "cursor-2",
        }
    if "/blocks/" in url:
        return {"results": [], "has_more": False}
    raise AssertionError(url)

def _fake_urlopen(req, timeout=30):
    url = req.full_url
    data = req.data.decode() if getattr(req, "data", None) else ""
    return _Resp(_payload(url, data))

urllib.request.urlopen = _fake_urlopen
"""
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(stub_dir)
            proc = subprocess.run(
                [sys.executable, str(script)],
                input=json.dumps(
                    {
                        "config": {"integration_token": "token", "page_limit": 1},
                        "cursor": json.dumps(
                            {
                                "last_edited_time": "2026-01-01T00:00:00+00:00",
                                "pages": {"page-1": {}, "page-2": {}},
                            }
                        ),
                    }
                ),
                text=True,
                capture_output=True,
                cwd=script.parent,
                env=env,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        deletes = [line for line in lines if line.get("delete") == "page"]
        self.assertEqual(deletes, [])

    def test_proton_mail_inline_text_part_stays_in_body(self):
        from email.message import EmailMessage
        import importlib.util

        spec = importlib.util.spec_from_file_location("proton_sync", REPO_ROOT / "adapters/proton-mail/sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        message = EmailMessage()
        message["Subject"] = "hello"
        message.make_mixed()
        inline = EmailMessage()
        inline.set_content("body text")
        inline["Content-Disposition"] = "inline"
        message.attach(inline)

        body, attachments = module.extract_text_and_attachments(message)
        self.assertIn("body text", body)
        self.assertEqual(attachments, [])

    def test_proton_mail_truncated_scan_keeps_backfill_open(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("proton_sync", REPO_ROOT / "adapters/proton-mail/sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        module.select_mailbox = lambda _imap, _mailbox: ("1", 3)
        module.search_uids = lambda _imap, _query: [1, 2, 3]
        module.fetch_message = lambda _imap, uid: (f"raw-{uid}".encode(), [], "", 10)
        processed_uids = []
        module.process_message = lambda raw_message, **kwargs: processed_uids.append(kwargs["uid"])

        processed, state = module.sync_mailbox(None, "All Mail", "user", {}, 2, {})
        self.assertEqual(processed, 2)
        self.assertEqual(processed_uids, [1, 2])
        self.assertEqual(state["last_uid"], 2)
        self.assertTrue(state["backfill_incomplete"])

    def test_slack_live_refresh_window_tracks_state(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("slack_sync", REPO_ROOT / "adapters/slack-live/sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class FakeClient:
            def __init__(self):
                self.calls = []

            def api_get(self, method, params=None):
                params = dict(params or {})
                self.calls.append((method, params))
                if method == "conversations.history" and "latest" not in params:
                    return {"ok": True, "messages": [], "response_metadata": {"next_cursor": ""}}
                if method == "conversations.history" and "latest" in params:
                    return {
                        "ok": True,
                        "messages": [{"ts": "1000.000001", "text": "edited older", "edited": {"ts": "2000.000001"}}],
                        "response_metadata": {"next_cursor": ""},
                    }
                raise AssertionError((method, params))

        archiver = module.SlackArchiver(
            {
                "token": "token",
                "channel_names": [],
                "channel_ids": [],
                "history_limit": 0,
                "full_history_days": 0,
                "include_archived": False,
                "include_private": True,
                "include_direct_messages": True,
                "include_group_dms": True,
            },
            {"version": 1, "channels": {}},
        )
        archiver.client = FakeClient()
        archiver.emit_message = lambda *_args, **_kwargs: None
        state = {"threads": {}, "last_history_ts": "2500.000001", "last_refresh_before_ts": "900.000001"}
        archiver.sync_channel_history({"id": "C1", "name": "general"}, state)
        self.assertEqual(state["last_refresh_before_ts"], "1000.000001")
        self.assertTrue(any("latest" in params for method, params in archiver.client.calls if method == "conversations.history"))

    def test_slack_live_keeps_old_thread_roots_in_cursor_state(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("slack_sync", REPO_ROOT / "adapters/slack-live/sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        archiver = module.SlackArchiver(
            {
                "token": "token",
                "channel_names": [],
                "channel_ids": [],
                "history_limit": 0,
                "full_history_days": 0,
                "include_archived": False,
                "include_private": True,
                "include_direct_messages": True,
                "include_group_dms": True,
            },
            {"version": 1, "channels": {}},
        )
        state = {
            "123.000001": "123.000001",
            "456.000001": "456.000001",
        }
        self.assertEqual(archiver.prune_threads(state), state)


if __name__ == "__main__":
    unittest.main()
