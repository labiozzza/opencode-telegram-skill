"""Join exported Telegram participants to private dialogs by numeric user_id."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterator, TextIO


def _iter_json_array(stream: TextIO) -> Iterator[dict[str, Any]]:
    """Stream a JSON array without loading the export into memory."""
    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    started = False
    ended = False

    while not ended:
        chunk = stream.read(64 * 1024)
        eof = not chunk
        buffer = buffer[position:] + chunk
        position = 0
        if not started:
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if position == len(buffer):
                if eof:
                    raise ValueError("JSON export is empty or malformed.")
                continue
            if buffer[position] != "[":
                raise ValueError("JSON export must contain an array of records.")
            position += 1
            started = True

        while True:
            while position < len(buffer) and (buffer[position].isspace() or buffer[position] == ","):
                position += 1
            if position < len(buffer) and buffer[position] == "]":
                ended = True
                position += 1
                break
            if position >= len(buffer):
                break
            try:
                value, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                if eof:
                    raise ValueError("JSON export ended before an array record was complete.")
                break
            if not isinstance(value, dict):
                raise ValueError("JSON export contains a non-object record.")
            yield value
            position = end
        if eof and not ended:
            raise ValueError("JSON export ended before the array closed.")


def _records(path: Path) -> Iterator[dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                yield dict(row)
        return
    if suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}: JSONL record {line_number} is not an object.")
                yield value
        return
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as stream:
            yield from _iter_json_array(stream)
        return
    raise ValueError(f"Unsupported export format: {path.suffix}")


def _integer_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _chat_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed != 0 else None


def _participant_files(paths: list[str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            result.extend(
                sorted(
                    child for child in path.iterdir()
                    if child.is_file()
                    and child.suffix.casefold() in {".csv", ".json", ".jsonl"}
                    and not child.name.casefold().endswith(".state.json")
                )
            )
        elif path.is_file():
            result.append(path)
        else:
            raise FileNotFoundError(f"Participant export does not exist: {path}")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in result:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    if not unique:
        raise ValueError("No participant CSV, JSON, or JSONL exports were found.")
    return unique


def _safe_csv(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _secure_file(path: Path) -> None:
    if os.name == "nt":
        try:
            from . import telegram as telegram_module
        except ImportError:
            import telegram as telegram_module
        telegram_module._set_private_permissions(path, 0o600)
    else:
        path.chmod(0o600)


def _write_matches(db: sqlite3.Connection, output: Path) -> int:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        _secure_file(temp)
        with temp.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=(
                    "user_id",
                    "dialog_id",
                    "dialog_title",
                    "dialog_username",
                    "matched_chat_ids",
                    "matched_chat_titles",
                ),
            )
            writer.writeheader()
            cursor = db.execute(
                """
                SELECT d.user_id, d.dialog_id, d.dialog_title, d.dialog_username,
                       p.chat_id, p.chat_title
                  FROM dialogs AS d
                  JOIN participants AS p ON p.user_id = d.user_id
                 ORDER BY d.user_id, p.chat_id
                """
            )
            current_id: int | None = None
            current: dict[str, Any] | None = None
            group_ids: list[int] = []
            group_titles: list[str | None] = []
            rows = 0

            def flush() -> None:
                nonlocal rows
                if current is None:
                    return
                writer.writerow(
                    {
                        **current,
                        "matched_chat_ids": json.dumps(group_ids, separators=(",", ":")),
                        "matched_chat_titles": json.dumps(group_titles, ensure_ascii=False, separators=(",", ":")),
                    }
                )
                rows += 1

            for row in cursor:
                user_id = int(row[0])
                if current_id is not None and user_id != current_id:
                    flush()
                    group_ids = []
                    group_titles = []
                current_id = user_id
                current = {
                    "user_id": user_id,
                    "dialog_id": _safe_csv(row[1]),
                    "dialog_title": _safe_csv(row[2]),
                    "dialog_username": _safe_csv(row[3]),
                }
                group_ids.append(int(row[4]))
                group_titles.append(row[5])
            flush()
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, output)
        _secure_file(output)
        return rows
    finally:
        temp.unlink(missing_ok=True)


def match_exports(dialogs_path: str, participant_paths: list[str], output_path: str) -> dict[str, Any]:
    dialogs_file = Path(dialogs_path).expanduser()
    if not dialogs_file.is_file():
        raise FileNotFoundError(f"Dialog export does not exist: {dialogs_file}")
    participants_files = _participant_files(participant_paths)
    output = Path(output_path).expanduser()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, database_name = tempfile.mkstemp(prefix=".telegram-match-", suffix=".sqlite3", dir=output.parent)
    os.close(fd)
    database = Path(database_name)
    try:
        _secure_file(database)
        db = sqlite3.connect(database)
        try:
            db.execute("PRAGMA journal_mode=MEMORY")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute(
                "CREATE TABLE dialogs (user_id INTEGER PRIMARY KEY, dialog_id TEXT, dialog_title TEXT, dialog_username TEXT)"
            )
            db.execute(
                "CREATE TABLE participants (user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, chat_title TEXT, PRIMARY KEY (user_id, chat_id))"
            )
            dialog_rows = 0
            for row in _records(dialogs_file):
                if str(row.get("type", "")).casefold() != "private":
                    continue
                user_id = _integer_id(row.get("user_id"))
                if user_id is None:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO dialogs VALUES (?, ?, ?, ?)",
                    (
                        user_id,
                        str(row.get("id") or user_id),
                        row.get("title"),
                        row.get("username"),
                    ),
                )
                dialog_rows += 1
            db.commit()

            participant_rows = 0
            for path in participants_files:
                for row in _records(path):
                    user_id = _integer_id(row.get("user_id"))
                    chat_id = _chat_id(row.get("chat_id"))
                    if user_id is None or chat_id is None:
                        continue
                    db.execute(
                        "INSERT OR REPLACE INTO participants VALUES (?, ?, ?)",
                        (user_id, chat_id, row.get("chat_title")),
                    )
                    participant_rows += 1
                db.commit()
            match_count = _write_matches(db, output)
            return {
                "status": "ok",
                "matching_rule": "numeric Telegram user_id only",
                "private_dialogs_indexed": dialog_rows,
                "participant_rows_indexed": participant_rows,
                "matched_users": match_count,
                "output": str(output),
            }
        finally:
            db.close()
    finally:
        database.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialogs", required=True, help="Dialogs CSV or JSON array export")
    parser.add_argument(
        "--participants",
        nargs="+",
        required=True,
        help="Participant CSV/JSON/JSONL exports, or a directory containing them",
    )
    parser.add_argument("--output", required=True, help="Destination CSV for exact user_id matches")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(match_exports(args.dialogs, args.participants, args.output), ensure_ascii=False))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
