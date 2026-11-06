#!/usr/bin/env python3
"""
Send SponsorScan's newly discovered jobs to you on Telegram.

One message lists the new matches, with an "Open" button per job, so your
phone buzzes once per run rather than once per job.

Required environment variables
------------------------------
TELEGRAM_BOT_TOKEN
    The token @BotFather gave you when you created the bot.

TELEGRAM_CHAT_ID
    Your chat with the bot. Send the bot any message, then run this script
    with --find-chat to print it. Not needed for --find-chat itself.

Optional environment variables
------------------------------
NEW_JOBS_CSV
    Path to the new-jobs CSV file.
    Default: new_jobs_48h.csv

SPONSORSCAN_PROFILE_NAME
    Display name included in the message.
    Default: SponsorScan

REPORT_HOURS
    Report time window included in the message.
    Default: 48

SPONSORSCAN_DB
    Database written by fetch-jobs. Boards that failed to fetch are noted at
    the end of the message.
    Default: sponsorscan.db

Modes
-----
--find-chat  Print the chats that have messaged the bot, to get TELEGRAM_CHAT_ID.
--check      Confirm the token and the chat, sending nothing.
--test       Send one short test message.
(none)       Send the new jobs. Nothing is sent when there are none.

The token is a password for the bot: keep it out of chat logs and commits. This
script never prints it.
"""

from __future__ import annotations

import argparse
import csv
import html
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_CSV = "new_jobs_48h.csv"
DEFAULT_DB = "sponsorscan.db"
API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 4096       # Telegram's limit, in characters, after formatting
MAX_JOBS_LISTED = 15     # more than this is a digest nobody reads on a phone
MAX_BUTTONS = 10


class TelegramError(Exception):
    """A failure whose message says what to do next."""


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise TelegramError(f"Required environment variable '{name}' is missing. "
                            "See docs/TELEGRAM_SETUP.md.")
    return value


def explain(method: str, status: int, description: str) -> str:
    """Turn a Telegram error into the step that fixes it."""
    text = description.lower()
    if status == 401 or (status == 404 and method == "getMe"):
        return ("Telegram rejected the bot token. Copy it again from @BotFather "
                "(send /mybots, choose the bot, then API Token) and set "
                "TELEGRAM_BOT_TOKEN to it.")
    if "chat not found" in text:
        return ("Telegram cannot find that chat. Open your bot in Telegram, press "
                "Start or send it any message, then run this script with "
                "--find-chat and set TELEGRAM_CHAT_ID to the number it prints.")
    if status == 403:
        return ("The bot is not allowed to message this chat. If you blocked it, "
                "unblock it in Telegram and send it a message.")
    return f"Telegram returned an error for {method}: {description or status}"


def call(token: str, method: str, payload: dict | None = None, attempts: int = 3) -> dict:
    """Call one Bot API method and return its result. Waits out rate limits."""
    import requests

    url = API.format(token=token, method=method)
    for attempt in range(attempts):
        try:
            response = requests.post(url, json=payload or {}, timeout=30)
        except requests.RequestException as exc:
            # The exception text can contain the URL, and so the token.
            raise TelegramError(f"Could not reach Telegram ({type(exc).__name__}). "
                                "Check your internet connection.") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if body.get("ok"):
            return body.get("result")
        retry = (body.get("parameters") or {}).get("retry_after")
        if response.status_code == 429 and retry and attempt + 1 < attempts:
            time.sleep(min(int(retry), 30))
            continue
        raise TelegramError(explain(method, response.status_code, body.get("description", "")))
    raise TelegramError(f"Telegram kept asking to slow down on {method}. Try again later.")


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"New-jobs CSV not found: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        return [{str(key): (value or "").strip() for key, value in row.items() if key is not None}
                for row in reader if any((value or "").strip() for value in row.values())]


def load_failures(db_path: Path) -> list[str]:
    """Boards the last fetch-jobs run could not read."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(db_path)
    try:
        return [row[0] for row in con.execute("SELECT board FROM fetch_failures ORDER BY board")]
    except sqlite3.OperationalError:
        return []  # a database from before failures were recorded
    finally:
        con.close()


def safe_link(url: str) -> str:
    return url if url.startswith(("https://", "http://")) else ""


def job_block(row: dict[str, str], index: int) -> str:
    """One job, in Telegram's HTML subset. Every value is escaped."""
    esc = html.escape
    title = row.get("title") or "Untitled role"
    url = safe_link(row.get("url", ""))
    heading = f'<a href="{esc(url, quote=True)}">{esc(title)}</a>' if url else f"<b>{esc(title)}</b>"
    place = ", ".join(part for part in (row.get("company", ""), row.get("location", "")) if part)
    facts = []
    if row.get("resume_fit_score"):
        facts.append(f"Profile fit {esc(row['resume_fit_score'])}/100.")
    certified = row.get("lca_certified", "")
    if certified and certified != "0":
        facts.append(f"{esc(certified)} certified H-1B filings.")
    lines = [f"{index}. {heading}"]
    if place:
        lines.append(esc(place))
    if facts:
        lines.append(" ".join(facts))
    return "\n".join(lines)


