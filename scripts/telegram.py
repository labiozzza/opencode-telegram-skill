#!/usr/bin/env python3
"""Telegram chat export and explicitly authorized account actions for OpenCode."""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse
from filelock import FileLock, Timeout as FileLockTimeout

from telethon import TelegramClient, types, utils
from telethon.tl.functions.channels import DeleteChannelRequest
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.errors import (
    ApiIdInvalidError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

try:
    from .updater import apply_update, check_for_update
    from . import participants as participants_module
    from . import folders_forums as folders_forums_module
    from .media import (
        DEFAULT_MAX_FILE_BYTES,
        DEFAULT_MAX_FILES,
        DEFAULT_MAX_MESSAGES,
        DEFAULT_MAX_TOTAL_BYTES,
        MAX_FILE_BYTES_LIMIT,
        MAX_FILES_LIMIT,
        MAX_MESSAGES_LIMIT,
        MAX_TOTAL_BYTES_LIMIT,
        MEDIA_TYPES,
        MediaStorageError,
        MediaStore,
        attachment_descriptor,
        download_attachment,
        is_network_error,
        media_error,
        skipped_attachment,
    )
    from .participants import (
        DEFAULT_MAX_DIALOGS,
        DEFAULT_MAX_PARTICIPANTS,
        MAX_DIALOGS_LIMIT,
        MAX_PARTICIPANTS_LIMIT,
    )
except ImportError:  # Running this file directly from the installed scripts directory.
    from updater import apply_update, check_for_update
    import participants as participants_module
    import folders_forums as folders_forums_module
    from media import (
        DEFAULT_MAX_FILE_BYTES,
        DEFAULT_MAX_FILES,
        DEFAULT_MAX_MESSAGES,
        DEFAULT_MAX_TOTAL_BYTES,
        MAX_FILE_BYTES_LIMIT,
        MAX_FILES_LIMIT,
        MAX_MESSAGES_LIMIT,
        MAX_TOTAL_BYTES_LIMIT,
        MEDIA_TYPES,
        MediaStorageError,
        MediaStore,
        attachment_descriptor,
        download_attachment,
        is_network_error,
        media_error,
        skipped_attachment,
    )
    from participants import (
        DEFAULT_MAX_DIALOGS,
        DEFAULT_MAX_PARTICIPANTS,
        MAX_DIALOGS_LIMIT,
        MAX_PARTICIPANTS_LIMIT,
    )

logging.basicConfig(level=logging.ERROR)
logging.getLogger("telethon").setLevel(logging.ERROR)

UTC = timezone.utc
DEFAULT_LIMIT = 5000
DEFAULT_DAYS = 30
MAX_LIMIT = 100_000
AUTH_EXCEPTIONS = (
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


def data_dir() -> Path:
    """Allow a private test root without placing test data in a real account."""
    return Path(os.environ.get("TELEGRAM_SKILL_HOME", "~/.local/share/telegram-skill")).expanduser()


def _windows_user_sid() -> str:
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = list(csv.reader(result.stdout.splitlines()))
    if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise OSError("Could not determine the current Windows user SID")
    return rows[0][1]


def _windows_private_acl(path: Path, *, directory: bool = False) -> None:
    """Replace inherited ACLs with an owner-only DACL on a secret path."""
    sid = _windows_user_sid()
    subprocess.run(
        ["icacls", str(path), "/reset"],
        check=True,
        capture_output=True,
        text=True,
    )
    grant = f"*{sid}:(OI)(CI)F" if directory else f"*{sid}:F"
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
        check=True,
        capture_output=True,
        text=True,
    )


def _set_private_permissions(path: Path, mode: int, *, directory: bool = False) -> None:
    if os.name == "nt":
        _windows_private_acl(path, directory=directory)
    else:
        path.chmod(mode)


def ensure_data_dir() -> Path:
    root = data_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _set_private_permissions(root, 0o700, directory=True)
    exports = root / "exports"
    exports.mkdir(mode=0o700, exist_ok=True)
    _set_private_permissions(exports, 0o700, directory=True)
    return root


def ensure_session_data_dir() -> Path:
    """Prepare the private account-data root without touching saved exports."""
    root = data_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _set_private_permissions(root, 0o700, directory=True)
    return root


def credentials_path() -> Path:
    return data_dir() / "credentials.json"


def session_base() -> Path:
    return data_dir() / "account"


def session_path() -> Path:
    return data_dir() / "account.session"


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _set_private_permissions(path.parent, 0o700, directory=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1  # fdopen owns the descriptor from this point onward.
            if os.name == "nt":
                _set_private_permissions(temp, 0o600)
            else:
                fchmod = getattr(os, "fchmod", None)
                if callable(fchmod):
                    fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def save_credentials(api_id: int, api_hash: str) -> None:
    atomic_json(credentials_path(), {"api_id": int(api_id), "api_hash": api_hash})


def load_credentials() -> dict[str, Any] | None:
    path = credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        api_id, api_hash = int(data["api_id"]), str(data["api_hash"])
        if api_id <= 0 or not api_hash:
            return None
        return {"api_id": api_id, "api_hash": api_hash}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def secure_auth_files() -> None:
    root = data_dir()
    for path in root.glob("account.session*"):
        if path.is_file():
            _set_private_permissions(path, 0o600)
    path = credentials_path()
    if path.is_file():
        _set_private_permissions(path, 0o600)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_date(value: str, *, end: bool = False) -> datetime:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Дата должна быть в формате YYYY-MM-DD: {value}") from exc
    return datetime.combine(parsed, time.max if end else time.min, tzinfo=UTC)


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def display_name(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or getattr(entity, "username", None) or str(getattr(entity, "id", "Unknown"))


def entity_record(entity: Any, dialog: Any = None) -> dict[str, Any]:
    return {
        "id": int(getattr(dialog, "id", None) or utils.get_peer_id(entity)),
        "title": display_name(entity),
        "username": getattr(entity, "username", None),
        "type": entity_type(entity),
    }


def entity_type(entity: Any) -> str:
    name = type(entity).__name__.lower()
    if "channel" in name:
        return "channel" if getattr(entity, "broadcast", False) else "group"
    if "chat" in name:
        return "group"
    if "user" in name:
        return "private"
    return "unknown"


def normalize_query(query: str) -> str:
    value = query.strip()
    if value.startswith("https://") or value.startswith("http://"):
        parsed = urlparse(value)
        if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            return value
        path = parsed.path.strip("/")
        if path.startswith("+") or path.startswith("joinchat/"):
            return value
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "c" and parts[1].isdigit():
            return str(utils.get_peer_id(types.PeerChannel(int(parts[1]))))
        value = parts[0]
    return value.lstrip("@").strip()


async def iter_dialogs(client: Any) -> AsyncIterator[Any]:
    async for dialog in client.iter_dialogs():
        yield dialog


async def find_chats(client: Any, query: str, dialogs: list[Any] | None = None) -> list[dict[str, Any]]:
    needle = normalize_query(query).casefold()
    if not needle:
        return []
    matches: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    for dialog in dialogs if dialogs is not None else await collect_dialogs(client):
        entity = dialog.entity
        item = entity_record(entity, dialog)
        values = [str(item["id"]), item["title"], item.get("username") or ""]
        normalized = [normalize_query(value).casefold() for value in values]
        if needle in normalized:
            exact.append(item)
        elif any(needle in value.casefold() for value in values if value):
            matches.append(item)
    return exact or matches[:20]


async def collect_dialogs(client: Any) -> list[Any]:
    return [dialog async for dialog in iter_dialogs(client)]


async def resolve_chat(client: Any, query: str) -> tuple[Any | None, list[dict[str, Any]]]:
    raw = query.strip()
    needle = normalize_query(raw)
    dialogs = await collect_dialogs(client)

    if re.fullmatch(r"-?\d+", raw):
        wanted = int(raw)
        for dialog in dialogs:
            if int(dialog.id) == wanted or int(getattr(dialog.entity, "id", 0)) == wanted:
                return dialog.entity, []
        try:
            return await client.get_entity(wanted), []
        except Exception:
            pass
        return None, []

    matches = await find_chats(client, raw, dialogs)
    if len(matches) == 1:
        wanted = matches[0]["id"]
        for dialog in dialogs:
            if int(dialog.id) == wanted:
                return dialog.entity, []
    if len(matches) > 1:
        exact = [item for item in matches if (item.get("username") or "").casefold() == needle.casefold()]
        if len(exact) == 1:
            for dialog in dialogs:
                if int(dialog.id) == exact[0]["id"]:
                    return dialog.entity, []
        return None, matches

    # Resolve a public username/link only; never join or import an invite.
    if raw.startswith(("https://", "http://")) and ("+" in raw or "joinchat/" in raw):
        return None, []
    try:
        entity = await client.get_entity(needle)
        return entity, []
    except Exception:
        return None, []


def attachment_info(message: Any) -> list[dict[str, Any]]:
    media = getattr(message, "media", None)
    if media is None:
        return []
    info: dict[str, Any] = {"type": type(media).__name__}
    document = getattr(media, "document", None)
    photo = getattr(media, "photo", None)
    if document is not None:
        info.update({"mime_type": getattr(document, "mime_type", None), "size": getattr(document, "size", None)})
        for attr in getattr(document, "attributes", ()) or ():
            attr_name = type(attr).__name__
            if attr_name == "DocumentAttributeFilename":
                info["file_name"] = getattr(attr, "file_name", None)
            elif attr_name == "DocumentAttributeVideo":
                info["type"] = "video"
                info["duration"] = getattr(attr, "duration", None)
            elif attr_name == "DocumentAttributeAudio":
                info["type"] = "audio"
                info["duration"] = getattr(attr, "duration", None)
            elif attr_name == "DocumentAttributeSticker":
                info["type"] = "sticker"
    elif photo is not None:
        info.update({"type": "photo", "size": getattr(photo, "size", None)})
    elif type(media).__name__ == "MessageMediaWebPage":
        info["type"] = "webpage"
    return [info]


def serialize_message(message: Any, chat_id: int) -> dict[str, Any]:
    sender = getattr(message, "sender", None)
    text = getattr(message, "message", None) or getattr(message, "text", None) or ""
    attachments = attachment_info(message)
    if type(message).__name__ == "MessageService":
        kind = "service"
    elif attachments:
        kind = "text_with_attachment" if text else attachments[0]["type"]
    else:
        kind = "text" if text else "other"
    return {
        "message_id": int(message.id),
        "chat_id": int(chat_id),
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": display_name(sender) if sender is not None else None,
        "date": iso_utc(getattr(message, "date", None)),
        "text": text,
        "reply_to_msg_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        "edit_date": iso_utc(getattr(message, "edit_date", None)),
        "type": kind,
        "attachments": attachments,
    }


def repair_jsonl(path: Path) -> tuple[int, int | None]:
    """Validate records and truncate a corrupt/partial tail after interruption."""
    if not path.exists():
        return 0, None
    last_good = 0
    count = 0
    last_id: int | None = None
    last_line_terminated = True
    with path.open("rb") as stream:
        while True:
            line = stream.readline()
            if not line:
                break
            last_line_terminated = line.endswith(b"\n")
            try:
                record = json.loads(line)
                message_id = int(record["message_id"])
            except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError):
                break
            last_good = stream.tell()
            count += 1
            last_id = max(last_id or message_id, message_id)
    if path.stat().st_size != last_good:
        with path.open("r+b") as stream:
            stream.truncate(last_good)
    elif last_good and not last_line_terminated:
        # A process may stop after writing the complete JSON object but before
        # its line terminator. Preserve the valid record and restore the boundary.
        with path.open("ab") as stream:
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    return count, last_id


def existing_ids(path: Path) -> set[int]:
    ids: set[int] = set()
    if not path.exists():
        return ids
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                ids.add(int(json.loads(line)["message_id"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                break
    return ids


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("Could not finish appending a JSONL record")
            remaining = remaining[written:]
    finally:
        os.close(fd)
    path.chmod(0o600)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def chat_dir(chat_id: int) -> Path:
    return data_dir() / "exports" / f"chat_{chat_id}"


def safe_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, MediaStorageError):
        return {"code": "media_storage_error", "message": str(exc)}
    if isinstance(exc, FloodWaitError):
        seconds = int(getattr(exc, "seconds", 0))
        retry = datetime.now(UTC) + timedelta(seconds=seconds)
        return {"code": "flood_wait", "message": "Telegram temporarily limited requests; retry later.", "retry_after_seconds": seconds, "retry_at": iso_utc(retry)}
    if isinstance(exc, AUTH_EXCEPTIONS):
        return {"code": "authorization_revoked", "message": "Telegram rejected the saved authorization as revoked or expired. The session was preserved; run `telegram status` to diagnose, then authorize again only if needed."}
    if isinstance(exc, sqlite3.DatabaseError):
        message = str(exc).casefold()
        if "locked" in message or "busy" in message:
            return {"code": "session_busy", "message": "The Telegram session database is temporarily locked by another process. No session data was changed; retry later."}
        if any(marker in message for marker in ("malformed", "not a database", "corrupt", "disk image")):
            return {"code": "session_database_corrupt", "message": "SQLite reports that the Telegram session database is damaged. The file was preserved; run `telegram diagnose` before considering user action."}
        if any(marker in message for marker in ("unable to open", "readonly", "permission", "i/o error", "disk i/o")):
            return {"code": "session_file_unavailable", "message": "The Telegram session file is currently unavailable to SQLite. Check file access and disk availability; it was not changed."}
        return {"code": "session_database_error", "message": "SQLite reported a Telegram session database error; corruption is not confirmed. Run `telegram diagnose`."}
    if is_network_error(exc):
        return {"code": "network_error", "message": "Telegram could not be reached due to a network timeout or connection failure. The saved session was preserved; retry the read later."}
    if isinstance(exc, (OSError, FileNotFoundError, PermissionError)):
        return {"code": "session_file_unavailable", "message": "The Telegram session file or its directory is unavailable. Check access and disk availability; no authorization data was changed."}
    if isinstance(exc, PhoneCodeInvalidError):
        return {"code": "invalid_code", "message": "Telegram login code was invalid. Run `telegram auth` and enter the latest code in Terminal."}
    if isinstance(exc, PhoneCodeExpiredError):
        return {"code": "expired_code", "message": "Telegram login code expired. Run `telegram auth` to request a new code in Terminal."}
    if isinstance(exc, PhoneNumberInvalidError):
        return {"code": "invalid_phone", "message": "Telegram rejected the phone number. Run `telegram auth` and enter it in international format."}
    if isinstance(exc, ApiIdInvalidError):
        return {"code": "invalid_api_credentials", "message": "Telegram rejected api_id/api_hash. Check them at https://my.telegram.org, then run `telegram auth --reset-api` in Terminal."}
    if isinstance(exc, SessionPasswordNeededError):
        return {"code": "two_factor_required", "message": "Telegram account requires 2FA. Run `telegram auth` in an interactive terminal and enter the password there."}
    if isinstance(exc, ValueError):
        return {"code": "invalid_input", "message": str(exc)}
    return {"code": "telegram_error", "message": f"{type(exc).__name__}: operation failed. Check connectivity and authorization, then retry once."}


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


def client_from_credentials(credentials: dict[str, Any], *, preserve_exports: bool = False) -> TelegramClient:
    if preserve_exports:
        ensure_session_data_dir()
    else:
        ensure_data_dir()
    client = TelegramClient(str(session_base()), credentials["api_id"], credentials["api_hash"])
    client._telegram_session_file_lock = FileLock(str(session_path()) + ".lock")
    return client


async def connect_client(client: Any) -> None:
    lock = getattr(client, "_telegram_session_file_lock", None)
    if lock is not None:
        try:
            await asyncio.to_thread(lock.acquire, timeout=30)
        except FileLockTimeout as exc:
            raise sqlite3.OperationalError("database is locked: another Telegram process owns the session") from exc
    try:
        await client.connect()
    except BaseException:
        if lock is not None and lock.is_locked:
            lock.release()
        raise


_CODE_DELIVERY_LABELS = {
    "SentCodeTypeApp": "Telegram app",
    "SentCodeTypeSms": "SMS",
    "SentCodeTypeCall": "phone call",
    "SentCodeTypeFlashCall": "flash call",
    "SentCodeTypeMissedCall": "missed call",
    "SentCodeTypeEmailCode": "email",
    "SentCodeTypeSetUpEmailRequired": "email setup required",
    "SentCodeTypeFragmentSms": "Fragment SMS",
    "SentCodeTypeFirebaseSms": "Firebase SMS",
    "SentCodeTypeSmsPhrase": "SMS phrase",
    "SentCodeTypeSmsWord": "SMS word",
}


def _code_delivery_label(code_type: Any) -> str:
    name = type(code_type).__name__
    return _CODE_DELIVERY_LABELS.get(name, name.removeprefix("SentCodeType") or "unknown")


def _print_code_delivery(sent_code: Any) -> None:
    code_type = getattr(sent_code, "type", None)
    label = _code_delivery_label(code_type)
    length = getattr(code_type, "length", None)
    detail = f" ({length} characters)" if isinstance(length, int) and length > 0 else ""
    print(f"Telegram code delivery: {label}{detail}.", file=sys.stderr)

    next_type = getattr(sent_code, "next_type", None)
    if next_type is None:
        print("Telegram has not offered another delivery method yet.", file=sys.stderr)
        return
    next_label = _code_delivery_label(next_type)
    timeout = getattr(sent_code, "timeout", None)
    if isinstance(timeout, (int, float)) and timeout > 0:
        print(
            f"Resend is available via {next_label} after {int(timeout)} seconds. "
            "You can request it once from this prompt.",
            file=sys.stderr,
        )
    else:
        print(f"Resend is available via {next_label}. You can request it once from this prompt.", file=sys.stderr)


async def _login_with_phone_code(client: Any, phone: str) -> None:
    sent_code = await client.send_code_request(phone)
    _print_code_delivery(sent_code)

    code = input("Telegram login code (or type `resend` once): ").strip()
    if code.casefold() == "resend":
        next_type = getattr(sent_code, "next_type", None)
        if next_type is None:
            print("Telegram has not offered a resend method; enter the code already sent.", file=sys.stderr)
        else:
            timeout = getattr(sent_code, "timeout", None)
            wait_seconds = max(0, int(timeout)) if isinstance(timeout, (int, float)) else 0
            if wait_seconds:
                print(f"Waiting {wait_seconds} seconds before the one allowed resend.", file=sys.stderr)
                await asyncio.sleep(wait_seconds)
            sent_code = await client(ResendCodeRequest(phone, sent_code.phone_code_hash))
            _print_code_delivery(sent_code)
        code = input("Telegram login code: ").strip()

    if not code:
        raise ValueError("Telegram login code cannot be empty.")
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent_code.phone_code_hash)
    except SessionPasswordNeededError:
        password = getpass.getpass("Telegram 2FA password (input hidden): ")
        await client.sign_in(password=password)


def _render_terminal_qr(url: str) -> None:
    """Render the login URL as a QR matrix locally; never print the URL/token."""
    import qrcode

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    lines = []
    for row in range(0, len(matrix), 2):
        upper = matrix[row]
        lower = matrix[row + 1] if row + 1 < len(matrix) else [False] * len(upper)
        lines.append("".join(
            "█" if top and bottom else "▀" if top else "▄" if bottom else " "
            for top, bottom in zip(upper, lower)
        ))
    print("\n".join(lines), file=sys.stderr)


async def _login_with_qr(client: Any, *, max_qr_codes: int = 3) -> None:
    login = await client.qr_login()
    for attempt in range(max_qr_codes):
        _render_terminal_qr(login.url)
        expires_at = login.expires
        timeout = max(0.1, (expires_at - datetime.now(UTC)).total_seconds())
        print(
            f"Scan this QR in Telegram → Settings → Devices. "
            f"It expires at {expires_at.astimezone().strftime('%H:%M:%S')} local time.",
            file=sys.stderr,
        )
        try:
            await login.wait(timeout=timeout)
            return
        except asyncio.TimeoutError:
            print("QR code expired.", file=sys.stderr)
            if attempt + 1 >= max_qr_codes:
                raise ValueError("QR code expired; run `telegram auth --qr` again to request a new one.")
            answer = input("QR code expired. Press Enter to show a new one, or type `q` to cancel: ").strip().casefold()
            if answer == "q":
                raise ValueError("QR authorization cancelled after the code expired.")
            await login.recreate()
        except SessionPasswordNeededError:
            password = getpass.getpass("Telegram 2FA password (input hidden): ")
            await client.sign_in(password=password)
            return


async def close_client(client: Any) -> None:
    try:
        await client.disconnect()
    finally:
        lock = getattr(client, "_telegram_session_file_lock", None)
        if lock is not None and lock.is_locked:
            lock.release()
        secure_auth_files()


async def command_auth(reset_api: bool = False, qr: bool = False) -> int:
    if not sys.stdin.isatty():
        return emit({"status": "error", "error": {"code": "interactive_terminal_required", "message": "Run `telegram auth` in a normal interactive Terminal. Never paste the code or 2FA password into OpenCode chat."}}, 2)
    ensure_data_dir()
    credentials = load_credentials()
    if reset_api and credentials is not None and session_path().exists():
        # A reset request must never replace credentials backing a live session.
        existing_client = client_from_credentials(credentials)
        try:
            await connect_client(existing_client)
            if await existing_client.is_user_authorized():
                me = await existing_client.get_me()
                return emit({
                    "status": "authorized",
                    "user_id": int(me.id),
                    "display_name": display_name(me),
                    "session_path": str(session_path()),
                    "message": "Existing authorization is active; credentials were not reset.",
                })
        except Exception:
            # If the existing session cannot authorize, continue with the
            # explicitly requested credential-reset flow below.
            pass
        finally:
            await close_client(existing_client)
    if reset_api:
        credentials = None
    if credentials is None:
        try:
            api_id = int(input("Telegram api_id (from https://my.telegram.org): ").strip())
        except ValueError:
            return emit({"status": "error", "error": {"code": "invalid_api_id", "message": "api_id must be a number."}}, 2)
        api_hash = getpass.getpass("Telegram api_hash (input hidden): ").strip()
        if not api_hash:
            return emit({"status": "error", "error": {"code": "missing_api_hash", "message": "api_hash cannot be empty."}}, 2)
        save_credentials(api_id, api_hash)
        credentials = {"api_id": api_id, "api_hash": api_hash}

    client = client_from_credentials(credentials)
    try:
        await connect_client(client)
        if not await client.is_user_authorized():
            if qr:
                await _login_with_qr(client)
            else:
                phone = input("Telegram phone number in international format: ").strip()
                if not phone:
                    return emit({"status": "error", "error": {"code": "missing_phone", "message": "Phone number is required. Run `telegram auth` again."}}, 2)
                await _login_with_phone_code(client, phone)
        if not await client.is_user_authorized():
            return emit({"status": "error", "error": {"code": "authorization_failed", "message": "Telegram did not authorize this session. Run `telegram auth` again in Terminal."}}, 2)
        me = await client.get_me()
        return emit({"status": "authorized", "user_id": int(me.id), "display_name": display_name(me), "session_path": str(session_path())})
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 2)
    finally:
        await close_client(client)


async def authorized_client(*, preserve_exports: bool = False) -> tuple[TelegramClient | None, dict[str, Any] | None]:
    credentials = load_credentials()
    if credentials is None or not session_path().exists():
        return None, {"code": "needs_auth", "message": "Telegram is not authorized yet. Run `telegram auth` in an interactive Terminal; do not send the code in chat."}
    client = client_from_credentials(credentials, preserve_exports=preserve_exports)
    try:
        await connect_client(client)
        if not await client.is_user_authorized():
            await close_client(client)
            return None, {"code": "authorization_missing", "message": "Telegram reports that this session is not currently authorized. The session file was preserved; run `telegram diagnose` before deciding whether interactive authorization is needed."}
        return client, None
    except AUTH_EXCEPTIONS as exc:
        await close_client(client)
        return None, safe_error(exc)
    except Exception:
        await close_client(client)
        raise


async def command_status() -> int:
    credentials = load_credentials()
    if credentials is None:
        return emit({"status": "needs_auth", "authorized": False, "message": "Run `telegram auth` in Terminal."})
    if not session_path().exists():
        return emit({"status": "needs_auth", "authorized": False, "message": "API credentials exist but no session. Run `telegram auth` in Terminal."})
    try:
        client, error = await authorized_client()
        if error:
            return emit({"status": error["code"], "authorized": False, "error": error}, 1)
        me = await client.get_me()
        return emit({"status": "authorized", "authorized": True, "user_id": int(me.id), "display_name": display_name(me)})
    except Exception as exc:
        return emit({"status": "error", "authorized": False, "error": safe_error(exc)}, 1)
    finally:
        if "client" in locals() and client is not None:
            await close_client(client)


async def command_diagnose() -> int:
    """Safely inspect file metadata/SQLite and verify authorization via Telegram."""
    path = session_path()
    session: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if path.is_file():
        try:
            session["size_bytes"] = path.stat().st_size
            db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
            try:
                check = db.execute("PRAGMA quick_check").fetchone()
                session["sqlite"] = "ok" if check and check[0] == "ok" else "integrity_error"
            finally:
                db.close()
        except sqlite3.OperationalError as exc:
            session["sqlite"] = "busy" if "locked" in str(exc).casefold() else "unavailable"
        except sqlite3.DatabaseError:
            session["sqlite"] = "corrupt"
        except OSError:
            session["sqlite"] = "unavailable"

    authorization = "not_checked"
    credentials = load_credentials()
    if credentials and session["exists"]:
        client = client_from_credentials(credentials)
        try:
            for attempt, delay in enumerate((0, 0.4, 1.0), 1):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await connect_client(client)
                    authorization = "authorized" if await client.is_user_authorized() else "not_authorized"
                    break
                except (ConnectionError, TimeoutError, asyncio.TimeoutError):
                    authorization = "network_unavailable"
                    if attempt == 3:
                        break
                    client = client_from_credentials(credentials)
                except AUTH_EXCEPTIONS:
                    authorization = "revoked_or_expired"
                    break
                except Exception as exc:
                    authorization = safe_error(exc)["code"]
                    break
                finally:
                    if getattr(client, "_connected", False):
                        await close_client(client)
        finally:
            lock = getattr(client, "_telegram_session_file_lock", None)
            if lock is not None and lock.is_locked:
                await close_client(client)
    return emit({"status": "diagnosed", "session": session, "authorization": authorization})


async def command_chats(query: str) -> int:
    client, error = await authorized_client()
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        chats = await find_chats(client, query)
        return emit({"status": "ok", "query": query, "count": len(chats), "chats": chats})
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)
    finally:
        await close_client(client)


async def resolve_or_result(client: TelegramClient, query: str) -> tuple[Any | None, int | None]:
    entity, choices = await resolve_chat(client, query)
    if len(choices) > 1:
        return None, emit({"status": "ambiguous", "query": query, "choices": choices, "message": "Choose a chat by ID and retry."}, 2)
    if entity is None:
        return None, emit({"status": "not_found", "query": query, "message": "No accessible chat matched. Use `telegram chats --query ...` to search dialog names."}, 1)
    return entity, None


async def command_info(query: str) -> int:
    client, error = await authorized_client()
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        chat = entity_record(entity)
        chat_id = chat["id"]
        metadata_path = chat_dir(chat_id) / "metadata.json"
        _, last_id = repair_jsonl(chat_dir(chat_id) / "messages.jsonl")
        return emit({"status": "ok", "chat": chat, "last_saved_id": last_id, "metadata_path": str(metadata_path), "metadata": read_metadata(metadata_path)})
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)
    finally:
        await close_client(client)


