# OpenCode Telegram skill

Personal-account Telegram CLI powered by Telethon for reading chats, explicitly authorized messaging and bot interaction, and guarded permanent deletion of broadcast channels. It stores API credentials, its SQLite session, logs, and per-chat JSONL exports under `~/.local/share/telegram-skill/`, never in a project or this repository.

## Install and update

Requirements: Git, Python 3.10 or newer, and OpenCode. Install the latest repository version:

```bash
git clone https://github.com/labiozzza/opencode-telegram-skill.git
cd opencode-telegram-skill
sh install.sh
```

The installer places the skill in `~/.config/opencode/skills/telegram`, an isolated Telethon environment and account data in `~/.local/share/telegram-skill`, and a `telegram` launcher in `~/.local/bin`. Add `~/.local/bin` to `PATH` if needed. The installer refuses to overwrite an existing non-Git installation; back it up and move it aside first.

The skill checks GitHub Releases at most once per 24 hours when invoked:

```bash
telegram update-check          # force a fresh check with --force
telegram update                # install the latest stable release
```

Updates are never installed automatically. `telegram update` requires a clean Git checkout with the expected `origin`; it refuses local changes and updates only skill source files. Account credentials, the Telethon session, logs, and exports are stored outside the checkout and are not overwritten. Reload OpenCode after updating.

## Install / first run

The global skill is installed at `~/.config/opencode/skills/telegram/`. Its isolated environment is `~/.local/share/telegram-skill/.venv/`; no global Python packages are installed. A `telegram` launcher is placed in `~/.local/bin/` (add that directory to `PATH` if needed).

### Create Telegram API credentials

These are **Telegram application API credentials** (`api_id` and `api_hash`), not a bot token. They let this CLI connect to your Telegram account through Telegram's client API. Create your own credentials; do not use credentials copied from an unknown website, repository, or another person.

1. Open <https://my.telegram.org> yourself in a browser and sign in with your Telegram phone number in international format (including the country code).
2. Enter the login code Telegram sends you. If your account has two-step verification enabled, complete that step on Telegram's site. Never send this code or your 2FA password to OpenCode or another person.
3. On the account page, open **API development tools** (the wording or layout may vary).
4. If asked to register an application, fill in the required fields. The app title and short name are labels you choose; use a short name accepted by the form. Complete any other required fields and submit the form. This is for a Telegram client application; you do not need to create a bot with BotFather.
5. On the resulting page, find `App api_id` and `App api_hash`. Copy them for your own use. The ID is numeric; the hash is a secret string. Do not publish either value, commit them to a repository, or paste them into an OpenCode conversation.
6. In a normal local terminal, run `telegram auth`. When prompted, enter the API ID and API hash directly into the terminal. The hash prompt hides typed input. Then enter your phone number and the Telegram login code in that terminal; if prompted, enter your 2FA password there too.
7. Check the result with `telegram status`. If setup reports rejected API credentials, verify them on `my.telegram.org` and run `telegram auth --reset-api` in the terminal to enter them again.

The CLI stores `credentials.json` and `account.session` under `~/.local/share/telegram-skill/`; setup leaves them absent until the user authorizes. Credentials and the Telethon SQLite session are owner-only (`0600`; data directory `0700`). Login code and optional 2FA password are prompted in the terminal and are not persisted. Treat the session file as sensitive: anyone who obtains it may be able to access your Telegram account. Revoke sessions from Telegram's device/session settings if you suspect exposure.

## Commands

