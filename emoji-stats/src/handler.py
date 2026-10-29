import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
lambda_client = boto3.client("lambda")

DISCORD_API = "https://discord.com/api/v10"

# Discord's edge rejects urllib's default "Python-urllib/x.y" User-Agent with a bodyless 403,
# so every outbound request sends an explicit one (same lesson as x-to-discord).
USER_AGENT = "emoji-stats (https://github.com/NakaiKt/discord-bot, 1.0)"
REQUEST_TIMEOUT = 15
RATE_LIMIT_MAX_ATTEMPTS = 5

COMMAND_NAME = "emoji-stats"
DEFAULT_DAYS = 30
MAX_DAYS = 90

# Interaction types / response types / message flags from the Discord API.
INTERACTION_PING = 1
INTERACTION_APPLICATION_COMMAND = 2
RESPONSE_PONG = 1
RESPONSE_CHANNEL_MESSAGE = 4
RESPONSE_DEFERRED_CHANNEL_MESSAGE = 5
FLAG_EPHEMERAL = 1 << 6

# GUILD_TEXT, GUILD_VOICE (text chat in voice), GUILD_ANNOUNCEMENT, GUILD_STAGE_VOICE
READABLE_CHANNEL_TYPES = {0, 2, 5, 13}

CUSTOM_EMOJI = re.compile(r"<a?:\w+:(\d+)>")
DISCORD_EPOCH_MS = 1420070400000

# Stop crawling this long before the earlier of the Lambda timeout and the 15-minute
# interaction token expiry, so the partial result can still be posted.
TIME_RESERVE_MS = 60_000
INTERACTION_TOKEN_TTL_SECONDS = 900

TOP_N = 25
EMBED_DESCRIPTION_LIMIT = 4096

_bot_token_cache = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lambda_handler(event, context):
    # A Function URL event always carries requestContext; the async self-invocation never does.
    # So a caller on the public URL cannot forge a "collect" event.
    if "requestContext" in event:
        return _handle_interaction(event, context)
    if event.get("action") == "collect":
        return _handle_collect(event, context)
    logger.warning("Unknown event shape; ignoring")
    return {"status": "ignored"}


# ---------------------------------------------------------------------------
# Interaction (Function URL): must answer within 3 seconds
# ---------------------------------------------------------------------------


def _http_response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _verified_body(event):
    """Return the raw body only for a recent, correctly signed Discord request."""
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    signature = headers.get("x-signature-ed25519")
    timestamp = headers.get("x-signature-timestamp")
    if not signature or not timestamp:
        return None

    try:
        # A signature proves authenticity, not freshness; reject replayed requests.
        if abs(time.time() - int(timestamp)) > 300:
            return None
        body = event.get("body") or ""
        raw = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body.encode("utf-8")
        verify_key = VerifyKey(bytes.fromhex(os.environ["DISCORD_PUBLIC_KEY"]))
        verify_key.verify(timestamp.encode("utf-8") + raw, bytes.fromhex(signature))
        return raw
    except (BadSignatureError, ValueError, TypeError, UnicodeError):
        return None


def _ephemeral_message(content):
    return _http_response(
        200,
        {"type": RESPONSE_CHANNEL_MESSAGE, "data": {"content": content, "flags": FLAG_EPHEMERAL}},
    )


def _handle_interaction(event, context):
    raw = _verified_body(event)
    if raw is None:
        # Discord itself sends deliberately bad signatures when the endpoint URL is saved,
        # and refuses the URL unless they are rejected with 401.
        return _http_response(401, {"error": "invalid request signature"})

    interaction = json.loads(raw)
    if interaction.get("type") == INTERACTION_PING:
        return _http_response(200, {"type": RESPONSE_PONG})

    if interaction.get("type") != INTERACTION_APPLICATION_COMMAND:
        return _http_response(400, {"error": "unsupported interaction type"})

    if interaction.get("data", {}).get("name") != COMMAND_NAME:
        return _ephemeral_message("未対応のコマンドです。")

    # The command is registered only in this guild; this check is the second line of defense.
    if interaction.get("guild_id") != os.environ["GUILD_ID"]:
        logger.warning("Rejected command from guild_id=%s", interaction.get("guild_id"))
        return _ephemeral_message("このサーバーでは使えません。")

    options = {opt["name"]: opt["value"] for opt in interaction["data"].get("options", [])}
    days = min(max(int(options.get("days", DEFAULT_DAYS)), 1), MAX_DAYS)
    public = bool(options.get("public", False))

    lambda_client.invoke(
        FunctionName=context.function_name,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "action": "collect",
                "application_id": interaction["application_id"],
                "interaction_token": interaction["token"],
                "days": days,
                "accepted_at": time.time(),
            }
        ).encode("utf-8"),
    )
    logger.info("Accepted command: days=%s public=%s", days, public)

    # The deferred response decides the final visibility: ephemeral here means the edited
    # result is ephemeral too.
    data = {} if public else {"flags": FLAG_EPHEMERAL}
    return _http_response(200, {"type": RESPONSE_DEFERRED_CHANNEL_MESSAGE, "data": data})