async def channel_link(client: Any, entity: Any) -> str | None:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}"
    # Private channels have no public username. Link to their latest post so
    # the user can verify the exact channel before confirming deletion.
    try:
        messages = await client.get_messages(entity, limit=1)
    except Exception:
        return None
    if not messages:
        return None
    message_id = getattr(messages[0], "id", None)
    channel_id = getattr(entity, "id", None)
    if not message_id or not channel_id:
        return None
    return f"https://t.me/c/{int(channel_id)}/{int(message_id)}"


async def command_delete_channel(query: str, *, confirm_id: int | None) -> int:
    """Permanently delete a broadcast channel after dry-run and exact-ID confirmation."""
    client, error = await authorized_client()
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        if entity_type(entity) != "channel" or not getattr(entity, "broadcast", False):
            return emit({
                "status": "unsupported_target",
                "message": "This command permanently deletes broadcast channels only; groups and private dialogs are not supported.",
            }, 2)

        target = entity_record(entity)
        link = await channel_link(client, entity)
        if not link:
            return emit({
                "status": "link_unavailable",
                "target": target,
                "message": "Cannot provide a link to verify this channel, so deletion is blocked. Supply a public channel or a private-channel link to a post.",
            }, 2)
        if confirm_id is None:
            return emit({
                "status": "confirmation_required",
                "action": "permanently_delete_channel",
                "target": target,
                "link": link,
                "confirmation_id": target["id"],
                "message": "No changes made. Ask the user to explicitly confirm permanent deletion of this channel, then rerun with --confirm-id matching confirmation_id.",
            })
        if confirm_id != target["id"]:
            return emit({
                "status": "confirmation_mismatch",
                "target": target,
                "link": link,
                "expected_confirmation_id": target["id"],
                "message": "Deletion blocked: --confirm-id must exactly match the resolved channel ID.",
            }, 2)

        await client(DeleteChannelRequest(channel=entity))
        return emit({
            "status": "deleted",
            "action": "permanently_delete_channel",
            "target": target,
            "link": link,
        })
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)
    finally:
        await close_client(client)


