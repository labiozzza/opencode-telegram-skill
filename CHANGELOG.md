# Changelog

## 0.4.0

- Add explicitly scoped `telegram media download` and date-bounded `telegram media export`.
- Store originals outside the repository by default with size caps, no-overwrite atomic publication, SHA-256, deduplication, and a JSONL source manifest; account for bytes received by aborted transfers.
- Keep forum-topic media in separate folders; keep downloads separate from chat export and analysis.
- Preserve successful source records and partial export progress; preserve existing authorization and prevent implicit chat selection or message side effects.
- Add `telegram dialogs` and resumable `telegram participants` JSON/CSV exports with owner-role metadata, `user_id` indexing, and explicit API completeness reporting.
- Keep attachment interpretation outside the CLI: agents may use their available native tools on local downloads; no bundled parser, OCR, or transcription is run.
- Repair a truncated final participant JSONL record before resuming the standalone participant export.
- Recover media files published before an interrupted manifest write, and preserve complete final JSONL records without a newline.
- Replace unsafe Telegram-provided file suffixes, harden existing media paths before reuse, and roll back a published original if staging cleanup fails.
- Defer account-wide participant matching, conversation search, and review catalogs to the separate v0.5.0 stage.

## 0.3.0

- Serialize Telethon session lifetimes across processes with a bounded lock.
- Distinguish SQLite lock, corruption, inaccessible files, network failures, and rejected authorization.
- Add `telegram diagnose` with read-only SQLite integrity and bounded authorization diagnostics.
- Preserve the session and prohibit automatic retries for message sends.

## 0.2.0

- Show Telegram's actual login-code delivery and any server-provided resend method and wait.
- Allow one explicit code resend per auth command.
- Add `telegram auth --qr` with a locally rendered terminal QR, interactive expiry renewal, and 2FA support.

## 0.1.0

- First public release of the OpenCode Telegram skill.
- Includes personal-account chat reading/export, explicitly confirmed messaging and bot callback interactions, and guarded broadcast-channel deletion.
- Adds cached GitHub release checks and an explicit update command.
