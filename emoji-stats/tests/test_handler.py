"""Regression tests. Run from the repository root:

    pip install -r emoji-stats/requirements.txt
    python3 -m unittest discover -s emoji-stats/tests -v

boto3 is stubbed (no AWS access). PyNaCl is real, because the signature check is the only
thing protecting the public Function URL; without PyNaCl those tests are skipped.
"""
import base64
import importlib.util
import io
import json
import os
import sys
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

try:
    from nacl.signing import SigningKey
except ImportError:  # pragma: no cover - only when PyNaCl is not installed
    SigningKey = None
    for name in ("nacl", "nacl.exceptions", "nacl.signing"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["nacl.exceptions"].BadSignatureError = type("BadSignatureError", (Exception,), {})
    sys.modules["nacl.signing"].VerifyKey = Mock()

sys.modules["boto3"] = types.SimpleNamespace(client=lambda service: MagicMock())

SIGNING_KEY = SigningKey.generate() if SigningKey else None
os.environ.update(
    {
        "GUILD_ID": "111",
        "DISCORD_PUBLIC_KEY": SIGNING_KEY.verify_key.encode().hex() if SIGNING_KEY else "00" * 32,
        "SSM_PARAM_BOT_TOKEN": "/emoji-stats/discord-bot-token",
    }
)

spec = importlib.util.spec_from_file_location(
    "emoji_stats_handler", Path(__file__).resolve().parents[1] / "src" / "handler.py"
)
handler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler)

FAR_FUTURE = float("inf")


def snowflake_days_ago(days):
    ms = int(time.time() * 1000) - int(days * 86_400_000)
    return str((ms - handler.DISCORD_EPOCH_MS) << 22)


def lambda_context(remaining_ms=800_000):
    return types.SimpleNamespace(
        function_name="emoji-stats", get_remaining_time_in_millis=lambda: remaining_ms
    )