def read_metadata(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def consume_messages(client: Any, entity: Any, *, limit: int, since: datetime | None = None, until: datetime | None = None, min_id: int = 0) -> tuple[list[Any], bool]:
    kwargs: dict[str, Any] = {"limit": limit}
    if min_id:
        kwargs.update({"min_id": min_id, "reverse": True})
    else:
        if until is not None:
            kwargs["offset_date"] = until + timedelta(seconds=1)
    messages: list[Any] = []
    async for message in client.iter_messages(entity, **kwargs):
        msg_date = getattr(message, "date", None)
        if msg_date is not None:
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=UTC)
            msg_date = msg_date.astimezone(UTC)
            if since is not None and msg_date < since:
                if not min_id:
                    break
                continue
            if until is not None and msg_date > until:
                continue
        messages.append(message)
        if len(messages) >= limit:
            return messages, True
    return messages, False


async def command_export(query: str, *, days: int | None, since_arg: str | None, until_arg: str | None, limit: int, sync_only: bool) -> int:
    client, error = await authorized_client()
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        chat = entity_record(entity)
        chat_id = chat["id"]
        folder = chat_dir(chat_id)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
        messages_path = folder / "messages.jsonl"
        metadata_path = folder / "metadata.json"
        existing_count, last_id = repair_jsonl(messages_path)
        previous_metadata = read_metadata(metadata_path) or {}
        now = datetime.now(UTC)
        explicit_range = since_arg is not None or until_arg is not None or days is not None
        resume_range = previous_metadata.get("export_in_progress") is True
        if resume_range:
            try:
                since = parse_timestamp(previous_metadata["period_start"])
                until = parse_timestamp(previous_metadata["period_end"])
            except (KeyError, TypeError, ValueError):
                resume_range = False

        if resume_range:
            fetched, capped = await consume_messages(client, entity, limit=limit, since=since, until=until)
            mode = "range"
            period_start = iso_utc(since)
            period_end = iso_utc(until)
        elif sync_only or (last_id is not None and not explicit_range):
            since = until = None
            fetch_limit = limit
            fetched, capped = await consume_messages(client, entity, limit=fetch_limit, min_id=last_id or 0)
            mode = "incremental"
            period_start = previous_metadata.get("last_sync_at") or previous_metadata.get("period_start")
            period_end = iso_utc(now)
        else:
            since = parse_date(since_arg) if since_arg else now - timedelta(days=days if days is not None else DEFAULT_DAYS)
            until = parse_date(until_arg, end=True) if until_arg else now
            if since > until:
                return emit({"status": "error", "error": {"code": "invalid_range", "message": "--since must be earlier than --until."}}, 2)
            mode = "range"
            period_start = iso_utc(since)
            period_end = iso_utc(until)
            atomic_json(metadata_path, {
                **previous_metadata,
                "chat": chat,
                "last_export_mode": "range",
                "period_start": period_start,
                "period_end": period_end,
                "message_count": existing_count,
                "last_saved_message_id": last_id,
                "export_in_progress": True,
                "complete": False,
            })
            fetched, capped = await consume_messages(client, entity, limit=limit, since=since, until=until)

        prior_ids = existing_ids(messages_path) if mode == "range" else set()
        written = 0
        for message in fetched:
            record = serialize_message(message, chat_id)
            if mode == "incremental" and last_id is not None and record["message_id"] <= last_id:
                continue
            if record["message_id"] in prior_ids:
                continue
            append_record(messages_path, record)
            prior_ids.add(record["message_id"])
            written += 1
            last_id = max(last_id or record["message_id"], record["message_id"])

        total_count = existing_count + written
        checkpoint = last_id
        complete = not capped
        synced_at = iso_utc(datetime.now(UTC))
        previous_metadata = read_metadata(metadata_path) or {}
        metadata = {
            "chat": chat,
            "last_export_mode": mode,
            "export_in_progress": False,
            "period_start": previous_metadata.get("period_start") if mode == "incremental" else period_start,
            "period_end": previous_metadata.get("period_end") if mode == "incremental" else period_end,
            "last_sync_at": synced_at,
            "last_sync_window": {"start": period_start, "end": period_end},
            "message_count": total_count,
            "messages_processed_this_run": written,
            "last_saved_message_id": checkpoint,
            "synced_at": synced_at,
            "complete": complete,
            "limit": limit,
            "attachments_downloaded": False,
            "notes": "Incremental sync does not detect edits or deletions to previously exported messages." if total_count else None,
        }
        atomic_json(metadata_path, metadata)
        return emit({"status": "ok", "mode": mode, "chat_id": chat_id, "chat_title": chat["title"], "messages_processed": written, "message_count": total_count, "last_saved_message_id": checkpoint, "messages_path": str(messages_path), "metadata_path": str(metadata_path), "complete": complete, "limit_reached": capped})
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)
    finally:
        await close_client(client)


