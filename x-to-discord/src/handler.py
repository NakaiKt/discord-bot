import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

X_API_BASE = "https://api.x.com/2"

# Discord's edge rejects requests that keep urllib's default "Python-urllib/x.y" User-Agent,
# answering with a bodyless 403 before the request ever reaches the webhook. Every outbound
# request therefore sends an explicit User-Agent.
USER_AGENT = "x-to-discord-forwarder (https://github.com/NakaiKt/discord-bot, 1.0)"

REQUEST_TIMEOUT = 15
DISCORD_MAX_ATTEMPTS = 3
# Longer waits than this would risk the Lambda timeout; the next scheduled run retries instead.
DISCORD_RETRY_AFTER_CAP = 5.0


def _get_ssm_parameter(name, decrypt):
    response = ssm.get_parameter(Name=name, WithDecryption=decrypt)
    return response["Parameter"]["Value"]


def _get_last_tweet_id(state_param_name):
    try:
        return _get_ssm_parameter(state_param_name, decrypt=False)
    except ssm.exceptions.ParameterNotFound:
        return None


def _set_last_tweet_id(state_param_name, tweet_id):
    ssm.put_parameter(Name=state_param_name, Value=tweet_id, Type="String", Overwrite=True)


def _fetch_tweets(bearer_token, user_id, since_id):
    params = {
        "exclude": "replies,retweets",
        "max_results": "100" if since_id else "5",
    }
    if since_id:
        params["since_id"] = since_id

    url = f"{X_API_BASE}/users/{user_id}/tweets?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.load(response)


def _describe_http_error(error):
    """Summarize an HTTPError for logs.

    A 403 from Discord is ambiguous without this: an edge block (bodyless or an HTML page
    carrying a Cloudflare "error code: 10xx") and an application-level rejection
    (JSON such as {"message": "Missing Permissions", "code": 50013}) share the status code.
    """
    try:
        body = error.read().decode("utf-8", errors="replace").strip()
    except OSError:
        body = "<unreadable>"
    return f"status={error.code} server={error.headers.get('Server')} body={body[:500]!r}"


def _retry_after_seconds(error):
    """Seconds Discord asks us to wait after a 429, or None when it did not say."""
    header = error.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _post_to_discord(webhook_url, tweet_id):
    link = f"https://x.com/i/web/status/{tweet_id}"
    body = json.dumps({"content": link}).encode("utf-8")

    for attempt in range(1, DISCORD_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                response.read()
            return
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == DISCORD_MAX_ATTEMPTS:
                raise
            retry_after = _retry_after_seconds(error)
            if retry_after is None or retry_after > DISCORD_RETRY_AFTER_CAP:
                raise
            # Only closed on the retry path: the caller still needs to read the body
            # of an error we re-raise.
            error.close()
            logger.warning(
                "Discord rate limited for tweet_id=%s (attempt %s/%s); retrying in %ss",
                tweet_id,
                attempt,
                DISCORD_MAX_ATTEMPTS,
                retry_after,
            )
            time.sleep(retry_after)


def lambda_handler(event, context):
    user_id = os.environ["X_USER_ID"]
    bearer_token = _get_ssm_parameter(os.environ["SSM_PARAM_X_BEARER_TOKEN"], decrypt=True)
    webhook_url = _get_ssm_parameter(os.environ["SSM_PARAM_DISCORD_WEBHOOK_URL"], decrypt=True)
    state_param_name = os.environ["SSM_PARAM_STATE"]

    last_tweet_id = _get_last_tweet_id(state_param_name)

    try:
        payload = _fetch_tweets(bearer_token, user_id, last_tweet_id)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            logger.warning("X API rate limited; skipping this run")
            return {"status": "rate_limited"}
        raise
    except urllib.error.URLError:
        logger.exception("X API request failed; skipping this run")
        return {"status": "fetch_failed"}

    if last_tweet_id is None:
        newest_id = payload.get("meta", {}).get("newest_id")
        if newest_id:
            _set_last_tweet_id(state_param_name, newest_id)
            logger.info("First run: recorded newest_id=%s without sending", newest_id)
        else:
            logger.info("First run: no posts found yet; state left unset")
        return {"status": "initialized"}

    tweets = payload.get("data", [])
    # X returns newest-first; sort oldest-first so Discord receives posts in chronological order.
    tweets.sort(key=lambda tweet: int(tweet["id"]))

    sent_count = 0
    for tweet in tweets:
        try:
            _post_to_discord(webhook_url, tweet["id"])
        except urllib.error.HTTPError as error:
            # Checked before URLError, which it subclasses, so the response details survive.
            logger.error(
                "Discord send failed for tweet_id=%s; stopping run (%s)",
                tweet["id"],
                _describe_http_error(error),
            )
            break
        except urllib.error.URLError:
            logger.exception("Discord send failed for tweet_id=%s; stopping run", tweet["id"])
            break
        # Advance state per tweet, right after each send, so a mid-run failure never re-sends
        # an already-posted tweet on the next poll.
        _set_last_tweet_id(state_param_name, tweet["id"])
        sent_count += 1

    return {"status": "ok", "sent": sent_count}
