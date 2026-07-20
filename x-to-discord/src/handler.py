import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

X_API_BASE = "https://api.x.com/2"


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
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer_token}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _post_to_discord(webhook_url, tweet_id):
    link = f"https://x.com/i/web/status/{tweet_id}"
    body = json.dumps({"content": link}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


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
        except urllib.error.URLError:
            logger.exception("Discord send failed for tweet_id=%s; stopping run", tweet["id"])
            break
        # Advance state per tweet, right after each send, so a mid-run failure never re-sends
        # an already-posted tweet on the next poll.
        _set_last_tweet_id(state_param_name, tweet["id"])
        sent_count += 1

    return {"status": "ok", "sent": sent_count}