async def command_all_history_export(query: str, *, limit: int) -> int:
    """Export all API-visible history in bounded, resumable pages."""
    client, error = await authorized_client()
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        chat = entity_record(entity)
        folder = chat_dir(chat["id"])
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        _set_private_permissions(folder, 0o700, directory=True)
        messages_path = folder / "all_history.jsonl"
        metadata_path = folder / "all_history.metadata.json"
        existing_count, _ = repair_jsonl(messages_path)
        known_ids = existing_ids(messages_path)
        previous = read_metadata(metadata_path) or {}
        sweep_complete = previous.get("history_sweep_complete") is True
        mode = "incremental" if sweep_complete else (
            "resume" if previous.get("history_in_progress") is True or known_ids else "initial"
        )

        if mode == "incremental":
            checkpoint = max(known_ids, default=0)
            kwargs: dict[str, Any] = {"limit": limit + 1}
            if checkpoint:
                kwargs.update({"min_id": checkpoint, "reverse": True})
        else:
            saved_cursor = previous.get("history_offset_id") if previous.get("history_in_progress") else None
            try:
                cursor = int(saved_cursor or 0)
            except (TypeError, ValueError):
                cursor = 0
            if mode == "resume" and not cursor:
                cursor = min(known_ids, default=0)
            kwargs = {"limit": limit + 1}
            if cursor:
                kwargs["offset_id"] = cursor

        started_at = iso_utc(datetime.now(UTC))
        atomic_json(
            metadata_path,
            {
                **previous,
                "schema_version": 1,
                "chat": chat,
                "scope": "all Telegram-visible messages returned by iter_messages",
                "history_in_progress": mode != "incremental",
                "history_offset_id": kwargs.get("offset_id", 0),
                "history_sweep_complete": sweep_complete,
                "complete": False,
                "mode": mode,
                "started_or_resumed_at": started_at,
            },
        )

        scanned = 0
        written = 0
        next_cursor = int(kwargs.get("offset_id", 0) or 0)
        capped = False
        iteration_error: dict[str, Any] | None = None
        try:
            async for message in client.iter_messages(entity, **kwargs):
                if scanned >= limit:
                    capped = True
                    break
                scanned += 1
                message_id = int(message.id)
                if mode != "incremental":
                    next_cursor = min(next_cursor or message_id, message_id)
                if message_id in known_ids:
                    continue
                append_record(messages_path, serialize_message(message, chat["id"]))
                known_ids.add(message_id)
                written += 1
        except Exception as exc:
            iteration_error = safe_error(exc)

        page_complete = not capped and iteration_error is None
        if mode == "incremental":
            new_sweep_complete = sweep_complete
        else:
            new_sweep_complete = page_complete
        complete = page_complete and new_sweep_complete
        finished_at = iso_utc(datetime.now(UTC))
        metadata = {
            "schema_version": 1,
            "chat": chat,
            "scope": "all Telegram-visible messages returned by iter_messages",
            "mode": mode,
            "history_in_progress": mode != "incremental" and not page_complete,
            "history_offset_id": next_cursor if mode != "incremental" and not page_complete else None,
            "history_sweep_complete": new_sweep_complete,
            "messages_scanned_this_run": scanned,
            "messages_processed_this_run": written,
            "message_count": existing_count + written,
            "complete": complete,
            "limit": limit,
            "limit_reached": capped,
            "error": iteration_error,
            "coverage_note": "Complete means Telegram's accessible iterator was exhausted; it does not imply deleted, hidden, or inaccessible messages were recovered.",
            "updated_at": finished_at,
        }
        atomic_json(metadata_path, metadata)
        return emit(
            {
                "status": "complete" if complete else "partial",
                "mode": mode,
                "chat_id": chat["id"],
                "messages_scanned": scanned,
                "messages_processed": written,
                "message_count": existing_count + written,
                "history_sweep_complete": new_sweep_complete,
                "complete": complete,
                "limit_reached": capped,
                "error": iteration_error,
                "messages_path": str(messages_path),
                "metadata_path": str(metadata_path),
            },
            0 if complete else 1,
        )
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)
    finally:
        await close_client(client)


