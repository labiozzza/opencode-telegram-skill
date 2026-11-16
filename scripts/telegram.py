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
except ImportError:  # Running this file directly from the installed scripts directory.
    from updater import apply_update, check_for_update

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
    if isinstance(exc, FloodWaitError):
        seconds = int(getattr(exc, "seconds", 0))
        retry = datetime.now(UTC) + timedelta(seconds=seconds)
        return {"code": "flood_wait", "message": "Telegram temporarily limited requests; retry later.", "retry_after_seconds": seconds, "retry_at": iso_utc(retry)}
    if isinstance(exc, AUTH_EXCEPTIONS):
        return {"code": "invalid_session", "message": "Saved Telegram session is invalid or revoked. Run `telegram auth` in Terminal to sign in again."}
    if isinstance(exc, sqlite3.DatabaseError):
        return {"code": "invalid_session", "message": "Saved Telegram session file is damaged. Run `telegram auth` in Terminal to sign in again."}
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


def client_from_credentials(credentials: dict[str, Any]) -> TelegramClient:
    ensure_data_dir()
    return TelegramClient(str(session_base()), credentials["api_id"], credentials["api_hash"])


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
            await existing_client.connect()
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
        await client.connect()
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


async def authorized_client() -> tuple[TelegramClient | None, dict[str, Any] | None]:
    credentials = load_credentials()
    if credentials is None or not session_path().exists():
        return None, {"code": "needs_auth", "message": "Telegram is not authorized yet. Run `telegram auth` in an interactive Terminal; do not send the code in chat."}
    client = client_from_credentials(credentials)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await close_client(client)
            return None, {"code": "invalid_session", "message": "Saved Telegram session is not authorized. Run `telegram auth` in Terminal to sign in again."}
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
    chats = sub.add_parser("chats", help="Search accessible dialog metadata")
    chats.add_argument("--query", required=True)
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
        if name in {"export", "sync"}:
            command.add_argument("--max-messages", type=int, default=DEFAULT_LIMIT)
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
    if args.command == "chats":
        return await command_chats(args.query)
    if args.command == "delete-channel":
        return await command_delete_channel(args.chat, confirm_id=args.confirm_id)
    if args.command == "info":
        return await command_info(args.chat)
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