def build_messages(rows: list[dict[str, str]], name: str, hours: str,
                   failures: list[str]) -> list[str]:
    """The digest, split into messages under Telegram's length limit."""
    count = len(rows)
    header = (f"<b>SponsorScan: {count} new job match{'es' if count != 1 else ''}</b>\n"
              f"For {html.escape(name)}, posted in the last {html.escape(hours)} hours.")
    blocks = [job_block(row, index) for index, row in enumerate(rows[:MAX_JOBS_LISTED], 1)]
    if count > MAX_JOBS_LISTED:
        blocks.append(f"And {count - MAX_JOBS_LISTED} more in your dashboard and the attached report.")
    if failures:
        names = sorted({(board.split("/") + [board])[1] for board in failures})
        blocks.append(f"<i>{len(failures)} job board{'s' if len(failures) != 1 else ''} did not respond "
                      f"({html.escape(', '.join(names[:8]))}), so some jobs may be missing.</i>")
    messages, current = [], header
    for block in blocks:
        if len(current) + 2 + len(block) > MAX_MESSAGE:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"
    messages.append(current)
    return messages


def open_buttons(rows: list[dict[str, str]]) -> dict | None:
    """An "Open" button per listed job, two to a row."""
    buttons = []
    for row in rows[:MAX_BUTTONS]:
        url = safe_link(row.get("url", ""))
        if url:
            label = f"Open: {row.get('company') or row.get('title') or 'job'}"
            buttons.append({"text": label[:60], "url": url})
    if not buttons:
        return None
    return {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}


def send(token: str, chat_id: str, text: str, markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "link_preview_options": {"is_disabled": True}}
    if markup:
        payload["reply_markup"] = markup
    call(token, "sendMessage", payload)


def find_chats(token: str) -> list[tuple[str, str]]:
    """(chat id, who) for each chat that has messaged the bot recently."""
    seen = {}
    for update in call(token, "getUpdates", {"limit": 100}) or []:
        message = update.get("message") or update.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            who = chat.get("title") or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part)
            seen[str(chat["id"])] = who or chat.get("username") or "unknown"
    return list(seen.items())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send SponsorScan's new jobs to Telegram.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--find-chat", action="store_true",
                      help="Print the chats that have messaged the bot")
    mode.add_argument("--check", action="store_true",
                      help="Confirm the token and chat without sending anything")
    mode.add_argument("--test", action="store_true", help="Send one short test message")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = require_env("TELEGRAM_BOT_TOKEN")
        if args.find_chat:
            bot = call(token, "getMe")
            chats = find_chats(token)
            if not chats:
                print(f"No messages found for @{bot.get('username')}. Open the bot in "
                      "Telegram, press Start or send it any message, then run this again.")
                return 1
            for chat_id, who in chats:
                print(f"TELEGRAM_CHAT_ID={chat_id}  ({who})")
            return 0

        chat_id = require_env("TELEGRAM_CHAT_ID")
        if args.check:
            bot = call(token, "getMe")
            chat = call(token, "getChat", {"chat_id": chat_id})
            who = chat.get("title") or chat.get("first_name") or chat_id
            print(f"Connected as @{bot.get('username')}. Messages will go to {who}. "
                  "Check passed. Nothing was sent.")
            return 0
        if args.test:
            send(token, chat_id, "<b>SponsorScan is connected.</b>\nNew job matches will arrive here.")
            print("Sent a test message. Check Telegram.")
            return 0

        csv_path = Path(os.getenv("NEW_JOBS_CSV", DEFAULT_CSV)).expanduser()
        rows = load_rows(csv_path)
        if not rows:
            print(f"No new jobs were found in {csv_path}. Skipping the Telegram alert.")
            return 0
        name = os.getenv("SPONSORSCAN_PROFILE_NAME", "SponsorScan").strip() or "SponsorScan"
        hours = os.getenv("REPORT_HOURS", "48").strip() or "48"
        failures = load_failures(Path(os.getenv("SPONSORSCAN_DB", DEFAULT_DB)).expanduser())
        messages = build_messages(rows, name, hours, failures)
        for index, text in enumerate(messages):
            # Buttons go on the last message, under the jobs they open.
            send(token, chat_id, text, open_buttons(rows) if index == len(messages) - 1 else None)
            if index + 1 < len(messages):
                time.sleep(1.1)  # Telegram asks for about one message per second per chat
        print(f"Sent {len(rows)} new job match{'es' if len(rows) != 1 else ''} to Telegram.")
        return 0
    except (TelegramError, FileNotFoundError, OSError) as exc:
        print(f"Telegram alert failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