@unittest.skipIf(SigningKey is None, "PyNaCl is not installed")
class InteractionTests(unittest.TestCase):
    def setUp(self):
        handler.lambda_client = MagicMock()

    def url_event(self, payload, *, sign=True, base64_body=False, timestamp=None, tamper=False):
        body = json.dumps(payload)
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        signature = SIGNING_KEY.sign((timestamp + body).encode()).signature.hex()
        if not sign:
            signature = "00" * 64
        if tamper:
            body = body.replace("111", "222")
        return {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {"X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp},
            "body": base64.b64encode(body.encode()).decode() if base64_body else body,
            "isBase64Encoded": base64_body,
        }

    def command(self, guild="111", options=None):
        return {
            "type": 2,
            "application_id": "app",
            "token": "tok",
            "guild_id": guild,
            "data": {"name": "emoji-stats", "options": options or []},
        }

    def call(self, event):
        response = handler.lambda_handler(event, lambda_context())
        return response["statusCode"], json.loads(response["body"])

    def test_ping_with_base64_body(self):
        self.assertEqual(self.call(self.url_event({"type": 1}, base64_body=True)), (200, {"type": 1}))

    def test_rejects_bad_signature(self):
        self.assertEqual(self.call(self.url_event({"type": 1}, sign=False))[0], 401)

    def test_rejects_tampered_body(self):
        self.assertEqual(self.call(self.url_event(self.command(), tamper=True))[0], 401)

    def test_rejects_missing_headers(self):
        event = self.url_event({"type": 1})
        event["headers"] = {}
        self.assertEqual(self.call(event)[0], 401)

    def test_rejects_stale_timestamp_even_when_signed(self):
        event = self.url_event({"type": 1}, timestamp=int(time.time()) - 600)
        self.assertEqual(self.call(event)[0], 401)

    def test_other_guild_gets_ephemeral_refusal_without_crawl(self):
        status, body = self.call(self.url_event(self.command(guild="999")))
        self.assertEqual((status, body["type"], body["data"]["flags"]), (200, 4, 64))
        handler.lambda_client.invoke.assert_not_called()

    def test_defers_ephemerally_and_starts_async_crawl(self):
        status, body = self.call(self.url_event(self.command(options=[{"name": "days", "value": 500}])))
        self.assertEqual((status, body), (200, {"type": 5, "data": {"flags": 64}}))
        kwargs = handler.lambda_client.invoke.call_args.kwargs
        self.assertEqual(kwargs["InvocationType"], "Event")
        payload = json.loads(kwargs["Payload"])
        self.assertEqual((payload["action"], payload["days"], payload["interaction_token"]), ("collect", 90, "tok"))
        self.assertAlmostEqual(payload["accepted_at"], time.time(), delta=5)

    def test_public_option_defers_visibly(self):
        _, body = self.call(self.url_event(self.command(options=[{"name": "public", "value": True}])))
        self.assertEqual(body, {"type": 5, "data": {}})

    def test_collect_payload_through_url_is_not_trusted(self):
        # A body shaped like the internal event still goes through signature verification.
        self.assertEqual(self.call(self.url_event({"action": "collect"}, sign=False))[0], 401)


class ScanTests(unittest.TestCase):
    def test_content_excludes_bots_and_webhooks_but_counts_reactions(self):
        stats = {"123": {"content": 0, "reaction": 0}}
        now_id = snowflake_days_ago(0)
        messages = [
            {"id": now_id, "author": {"bot": False}, "content": "<:ok:123> <:ok:123>", "reactions": []},
            {"id": now_id, "author": {"bot": True}, "content": "<:ok:123>",
             "reactions": [{"emoji": {"id": "123"}, "count": 2}]},
            {"id": now_id, "author": {"bot": False}, "webhook_id": "1",
             "content": "<a:ok:123>", "reactions": [{"emoji": {"id": None, "name": "😂"}, "count": 9}]},
            {"id": now_id, "author": {}, "content": None},
        ]
        with patch.object(handler, "_discord_request", return_value=messages):
            self.assertEqual(handler._scan_channel("42", 0, stats, FAR_FUTURE), 4)
        self.assertEqual(stats["123"], {"content": 2, "reaction": 2})

    def test_paginates_and_stops_at_cutoff(self):
        stats = {}
        recent, old = snowflake_days_ago(1), snowflake_days_ago(40)
        pages = [[{"id": recent}] * 100, [{"id": recent}] * 30 + [{"id": old}] * 70]
        calls = []

        def request(method, path, stop_at=None):
            calls.append(path)
            return pages.pop(0)

        cutoff = int(time.time() * 1000) - 30 * 86_400_000
        with patch.object(handler, "_discord_request", side_effect=request):
            self.assertEqual(handler._scan_channel("42", cutoff, stats, FAR_FUTURE), 130)
        self.assertEqual(len(calls), 2)
        self.assertIn(f"before={recent}", calls[1])


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.emojis = [
            {"id": "101", "name": "yay", "animated": False},
            {"id": "102", "name": "kusa", "animated": True},
            {"id": "103", "name": "old", "animated": False},
        ]
        self.channels = [
            {"id": "ok", "type": 0, "last_message_id": snowflake_days_ago(1)},
            {"id": "forbidden", "type": 0, "last_message_id": snowflake_days_ago(1)},
            {"id": "flaky", "type": 0, "last_message_id": snowflake_days_ago(1)},
            {"id": "stale", "type": 0, "last_message_id": snowflake_days_ago(60)},
            {"id": "category", "type": 4},
        ]
        self.requested = []

    def request(self, method, path, body=None, authenticated=True, stop_at=None):
        self.requested.append(path)
        if path.endswith("/emojis"):
            return self.emojis
        if path.endswith("/channels"):
            return self.channels
        if path.endswith("/threads/active"):
            return {"threads": [{"id": "thread", "type": 11, "last_message_id": snowflake_days_ago(2)}]}
        if path.startswith("/channels/forbidden/"):
            raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)
        if path.startswith("/channels/flaky/"):
            raise urllib.error.URLError(TimeoutError("timed out"))
        if path.startswith("/channels/ok/"):
            return [{"id": snowflake_days_ago(1), "author": {}, "content": "<:yay:101> <a:kusa:102>",
                     "reactions": [{"emoji": {"id": "102"}, "count": 3}]}]
        if path.startswith("/channels/thread/"):
            return [{"id": snowflake_days_ago(2), "author": {}, "content": "<:yay:101>"}]
        raise AssertionError(f"unexpected request {path}")

    def test_counts_and_skips(self):
        with patch.object(handler, "_discord_request", side_effect=self.request):
            stats, summary = handler._aggregate("111", 30, FAR_FUTURE)
        self.assertEqual(summary, {"channels": 2, "messages": 2, "skipped": 2, "timed_out": False})
        self.assertEqual((stats["101"]["content"], stats["102"]["content"], stats["102"]["reaction"]), (2, 1, 3))
        self.assertFalse(any("/channels/stale/" in p or "/channels/category/" in p for p in self.requested))

    def test_bad_token_fails_the_run(self):
        def unauthorized(method, path, **kwargs):
            if path.startswith("/channels/"):
                raise urllib.error.HTTPError(path, 401, "Unauthorized", {}, None)
            return self.request(method, path, **kwargs)

        with patch.object(handler, "_discord_request", side_effect=unauthorized):
            with self.assertRaises(urllib.error.HTTPError):
                handler._aggregate("111", 30, FAR_FUTURE)

    def test_timeout_returns_partial_result(self):
        with patch.object(handler, "_discord_request", side_effect=self.request):
            with patch.object(handler, "_scan_channel", side_effect=handler._OutOfTime):
                _, summary = handler._aggregate("111", 7, FAR_FUTURE)
        self.assertTrue(summary["timed_out"])


