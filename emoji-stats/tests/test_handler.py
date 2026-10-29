"""Focused regression tests. Run: python -m unittest discover -s emoji-stats/tests -v

Tests load handler with fake boto3 and nacl modules; production dependencies are not
required just to exercise API pagination and counting.
"""
import importlib.util
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

for name in ("boto3", "nacl", "nacl.exceptions", "nacl.signing"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["boto3"].client = lambda service: Mock()
sys.modules["nacl.exceptions"].BadSignatureError = type("BadSignatureError", (Exception,), {})
sys.modules["nacl.signing"].VerifyKey = Mock()

spec = importlib.util.spec_from_file_location(
    "emoji_stats_handler", Path(__file__).resolve().parents[1] / "src" / "handler.py"
)
handler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.context = Mock()
        self.context.get_remaining_time_in_millis.return_value = 300000

    def test_content_excludes_bots_and_webhooks_but_counts_reactions(self):
        stats = {"123": {"content": 0, "reaction": 0}}
        now_id = str((int(time.time() * 1000) - handler.DISCORD_EPOCH_MS) << 22)
        messages = [
            {"id": now_id, "author": {"bot": False}, "content": "<:ok:123>", "reactions": []},
            {"id": now_id, "author": {"bot": True}, "content": "<:ok:123>",
             "reactions": [{"emoji": {"id": "123"}, "count": 2}]},
            {"id": now_id, "author": {"bot": False}, "webhook_id": "1",
             "content": "<:ok:123>", "reactions": []},
        ]
        with patch.object(handler, "_discord_request", return_value=messages):
            self.assertEqual(handler._scan_channel("42", 0, stats, self.context), 3)
        self.assertEqual(stats["123"], {"content": 1, "reaction": 2})

    def test_public_archive_uses_timestamp_cursor(self):
        pages = [
            {"threads": [{"id": "11", "thread_metadata": {"archive_timestamp": "2026-10-20T00:00:00Z"}}], "has_more": True},
            {"threads": [{"id": "22", "thread_metadata": {"archive_timestamp": "2026-10-10T00:00:00Z"}}], "has_more": False},
        ]
        with patch.object(handler, "_discord_request", side_effect=pages) as req:
            self.assertEqual([t["id"] for t in handler._iter_archived_threads("1", False, None)], ["11", "22"])
        self.assertIn("before=2026-10-20T00%3A00%3A00Z", req.call_args_list[1].args[1])

    def test_private_archive_uses_thread_id_cursor(self):
        pages = [
            {"threads": [{"id": "111"}], "has_more": True},
            {"threads": [], "has_more": False},
        ]
        with patch.object(handler, "_discord_request", side_effect=pages) as req:
            self.assertEqual(len(list(handler._iter_archived_threads("1", True, None))), 1)
        self.assertIn("before=111", req.call_args_list[1].args[1])

    def test_rate_limit_wait_aborts_before_deadline(self):
        with patch.object(handler.time, "monotonic", return_value=5):
            with self.assertRaises(handler._OutOfTime):
                handler._sleep_with_deadline(2, 6)

    def test_expired_signature_is_rejected(self):
        event = {"headers": {"X-Signature-Ed25519": "00",
                             "X-Signature-Timestamp": str(int(time.time()) - 600)},
                 "body": "{}"}
        self.assertIsNone(handler._verified_body(event))

    def test_scan_timeout_returns_partial_result(self):
        def request(method, path, stop_at=None):
            if path.endswith("/emojis"):
                return [{"id": "123", "name": "ok", "animated": False}]
            if path.endswith("/channels"):
                return [{"id": "1", "type": 0, "last_message_id": "999999999999999999"}]
            if path.endswith("/threads/active"):
                return {"threads": []}
            return []
        with patch.object(handler, "_discord_request", side_effect=request):
            with patch.object(handler, "_scan_channel", side_effect=handler._OutOfTime):
                stats, summary = handler._aggregate("guild", 7, self.context)
        self.assertTrue(summary["timed_out"])
        self.assertEqual(stats["123"]["content"], 0)


if __name__ == "__main__":
    unittest.main()