def _media_store(output_dir: str | None, chat_id: int) -> MediaStore:
    if output_dir:
        root = Path(output_dir).expanduser()
    else:
        # Do not call ensure_data_dir(): it also updates ACLs on the
        # pre-existing exports directory, which media commands must leave alone.
        root = data_dir() / "media"
    return MediaStore(root, chat_id, secure_path=_set_private_permissions)


def _media_safe_error(exc: BaseException) -> dict[str, Any]:
    return media_error(exc, safe_error)


async def command_media_download(
    query: str,
    *,
    message_id: int,
    output_dir: str | None,
    allowed_types: set[str] | None,
    max_file_bytes: int,
    max_total_bytes: int,
) -> int:
    client, error = await authorized_client(preserve_exports=True)
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        message = await client.get_messages(entity, ids=message_id)
        if isinstance(message, (list, tuple)):
            message = message[0] if message else None
        if message is None:
            return emit({
                "status": "message_not_found",
                "chat_id": entity_record(entity)["id"],
                "message_id": message_id,
                "message": "The message is deleted, unavailable, or not visible in this chat.",
            }, 1)

        chat = entity_record(entity)
        store = await asyncio.to_thread(_media_store, output_dir, chat["id"])
        record, downloaded_bytes = await download_attachment(
            client,
            message,
            store=store,
            chat_id=chat["id"],
            chat_title=chat["title"],
            max_file_bytes=max_file_bytes,
            remaining_total_bytes=max_total_bytes,
            allowed_types=allowed_types,
            safe_error=_media_safe_error,
        )
        status = record["status"]
        return emit({
            "status": status,
            "media": record,
            "manifest_path": str(store.manifest_path),
            "bytes_downloaded": downloaded_bytes,
        }, 0 if status in {"downloaded", "already_present"} else 1)
    except Exception as exc:
        return emit({"status": "error", "error": _media_safe_error(exc)}, 1)
    finally:
        await close_client(client)


async def command_media_export(
    query: str,
    *,
    since_arg: str,
    until_arg: str,
    output_dir: str | None,
    max_files: int,
    max_messages: int,
    max_file_bytes: int,
    max_total_bytes: int,
    allowed_types: set[str] | None,
    topic_id: int | None = None,
    offset_id: int | None = None,
) -> int:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since_arg) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until_arg):
            raise ValueError("Media export dates must use YYYY-MM-DD in UTC.")
        since = parse_date(since_arg)
        until = parse_date(until_arg, end=True)
    except ValueError as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 2)
    if since > until:
        return emit({
            "status": "error",
            "error": {"code": "invalid_range", "message": "--since must be earlier than or equal to --until."},
        }, 2)
    if topic_id is not None and topic_id < 1:
        return emit({"status": "error", "error": {"code": "invalid_topic_id", "message": "--topic-id must be a positive integer."}}, 2)
    if offset_id is not None and offset_id < 1:
        return emit({"status": "error", "error": {"code": "invalid_offset_id", "message": "--offset-id must be a positive integer."}}, 2)

    client, error = await authorized_client(preserve_exports=True)
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        chat = entity_record(entity)
        if topic_id is not None and getattr(entity, "forum", None) is not True:
            return emit(
                {"status": "not_a_forum", "chat": chat, "topic_id": topic_id, "message": "Telegram did not report this chat as a forum."},
                2,
            )
        store = await asyncio.to_thread(_media_store, output_dir, chat["id"])
        results: list[dict[str, Any]] = []
        messages_scanned = 0
        undated_messages_skipped = 0
        attachments_seen = 0
        files_considered = 0
        bytes_downloaded = 0
        max_files_reached = False
        max_messages_reached = False
        next_offset_id: int | None = None
        interrupted_by_flood_wait = False
        iteration_error: dict[str, Any] | None = None

        async def messages_with_error_capture():
            nonlocal iteration_error
            try:
                kwargs: dict[str, Any] = {
                    # Fetch one sentinel message so an exactly-full but
                    # exhausted chat/range is not falsely reported as capped.
                    "limit": max_messages + 1,
                    "offset_date": until + timedelta(seconds=1),
                    "wait_time": 1,
                }
                if offset_id is not None:
                    kwargs["offset_id"] = offset_id
                if topic_id is not None and topic_id != 1:
                    kwargs["reply_to"] = topic_id
                async for item in client.iter_messages(entity, **kwargs):
                    yield item
            except Exception as exc:
                iteration_error = _media_safe_error(exc)

        async for message in messages_with_error_capture():
            if messages_scanned >= max_messages:
                extra_date = getattr(message, "date", None)
                if isinstance(extra_date, datetime):
                    if extra_date.tzinfo is None:
                        extra_date = extra_date.replace(tzinfo=UTC)
                    if extra_date.astimezone(UTC) < since:
                        break
                max_messages_reached = True
                break
            messages_scanned += 1
            message_id = getattr(message, "id", None)
            if isinstance(message_id, int):
                next_offset_id = message_id
            if topic_id is not None and not folders_forums_module.message_belongs_to_topic(message, topic_id):
                continue
            message_date = getattr(message, "date", None)
            if not isinstance(message_date, datetime):
                undated_messages_skipped += 1
                continue
            if message_date.tzinfo is None:
                message_date = message_date.replace(tzinfo=UTC)
            message_date = message_date.astimezone(UTC)
            if message_date < since:
                break
            if message_date > until:
                continue

            descriptor = attachment_descriptor(message)
            if descriptor is None:
                continue
            attachments_seen += 1

            if allowed_types and descriptor["attachment_type"] not in allowed_types:
                record = await skipped_attachment(
                    message,
                    store=store,
                    chat_id=chat["id"],
                    chat_title=chat["title"],
                    reason="type_filter",
                    topic_id=topic_id,
                )
                results.append(record)
                continue

            if files_considered >= max_files:
                max_files_reached = True
                results.append(await skipped_attachment(
                    message,
                    store=store,
                    chat_id=chat["id"],
                    chat_title=chat["title"],
                    reason="max_files_limit_exceeded",
                    topic_id=topic_id,
                ))
                continue
            files_considered += 1
            record, transferred = await download_attachment(
                client,
                message,
                store=store,
                chat_id=chat["id"],
                chat_title=chat["title"],
                max_file_bytes=max_file_bytes,
                remaining_total_bytes=max(0, max_total_bytes - bytes_downloaded),
                allowed_types=allowed_types,
                safe_error=_media_safe_error,
                topic_id=topic_id,
            )
            results.append(record)
            bytes_downloaded += transferred
            if (record.get("error") or {}).get("code") == "flood_wait":
                interrupted_by_flood_wait = True
                break
        counts = {
            "downloaded": sum(item.get("status") == "downloaded" for item in results),
            "already_present": sum(item.get("status") == "already_present" for item in results),
            "skipped": sum(
                item.get("status") == "skipped" and item.get("reason") != "type_filter"
                for item in results
            ),
            "filtered": sum(item.get("reason") == "type_filter" for item in results),
            "errors": sum(item.get("status") in {"error", "unavailable"} for item in results),
        }
        partial = bool(
            counts["skipped"]
            or counts["errors"]
            or max_files_reached
            or max_messages_reached
            or interrupted_by_flood_wait
            or undated_messages_skipped
            or iteration_error is not None
        )
        payload = {
            "status": "partial" if partial else "complete",
            "chat_id": chat["id"],
            "chat_title": chat["title"],
            "topic_id": topic_id,
            "period_start": iso_utc(since),
            "period_end": iso_utc(until),
            "messages_scanned": messages_scanned,
            "undated_messages_skipped": undated_messages_skipped,
            "attachments_seen": attachments_seen,
            "files_considered": files_considered,
            "bytes_downloaded": bytes_downloaded,
            "counts": counts,
            "max_files_reached": max_files_reached,
            "max_messages_reached": max_messages_reached,
            "next_offset_id": next_offset_id if max_messages_reached else None,
            "limits": [
                {
                    "limit": "max_files",
                    "status": "skipped",
                    "reason": f"More than {max_files} eligible attachments fell within the selected range.",
                } if max_files_reached else None,
                {
                    "limit": "max_messages",
                    "status": "skipped",
                    "reason": f"The scan reached the {max_messages}-message cap.",
                } if max_messages_reached else None,
                {
                    "limit": "date_range",
                    "status": "skipped",
                    "reason": f"{undated_messages_skipped} messages without timestamps were excluded from the bounded export.",
                } if undated_messages_skipped else None,
            ],
            "manifest_path": str(store.manifest_path),
            "results": results,
        }
        if iteration_error is not None:
            payload["error"] = iteration_error
        payload["limits"] = [item for item in payload["limits"] if item is not None]
        return emit(payload, 1 if partial else 0)
    except Exception as exc:
        return emit({"status": "error", "error": _media_safe_error(exc)}, 1)
    finally:
        await close_client(client)


