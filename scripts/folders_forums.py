"""Read-only Telegram folder inventory and resumable forum-topic exports."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import utils
from telethon.tl import functions

try:
    from . import participants as participants_module
except ImportError:  # Running telegram.py directly from the installed scripts directory.
    import participants as participants_module


UTC = timezone.utc
FOLDER_RULE_FIELDS = (
    "contacts",
    "non_contacts",
    "groups",
    "broadcasts",
    "bots",
    "exclude_muted",
    "exclude_read",
    "exclude_archived",
)
FOLDER_SUPPORTED_TYPES = {"DialogFilter", "DialogFilterChatlist"}
FOLDER_FIELDS = (
    "folder_id",
    "folder_name",
    "filter_type",
    "pinned_chat_ids",
    "include_chat_ids",
    "exclude_chat_ids",
    "rules",
    "has_my_invites",
    "supported",
)
CHAT_FIELDS = (
    "folder_id",
    "folder_name",
    "chat_id",
    "title",
    "type",
    "username",
    "my_role",
    "participants_count",
    "is_forum",
    "link",
    "folder_memberships",
    "role_source",
)
TOPIC_FIELDS = (
    "chat_id",
    "chat_title",
    "topic_id",
    "title",
    "status",
    "closed",
    "hidden",
    "pinned",
    "title_missing",
    "date",
    "reported_topic_count",
    "metadata_source",
)
DEFAULT_MAX_TOPICS = 10_000
MAX_TOPICS_LIMIT = 100_000


def _filter_title(value: Any) -> str:
    title = getattr(value, "text", value)
    return str(title) if title is not None else ""


def _peer_ids(peers: Any) -> tuple[list[int], bool]:
    result: list[int] = []
    complete = True
    for peer in peers or ():
        try:
            result.append(int(utils.get_peer_id(peer)))
        except (TypeError, ValueError):
            complete = False
    return result, complete


def _folder_record(folder: Any) -> tuple[dict[str, Any], bool]:
    folder_id = getattr(folder, "id", None)
    try:
        folder_id = int(folder_id)
    except (TypeError, ValueError):
        folder_id = None
    folder_type = type(folder).__name__
    pinned, pinned_ok = _peer_ids(getattr(folder, "pinned_peers", ()))
    included, included_ok = _peer_ids(getattr(folder, "include_peers", ()))
    excluded, excluded_ok = _peer_ids(getattr(folder, "exclude_peers", ()))
    rules = {name: getattr(folder, name, None) for name in FOLDER_RULE_FIELDS}
    record = {
        "folder_id": folder_id,
        "folder_name": _filter_title(getattr(folder, "title", None)),
        "filter_type": folder_type,
        "pinned_chat_ids": pinned,
        "include_chat_ids": included,
        "exclude_chat_ids": excluded,
        "rules": rules,
        "has_my_invites": getattr(folder, "has_my_invites", None),
        "supported": folder_type in FOLDER_SUPPORTED_TYPES and folder_id is not None,
    }
    return record, bool(record["supported"] and pinned_ok and included_ok and excluded_ok)


async def _get_folder_records(client: Any) -> tuple[list[dict[str, Any]], bool, dict[str, Any] | None]:
    response = await client(functions.messages.GetDialogFiltersRequest())
    raw = getattr(response, "filters", response if isinstance(response, (list, tuple)) else None)
    if raw is None:
        return [], False, {
            "code": "unsupported_folder_response",
            "message": "Telegram returned an unrecognized dialog-filter response; folder membership was not guessed.",
        }
    records: list[dict[str, Any]] = []
    complete = True
    for folder in raw:
        record, supported = _folder_record(folder)
        records.append(record)
        complete = complete and supported
    return records, complete, None


def _folder_csv_fields() -> tuple[str, ...]:
    return FOLDER_FIELDS


def _chat_title(entity: Any) -> str | None:
    title = getattr(entity, "title", None)
    if isinstance(title, str) and title:
        return title
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    return str(username) if username else None


def _reported_count(entity: Any) -> int | None:
    value = getattr(entity, "participants_count", None)
    try:
        value = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is None or value >= 0 else None


def _chat_record(tg: Any, dialog: Any, folder: dict[str, Any]) -> dict[str, Any]:
    entity = dialog.entity
    role, role_source, _owned = participants_module._account_role(entity, dialog)
    username = getattr(entity, "username", None)
    link = f"https://t.me/{username}" if isinstance(username, str) and username else None
    raw_forum = getattr(entity, "forum", None)
    is_forum = bool(raw_forum) if isinstance(raw_forum, bool) else None
    try:
        chat_id = int(dialog.id)
    except (TypeError, ValueError):
        chat_id = int(utils.get_peer_id(entity))
    membership = {
        "folder_id": folder["folder_id"],
        "folder_name": folder["folder_name"],
    }
    return {
        "folder_id": [folder["folder_id"]],
        "folder_name": [folder["folder_name"]],
        "chat_id": chat_id,
        "title": _chat_title(entity),
        "type": tg.entity_type(entity),
        "username": username,
        "my_role": role,
        "participants_count": _reported_count(entity),
        "is_forum": is_forum,
        "link": link,
        "folder_memberships": [membership],
        "role_source": role_source,
    }


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _resolve_folders(
    folders: list[dict[str, Any]], queries: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    chosen: list[dict[str, Any]] = []
    seen: set[int] = set()
    for query in queries:
        value = query.strip()
        if value.isdecimal():
            matches = [row for row in folders if row["folder_id"] == int(value)]
        else:
            normalized = _normalized_name(value)
            matches = [
                row for row in folders
                if _normalized_name(str(row["folder_name"])) == normalized
            ]
        if len(matches) > 1:
            return [], {
                "status": "ambiguous_folder",
                "query": query,
                "choices": [
                    {"folder_id": row["folder_id"], "folder_name": row["folder_name"]}
                    for row in matches
                ],
                "message": "Choose a folder by ID; duplicate folder names are not guessed.",
            }
        if not matches:
            return [], {
                "status": "folder_not_found",
                "query": query,
                "message": "No Telegram folder matched this exact name or ID.",
            }
        folder = matches[0]
        if folder["folder_id"] not in seen:
            chosen.append(folder)
            seen.add(folder["folder_id"])
    return chosen, None


def _write_records(
    tg: Any,
    *,
    output_dir: str | None,
    name: str,
    records: list[dict[str, Any]],
    fields: tuple[str, ...],
    output_format: str,
) -> tuple[Path, Path]:
    root = participants_module._private_root(tg, output_dir)
    index_dir = participants_module._ensure_child_dir(tg, root, "index")
    json_path = participants_module._write_export(
        tg,
        index_dir,
        name=name,
        rows=records,
        output_format="json",
        fields=fields,
    )
    csv_path = participants_module._write_export(
        tg,
        index_dir,
        name=name,
        rows=records,
        output_format="csv",
        fields=fields,
    )
    selected = json_path if output_format == "json" else csv_path
    return selected, json_path


async def command_folders_list(
    tg: Any,
    *,
    output_format: str,
    output_dir: str | None,
) -> int:
    client, error = await tg.authorized_client(preserve_exports=True)
    if error:
        return tg.emit({"status": error["code"], "error": error}, 1)
    try:
        folders, supported, error = await _get_folder_records(client)
        if error:
            return tg.emit({"status": "partial", "complete": False, "folders": [], "error": error}, 1)
        path, index_path = _write_records(
            tg,
            output_dir=output_dir,
            name="folders",
            records=folders,
            fields=_folder_csv_fields(),
            output_format=output_format,
        )
        complete = supported
        return tg.emit(
            {
                "status": "ok" if complete else "partial",
                "folders": folders,
                "count": len(folders),
                "complete": complete,
                "limitation_reason": None if complete else "One or more folder records used unsupported or incomplete Telegram fields.",
                "path": str(path),
                "index_path": str(index_path),
            },
            0 if complete else 1,
        )
    except Exception as exc:
        return tg.emit(
            {"status": "error", "complete": False, "folders": [], "chats": [], "error": tg.safe_error(exc)},
            1,
        )
    finally:
        await tg.close_client(client)


async def command_folder_chats(
    tg: Any,
    queries: list[str],
    *,
    output_format: str,
    output_dir: str | None,
    max_chats: int,
) -> int:
    client, error = await tg.authorized_client(preserve_exports=True)
    if error:
        return tg.emit({"status": error["code"], "error": error}, 1)
    try:
        folders, _filters_complete, filter_error = await _get_folder_records(client)
        if filter_error:
            return tg.emit(
                {"status": "partial", "complete": False, "folders": [], "chats": [], "error": filter_error},
                1,
            )
        selected, resolution_error = _resolve_folders(folders, queries)
        if resolution_error:
            return tg.emit(resolution_error, 2)

        chats_by_id: dict[int, dict[str, Any]] = {}
        folder_results: list[dict[str, Any]] = []
        for folder in selected:
            records: list[dict[str, Any]] = []
            error_record: dict[str, Any] | None = None
            limit_reached = False
            try:
                iterator = client.iter_dialogs(
                    limit=max_chats + 1,
                    folder=folder["folder_id"],
                    ignore_pinned=False,
                )
                async for dialog in iterator:
                    if len(records) >= max_chats:
                        limit_reached = True
                        break
                    record = _chat_record(tg, dialog, folder)
                    records.append(record)
                    previous = chats_by_id.get(record["chat_id"])
                    if previous is None:
                        chats_by_id[record["chat_id"]] = record
                    elif folder["folder_id"] not in previous["folder_id"]:
                        previous["folder_id"].append(folder["folder_id"])
                        previous["folder_name"].append(folder["folder_name"])
                        previous["folder_memberships"].append(record["folder_memberships"][0])
            except Exception as exc:
                error_record = tg.safe_error(exc)
            folder_results.append(
                {
                    "folder_id": folder["folder_id"],
                    "folder_name": folder["folder_name"],
                    "chats_returned": len(records),
                    "complete": error_record is None and not limit_reached and folder["supported"],
                    "limit_reached": limit_reached,
                    "error": error_record,
                    "limitation_reason": (
                        "The folder uses an unsupported filter type."
                        if not folder["supported"]
                        else "The chat limit was reached."
                        if limit_reached
                        else error_record["message"]
                        if error_record
                        else None
                    ),
                }
            )

        records = sorted(chats_by_id.values(), key=lambda row: row["chat_id"])
        path, index_path = _write_records(
            tg,
            output_dir=output_dir,
            name="folder_chats",
            records=records,
            fields=CHAT_FIELDS,
            output_format=output_format,
        )
        # Unsupported filters outside the user's selected set do not make this
        # selected inventory incomplete. Each selected filter is checked above.
        complete = all(item["complete"] for item in folder_results)
        return tg.emit(
            {
                "status": "ok" if complete else "partial",
                "complete": complete,
                "folders": folder_results,
                "chats": records,
                "unique_chats": len(records),
                "path": str(path),
                "index_path": str(index_path),
                "limitation_reason": None if complete else "At least one folder membership could not be completely verified.",
            },
            0 if complete else 1,
        )
    except Exception as exc:
        return tg.emit(
            {"status": "error", "complete": False, "folders": [], "chats": [], "error": tg.safe_error(exc)},
            1,
        )
    finally:
        await tg.close_client(client)


def _topic_status(topic: Any) -> str:
    if getattr(topic, "closed", None) is True:
        return "closed"
    if getattr(topic, "hidden", None) is True:
        return "hidden"
    if getattr(topic, "closed", None) is False:
        return "open"
    return "unknown"


def _topic_record(topic: Any, *, chat: dict[str, Any], reported_count: int | None) -> dict[str, Any]:
    topic_id = getattr(topic, "id", None)
    try:
        topic_id = int(topic_id)
    except (TypeError, ValueError):
        topic_id = None
    title = getattr(topic, "title", None)
    date = getattr(topic, "date", None)
    if isinstance(date, datetime):
        date = date.astimezone(UTC).isoformat().replace("+00:00", "Z") if date.tzinfo else date.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    return {
        "chat_id": chat["id"],
        "chat_title": chat["title"],
        "topic_id": topic_id,
        "title": str(title) if title is not None else None,
        "status": "deleted" if type(topic).__name__ == "ForumTopicDeleted" else _topic_status(topic),
        "closed": getattr(topic, "closed", None),
        "hidden": getattr(topic, "hidden", None),
        "pinned": getattr(topic, "pinned", None),
        "title_missing": getattr(topic, "title_missing", None),
        "date": date,
        "reported_topic_count": reported_count,
        "metadata_source": "telegram_api",
    }


def _response_topics(response: Any) -> list[Any] | None:
    topics = getattr(response, "topics", None)
    return list(topics) if topics is not None else None


async def _fetch_topics(
    client: Any,
    entity: Any,
    *,
    chat: dict[str, Any],
    max_topics: int,
) -> tuple[list[dict[str, Any]], int | None, bool, dict[str, Any] | None]:
    peer = await client.get_input_entity(entity)
    offset_date = datetime(1970, 1, 1, tzinfo=UTC)
    offset_id = 0
    offset_topic = 0
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    cursors: set[tuple[int, int, int]] = set()
    reported_count: int | None = None
    error: dict[str, Any] | None = None

    while len(rows) < max_topics:
        response = await client(
            functions.messages.GetForumTopicsRequest(
                peer=peer,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=min(100, max_topics - len(rows)),
            )
        )
        page = _response_topics(response)
        if page is None:
            error = {
                "code": "unsupported_topics_response",
                "message": "Telegram returned an unrecognized forum-topic response.",
            }
            break
        count_value = getattr(response, "count", None)
        if isinstance(count_value, int) and count_value >= 0:
            reported_count = count_value
        elif reported_count is None:
            error = {
                "code": "missing_topic_count",
                "message": "Telegram did not return a topic count; full pagination cannot be verified.",
            }
            break

        for topic in page:
            topic_id = getattr(topic, "id", None)
            if not isinstance(topic_id, int) or topic_id < 1 or topic_id in seen:
                continue
            seen.add(topic_id)
            rows.append(_topic_record(topic, chat=chat, reported_count=reported_count))

        if reported_count is not None and len(seen) >= reported_count:
            break
        if not page:
            error = {
                "code": "topic_pagination_ended_early",
                "message": "Telegram returned no topics before the reported topic count was reached.",
            }
            break

        last_topic = page[-1]
        last_topic_id = getattr(last_topic, "id", None)
        last_message_id = getattr(last_topic, "top_message", None)
        message_by_id = {
            getattr(message, "id", None): message
            for message in (getattr(response, "messages", None) or ())
        }
        last_message = message_by_id.get(last_message_id)
        last_message_date = getattr(last_message, "date", None)
        if (
            not isinstance(last_topic_id, int)
            or not isinstance(last_message_id, int)
            or not isinstance(last_message_date, datetime)
        ):
            error = {
                "code": "topic_pagination_offset_unavailable",
                "message": "Telegram omitted the last topic's actual last-message date or ID; pagination stopped rather than guessing.",
            }
            break
        if last_message_date.tzinfo is None:
            last_message_date = last_message_date.replace(tzinfo=UTC)
        next_cursor = (
            int(last_message_date.timestamp()),
            int(last_message_id),
            int(last_topic_id),
        )
        if next_cursor in cursors:
            error = {
                "code": "topic_pagination_stalled",
                "message": "Telegram repeated a forum-topic pagination cursor; the remaining list is unverified.",
            }
            break
        cursors.add(next_cursor)
        offset_date, offset_id, offset_topic = last_message_date, last_message_id, last_topic_id

    complete = error is None and (reported_count is None or len(seen) >= reported_count)
    if error is None and len(rows) >= max_topics and reported_count is not None and len(seen) < reported_count:
        error = {
            "code": "topic_limit_reached",
            "message": f"The configured {max_topics}-topic limit was reached.",
        }
        complete = False
    if 1 not in seen:
        rows.append(
            {
                "chat_id": chat["id"],
                "chat_title": chat["title"],
                "topic_id": 1,
                "title": "General",
                "status": "unknown",
                "closed": None,
                "hidden": None,
                "pinned": None,
                "title_missing": None,
                "date": None,
                "reported_topic_count": reported_count,
                "metadata_source": "telegram_forum_protocol",
            }
        )
    rows.sort(key=lambda item: (item["topic_id"] != 1, item["topic_id"] or 0))
    return rows, reported_count, complete, error


async def command_topics(
    tg: Any,
    query: str,
    *,
    output_format: str,
    output_dir: str | None,
    max_topics: int,
) -> int:
    client, error = await tg.authorized_client(preserve_exports=True)
    if error:
        return tg.emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await tg.resolve_or_result(client, query)
        if result is not None:
            return result
        chat = tg.entity_record(entity)
        if getattr(entity, "forum", None) is not True:
            return tg.emit(
                {
                    "status": "not_a_forum",
                    "complete": False,
                    "chat": chat,
                    "message": "Telegram did not report this chat as a forum; no topic list was inferred.",
                },
                2,
            )
        records, reported_count, complete, topic_error = await _fetch_topics(
            client, entity, chat=chat, max_topics=max_topics
        )
        name = f"topics_chat_{chat['id']}"
        path, index_path = _write_records(
            tg,
            output_dir=output_dir,
            name=name,
            records=records,
            fields=TOPIC_FIELDS,
            output_format=output_format,
        )
        return tg.emit(
            {
                "status": "ok" if complete else "partial",
                "chat": chat,
                "topics": records,
                "topics_returned": len(records),
                "reported_topic_count": reported_count,
                "complete": complete,
                "error": topic_error,
                "path": str(path),
                "index_path": str(index_path),
            },
            0 if complete else 1,
        )
    except Exception as exc:
        return tg.emit({"status": "error", "error": tg.safe_error(exc)}, 1)
    finally:
        await tg.close_client(client)


def message_topic_id(message: Any) -> int | None:
    """Resolve actual Telegram topic fields; General is protocol topic ID 1."""
    action = getattr(message, "action", None)
    if type(action).__name__ == "MessageActionTopicCreate":
        message_id = getattr(message, "id", None)
        return int(message_id) if isinstance(message_id, int) and message_id > 0 else None
    reply_to = getattr(message, "reply_to", None)
    topic_id = getattr(reply_to, "reply_to_top_id", None)
    if isinstance(topic_id, int) and topic_id > 0:
        return topic_id
    if getattr(reply_to, "forum_topic", False):
        return 1
    return None


def message_belongs_to_topic(message: Any, topic_id: int) -> bool:
    actual_topic_id = message_topic_id(message)
    if actual_topic_id is None:
        # In a forum, messages without a thread root belong to General.
        return topic_id == 1
    return actual_topic_id == topic_id


def _topic_paths(tg: Any, chat_id: int, topic_id: int, output_dir: str | None) -> tuple[Path, Path, Path]:
    if output_dir:
        root = participants_module._private_root(tg, output_dir)
        chat_dir = participants_module._ensure_child_dir(tg, root, f"chat_{chat_id}")
    else:
        chat_dir = participants_module._private_root(tg, str(tg.chat_dir(chat_id)))
    topics_dir = participants_module._ensure_child_dir(tg, chat_dir, "topics")
    topic_dir = participants_module._ensure_child_dir(tg, topics_dir, f"topic_{topic_id}")
    return topic_dir, topic_dir / "messages.jsonl", topic_dir / "metadata.json"


async def command_export_topic(
    tg: Any,
    query: str,
    *,
    topic_id: int,
    max_messages: int,
    output_dir: str | None,
) -> int:
    client, error = await tg.authorized_client(preserve_exports=True)
    if error:
        return tg.emit({"status": error["code"], "error": error}, 1)
    try:
        entity, result = await tg.resolve_or_result(client, query)
        if result is not None:
            return result
        if getattr(entity, "forum", None) is not True:
            return tg.emit(
                {"status": "not_a_forum", "topic_id": topic_id, "message": "Telegram did not report this chat as a forum."},
                2,
            )
        chat = tg.entity_record(entity)
        topic_dir, messages_path, metadata_path = _topic_paths(tg, chat["id"], topic_id, output_dir)
        existing_count, _last_id = tg.repair_jsonl(messages_path)
        known_ids = tg.existing_ids(messages_path)
        previous = tg.read_metadata(metadata_path) or {}
        resume = previous.get("history_in_progress") is True
        cursor = previous.get("history_offset_id") if resume else 0
        try:
            cursor = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            cursor = min(known_ids) if known_ids and resume else 0
        if resume and not cursor and known_ids:
            cursor = min(known_ids)
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        tg.atomic_json(
            metadata_path,
            {
                **previous,
                "schema_version": 1,
                "chat": chat,
                "topic_id": topic_id,
                "topic_is_general": topic_id == 1,
                "history_in_progress": True,
                "history_offset_id": cursor,
                "complete": False,
                "started_or_resumed_at": started_at,
                "message_count": existing_count,
                "resume_strategy": "continue_before_lowest_scanned_message_id_and_deduplicate",
            },
        )

        kwargs: dict[str, Any] = {"limit": max_messages + 1, "offset_id": cursor}
        if topic_id != 1:
            kwargs["reply_to"] = topic_id
        scanned = 0
        written = 0
        next_cursor = cursor
        capped = False
        iteration_error: dict[str, Any] | None = None

        # Include the topic's root service message when Telegram still exposes it.
        if topic_id != 1 and topic_id not in known_ids and hasattr(client, "get_messages"):
            try:
                root_message = await client.get_messages(entity, ids=topic_id)
                if isinstance(root_message, (list, tuple)):
                    root_message = root_message[0] if root_message else None
                if root_message is not None and int(getattr(root_message, "id", 0)) == topic_id:
                    record = tg.serialize_message(root_message, chat["id"])
                    record.update(
                        {
                            "topic_id": topic_id,
                            "reply_to_top_id": getattr(getattr(root_message, "reply_to", None), "reply_to_top_id", None),
                            "forum_topic": getattr(getattr(root_message, "reply_to", None), "forum_topic", None),
                        }
                    )
                    tg.append_record(messages_path, record)
                    known_ids.add(topic_id)
                    written += 1
            except Exception as exc:
                iteration_error = tg.safe_error(exc)

        try:
            async for message in client.iter_messages(entity, **kwargs):
                if scanned >= max_messages:
                    capped = True
                    break
                scanned += 1
                message_id = int(getattr(message, "id"))
                if not message_belongs_to_topic(message, topic_id):
                    next_cursor = min(next_cursor or message_id, message_id)
                    continue
                if message_id in known_ids:
                    next_cursor = min(next_cursor or message_id, message_id)
                    continue
                record = tg.serialize_message(message, chat["id"])
                reply_to = getattr(message, "reply_to", None)
                record.update(
                    {
                        "topic_id": topic_id,
                        "reply_to_top_id": getattr(reply_to, "reply_to_top_id", None),
                        "forum_topic": getattr(reply_to, "forum_topic", None),
                    }
                )
                tg.append_record(messages_path, record)
                known_ids.add(message_id)
                written += 1
                next_cursor = min(next_cursor or message_id, message_id)
        except Exception as exc:
            iteration_error = tg.safe_error(exc)

        complete = not capped and iteration_error is None
        count = existing_count + written
        finished_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        metadata = {
            "schema_version": 1,
            "chat": chat,
            "topic_id": topic_id,
            "topic_is_general": topic_id == 1,
            "history_in_progress": not complete,
            "history_offset_id": next_cursor if not complete else None,
            "messages_scanned_this_run": scanned,
            "messages_processed_this_run": written,
            "message_count": count,
            "complete": complete,
            "limit_reached": capped,
            "limit": max_messages,
            "error": iteration_error,
            "coverage_scope": "Messages returned by Telegram for this topic; hidden, deleted, or inaccessible messages are not inferred.",
            "updated_at": finished_at,
        }
        tg.atomic_json(metadata_path, metadata)
        return tg.emit(
            {
                "status": "complete" if complete else "partial",
                "chat_id": chat["id"],
                "topic_id": topic_id,
                "messages_scanned": scanned,
                "messages_processed": written,
                "message_count": count,
                "complete": complete,
                "limit_reached": capped,
                "error": iteration_error,
                "messages_path": str(messages_path),
                "metadata_path": str(metadata_path),
            },
            0 if complete else 1,
        )
    except Exception as exc:
        return tg.emit({"status": "error", "error": tg.safe_error(exc)}, 1)
    finally:
        await tg.close_client(client)
