"""Private, bounded exports of accessible dialogs and group participants."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    from . import media as media_module
except ImportError:  # Running telegram.py directly from the installed scripts directory.
    import media as media_module


UTC = timezone.utc
DEFAULT_MAX_DIALOGS = 10_000
MAX_DIALOGS_LIMIT = 100_000
DEFAULT_MAX_PARTICIPANTS = 10_000
MAX_PARTICIPANTS_LIMIT = 100_000
MAX_JSON_VALUE_CHARS = 16 * 1024 * 1024


def _ensure_private_dir(tg: Any, path: Path) -> Path:
    path = path.expanduser()
    if path.exists() or path.is_symlink():
        if media_module._is_link_or_reparse_point(path):
            raise ValueError("Export output paths cannot be symlinks or reparse points.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Export output path must be a directory.")
    tg._set_private_permissions(resolved, 0o700, directory=True)
    return resolved


def _ensure_child_dir(tg: Any, root: Path, *parts: str) -> Path:
    current = root
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise ValueError("Unsafe export output path component.")
        child = current / part
        existed = child.exists() or child.is_symlink()
        if existed and media_module._is_link_or_reparse_point(child):
            raise ValueError("Export output paths cannot contain symlinks or reparse points.")
        child.mkdir(mode=0o700, exist_ok=True)
        current = child.resolve(strict=True)
        if not current.is_dir() or not current.is_relative_to(root):
            raise ValueError("Export output path resolves outside its private root.")
        if not existed:
            tg._set_private_permissions(current, 0o700, directory=True)
    return current


def _private_root(tg: Any, output_dir: str | None) -> Path:
    root = Path(output_dir).expanduser() if output_dir else tg.data_dir() / "research"
    return _ensure_private_dir(tg, root)


def _export_error(tg: Any, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, OSError) and not media_module.is_network_error(exc) and not isinstance(
        exc, media_module.MediaStorageError
    ):
        return {
            "code": "export_storage_error",
            "message": "Private export output could not be read or written.",
        }
    return tg.safe_error(exc)


def _write_private_bytes(tg: Any, path: Path, content: bytes) -> None:
    parent = _ensure_private_dir(tg, path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temp = Path(temp_name)
    try:
        tg._set_private_permissions(temp, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _write_json(tg: Any, path: Path, payload: Any) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_private_bytes(tg, path, content)


def _write_private_text(tg: Any, path: Path, write: Any) -> None:
    parent = _ensure_private_dir(tg, path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temp = Path(temp_name)
    try:
        tg._set_private_permissions(temp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            fd = -1
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _write_json_array(tg: Any, path: Path, rows: Iterable[dict[str, Any]]) -> None:
    def write(stream: Any) -> None:
        stream.write("[")
        first = True
        for row in rows:
            if not first:
                stream.write(",")
            json.dump(row, stream, ensure_ascii=False, separators=(",", ":"))
            first = False
        stream.write("]\n")

    _write_private_text(tg, path, write)


def _append_jsonl_batch(tg: Any, path: Path, payloads: list[dict[str, Any]]) -> None:
    if not payloads:
        return
    parent = _ensure_private_dir(tg, path.parent)
    if (path.exists() or path.is_symlink()) and not media_module._is_safe_regular_file(path):
        raise ValueError("Participant data files must be private regular files without links.")
    _repair_jsonl_tail(path)
    encoded = b"".join(
        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for payload in payloads
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        tg._set_private_permissions(path, 0o600)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("Could not append a participant record.")
            remaining = remaining[written:]
    finally:
        os.close(fd)
    if os.name != "nt":
        path.chmod(0o600)
    try:
        parent.chmod(0o700)
    except OSError:
        pass


def _last_newline_start(stream: Any, end: int) -> int:
    position = end
    while position > 0:
        start = max(0, position - 64 * 1024)
        stream.seek(start)
        chunk = stream.read(position - start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        position = start
    return 0


def _repair_jsonl_tail(path: Path) -> None:
    if not path.exists():
        return
    if not media_module._is_safe_regular_file(path):
        raise OSError("Existing participant state is not a safe regular file.")
    with path.open("r+b") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if not size:
            return
        stream.seek(size - 1)
        terminated = stream.read(1) == b"\n"
        line_end = size - 1 if terminated else size
        line_start = _last_newline_start(stream, line_end)
        line_size = line_end - line_start
        if line_size == 0:
            if not terminated:
                stream.truncate(line_start)
                stream.flush()
                os.fsync(stream.fileno())
            return
        if line_size > MAX_JSON_VALUE_CHARS:
            if terminated:
                raise OSError("The final participant JSONL record exceeds the safe record limit.")
            stream.truncate(line_start)
            stream.flush()
            os.fsync(stream.fileno())
            return
        stream.seek(line_start)
        tail = stream.read(line_size)
        try:
            row = json.loads(tail)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if terminated:
                raise OSError("The final participant JSONL record is malformed.")
            stream.truncate(line_start)
            stream.flush()
            os.fsync(stream.fileno())
            return
        if not isinstance(row, dict):
            if terminated:
                raise OSError("The final participant JSONL record is malformed.")
            stream.truncate(line_start)
            stream.flush()
            os.fsync(stream.fileno())
            return
        if not terminated:
            stream.seek(0, os.SEEK_END)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())


def _iter_jsonl(tg: Any, path: Path) -> Iterator[dict[str, Any]]:
    if (path.exists() or path.is_symlink()) and not media_module._is_safe_regular_file(path):
        raise OSError("Existing participant state is not a safe regular file.")
    try:
        if path.exists():
            tg._set_private_permissions(path, 0o600)
            _repair_jsonl_tail(path)
        stream = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return
    with stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OSError(f"Participant JSONL record {line_number} is malformed.") from exc
            if not isinstance(row, dict):
                raise OSError(f"Participant JSONL record {line_number} is not an object.")
            yield row


def _read_jsonl(tg: Any, path: Path) -> list[dict[str, Any]]:
    """Compatibility helper for small tests; production paths stream JSONL."""
    return list(_iter_jsonl(tg, path))


def _csv_value(value: Any) -> str:
    if value is None:
        text = ""
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = "".join(
        character
        if unicodedata.category(character) != "Cc" or character in "\t\n\r"
        else f"\\x{ord(character):02x}" if ord(character) <= 0xFF else f"\\u{ord(character):04x}"
        for character in text
    )
    index = 0
    while index < len(text) and (
        text[index].isspace() or unicodedata.category(text[index]) in {"Cc", "Cf"}
    ):
        index += 1
    if index < len(text) and text[index] in "=+-@":
        return "'" + text
    return text


def _write_csv(tg: Any, path: Path, rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> None:
    def write(stream: Any) -> None:
        stream.write("\ufeff")
        writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})

    _write_private_text(tg, path, write)


def _write_export(
    tg: Any,
    root: Path,
    *,
    name: str,
    rows: Iterable[dict[str, Any]],
    output_format: str,
    fields: tuple[str, ...],
) -> Path:
    suffix = "json" if output_format == "json" else "csv"
    path = root / f"{name}.{suffix}"
    if output_format == "json":
        _write_json_array(tg, path, rows)
    else:
        _write_csv(tg, path, rows, fields)
    return path


def _account_role(entity: Any, dialog: Any) -> tuple[str, str, bool | None]:
    if getattr(dialog, "is_user", False) or "user" in type(entity).__name__.casefold():
        return "private_dialog", "dialog_type", None
    if bool(getattr(entity, "creator", False)):
        return "owner", "telegram_creator_flag", True
    if getattr(entity, "admin_rights", None):
        return "admin", "telegram_admin_rights", False
    return "member_or_subscriber", "inferred_from_accessible_dialog", False


def _dialog_record(tg: Any, dialog: Any) -> dict[str, Any]:
    entity = dialog.entity
    record = tg.entity_record(entity, dialog)
    role, role_source, owned = _account_role(entity, dialog)
    is_private = record["type"] == "private"
    participant_count = getattr(entity, "participants_count", None)
    try:
        participant_count = int(participant_count) if participant_count is not None else None
        if participant_count is not None and participant_count < 0:
            participant_count = None
    except (TypeError, ValueError):
        participant_count = None
    return {
        **record,
        "user_id": int(getattr(entity, "id")) if is_private and getattr(entity, "id", None) is not None else None,
        "is_bot": bool(getattr(entity, "bot", False)) if is_private else None,
        "reported_participants_count": participant_count,
        "account_role": role,
        "role_source": role_source,
        "is_owned": owned,
        "is_managed": owned is True or role == "admin",
        "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
    }


def _participant_role(user: Any) -> str:
    participant = getattr(user, "participant", None)
    name = type(participant).__name__.casefold() if participant is not None else ""
    if "creator" in name:
        return "creator"
    if "admin" in name:
        return "admin"
    if "banned" in name or "kicked" in name:
        return "restricted_or_removed"
    if "left" in name:
        return "left"
    if participant is None:
        return "unknown"
    return "member"


def _participant_record(user: Any, chat: dict[str, Any]) -> dict[str, Any]:
    user_id = getattr(user, "id", None)
    if user_id is None:
        return {}
    return {
        "user_id": int(user_id),
        "first_name": getattr(user, "first_name", None),
        "last_name": getattr(user, "last_name", None),
        "display_name": (
            f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
            or getattr(user, "username", None)
        ),
        "username": getattr(user, "username", None),
        "is_bot": bool(getattr(user, "bot", False)),
        "participant_role": _participant_role(user),
        "chat_id": int(chat["id"]),
        "chat_title": chat["title"],
    }


def _participant_csv_fields() -> tuple[str, ...]:
    return (
        "user_id",
        "first_name",
        "last_name",
        "display_name",
        "username",
        "is_bot",
        "participant_role",
        "chat_id",
        "chat_title",
    )


async def _collect_dialogs(
    client: Any,
    limit: int | None = None,
    *,
    safe_error: Any = None,
) -> tuple[list[Any], bool, dict[str, Any] | None]:
    dialogs: list[Any] = []
    capped = False
    error: dict[str, Any] | None = None
    try:
        iterator = client.iter_dialogs(limit=(limit + 1) if limit is not None else None)
        async for dialog in iterator:
            if limit is not None and len(dialogs) >= limit:
                capped = True
                break
            dialogs.append(dialog)
    except Exception as exc:
        error = safe_error(exc) if safe_error is not None else {
            "code": type(exc).__name__,
            "message": "Dialog iteration failed.",
        }
    return dialogs, capped, error


def _dialog_csv_fields() -> tuple[str, ...]:
    return (
        "id",
        "title",
        "username",
        "type",
        "user_id",
        "is_bot",
        "reported_participants_count",
        "account_role",
        "role_source",
        "is_owned",
        "unread_count",
    )


async def command_dialogs(
    tg: Any,
    *,
    output_format: str,
    output_dir: str | None,
    max_dialogs: int = DEFAULT_MAX_DIALOGS,
) -> int:
    client, error = await tg.authorized_client(preserve_exports=True)
    if error:
        return tg.emit({"status": error["code"], "error": error}, 1)
    try:
        raw_dialogs, capped, iteration_error = await _collect_dialogs(
            client, max_dialogs, safe_error=lambda exc: _export_error(tg, exc)
        )
        records = [_dialog_record(tg, item) for item in raw_dialogs]
        root = _private_root(tg, output_dir)
        index_dir = _ensure_child_dir(tg, root, "index")
        path = _write_export(
            tg,
            index_dir,
            name="dialogs",
            rows=records,
            output_format=output_format,
            fields=_dialog_csv_fields(),
        )
        _write_json(tg, index_dir / "dialogs.json", records)
        complete = not capped and iteration_error is None
        payload = {
            "status": "ok" if complete else "partial",
            "dialogs": len(records),
            "groups_and_channels": sum(item["type"] in {"group", "channel"} for item in records),
            "private_dialogs": sum(item["type"] == "private" for item in records),
            "owned_groups_and_channels": sum(item["is_owned"] is True for item in records),
            "managed_groups_and_channels": sum(item["is_managed"] is True for item in records),
            "complete": complete,
            "limit_reached": capped,
            "error": iteration_error,
            "path": str(path),
            "index_path": str(index_dir / "dialogs.json"),
        }
        return tg.emit(payload, 0 if complete else 1)
    except Exception as exc:
        return tg.emit({"status": "error", "error": _export_error(tg, exc)}, 1)
    finally:
        await tg.close_client(client)


async def _scan_participants(
    tg: Any,
    client: Any,
    entity: Any,
    chat: dict[str, Any],
    *,
    max_new: int,
    output_format: str,
    root: Path,
) -> dict[str, Any]:
    participants_dir = _ensure_child_dir(tg, root, "participants")
    jsonl_path = participants_dir / f"chat_{chat['id']}.jsonl"
    state_path = participants_dir / f"chat_{chat['id']}.state.json"
    stored = await asyncio.to_thread(_read_jsonl, tg, jsonl_path)
    known_ids = {int(row["user_id"]) for row in stored if row.get("user_id") is not None}
    current_ids: set[int] = set()
    expected = getattr(entity, "participants_count", None)
    try:
        expected = int(expected) if expected is not None else None
        if expected is not None and expected < 0:
            expected = None
    except (TypeError, ValueError):
        expected = None

    replayed = 0
    pending_records: list[dict[str, Any]] = []
    persisted_added = 0
    capped = False
    error: dict[str, Any] | None = None
    try:
        # Telethon paginates this iterator internally. It has no public offset
        # argument, so resume replays known IDs and appends only new users.
        async for user in client.iter_participants(entity, limit=None):
            record = _participant_record(user, chat)
            if not record:
                continue
            user_id = record["user_id"]
            current_ids.add(user_id)
            if user_id in known_ids:
                replayed += 1
                continue
            if len(pending_records) >= max_new:
                capped = True
                break
            pending_records.append(record)
    except Exception as exc:
        error = _export_error(tg, exc)

    if pending_records:
        try:
            await asyncio.to_thread(_append_jsonl_batch, tg, jsonl_path, pending_records)
            stored.extend(pending_records)
            known_ids.update(record["user_id"] for record in pending_records)
            persisted_added = len(pending_records)
        except Exception as exc:
            error = _export_error(tg, exc)

    added = persisted_added
    total = len(known_ids)
    complete_vs_reported = None if expected is None else len(current_ids) >= expected
    if error:
        status = "partial"
        reason = error["message"]
    elif capped:
        status = "partial"
        reason = "participant_limit_reached"
    elif complete_vs_reported is False:
        status = "partial"
        reason = "Telegram returned fewer participants than the chat's reported count."
    elif expected is None:
        status = "unknown"
        reason = "Telegram did not expose a total participant count for this chat."
    else:
        status = "complete"
        reason = None

    records = sorted(stored, key=lambda item: int(item["user_id"]))
    exported = _write_export(
        tg,
        participants_dir,
        name=f"chat_{chat['id']}",
        rows=records,
        output_format=output_format,
        fields=_participant_csv_fields(),
    )
    state = {
        "schema_version": 1,
        "chat": chat,
        "participant_count_reported": expected,
        "unique_participants_saved": total,
        "participants_added_this_run": added,
        "resume_replayed_existing": replayed,
        "resume_strategy": "replay_from_start_and_deduplicate",
        "telethon_pagination": "iter_participants_managed",
        "complete_vs_reported_count": complete_vs_reported,
        "status": status,
        "limitation_reason": reason,
        "error": error,
        "participants_path": str(jsonl_path),
        "export_path": str(exported),
        "updated_at": tg.iso_utc(datetime.now(UTC)),
    }
    _write_json(tg, state_path, state)
    return state


async def command_participants(
    tg: Any,
    query: str,
    *,
    max_participants: int,
    output_format: str,
    output_dir: str | None,
) -> int:
    client, error = await tg.authorized_client(preserve_exports=True)
    if error:
        return tg.emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await tg.resolve_or_result(client, query)
        if result is not None:
            return result
        chat = tg.entity_record(entity)
        if chat["type"] not in {"group", "channel"}:
            return tg.emit(
                {
                    "status": "unsupported_chat_type",
                    "message": "Participant export is available only for groups and channels.",
                    "chat": chat,
                },
                2,
            )
        root = _private_root(tg, output_dir)
        result = await _scan_participants(
            tg,
            client,
            entity,
            chat,
            max_new=max_participants,
            output_format=output_format,
            root=root,
        )
        return tg.emit(result, 0 if result["status"] == "complete" else 1)
    except Exception as exc:
        return tg.emit({"status": "error", "error": _export_error(tg, exc)}, 1)
    finally:
        await tg.close_client(client)