def send_log_path() -> Path:
    return data_dir() / "send_log.jsonl"


def append_send_log(entry: dict[str, Any]) -> None:
    path = send_log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def resolve_cached(client: Any, dialogs: list[Any], query: str) -> tuple[Any | None, list[dict[str, Any]]]:
    """Resolve a chat against an already-collected dialog list (fast batch path)."""
    raw = query.strip()
    by_id: dict[int, Any] = {}
    for dialog in dialogs:
        by_id[int(dialog.id)] = dialog.entity
        by_id[utils.get_peer_id(dialog.entity)] = dialog.entity
    if re.fullmatch(r"-?\d+", raw):
        entity = by_id.get(int(raw))
        if entity is not None:
            return entity, []
        try:
            return await client.get_entity(int(raw)), []
        except Exception:
            return None, []
    matches = await find_chats(client, query, dialogs)
    if len(matches) == 1:
        entity = by_id.get(int(matches[0]["id"]))
        if entity is not None:
            return entity, []
        try:
            return await client.get_entity(int(matches[0]["id"])), []
        except Exception:
            return None, matches
    return None, matches


async def send_batch(items: list[dict[str, Any]], *, confirm: bool, bots_only: bool = False) -> int:
    """Send messages from the authenticated personal account.

    Dry run unless `confirm` is set: without it nothing is sent and the resolved
    recipients are returned for review.
    """
    if not items:
        return emit({"status": "error", "error": {"code": "empty_batch", "message": "Nothing to send."}}, 2)
    client, error = await authorized_client()
    if error:
        return emit({"status": "error", "error": error}, 1)
    results: list[dict[str, Any]] = []
    try:
        dialogs = await collect_dialogs(client)
        for item in items:
            query = str(item.get("chat", "")).strip()
            text = str(item.get("text", ""))
            if not query or not text.strip():
                results.append({"chat": query, "status": "skipped", "reason": "empty chat or text"})
                continue
            entity, choices = await resolve_cached(client, dialogs, query)
            if entity is None:
                results.append({
                    "chat": query,
                    "status": "error",
                    "reason": "not_found" if not choices else "ambiguous",
                    "choices": choices[:5],
                })
                continue
            record = entity_record(entity)
            if bots_only and not getattr(entity, "bot", False):
                results.append({"chat": query, "status": "error", "reason": "not_a_bot", "to": record})
                continue
            if not confirm:
                results.append({"chat": query, "status": "dry_run", "to": record, "text": text})
                continue
            message = await client.send_message(entity, text)
            entry = {
                "sent_at": iso_utc(datetime.now(UTC)),
                "chat_id": record["id"],
                "chat_title": record["title"],
                "username": record.get("username"),
                "message_id": int(message.id),
                "text": text,
            }
            append_send_log(entry)
            results.append({
                "chat": query,
                "status": "sent",
                "to": record,
                "message_id": entry["message_id"],
                "sent_at": entry["sent_at"],
            })
            await asyncio.sleep(1.0)
        sent = sum(1 for item in results if item["status"] == "sent")
        failed = [item for item in results if item["status"] == "error"]
        return emit({"status": "ok", "confirm": confirm, "sent": sent, "failed": len(failed), "results": results}, 1 if failed else 0)
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc), "results": results}, 1)
    finally:
        await close_client(client)


async def bot_read(query: str, *, limit: int) -> int:
    client, error = await authorized_client()
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        if not getattr(entity, "bot", False):
            return emit({"status": "not_a_bot", "message": "This command only reads direct bot conversations."}, 2)
        messages = [serialize_message(message, entity_record(entity)["id"]) async for message in client.iter_messages(entity, limit=limit)]
        return emit({"status": "ok", "bot": entity_record(entity), "count": len(messages), "messages": messages})
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)
    finally:
        await close_client(client)


async def bot_buttons(query: str, *, message_id: int) -> int:
    client, error = await authorized_client()
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        if not getattr(entity, "bot", False):
            return emit({"status": "not_a_bot", "message": "This command only inspects direct bot conversations."}, 2)
        message = await client.get_messages(entity, ids=message_id)
        if message is None:
            return emit({"status": "message_not_found", "message_id": message_id}, 1)
        rows = []
        for row_index, row in enumerate(getattr(message, "buttons", None) or ()):
            for column_index, button in enumerate(row):
                raw = getattr(button, "button", button)
                rows.append({
                    "row": row_index,
                    "column": column_index,
                    "text": str(getattr(button, "text", "")),
                    "type": "callback" if getattr(raw, "data", None) is not None else "url" if getattr(raw, "url", None) else "other",
                })
        return emit({"status": "ok", "bot": entity_record(entity), "message_id": message_id, "message_text": getattr(message, "message", "") or "", "buttons": rows})
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)
    finally:
        await close_client(client)


