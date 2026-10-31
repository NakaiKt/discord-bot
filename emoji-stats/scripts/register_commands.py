"""Register (or overwrite) the /emoji-stats command in one guild.

Run once after creating the bot, and again whenever COMMANDS changes.
Reads DISCORD_APPLICATION_ID, DISCORD_GUILD_ID and DISCORD_BOT_TOKEN from the environment
and never prints the token.
"""

import json
import os
import sys
import urllib.error
import urllib.request

USER_AGENT = "emoji-stats (https://github.com/NakaiKt/discord-bot, 1.0)"

ADMINISTRATOR = str(1 << 3)

COMMANDS = [
    {
        "name": "emoji-stats",
        "type": 1,
        "description": "このサーバーのカスタム絵文字の使用率を集計します",
        # Default: administrators only. Server settings > Integrations can widen or narrow
        # this per role / channel without re-registering.
        "default_member_permissions": ADMINISTRATOR,
        "options": [
            {
                "type": 4,
                "name": "days",
                "description": "集計する日数（既定 30、最大 90）",
                "min_value": 1,
                "max_value": 90,
                "required": False,
            },
            {
                "type": 3,
                "name": "order",
                "description": "表示する順位（既定: 上位）",
                "required": False,
                "choices": [
                    {"name": "上位（使用回数が多い順）", "value": "top"},
                    {"name": "下位（未使用 → 使用回数が少ない順）", "value": "bottom"},
                ],
            },
        ],
    }
]


def main():
    try:
        application_id = os.environ["DISCORD_APPLICATION_ID"]
        guild_id = os.environ["DISCORD_GUILD_ID"]
        bot_token = os.environ["DISCORD_BOT_TOKEN"]
    except KeyError as missing:
        sys.exit(f"Missing environment variable: {missing}")

    # Bulk overwrite: the guild ends up with exactly COMMANDS, so re-running is safe.
    request = urllib.request.Request(
        f"https://discord.com/api/v10/applications/{application_id}/guilds/{guild_id}/commands",
        data=json.dumps(COMMANDS).encode("utf-8"),
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            registered = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        sys.exit(f"Registration failed: status={error.code} body={body[:500]}")

    for command in registered:
        print(f"Registered /{command['name']} (id={command['id']})")


if __name__ == "__main__":
    main()