class CollectTests(unittest.TestCase):
    def test_token_age_shortens_the_scan_budget(self):
        seen = {}

        def fake_aggregate(guild_id, days, stop_at):
            seen["budget"] = stop_at - time.monotonic()
            return {}, {"channels": 0, "messages": 0, "skipped": 0, "timed_out": False}

        event = {"action": "collect", "application_id": "app", "interaction_token": "tok",
                 "days": 30, "accepted_at": time.time() - 600}
        with patch.object(handler, "_aggregate", side_effect=fake_aggregate), \
                patch.object(handler, "_discord_request") as request:
            result = handler.lambda_handler(event, lambda_context(remaining_ms=900_000))
        # 900s token - 600s queued - 60s reserve, not the Lambda's 840s.
        self.assertAlmostEqual(seen["budget"], 240, delta=5)
        self.assertEqual(result["status"], "ok")
        method, path = request.call_args.args[:2]
        self.assertEqual((method, path), ("PATCH", "/webhooks/app/tok/messages/@original"))
        self.assertFalse(request.call_args.kwargs["authenticated"])

    def test_failure_still_reports_to_discord(self):
        event = {"action": "collect", "application_id": "app", "interaction_token": "tok", "days": 7}
        with patch.object(handler, "_aggregate", side_effect=RuntimeError("boom")), \
                patch.object(handler, "_discord_request") as request:
            result = handler.lambda_handler(event, lambda_context())
        self.assertEqual(result["status"], "failed")
        self.assertIn("失敗", request.call_args.kwargs["body"]["content"])


class RequestTests(unittest.TestCase):
    def setUp(self):
        handler._bot_token_cache = "secret"

    def response(self, payload=b"[1]", headers=None):
        response = MagicMock()
        response.read.return_value = payload
        response.headers = headers or {}
        response.__enter__.return_value = response
        return response

    def test_retries_429_with_user_agent_and_auth(self):
        sent = []

        def urlopen(request, timeout):
            sent.append(request)
            if len(sent) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "rl", {}, io.BytesIO(b'{"retry_after": 0.01}'))
            return self.response()

        with patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(handler._discord_request("GET", "/x"), [1])
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0].get_header("User-agent"), handler.USER_AGENT)
        self.assertEqual(sent[0].get_header("Authorization"), "Bot secret")

    def test_rate_limit_wait_aborts_before_deadline(self):
        with patch.object(handler.time, "monotonic", return_value=5):
            with self.assertRaises(handler._OutOfTime):
                handler._sleep_with_deadline(2, 6)

    def test_page_is_kept_when_bucket_wait_does_not_fit(self):
        drained = self.response(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "30"})
        with patch("urllib.request.urlopen", return_value=drained), \
                patch.object(handler.time, "sleep") as sleep:
            self.assertEqual(handler._discord_request("GET", "/x", stop_at=time.monotonic() + 5), [1])
        sleep.assert_not_called()

    def test_socket_timeout_at_deadline_is_out_of_time(self):
        stop_at = time.monotonic() + 0.05

        def urlopen(request, timeout):
            time.sleep(0.06)
            raise urllib.error.URLError(TimeoutError("timed out"))

        with patch("urllib.request.urlopen", side_effect=urlopen):
            with self.assertRaises(handler._OutOfTime):
                handler._discord_request("GET", "/x", stop_at=stop_at)


class EmbedTests(unittest.TestCase):
    def test_description_fits_discord_limit(self):
        stats = {
            str(i): {"emoji": {"id": str(i), "name": f"emoji_name_{i:03d}"}, "content": i % 3, "reaction": 0}
            for i in range(250)
        }
        summary = {"channels": 1, "messages": 1, "skipped": 1, "timed_out": True}
        embed = handler._build_embed(stats, summary, 30)
        self.assertLessEqual(len(embed["description"]), handler.EMBED_DESCRIPTION_LIMIT)
        self.assertIn("…ほか", embed["description"])
        self.assertIn("時間切れ", embed["footer"]["text"])


if __name__ == "__main__":
    unittest.main()