async def bot_click(query: str, *, message_id: int, row: int, column: int, confirm: bool, confirm_label: str | None) -> int:
    client, error = await authorized_client()
    if error:
        return emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await resolve_or_result(client, query)
        if result is not None:
            return result
        if not getattr(entity, "bot", False):
            return emit({"status": "not_a_bot", "message": "This command only interacts with direct bot conversations."}, 2)
        message = await client.get_messages(entity, ids=message_id)
        if message is None:
            return emit({"status": "message_not_found", "message_id": message_id}, 1)
        buttons = getattr(message, "buttons", None) or ()
        if row < 0 or row >= len(buttons) or column < 0 or column >= len(buttons[row]):
            return emit({"status": "button_not_found", "message_id": message_id, "row": row, "column": column}, 2)
        button = buttons[row][column]
        raw = getattr(button, "button", button)
        label = str(getattr(button, "text", ""))
        if getattr(raw, "data", None) is None or getattr(raw, "url", None):
            return emit({"status": "unsupported_button", "message": "Only callback buttons are supported; URL and other action buttons are never opened or activated."}, 2)
        if not confirm:
            return emit({"status": "confirmation_required", "action": "click_bot_callback", "bot": entity_record(entity), "message_id": message_id, "row": row, "column": column, "button_text": label, "message_text": getattr(message, "message", "") or "", "message": "No action taken. Show these exact details and ask the user to confirm before rerunning with --confirm and --confirm-label."})
        if confirm_label != label:
            return emit({"status": "confirmation_mismatch", "expected_button_text": label, "message": "Button text changed or did not match; no action taken."}, 2)
        response = await message.click(i=row, j=column)
        return emit({"status": "clicked", "bot": entity_record(entity), "message_id": message_id, "row": row, "column": column, "button_text": label, "response_type": type(response).__name__ if response is not None else None})
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)
    finally:
        await close_client(client)


def load_send_batch(path: str) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("messages", [])
    if not isinstance(payload, list):
        raise ValueError("Batch file must be a JSON list of {chat, text} objects or {\"messages\": [...]}.")
    return [item for item in payload if isinstance(item, dict)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram", description="Telegram personal-account CLI for OpenCode (Telethon)")
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="Authorize interactively in this terminal")
    auth.add_argument("--reset-api", action="store_true", help="Re-enter api_id/api_hash in this terminal")
    auth.add_argument("--qr", action="store_true", help="Authorize by scanning a QR code shown in this terminal")
    sub.add_parser("status", help="Check saved credentials and session")
    sub.add_parser("diagnose", help="Safely inspect session file, SQLite integrity, and Telegram authorization")
    chats = sub.add_parser("chats", help="Search accessible dialog metadata")
    chats.add_argument("--query", required=True)
    dialogs = sub.add_parser("dialogs", help="List accessible groups, channels, and private dialogs")
    dialogs.add_argument("--format", choices=("json", "csv"), default="json")
    dialogs.add_argument("--output-dir", help="Private export root (default: Telegram skill data directory)")
    dialogs.add_argument("--max-dialogs", type=int, default=DEFAULT_MAX_DIALOGS)
    folders = sub.add_parser("folders", help="Inspect Telegram folder definitions and effective membership")
    folder_sub = folders.add_subparsers(dest="folders_command", required=True)
    folder_list = folder_sub.add_parser("list", help="List Telegram folder IDs, rules, includes, excludes, and pins")
    folder_list.add_argument("--format", choices=("json", "csv"), default="json")
    folder_list.add_argument("--output-dir", help="Private export root (default: Telegram skill data directory)")
    folder_chats = folder_sub.add_parser("chats", help="List effective chats from one or more Telegram folders")
    folder_chats.add_argument("--folder", action="append", required=True, help="Exact folder name or ID; repeat to select more")
    folder_chats.add_argument("--format", choices=("json", "csv"), default="json")
    folder_chats.add_argument("--output-dir", help="Private export root (default: Telegram skill data directory)")
    folder_chats.add_argument("--max-chats", type=int, default=DEFAULT_MAX_DIALOGS)
    topics = sub.add_parser("topics", help="List all available topics in one Telegram forum")
    topics.add_argument("--chat", required=True, help="Accessible forum ID, username, or exact title")
    topics.add_argument("--format", choices=("json", "csv"), default="json")
    topics.add_argument("--output-dir", help="Private export root (default: Telegram skill data directory)")
    topics.add_argument("--max-topics", type=int, default=folders_forums_module.DEFAULT_MAX_TOPICS)
    participants = sub.add_parser("participants", help="Export accessible participants from one group or channel")
    participants.add_argument("--chat", required=True, help="Accessible group/channel ID, username, or exact title")
    participants.add_argument(
        "--max-participants",
        type=int,
        default=DEFAULT_MAX_PARTICIPANTS,
        help="Maximum new participants per run",
    )
    participants.add_argument("--format", choices=("json", "csv"), default="json")
    participants.add_argument("--output-dir", help="Private export root (default: Telegram skill data directory)")
    delete_channel = sub.add_parser(
        "delete-channel",
        help="Permanently delete a broadcast channel (dry run unless --confirm-id is supplied)",
    )
    delete_channel.add_argument("--chat", required=True, help="Channel ID, @username, or exact dialog title")
    delete_channel.add_argument(
        "--confirm-id",
        type=int,
        help="Execute permanent deletion only if this exactly matches the resolved channel ID",
    )
    for name in ("info", "export", "sync"):
        command = sub.add_parser(name, help=f"{'Inspect' if name == 'info' else 'Synchronize/export'} one selected chat")
        command.add_argument("--chat", required=True)
        if name == "export":
            command.add_argument("--days", type=int, default=None, help=f"Initial export lookback (default {DEFAULT_DAYS})")
            command.add_argument("--since", help="Inclusive start date YYYY-MM-DD (UTC)")
            command.add_argument("--until", help="Inclusive end date YYYY-MM-DD (UTC)")
            command.add_argument("--topic-id", type=int, help="Export all accessible messages in this forum topic (1 is General)")
            command.add_argument("--all-history", action="store_true", help="Export all Telegram-visible history in resumable bounded pages")
        if name in {"export", "sync"}:
            command.add_argument("--max-messages", type=int, default=DEFAULT_LIMIT)
    media = sub.add_parser("media", help="Download media from one explicitly selected chat")
    media_sub = media.add_subparsers(dest="media_command", required=True)
    media_download = media_sub.add_parser("download", help="Download one attachment by message ID")
    media_download.add_argument("--chat", required=True, help="Chat ID, @username, or exact dialog title")
    media_download.add_argument("--message-id", required=True, type=int)
    media_download.add_argument("--output-dir", help="Destination root (default: private Telegram skill data directory)")
    media_download.add_argument("--type", action="append", choices=MEDIA_TYPES, help="Limit to a media type; repeat to allow several")
    media_download.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    media_download.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    media_export = media_sub.add_parser("export", help="Download bounded media from one chat and UTC date range")
    media_export.add_argument("--chat", required=True, help="Chat ID, @username, or exact dialog title")
    media_export.add_argument("--since", required=True, help="Inclusive start date YYYY-MM-DD (UTC)")
    media_export.add_argument("--until", required=True, help="Inclusive end date YYYY-MM-DD (UTC)")
    media_export.add_argument("--output-dir", help="Destination root (default: private Telegram skill data directory)")
    media_export.add_argument("--type", action="append", choices=MEDIA_TYPES, help="Limit to a media type; repeat to allow several")
    media_export.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    media_export.add_argument("--max-messages", type=int, default=DEFAULT_MAX_MESSAGES)
    media_export.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    media_export.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    media_export.add_argument("--topic-id", type=int, help="Restrict the bounded media export to one forum topic (1 is General)")
    media_export.add_argument("--offset-id", type=int, help="Continue a partial media scan before the returned next_offset_id")
    send = sub.add_parser("send", help="Send one message (dry run unless --confirm)")
    send.add_argument("--chat", required=True, help="Chat ID, @username or exact dialog title")
    send.add_argument("--text", required=True, help="Exact message text")
    send.add_argument("--confirm", action="store_true", help="Actually send; without it only resolves the recipient")
    batch = sub.add_parser("send-batch", help="Send several messages from a JSON file (dry run unless --confirm)")
    batch.add_argument("--file", required=True, help="JSON list of {chat, text} objects or {\"messages\": [...]}")
    batch.add_argument("--confirm", action="store_true", help="Actually send; without it only resolves the recipients")
    bot_send = sub.add_parser("bot-send", help="Send exact text to a bot (dry run unless --confirm)")
    bot_send.add_argument("--chat", required=True, help="Bot @username or exact bot dialog title")
    bot_send.add_argument("--text", required=True, help="Exact message or command text")
    bot_send.add_argument("--confirm", action="store_true", help="Actually send after reviewing the dry run")
    bot_read_parser = sub.add_parser("bot-read", help="Read recent messages from a direct bot conversation")
    bot_read_parser.add_argument("--chat", required=True, help="Bot @username or exact bot dialog title")
    bot_read_parser.add_argument("--limit", type=int, default=20, help="Number of recent messages to read (1-100)")
    bot_buttons_parser = sub.add_parser("bot-buttons", help="List buttons on one message from a bot")
    bot_buttons_parser.add_argument("--chat", required=True)
    bot_buttons_parser.add_argument("--message-id", required=True, type=int)
    bot_click_parser = sub.add_parser("bot-click", help="Click one callback button (dry run unless confirmed)")
    bot_click_parser.add_argument("--chat", required=True)
    bot_click_parser.add_argument("--message-id", required=True, type=int)
    bot_click_parser.add_argument("--row", required=True, type=int, help="Zero-based button row")
    bot_click_parser.add_argument("--column", required=True, type=int, help="Zero-based button column")
    bot_click_parser.add_argument("--confirm", action="store_true", help="Click only after user confirms the dry-run preview")
    bot_click_parser.add_argument("--confirm-label", help="Exact button text shown in the dry run")
    update_check = sub.add_parser("update-check", help="Check the latest stable GitHub release (cached for 24 hours)")
    update_check.add_argument("--force", action="store_true", help="Ignore cached result and check GitHub now")
    sub.add_parser("update", help="Install the latest stable release; refuses dirty or non-Git installs")
    return parser


