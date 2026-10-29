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

# Stop crawling with this much Lambda time left, so the partial result can still be posted.
# The interaction token is valid for 15 minutes, the same as the Lambda timeout ceiling.
TIME_RESERVE_MS = 60_000

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
    """Return the raw request body when Discord's Ed25519 signature is valid, else None."""
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    signature = headers.get("x-signature-ed25519")
    timestamp = headers.get("x-signature-timestamp")
    if not signature or not timestamp:
        return None

    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")

    try:
        verify_key = VerifyKey(bytes.fromhex(os.environ["DISCORD_PUBLIC_KEY"]))
        verify_key.verify(timestamp.encode("utf-8") + raw, bytes.fromhex(signature))
    except (BadSignatureError, ValueError):
        return None
    return raw


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


def _discord_request(method, path, body=None, authenticated=True):
    headers = {"User-Agent": USER_AGENT}
    if authenticated:
        headers["Authorization"] = f"Bot {_bot_token()}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(DISCORD_API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = response.read()
                # Pace ourselves before the bucket runs dry instead of eating a 429.
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    time.sleep(float(response.headers.get("X-RateLimit-Reset-After") or 1))
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == RATE_LIMIT_MAX_ATTEMPTS:
                raise
            try:
                retry_after = float(json.loads(error.read()).get("retry_after", 1))
            except (ValueError, AttributeError):
                retry_after = 1.0
            error.close()
            logger.warning("Rate limited on %s %s; retrying in %ss", method, path, retry_after)
            time.sleep(retry_after)
    raise RuntimeError("unreachable")


def _snowflake_ms(snowflake):
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


def _has_time(context):
    return context.get_remaining_time_in_millis() > TIME_RESERVE_MS


def _scan_channel(channel_id, cutoff_ms, stats, context):
    """Count emoji in one channel's messages newer than cutoff. Returns the message count."""
    scanned = 0
    before = None
    while True:
        if not _has_time(context):
            raise _OutOfTime()
        query = {"limit": 100}
        if before:
            query["before"] = before
        messages = _discord_request(
            "GET", f"/channels/{channel_id}/messages?{urllib.parse.urlencode(query)}"
        )
        if not messages:
            return scanned

        # Messages come newest-first, so the first one older than cutoff ends the channel.
        for message in messages:
            if _snowflake_ms(message["id"]) < cutoff_ms:
                return scanned
            scanned += 1
            # Bots' text is not "usage"; reactions on bot posts (e.g. forwarded X posts) are.
            if not message.get("author", {}).get("bot"):
                for emoji_id in CUSTOM_EMOJI.findall(message.get("content", "")):
                    if emoji_id in stats:
                        stats[emoji_id]["content"] += 1
            for reaction in message.get("reactions", []):
                emoji_id = reaction.get("emoji", {}).get("id")
                if emoji_id in stats:
                    stats[emoji_id]["reaction"] += reaction.get("count", 0)

        if len(messages) < 100:
            return scanned
        before = messages[-1]["id"]


def _aggregate(guild_id, days, context):
    emojis = _discord_request("GET", f"/guilds/{guild_id}/emojis")
    stats = {emoji["id"]: {"emoji": emoji, "content": 0, "reaction": 0} for emoji in emojis}

    channels = [
        channel
        for channel in _discord_request("GET", f"/guilds/{guild_id}/channels")
        if channel.get("type") in READABLE_CHANNEL_TYPES
    ]
    threads = _discord_request("GET", f"/guilds/{guild_id}/threads/active").get("threads", [])

    cutoff_ms = int(time.time() * 1000) - days * 86_400_000
    summary = {"channels": 0, "messages": 0, "skipped": 0, "timed_out": False}

    for channel in channels + threads:
        last_message_id = channel.get("last_message_id")
        # Nothing posted in the window: skip without spending a request.
        if not last_message_id or _snowflake_ms(last_message_id) < cutoff_ms:
            continue
        try:
            summary["messages"] += _scan_channel(channel["id"], cutoff_ms, stats, context)
            summary["channels"] += 1
        except _OutOfTime:
            summary["timed_out"] = True
            break
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                # No View Channel / Read Message History permission there.
                summary["skipped"] += 1
                continue
            raise

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
        footer += f"／閲覧権限がない {summary['skipped']} チャンネルはスキップ"
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
        stats, summary = _aggregate(os.environ["GUILD_ID"], days, context)
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
