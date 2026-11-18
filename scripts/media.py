"""Safe, bounded Telegram media downloads and their local source manifest."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import ssl
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable


UTC = timezone.utc
DEFAULT_MAX_FILES = 100
MAX_FILES_LIMIT = 1_000
DEFAULT_MAX_MESSAGES = 5_000
MAX_MESSAGES_LIMIT = 100_000
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES_LIMIT = 2 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES_LIMIT = 10 * 1024 * 1024 * 1024
MEDIA_TYPES = (
    "photo",
    "image",
    "document",
    "other",
    "video",
    "video_note",
    "animation",
    "audio",
    "voice",
    "sticker",
)
_SAFE_FILE_EXTENSIONS = {
    ".bmp",
    ".csv",
    ".docx",
    ".flac",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".pdf",
    ".png",
    ".pptx",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".xlsx",
    ".zip",
}
_NETWORK_ERRNO_NAMES = (
    "EADDRNOTAVAIL",
    "ECONNABORTED",
    "ECONNREFUSED",
    "ECONNRESET",
    "EHOSTDOWN",
    "EHOSTUNREACH",
    "ENETDOWN",
    "ENETRESET",
    "ENETUNREACH",
    "ENOTCONN",
    "EPIPE",
    "ETIMEDOUT",
)
_NETWORK_ERRNOS = frozenset(
    value
    for name in _NETWORK_ERRNO_NAMES
    if (value := getattr(errno, name, None)) is not None
)
_NETWORK_WINERRORS = frozenset({
    10050,  # WSAENETDOWN
    10051,  # WSAENETUNREACH
    10052,  # WSAENETRESET
    10053,  # WSAECONNABORTED
    10054,  # WSAECONNRESET
    10055,  # WSAENOBUFS
    10057,  # WSAENOTCONN
    10060,  # WSAETIMEDOUT
    10061,  # WSAECONNREFUSED
    10064,  # WSAEHOSTDOWN
    10065,  # WSAEHOSTUNREACH
})


class MediaStorageError(ValueError):
    """The selected media output path is unsafe or unavailable."""


class DownloadLimitExceeded(Exception):
    """Raised when Telethon's progress callback reports a file over its budget."""


def is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (
        ConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
        socket.gaierror,
        socket.herror,
        ssl.SSLError,
    )):
        return True
    if not isinstance(exc, OSError):
        return False
    return (
        exc.errno in _NETWORK_ERRNOS
        or exc.errno in _NETWORK_WINERRORS
        or getattr(exc, "winerror", None) in _NETWORK_WINERRORS
    )


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & reparse_flag)


def _is_safe_regular_file(path: Path, *, require_single_link: bool = True) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or _is_link_or_reparse_point(path):
        return False
    return not require_single_link or getattr(info, "st_nlink", 1) == 1


