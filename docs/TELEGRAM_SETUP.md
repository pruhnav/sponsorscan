# Telegram Alert Setup

SponsorScan can message you on Telegram whenever a report finds new jobs. Each
run sends one message that lists the new matches, with an **Open** button for
each, so your phone buzzes once per run rather than once per job.

Telegram alerts are optional, and they are the quickest extra to set up: there
is no Google account, password or key file involved. You need:

- the Telegram app, on your phone or computer;
- a Telegram bot, which you create in step 1 (free, about a minute);
- a generated new-jobs CSV file.

The sender ships as `scripts/send_telegram.py`. Its `--check` flag confirms the
bot and your chat without sending anything.

## 1. Create your bot

1. In Telegram, search for **@BotFather** and open it. It is Telegram's own bot
   for making bots; check that it has the blue verified mark.
2. Send `/newbot`.
3. Choose a display name, such as `SponsorScan Alerts`.
4. Choose a username. It must end in `bot`, such as `alex_sponsorscan_bot`.
5. BotFather replies with a **token**: a long code like
   `123456789:AAH...`. This is the bot's password. Do not paste it into chats,
   issues or commits. If it leaks, send `/revoke` to BotFather for a new one.

## 2. Say hello to your bot

A bot cannot start a conversation, so open the link BotFather gave you
(`t.me/your_bot_name`) and press **Start**, or send it any message.

## 3. Find your chat ID and test

Run these in your own terminal, from your SponsorScan folder. The token is read
without showing it on screen.

PowerShell (Windows):

```powershell
$env:TELEGRAM_BOT_TOKEN = Read-Host "Bot token"
python scripts/send_telegram.py --find-chat
```

macOS or Linux:

```bash
read -s TELEGRAM_BOT_TOKEN; export TELEGRAM_BOT_TOKEN
python scripts/send_telegram.py --find-chat
```

It prints a line like `TELEGRAM_CHAT_ID=987654321  (Alex)`. If it says no
messages were found, go back to step 2 and send the bot a message.

Set the chat ID it printed, then check and send a test message:

```powershell
$env:TELEGRAM_CHAT_ID = "987654321"
python scripts/send_telegram.py --check
python scripts/send_telegram.py --test
```

On macOS or Linux use `export TELEGRAM_CHAT_ID="987654321"` for the first line.
A "SponsorScan is connected" message should arrive in Telegram.

## 4. Send real alerts

After a report has written its new-jobs CSV, run:

```powershell
python scripts/send_telegram.py
```

It reads `new_jobs_48h.csv` unless `NEW_JOBS_CSV` names your profile's file
(see `output_files` in your profile JSON). With no new jobs it sends nothing and
says so.

## 5. Get alerts automatically

Alerts arrive on their own once the GitHub Actions schedule runs SponsorScan
(see `docs/REFERENCE.md#github-actions-automation`). Add two repository secrets
under **Settings > Secrets and variables > Actions > New repository secret**:

- `TELEGRAM_BOT_TOKEN`: the token from step 1
- `TELEGRAM_CHAT_ID`: the number from step 3

With the GitHub CLI signed in, the token can go in without being typed into a
file (it prompts for the value):

```powershell
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID --body "987654321"
```

The workflow's Telegram step skips itself until both secrets exist.

## When something goes wrong

| Message says | What to do |
|---|---|
| "rejected the bot token" | Copy the token again from BotFather: `/mybots`, choose the bot, **API Token**. |
| "cannot find that chat" | Send the bot a message, run `--find-chat`, and use the number it prints. |
| "not allowed to message this chat" | You blocked the bot. Unblock it in Telegram and send it a message. |
| "Could not reach Telegram" | Check your internet connection, then try again. |