async def dispatch(args: argparse.Namespace) -> int:
    if args.command == "auth":
        return await command_auth(reset_api=args.reset_api, qr=args.qr)
    if args.command == "status":
        return await command_status()
    if args.command == "diagnose":
        return await command_diagnose()
    if args.command == "chats":
        return await command_chats(args.query)
    if args.command == "dialogs":
        if not 1 <= args.max_dialogs <= MAX_DIALOGS_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-dialogs must be between 1 and {MAX_DIALOGS_LIMIT}."}}, 2)
        return await participants_module.command_dialogs(
            sys.modules[__name__],
            output_format=args.format,
            output_dir=args.output_dir,
            max_dialogs=args.max_dialogs,
        )
    if args.command == "folders":
        if args.folders_command == "list":
            return await folders_forums_module.command_folders_list(
                sys.modules[__name__],
                output_format=args.format,
                output_dir=args.output_dir,
            )
        if not 1 <= args.max_chats <= MAX_DIALOGS_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-chats must be between 1 and {MAX_DIALOGS_LIMIT}."}}, 2)
        return await folders_forums_module.command_folder_chats(
            sys.modules[__name__],
            args.folder,
            output_format=args.format,
            output_dir=args.output_dir,
            max_chats=args.max_chats,
        )
    if args.command == "topics":
        if not 1 <= args.max_topics <= folders_forums_module.MAX_TOPICS_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-topics must be between 1 and {folders_forums_module.MAX_TOPICS_LIMIT}."}}, 2)
        return await folders_forums_module.command_topics(
            sys.modules[__name__],
            args.chat,
            output_format=args.format,
            output_dir=args.output_dir,
            max_topics=args.max_topics,
        )
    if args.command == "participants":
        if not 1 <= args.max_participants <= MAX_PARTICIPANTS_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-participants must be between 1 and {MAX_PARTICIPANTS_LIMIT}."}}, 2)
        return await participants_module.command_participants(
            sys.modules[__name__],
            args.chat,
            max_participants=args.max_participants,
            output_format=args.format,
            output_dir=args.output_dir,
        )
    if args.command == "delete-channel":
        return await command_delete_channel(args.chat, confirm_id=args.confirm_id)
    if args.command == "info":
        return await command_info(args.chat)
    if args.command == "media":
        allowed_types = set(args.type) if args.type else None
        if args.media_command == "download":
            if args.message_id < 1:
                return emit({"status": "error", "error": {"code": "invalid_message_id", "message": "--message-id must be a positive integer."}}, 2)
            if not 1 <= args.max_file_bytes <= MAX_FILE_BYTES_LIMIT:
                return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-file-bytes must be between 1 and {MAX_FILE_BYTES_LIMIT}."}}, 2)
            if not 1 <= args.max_total_bytes <= MAX_TOTAL_BYTES_LIMIT:
                return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-total-bytes must be between 1 and {MAX_TOTAL_BYTES_LIMIT}."}}, 2)
            return await command_media_download(
                args.chat,
                message_id=args.message_id,
                output_dir=args.output_dir,
                allowed_types=allowed_types,
                max_file_bytes=args.max_file_bytes,
                max_total_bytes=args.max_total_bytes,
            )
        if not 1 <= args.max_files <= MAX_FILES_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-files must be between 1 and {MAX_FILES_LIMIT}."}}, 2)
        if not 1 <= args.max_messages <= MAX_MESSAGES_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-messages must be between 1 and {MAX_MESSAGES_LIMIT}."}}, 2)
        if not 1 <= args.max_file_bytes <= MAX_FILE_BYTES_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-file-bytes must be between 1 and {MAX_FILE_BYTES_LIMIT}."}}, 2)
        if not 1 <= args.max_total_bytes <= MAX_TOTAL_BYTES_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-total-bytes must be between 1 and {MAX_TOTAL_BYTES_LIMIT}."}}, 2)
        return await command_media_export(
            args.chat,
            since_arg=args.since,
            until_arg=args.until,
            output_dir=args.output_dir,
            max_files=args.max_files,
            max_messages=args.max_messages,
            max_file_bytes=args.max_file_bytes,
            max_total_bytes=args.max_total_bytes,
            allowed_types=allowed_types,
            topic_id=args.topic_id,
            offset_id=args.offset_id,
        )
    if args.command == "send":
        return await send_batch([{"chat": args.chat, "text": args.text}], confirm=args.confirm)
    if args.command == "send-batch":
        try:
            items = load_send_batch(args.file)
        except (OSError, ValueError) as exc:
            return emit({"status": "error", "error": safe_error(exc)}, 2)
        return await send_batch(items, confirm=args.confirm)
    if args.command == "bot-send":
        return await send_batch([{"chat": args.chat, "text": args.text}], confirm=args.confirm, bots_only=True)
    if args.command == "bot-read":
        if not 1 <= args.limit <= 100:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": "--limit must be between 1 and 100."}}, 2)
        return await bot_read(args.chat, limit=args.limit)
    if args.command == "bot-buttons":
        return await bot_buttons(args.chat, message_id=args.message_id)
    if args.command == "bot-click":
        if args.row < 0 or args.column < 0:
            return emit({"status": "error", "error": {"code": "invalid_button_index", "message": "--row and --column must be zero or greater."}}, 2)
        if args.confirm and args.confirm_label is None:
            return emit({"status": "error", "error": {"code": "missing_confirm_label", "message": "--confirm-label from the dry run is required with --confirm."}}, 2)
        return await bot_click(args.chat, message_id=args.message_id, row=args.row, column=args.column, confirm=args.confirm, confirm_label=args.confirm_label)
    if args.command == "update-check":
        return emit(check_for_update(force=args.force))
    if args.command == "update":
        result = apply_update()
        return emit(result, 0 if result["status"] in {"updated", "up_to_date"} else 1)
    if args.command in {"export", "sync"}:
        if args.max_messages < 1 or args.max_messages > MAX_LIMIT:
            return emit({"status": "error", "error": {"code": "invalid_limit", "message": f"--max-messages must be between 1 and {MAX_LIMIT}."}}, 2)
        if args.command == "export" and args.topic_id is not None:
            if args.topic_id < 1:
                return emit({"status": "error", "error": {"code": "invalid_topic_id", "message": "--topic-id must be a positive integer."}}, 2)
            if args.days is not None or args.since is not None or args.until is not None:
                return emit({"status": "error", "error": {"code": "conflicting_scope", "message": "Forum topic exports cover accessible topic history and cannot be combined with --days, --since, or --until."}}, 2)
            return await folders_forums_module.command_export_topic(
                sys.modules[__name__],
                args.chat,
                topic_id=args.topic_id,
                max_messages=args.max_messages,
                output_dir=None,
            )
        if args.command == "export" and args.all_history:
            if args.days is not None or args.since is not None or args.until is not None:
                return emit({"status": "error", "error": {"code": "conflicting_scope", "message": "--all-history cannot be combined with --days, --since, or --until."}}, 2)
            return await command_all_history_export(args.chat, limit=args.max_messages)
        if args.command == "sync" and (getattr(args, "topic_id", None) is not None or getattr(args, "all_history", False)):
            return emit({"status": "error", "error": {"code": "unsupported_scope", "message": "Use `telegram export` for topic or all-history exports; sync remains chat-wide incremental sync."}}, 2)
        if args.command == "export" and args.days is not None and args.days < 1:
            return emit({"status": "error", "error": {"code": "invalid_days", "message": "--days must be at least 1."}}, 2)
        if args.command == "export" and args.since and args.days is not None:
            return emit({"status": "error", "error": {"code": "conflicting_period", "message": "Use either --days or --since/--until."}}, 2)
        try:
            if args.command == "export":
                if args.since:
                    parse_date(args.since)
                if args.until:
                    parse_date(args.until, end=True)
        except ValueError as exc:
            return emit({"status": "error", "error": safe_error(exc)}, 2)
        return await command_export(args.chat, days=getattr(args, "days", None), since_arg=getattr(args, "since", None), until_arg=getattr(args, "until", None), limit=args.max_messages, sync_only=args.command == "sync")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(dispatch(args))
    except KeyboardInterrupt:
        return emit({"status": "interrupted", "message": "Interrupted. Any incomplete final JSONL line will be repaired on the next export."}, 130)
    except Exception as exc:
        return emit({"status": "error", "error": safe_error(exc)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