# ---------------------------------------------------------------------------
# Collection (async self-invocation)
# ---------------------------------------------------------------------------


class _OutOfTime(Exception):
    pass


def _bot_token():
    global _bot_token_cache
    if _bot_token_cache is None:
        response = ssm.get_parameter(Name=os.environ["SSM_PARAM_BOT_TOKEN"], WithDecryption=True)
        _bot_token_cache = response["Parameter"]["Value"]
    return _bot_token_cache


def _check_deadline(stop_at):
    if stop_at is not None and time.monotonic() >= stop_at:
        raise _OutOfTime()


def _sleep_with_deadline(seconds, stop_at):
    if stop_at is not None and seconds >= stop_at - time.monotonic():
        raise _OutOfTime()
    time.sleep(seconds)


def _discord_request(method, path, body=None, authenticated=True, stop_at=None):
    headers = {"User-Agent": USER_AGENT}
    if authenticated:
        headers["Authorization"] = f"Bot {_bot_token()}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
        _check_deadline(stop_at)
        remaining = stop_at - time.monotonic() if stop_at is not None else REQUEST_TIMEOUT
        timeout = min(REQUEST_TIMEOUT, max(0.1, remaining))
        request = urllib.request.Request(DISCORD_API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    delay = float(response.headers.get("X-RateLimit-Reset-After") or 1)
                    # Never discard a page already read: if the wait does not fit, skip it and
                    # let the next request's deadline check stop the crawl.
                    if stop_at is None or delay < stop_at - time.monotonic():
                        time.sleep(delay)
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == RATE_LIMIT_MAX_ATTEMPTS:
                raise
            try:
                retry_after = float(json.loads(error.read()).get("retry_after", 1))
            except (ValueError, AttributeError):
                retry_after = 1.0
            finally:
                error.close()
            # Never log a webhook URL containing the short-lived interaction token.
            safe_path = "/webhooks/[redacted]" if path.startswith("/webhooks/") else path.split("?")[0]
            logger.warning("Rate limited on %s %s; retrying in %ss", method, safe_path, retry_after)
            _sleep_with_deadline(max(0.0, retry_after), stop_at)
        except (urllib.error.URLError, TimeoutError):
            # The socket timeout is clipped to the deadline, so a timeout at the deadline is
            # "out of time" (partial result), not a failure of the whole run.
            if stop_at is not None and time.monotonic() >= stop_at:
                raise _OutOfTime()
            raise
    raise RuntimeError("unreachable")


def _snowflake_ms(snowflake):
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


def _scan_channel(channel_id, cutoff_ms, stats, stop_at):
    """Count custom emoji on messages posted within the window (not reaction events)."""
    scanned = 0
    before = None
    while True:
        query = {"limit": 100}
        if before:
            query["before"] = before
        messages = _discord_request(
            "GET", f"/channels/{channel_id}/messages?{urllib.parse.urlencode(query)}",
            stop_at=stop_at,
        )
        if not messages:
            return scanned

        for message in messages:
            if _snowflake_ms(message["id"]) < cutoff_ms:
                return scanned
            scanned += 1
            author = message.get("author", {})
            if not author.get("bot") and not message.get("webhook_id"):
                for emoji_id in CUSTOM_EMOJI.findall(message.get("content") or ""):
                    if emoji_id in stats:
                        stats[emoji_id]["content"] += 1
            # Discord returns current reaction counts, without per-reaction timestamps.
            for reaction in message.get("reactions", []):
                emoji_id = reaction.get("emoji", {}).get("id")
                if emoji_id in stats:
                    stats[emoji_id]["reaction"] += reaction.get("count", 0)

        if len(messages) < 100:
            return scanned
        before = messages[-1]["id"]


def _aggregate(guild_id, days, stop_at):
    emojis = _discord_request("GET", f"/guilds/{guild_id}/emojis", stop_at=stop_at)
    stats = {emoji["id"]: {"emoji": emoji, "content": 0, "reaction": 0} for emoji in emojis}
    guild_channels = _discord_request("GET", f"/guilds/{guild_id}/channels", stop_at=stop_at)
    channels = [c for c in guild_channels if c.get("type") in READABLE_CHANNEL_TYPES]
    active_threads = _discord_request(
        "GET", f"/guilds/{guild_id}/threads/active", stop_at=stop_at
    ).get("threads", [])

    cutoff_ms = int(time.time() * 1000) - days * 86_400_000
    summary = {"channels": 0, "messages": 0, "skipped": 0, "timed_out": False}

    try:
        for channel in channels + active_threads:
            # An absent last_message_id does not prove there is no recent history.
            last_id = channel.get("last_message_id")
            if last_id and _snowflake_ms(last_id) < cutoff_ms:
                continue
            try:
                summary["messages"] += _scan_channel(channel["id"], cutoff_ms, stats, stop_at)
                summary["channels"] += 1
            except urllib.error.HTTPError as error:
                # 401 means a wrong bot token: every channel would fail, so fail the run.
                if error.code == 401:
                    raise
                # 403/404 (no permission) or a transient 5xx/429: skip just this channel.
                summary["skipped"] += 1
            except (urllib.error.URLError, TimeoutError):
                # One network hiccup should not throw away every other channel's counts.
                summary["skipped"] += 1
    except _OutOfTime:
        summary["timed_out"] = True

    return stats, summary


def _emoji_mention(emoji):
    prefix = "a" if emoji.get("animated") else ""
    return f"<{prefix}:{emoji['name']}:{emoji['id']}>"


def _build_embed(stats, summary, days):
    rows = sorted(
        stats.values(),
        key=lambda row: row["content"] + row["reaction"],
        reverse=True,
    )
    grand_total = sum(row["content"] + row["reaction"] for row in rows)
    used = [row for row in rows if row["content"] + row["reaction"] > 0]
    unused = [row for row in rows if row["content"] + row["reaction"] == 0]

    lines = []
    if not rows:
        lines.append("このサーバーにはカスタム絵文字がありません。")
    else:
        content_total = sum(row["content"] for row in rows)
        reaction_total = sum(row["reaction"] for row in rows)
        lines.append(f"計 **{grand_total}** 回（本文 {content_total} / リアクション {reaction_total}）")
        lines.append("")
        for rank, row in enumerate(used[:TOP_N], start=1):
            total = row["content"] + row["reaction"]
            share = total / grand_total * 100
            lines.append(
                f"{rank}. {_emoji_mention(row['emoji'])} **{total}** 回 ({share:.1f}%)"
                f"　本文 {row['content']} / リアクション {row['reaction']}"
            )
        if len(used) > TOP_N:
            lines.append(f"…ほか {len(used) - TOP_N} 個は使用あり")

    description = "\n".join(lines)
    if unused:
        description += f"\n\n**未使用 {len(unused)} 個**\n"
        for index, row in enumerate(unused):
            mention = _emoji_mention(row["emoji"]) + " "
            rest = f"…ほか {len(unused) - index} 個"
            if len(description) + len(mention) + len(rest) > EMBED_DESCRIPTION_LIMIT:
                description += rest
                break
            description += mention

    footer = f"{summary['channels']} チャンネル・{summary['messages']} 件のメッセージを集計"
    if summary["skipped"]:
        footer += f"／取得不可 {summary['skipped']} チャンネル"
    if summary["timed_out"]:
        footer += "／⚠ 時間切れのため一部のチャンネルは未集計"

    return {
        "title": f"カスタム絵文字の使用率（直近 {days} 日）",
        "description": description[:EMBED_DESCRIPTION_LIMIT],
        "footer": {"text": footer},
    }


def _handle_collect(event, context):
    days = int(event["days"])
    started = time.time()
    try:
        # The token clock starts at acceptance, so time spent in the async queue counts too.
        lambda_remaining = context.get_remaining_time_in_millis() / 1000
        accepted_at = float(event.get("accepted_at", time.time()))
        token_remaining = accepted_at + INTERACTION_TOKEN_TTL_SECONDS - time.time()
        scan_seconds = max(0.0, min(lambda_remaining, token_remaining) - TIME_RESERVE_MS / 1000)
        stop_at = time.monotonic() + scan_seconds
        stats, summary = _aggregate(os.environ["GUILD_ID"], days, stop_at)
        payload = {
            "content": "",
            "embeds": [_build_embed(stats, summary, days)],
            "allowed_mentions": {"parse": []},
        }
        logger.info("Collect completed in %.1fs: %s", time.time() - started, summary)
    except Exception:
        logger.exception("Collect failed")
        payload = {
            "content": "集計に失敗しました。CloudWatch Logs を確認してください。",
            "allowed_mentions": {"parse": []},
        }
        summary = None

    # Editing the deferred response authenticates with the interaction token in the path,
    # so no bot token is sent here.
    _discord_request(
        "PATCH",
        f"/webhooks/{event['application_id']}/{event['interaction_token']}/messages/@original",
        body=payload,
        authenticated=False,
    )
    return {"status": "ok" if summary else "failed", "summary": summary}
