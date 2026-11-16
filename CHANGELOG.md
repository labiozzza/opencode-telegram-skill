# Changelog

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