def _contained(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _restrict_path(
    path: Path,
    *,
    directory: bool,
    secure_path: Callable[..., None] | None,
) -> None:
    if secure_path is not None:
        secure_path(path, 0o700 if directory else 0o600, directory=directory)
    elif os.name != "nt":
        path.chmod(0o700 if directory else 0o600)


def _safe_root(output_dir: str | Path, secure_path: Callable[..., None] | None) -> Path:
    root = Path(output_dir).expanduser()
    existed = root.exists() or root.is_symlink()
    if existed and _is_link_or_reparse_point(root):
        raise MediaStorageError("The selected media output directory cannot be a symlink or reparse point.")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise MediaStorageError("The media output directory cannot be created or accessed.") from exc
    if not resolved.is_dir():
        raise MediaStorageError("The media output path must be a directory.")
    if not existed:
        _restrict_path(resolved, directory=True, secure_path=secure_path)
    # The user selected this root explicitly. Resolve it once, then reject
    # symlinks and Windows reparse points below it.
    return resolved


def _ensure_child_dirs(
    root: Path,
    *parts: str,
    secure_path: Callable[..., None] | None,
) -> Path:
    current = root
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise MediaStorageError("Unsafe media directory component.")
        child = current / part
        existed = child.exists() or child.is_symlink()
        if existed and _is_link_or_reparse_point(child):
            raise MediaStorageError("A symlink or reparse point in the media output path is not allowed.")
        try:
            child.mkdir(mode=0o700, exist_ok=True)
            resolved = child.resolve(strict=True)
        except OSError as exc:
            raise MediaStorageError("A media output subdirectory cannot be created or accessed.") from exc
        if not resolved.is_dir() or not _contained(root, resolved):
            raise MediaStorageError("The media output path resolves outside the selected directory.")
        _restrict_path(resolved, directory=True, secure_path=secure_path)
        current = resolved
    return current


def _message_date(message: Any) -> datetime | None:
    value = getattr(message, "date", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sender_name(sender: Any) -> str | None:
    if sender is None:
        return None
    first = getattr(sender, "first_name", None) or ""
    last = getattr(sender, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or getattr(sender, "username", None)


def _document_kind(document: Any, mime_type: str | None, file_name: str | None) -> str:
    kind = "document"
    for attribute in getattr(document, "attributes", ()) or ():
        name = type(attribute).__name__
        if name == "DocumentAttributeAnimated":
            return "animation"
        if name == "DocumentAttributeVideo":
            return "video_note" if getattr(attribute, "round_message", False) else "video"
        if name == "DocumentAttributeAudio":
            return "voice" if getattr(attribute, "voice", False) else "audio"
        if name == "DocumentAttributeSticker":
            return "sticker"
    mime = (mime_type or "").casefold()
    extension = PureWindowsPath((file_name or "").replace("/", "\\")).suffix.casefold()
    if mime == "image/gif" or extension == ".gif":
        return "animation"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime in {"", "application/octet-stream", "binary/octet-stream"} and not file_name:
        return "other"
    return kind


def attachment_descriptor(message: Any) -> dict[str, Any] | None:
    """Return metadata only for downloadable Telegram photo/document media."""
    media = getattr(message, "media", None)
    if media is None:
        return None
    document = getattr(media, "document", None)
    photo = getattr(media, "photo", None)
    if document is not None:
        file_name = None
        for attribute in getattr(document, "attributes", ()) or ():
            if type(attribute).__name__ == "DocumentAttributeFilename":
                file_name = getattr(attribute, "file_name", None)
                break
        mime_type = getattr(document, "mime_type", None)
        size = getattr(document, "size", None)
        return {
            "attachment_type": _document_kind(document, mime_type, file_name),
            "mime_type": mime_type,
            "original_file_name": file_name,
            "expected_size_bytes": size if isinstance(size, int) and size >= 0 else None,
        }
    if photo is not None:
        file = getattr(message, "file", None)
        size = getattr(file, "size", None)
        return {
            "attachment_type": "photo",
            "mime_type": "image/jpeg",
            "original_file_name": None,
            "expected_size_bytes": size if isinstance(size, int) and size >= 0 else None,
        }
    return None


def forum_topic_id(message: Any) -> int | str | None:
    """Return a topic root only when Telethon exposes a reliable identifier."""
    reply_to = getattr(message, "reply_to", None)
    topic_id = getattr(reply_to, "reply_to_top_id", None)
    if isinstance(topic_id, int) and topic_id > 0:
        return topic_id
    action = getattr(message, "action", None)
    if type(action).__name__ == "MessageActionTopicCreate":
        message_id = getattr(message, "id", None)
        return int(message_id) if isinstance(message_id, int) and message_id > 0 else None
    if getattr(reply_to, "forum_topic", False):
        # Telegram's General forum topic has no explicit top-message ID in
        # some reply headers. Keep it separate from non-forum messages.
        return "general"
    return None


def _file_extension(original_name: str | None, mime_type: str | None) -> str:
    # Only the extension is retained. Never use Telegram's filename as a path.
    basename = (original_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    suffixes = PureWindowsPath(basename).suffixes
    suffix = "".join(suffixes[-2:])
    if suffix:
        normalized = suffix.casefold()
        return normalized if normalized in _SAFE_FILE_EXTENSIONS else ".bin"
    guessed = mimetypes.guess_extension(mime_type or "", strict=False) or ".bin"
    if guessed == ".jpe":
        guessed = ".jpg"
    normalized_guess = guessed.casefold()
    return normalized_guess if normalized_guess in _SAFE_FILE_EXTENSIONS else ".bin"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class MediaStore:
    """A per-chat safe output tree and append-only JSONL source manifest."""

    def __init__(
        self,
        output_dir: str | Path,
        chat_id: int,
        *,
        secure_path: Callable[..., None] | None = None,
    ) -> None:
        self.root = _safe_root(output_dir, secure_path)
        self.chat_id = int(chat_id)
        self.secure_path = secure_path
        self.chat_root = _ensure_child_dirs(
            self.root,
            "telegram",
            f"chat_{self.chat_id}",
            secure_path=secure_path,
        )
        self.manifest_path = self.chat_root / "manifest.jsonl"
        if self.manifest_path.exists() or self.manifest_path.is_symlink():
            if not _is_safe_regular_file(self.manifest_path):
                raise MediaStorageError("The media manifest path is not a safe regular file.")
            _restrict_path(
                self.manifest_path,
                directory=False,
                secure_path=self.secure_path,
            )
        self._latest: dict[str, dict[str, Any]] = {}
        self._content_sources: dict[tuple[str, str], list[str]] = {}
        self._load_manifest()

    def _load_manifest(self) -> None:
        try:
            if self.manifest_path.exists():
                if not _is_safe_regular_file(self.manifest_path):
                    raise MediaStorageError("The media manifest path is not a safe regular file.")
                # A process may be interrupted during the final JSONL append.
                # Drop only the incomplete trailing record before resuming.
                with self.manifest_path.open("rb+") as stream:
                    stream.seek(0, os.SEEK_END)
                    end = stream.tell()
                    if end:
                        stream.seek(end - 1)
                        if stream.read(1) != b"\n":
                            cursor = end
                            boundary = 0
                            complete_record = False
                            while cursor > 0:
                                start = max(0, cursor - 64 * 1024)
                                stream.seek(start)
                                block = stream.read(cursor - start)
                                position = block.rfind(b"\n")
                                if position >= 0:
                                    boundary = start + position + 1
                                    break
                                cursor = start
                            stream.seek(boundary)
                            tail = stream.read()
                            try:
                                json.loads(tail.decode("utf-8"))
                                complete_record = True
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                pass
                            if complete_record:
                                stream.seek(0, os.SEEK_END)
                                stream.write(b"\n")
                            else:
                                stream.truncate(boundary)
                            stream.flush()
                            os.fsync(stream.fileno())
            with self.manifest_path.open("rb") as stream:
                for raw_line in stream:
                    try:
                        line = raw_line.decode("utf-8")
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    source_id = record.get("source_id") if isinstance(record, dict) else None
                    if isinstance(source_id, str):
                        self._latest[source_id] = record
            self._rebuild_content_index()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise MediaStorageError("The media manifest cannot be read.") from exc

    def _existing_download_target(self, source_id: str) -> tuple[dict[str, Any], Path, str] | None:
        record = self._latest.get(source_id)
        if not record or record.get("status") != "downloaded":
            return None
        relative = record.get("relative_path")
        expected_size = record.get("size_bytes")
        expected_hash = record.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        ):
            return None
        rel_path = PurePosixPath(relative)
        if (
            rel_path.is_absolute()
            or PureWindowsPath(relative).is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} or ":" in part for part in rel_path.parts)
        ):
            return None
        candidate = self.root.joinpath(*rel_path.parts)
        try:
            if any(
                _is_link_or_reparse_point(self.root.joinpath(*rel_path.parts[:index]))
                for index in range(1, len(rel_path.parts) + 1)
            ):
                return None
            resolved = candidate.resolve(strict=True)
            if not _contained(self.root, resolved) or not _is_safe_regular_file(resolved):
                return None
            if resolved.stat().st_size != expected_size:
                return None
            _restrict_path(resolved, directory=False, secure_path=self.secure_path)
        except MediaStorageError:
            raise
        except OSError as exc:
            raise MediaStorageError("The existing media file could not be secured.") from exc
        return record, resolved, expected_hash

    def existing_download(self, source_id: str) -> dict[str, Any] | None:
        target = self._existing_download_target(source_id)
        if target is None:
            return None
        record, path, expected_hash = target
        try:
            return record if _sha256(path) == expected_hash else None
        except OSError:
            return None

    async def existing_download_async(self, source_id: str) -> dict[str, Any] | None:
        target = self._existing_download_target(source_id)
        if target is None:
            return None
        record, path, expected_hash = target
        try:
            digest = await asyncio.to_thread(_sha256, path)
        except OSError:
            return None
        return record if digest == expected_hash else None

    def existing_content(self, digest: str, extension: str) -> dict[str, Any] | None:
        for source_id in self._content_sources.get((digest, extension.casefold()), []):
            existing = self.existing_download(source_id)
            if existing is not None:
                return existing
        return None

    async def existing_content_async(self, digest: str, extension: str) -> dict[str, Any] | None:
        for source_id in self._content_sources.get((digest, extension.casefold()), []):
            existing = await self.existing_download_async(source_id)
            if existing is not None:
                return existing
        return None

    @staticmethod
    def _content_key(record: dict[str, Any] | None) -> tuple[str, str] | None:
        if (
            not record
            or record.get("status") != "downloaded"
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
            or not isinstance(record.get("relative_path"), str)
        ):
            return None
        extension = PurePosixPath(record["relative_path"]).suffix.casefold()
        return (record["sha256"], extension) if extension else None

    def _rebuild_content_index(self) -> None:
        self._content_sources.clear()
        for source_id, record in self._latest.items():
            key = self._content_key(record)
            if key is not None:
                self._content_sources.setdefault(key, []).append(source_id)

    def _remove_content_source(self, key: tuple[str, str] | None, source_id: str) -> None:
        if key is None:
            return
        sources = self._content_sources.get(key)
        if sources is None:
            return
        self._content_sources[key] = [item for item in sources if item != source_id]
        if not self._content_sources[key]:
            del self._content_sources[key]

    def matching_published_file(
        self,
        directory: Path,
        message_id: int,
        attachment_index: int,
        extension: str,
        digest: str,
        size: int,
    ) -> Path | None:
        """Find an identical file left behind if publication beat manifest append."""
        stem = str(message_id) if attachment_index == 0 else f"{message_id}-{attachment_index}"
        for collision in range(10_000):
            suffix = "" if collision == 0 else f"-{collision}"
            target = directory / f"{stem}{suffix}{extension}"
            if _is_link_or_reparse_point(target):
                raise MediaStorageError("A symlink or reparse point occupies the target filename.")
            if not target.exists() or not _is_safe_regular_file(target):
                continue
            try:
                if target.stat().st_size == size and _sha256(target) == digest:
                    _restrict_path(target, directory=False, secure_path=self.secure_path)
                    return target
            except OSError as exc:
                raise MediaStorageError("An existing media file could not be checked safely.") from exc
        return None

    def append(self, record: dict[str, Any]) -> None:
        if self.manifest_path.exists() or self.manifest_path.is_symlink():
            if not _is_safe_regular_file(self.manifest_path):
                raise MediaStorageError("The media manifest path is not a safe regular file.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(self.manifest_path, flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1:
                raise MediaStorageError("The media manifest path is not a safe regular file.")
            _restrict_path(self.manifest_path, directory=False, secure_path=self.secure_path)
            payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            with os.fdopen(descriptor, "ab") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise MediaStorageError("The media manifest could not be written.") from exc
        finally:
            if "descriptor" in locals() and descriptor >= 0:
                os.close(descriptor)
        source_id = str(record["source_id"])
        previous_key = self._content_key(self._latest.get(source_id))
        content_key = self._content_key(record)
        if previous_key != content_key:
            self._remove_content_source(previous_key, source_id)
        self._latest[source_id] = record
        if content_key is not None and previous_key != content_key:
            self._content_sources.setdefault(content_key, []).append(source_id)

    def originals_dir(self, message: Any, *, topic_id: int | None = None) -> Path:
        date_value = _message_date(message)
        year = f"{date_value.year:04d}" if date_value else "unknown"
        month = f"{date_value.month:02d}" if date_value else "00"
        topic_id = topic_id if topic_id is not None else forum_topic_id(message)
        if topic_id is None:
            return _ensure_child_dirs(
                self.root,
                "telegram",
                f"chat_{self.chat_id}",
                "originals",
                year,
                month,
                secure_path=self.secure_path,
            )
        return _ensure_child_dirs(
            self.root,
            "telegram",
            f"chat_{self.chat_id}",
            "topics",
            f"topic_{topic_id}",
            "originals",
            year,
            month,
            secure_path=self.secure_path,
        )

    def commit_temp(self, temp: Path, directory: Path, message_id: int, attachment_index: int, extension: str) -> Path:
        stem = str(message_id) if attachment_index == 0 else f"{message_id}-{attachment_index}"
        for collision in range(10_000):
            suffix = "" if collision == 0 else f"-{collision}"
            target = directory / f"{stem}{suffix}{extension}"
            if _is_link_or_reparse_point(target):
                raise MediaStorageError("A symlink or reparse point occupies the target filename.")
            if target.exists():
                continue
            published = False
            try:
                # A hard link is an atomic no-overwrite publish on the same
                # filesystem. The Windows rename fallback also refuses an
                # existing destination.
                try:
                    os.link(temp, target)
                except FileExistsError:
                    continue
                except OSError as link_exc:
                    if os.name != "nt":
                        raise link_exc
                    try:
                        os.rename(temp, target)
                    except FileExistsError:
                        continue
                    published = True
                else:
                    published = True
                    temp.unlink()
                _fsync_dir(directory)
                return target
            except OSError as exc:
                if published:
                    try:
                        target.unlink()
                    except OSError as cleanup_exc:
                        raise MediaStorageError(
                            "The published download could not be cleaned up after a storage failure."
                        ) from cleanup_exc
                raise MediaStorageError(
                    "The completed download could not be atomically published or cleaned up."
                ) from exc
        raise MediaStorageError("Too many files already use the generated media filename.")


def _source_record(
    message: Any,
    chat_id: int,
    chat_title: str,
    descriptor: dict[str, Any],
    *,
    attachment_index: int = 0,
    topic_id: int | None = None,
) -> dict[str, Any]:
    message_id = int(getattr(message, "id"))
    sender = getattr(message, "sender", None)
    sender_id = getattr(message, "sender_id", None)
    grouped_id = getattr(message, "grouped_id", None)
    date_value = _message_date(message)
    return {
        "schema_version": 1,
        "source_id": f"{int(chat_id)}:{message_id}:{attachment_index}",
        "chat_id": int(chat_id),
        "chat_title": chat_title,
        "message_id": message_id,
        "message_date_utc": date_value.isoformat().replace("+00:00", "Z") if date_value else None,
        "sender_id": int(sender_id) if isinstance(sender_id, int) else None,
        "sender": _sender_name(sender),
        "attachment_index": attachment_index,
        "attachment_type": descriptor["attachment_type"],
        "mime_type": descriptor["mime_type"],
        "original_file_name": descriptor["original_file_name"],
        "path": None,
        "relative_path": None,
        "size_bytes": None,
        "sha256": None,
        "status": "pending",
        "forum_topic_id": topic_id if topic_id is not None else forum_topic_id(message),
        "media_group_id": int(grouped_id) if isinstance(grouped_id, int) else None,
    }


def _persist_status(store: MediaStore, record: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    record["status"] = status
    record["reason"] = reason
    store.append(record)
    return record


async def skipped_attachment(
    message: Any,
    *,
    store: MediaStore,
    chat_id: int,
    chat_title: str,
    reason: str,
    topic_id: int | None = None,
) -> dict[str, Any]:
    descriptor = attachment_descriptor(message)
    if descriptor is None:
        return {
            "chat_id": int(chat_id),
            "message_id": getattr(message, "id", None),
            "status": "attachment_not_found",
            "reason": "This message has no downloadable Telegram photo or document.",
        }
    record = _source_record(message, chat_id, chat_title, descriptor, topic_id=topic_id)
    existing = await store.existing_download_async(record["source_id"])
    if existing is not None:
        # A run-level filter/cap result must not replace the durable record of a
        # successfully downloaded source. Return the skip to this caller only.
        return {**record, **existing, "status": "skipped", "reason": reason}
    return _persist_status(store, record, "skipped", reason)


async def download_attachment(
    client: Any,
    message: Any,
    *,
    store: MediaStore,
    chat_id: int,
    chat_title: str,
    max_file_bytes: int,
    remaining_total_bytes: int,
    allowed_types: set[str] | None,
    safe_error: Callable[[BaseException], dict[str, Any]],
    topic_id: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Download one source attachment; never execute or inspect its contents."""
    descriptor = attachment_descriptor(message)
    if descriptor is None:
        return ({
            "chat_id": int(chat_id),
            "message_id": getattr(message, "id", None),
            "status": "attachment_not_found",
            "reason": "This message has no downloadable Telegram photo or document.",
        }, 0)

    record = _source_record(message, chat_id, chat_title, descriptor, topic_id=topic_id)
    if allowed_types and descriptor["attachment_type"] not in allowed_types:
        return (
            await skipped_attachment(
                message,
                store=store,
                chat_id=chat_id,
                chat_title=chat_title,
                reason="type_filter",
                topic_id=topic_id,
            ),
            0,
        )
    existing = await store.existing_download_async(record["source_id"])
    if existing is not None:
        return ({**record, **existing, "status": "already_present"}, 0)
    if remaining_total_bytes <= 0:
        return (_persist_status(store, record, "skipped", "total_size_limit_exceeded"), 0)

    expected_size = descriptor.get("expected_size_bytes")
    if isinstance(expected_size, int) and expected_size > max_file_bytes:
        return (_persist_status(store, record, "skipped", "file_size_limit_exceeded"), 0)
    if isinstance(expected_size, int) and expected_size > remaining_total_bytes:
        return (_persist_status(store, record, "skipped", "total_size_limit_exceeded"), 0)

    limit = min(max_file_bytes, remaining_total_bytes)
    directory = store.originals_dir(message, topic_id=topic_id)
    if isinstance(expected_size, int):
        try:
            if shutil.disk_usage(directory).free < expected_size:
                return (_persist_status(store, record, "skipped", "insufficient_disk_space"), 0)
        except OSError:
            # The transfer and its enforced byte limit remain authoritative if
            # the platform cannot report free space for a mounted destination.
            pass

    fd, temp_name = tempfile.mkstemp(prefix=".telegram-download-", suffix=".part", dir=directory)
    os.close(fd)
    temp = Path(temp_name)
    final_path: Path | None = None
    published_new_file = False
    received_bytes = 0

    def charge_staged_bytes() -> None:
        nonlocal received_bytes
        try:
            info = temp.lstat()
        except OSError:
            return
        if stat.S_ISREG(info.st_mode):
            received_bytes = max(received_bytes, info.st_size)

    try:
        _restrict_path(temp, directory=False, secure_path=store.secure_path)

        def progress_callback(received: int, _total: int | None) -> None:
            nonlocal received_bytes
            if isinstance(received, int) and received > received_bytes:
                received_bytes = received
            if received > limit:
                raise DownloadLimitExceeded

        downloaded = await client.download_media(
            message,
            file=str(temp),
            progress_callback=progress_callback,
        )
        if downloaded is None:
            charge_staged_bytes()
            record["status"] = "unavailable"
            record["reason"] = "Telegram returned no media file for this message."
            store.append(record)
            return record, received_bytes
        if isinstance(downloaded, (bytes, bytearray)):
            received_bytes = max(received_bytes, len(downloaded))
            if len(downloaded) > limit:
                raise DownloadLimitExceeded
            with temp.open("wb") as stream:
                stream.write(downloaded)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            returned_path = Path(os.fspath(downloaded)).expanduser()
            if returned_path.resolve(strict=False) != temp.resolve(strict=False):
                raise MediaStorageError("Telethon returned a path other than the staged download file.")

        if _is_link_or_reparse_point(temp) or not temp.is_file():
            raise MediaStorageError("The staged Telegram download is not a regular local file.")
        size = temp.stat().st_size
        received_bytes = max(received_bytes, size)
        if size > limit:
            raise DownloadLimitExceeded
        if isinstance(expected_size, int) and size != expected_size:
            record["status"] = "error"
            record["error"] = {
                "code": "download_size_mismatch",
                "message": "The downloaded size does not match Telegram's declared file size; no original was published.",
            }
            store.append(record)
            return record, received_bytes

        _fsync_file(temp)
        digest = await asyncio.to_thread(_sha256, temp)
        extension = _file_extension(descriptor.get("original_file_name"), descriptor.get("mime_type"))
        duplicate = await store.existing_content_async(digest, extension)
        if duplicate is not None:
            record.update({
                "path": duplicate.get("path"),
                "relative_path": duplicate.get("relative_path"),
                "size_bytes": size,
                "sha256": digest,
                "status": "downloaded",
                "deduplicated_from_source_id": duplicate.get("source_id"),
            })
            record.pop("reason", None)
            store.append(record)
            return record, received_bytes
        final_path = await asyncio.to_thread(
            store.matching_published_file,
            directory,
            int(record["message_id"]),
            int(record["attachment_index"]),
            extension,
            digest,
            size,
        )
        if final_path is None:
            final_path = store.commit_temp(
                temp,
                directory,
                int(record["message_id"]),
                int(record["attachment_index"]),
                extension,
            )
            published_new_file = True
        record.update({
            "path": str(final_path.resolve()),
            "relative_path": final_path.resolve().relative_to(store.root).as_posix(),
            "size_bytes": size,
            "sha256": digest,
            "status": "downloaded",
        })
        record.pop("reason", None)
        try:
            store.append(record)
        except Exception:
            # Do not leave a newly-published original without its source
            # manifest record when the local metadata write fails.
            if published_new_file:
                try:
                    final_path.unlink()
                    _fsync_dir(final_path.parent)
                except OSError:
                    pass
            raise
        return record, received_bytes
    except DownloadLimitExceeded:
        charge_staged_bytes()
        return (
            _persist_status(store, record, "skipped", "download_size_limit_exceeded"),
            received_bytes,
        )
    except Exception as exc:
        charge_staged_bytes()
        if final_path is not None and published_new_file:
            try:
                final_path.unlink()
                _fsync_dir(final_path.parent)
            except OSError:
                pass
        record["status"] = "error"
        record.pop("path", None)
        record.pop("relative_path", None)
        record.pop("size_bytes", None)
        record.pop("sha256", None)
        record["error"] = safe_error(exc)
        try:
            store.append(record)
        except MediaStorageError:
            pass
        return record, received_bytes
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # The random staged path is not reported as an original. Any
            # remaining .part file is safe to remove manually after diagnosis.
            pass


def media_error(exc: BaseException, safe_error: Callable[[BaseException], dict[str, Any]]) -> dict[str, Any]:
    if isinstance(exc, MediaStorageError):
        return {"code": "media_storage_error", "message": str(exc)}
    if is_network_error(exc):
        return safe_error(exc)
    if isinstance(exc, OSError):
        return {"code": "media_storage_error", "message": "The media output path or local storage is unavailable."}
    return safe_error(exc)