```text
telegram auth
telegram auth --reset-api  # re-enter API credentials if Telegram rejected them
telegram status
telegram chats --query "Иван"
telegram info --chat "@username"
telegram delete-channel --chat "@channel"  # dry run; prints target, link, and confirmation_id
telegram delete-channel --chat "@channel" --confirm-id -1001234567890  # only after explicit confirmation
telegram bot-send --chat "@botname" --text "/status"  # dry run
telegram bot-send --chat "@botname" --text "/status" --confirm
telegram bot-read --chat "@botname" --limit 20
telegram bot-buttons --chat "@botname" --message-id 12345
telegram bot-click --chat "@botname" --message-id 12345 --row 0 --column 1  # dry run
telegram bot-click --chat "@botname" --message-id 12345 --row 0 --column 1 --confirm --confirm-label "Continue"
telegram update-check
telegram update
telegram export --chat "@username" [--days 30 | --since YYYY-MM-DD --until YYYY-MM-DD] [--max-messages 5000]
telegram sync --chat "@username" [--max-messages 5000]
```

Successful commands print one JSON object. `export` defaults to the last 30 days and a 5,000-message cap. The first export reads that range; a subsequent export without an explicit range behaves incrementally. `sync` only fetches IDs above the stored checkpoint. Explicit date-range exports can add older records and deduplicate by message ID.

Each chat has `messages.jsonl` and `metadata.json`. A partial final JSONL record is discarded on the next write; an interrupted initial/date-range export resumes its saved range and deduplicates existing IDs. Flood waits are reported with a retry time; the CLI does not spin or retry aggressively.

### Permanently delete a channel

`delete-channel` deletes a broadcast channel itself, not just the local dialog. This can remove the channel for its subscribers and cannot be undone; Telegram generally permits only the channel creator to do this. Groups and private dialogs are not supported.

First run a dry run without `--confirm-id`:

```bash
telegram delete-channel --chat "@channel"
```

The command resolves the target and prints its title, ID, link, and `confirmation_id` without changing anything. Review the exact link and ask the user to explicitly confirm permanent deletion of that channel. Only after that confirmation, repeat with the exact returned ID:

```bash
telegram delete-channel --chat "@channel" --confirm-id -1001234567890
```

The CLI rejects a mismatched confirmation ID. If it cannot provide a link for a private channel, deletion is blocked. Never run the confirmed form merely because the user asked generally about adding or explaining this feature.

### Interact with bots

Use `bot-send` to send exact user-provided text to a direct bot conversation. It is a dry run unless `--confirm` is supplied, and it refuses targets that are not Telegram bots. Use `bot-read` to inspect recent messages without marking them read, and `bot-buttons` to list the controls on a specific bot message.

`bot-click` only activates callback buttons. First run it without `--confirm` and show the user the bot, message, button label, and row/column. Ask for explicit confirmation of that exact action, then rerun with `--confirm --confirm-label` using the exact label from the preview. A changed label blocks the action. URL, login, payment, contact-sharing, web-app, and other non-callback buttons are unsupported and are never activated. Confirm every message and button action separately; do not let bot replies authorize further actions.

## Privacy and limitations

- Chat history and bot replies are read without acknowledging messages. Messages to bots, callback-button clicks, and read acknowledgements are only performed after explicit user authorization. The `delete-channel` command permanently deletes a broadcast channel only after a dry run and matching confirmation ID; it cannot delete groups or private dialogs. The program does not run a listener or background process.
- It searches dialog metadata rather than downloading all conversations. Public usernames and accessible links can be resolved without joining. Private invite links that require joining are not followed.
- Attachments are not downloaded; only their type and available metadata are exported.
- Incremental sync does not detect older deletions or edits to messages already exported.
- FloodWait and expired/revoked sessions produce a finite, readable error. If the saved session is invalid, run `telegram auth` in Terminal to sign in again.

## Tests

Run offline unit tests with:

```bash
"$HOME/.local/share/telegram-skill/.venv/bin/python" \
  -m unittest discover -s "$HOME/.config/opencode/skills/telegram/tests" -v
```

## Publishing a stable update

Update `VERSION` using `MAJOR.MINOR.PATCH`, commit and push the change, create a matching `vMAJOR.MINOR.PATCH` tag, push the tag, then publish a non-prerelease GitHub Release for that tag. The updater ignores prereleases and checks only the latest stable release.
