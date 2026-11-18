from __future__ import annotations

import ast
import asyncio
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import folders_forums, telegram
from telethon.tl import functions, types


def make_filter(folder_id: int, title: str, *, groups=None, broadcasts=None, include=(), exclude=(), pinned=()):
    folder = type("DialogFilter", (), {})()
    for key, value in {
        "id": folder_id,
        "title": SimpleNamespace(text=title),
        "groups": groups,
        "contacts": None,
        "non_contacts": None,
        "broadcasts": broadcasts,
        "bots": None,
        "exclude_muted": None,
        "exclude_read": None,
        "exclude_archived": None,
        "include_peers": list(include),
        "exclude_peers": list(exclude),
        "pinned_peers": list(pinned),
    }.items():
        setattr(folder, key, value)
    return folder


def make_topic(topic_id: int, title: str, *, top_message: int, closed=False, hidden=False, pinned=False):
    return SimpleNamespace(
        id=topic_id,
        title=title,
        top_message=top_message,
        closed=closed,
        hidden=hidden,
        pinned=pinned,
        title_missing=False,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class FakeTelegram:
    def __init__(self, client, root: Path):
        self.client = client
        self.root = root
        self.output = []

    async def authorized_client(self, *, preserve_exports=False):
        return self.client, None

    async def close_client(self, client):
        return None

    async def resolve_or_result(self, client, query):
        return getattr(client, "entity", None), None

    def entity_record(self, entity, dialog=None):
        return {
            "id": int(getattr(entity, "chat_id", getattr(entity, "id", 0))),
            "title": getattr(entity, "title", "Forum"),
            "username": getattr(entity, "username", None),
            "type": "group",
        }

    def entity_type(self, entity):
        return getattr(entity, "chat_type", "group")

    def safe_error(self, exc):
        return {"code": type(exc).__name__, "message": str(exc)}

    def emit(self, payload, exit_code=0):
        self.output.append((payload, exit_code))
        return exit_code

    def data_dir(self):
        return self.root

    def chat_dir(self, chat_id):
        return self.root / "exports" / f"chat_{chat_id}"

    def _set_private_permissions(self, path, mode, *, directory=False):
        if os.name != "nt":
            Path(path).chmod(mode)

    def iso_utc(self, value):
        return telegram.iso_utc(value)

    def serialize_message(self, message, chat_id):
        return {
            "message_id": int(message.id),
            "chat_id": int(chat_id),
            "text": getattr(message, "message", ""),
            "reply_to_msg_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        }

    def repair_jsonl(self, path):
        return telegram.repair_jsonl(path)

    def existing_ids(self, path):
        return telegram.existing_ids(path)

    def read_metadata(self, path):
        return telegram.read_metadata(path)

    def atomic_json(self, path, payload):
        telegram.atomic_json(path, payload)

    def append_record(self, path, record):
        telegram.append_record(path, record)


class FolderClient:
    def __init__(self, folders, dialogs_by_folder=None):
        self.folders = folders
        self.dialogs_by_folder = dialogs_by_folder or {}
        self.iter_calls = []
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=self.folders)
        raise AssertionError(f"Unexpected request: {type(request).__name__}")

    def iter_dialogs(self, *, limit=None, folder=None, ignore_pinned=False):
        self.iter_calls.append((limit, folder, ignore_pinned))

        async def rows():
            for row in self.dialogs_by_folder.get(folder, []):
                yield row

        return rows()


class FolderAndForumTests(unittest.TestCase):
    def test_folder_rules_and_effective_membership_are_read_from_telegram(self):
        folder = make_filter(
            7,
            "My groups",
            groups=True,
            include=[types.PeerChannel(101)],
            exclude=[types.PeerChannel(202)],
            pinned=[types.PeerChannel(303)],
        )
        actual = SimpleNamespace(
            id=-1000000000101,
            entity=SimpleNamespace(
                id=101,
                title="Actual API result",
                username="visible_group",
                forum=True,
                participants_count=4,
                broadcast=False,
                creator=True,
            ),
            unread_count=0,
        )
        client = FolderClient([folder], {7: [actual]})
        with tempfile.TemporaryDirectory() as temp:
            tg = FakeTelegram(client, Path(temp))
            code = asyncio.run(
                folders_forums.command_folder_chats(
                    tg,
                    ["My groups"],
                    output_format="json",
                    output_dir=temp,
                    max_chats=20,
                )
            )
            payload, exit_code = tg.output[-1]
            self.assertEqual((code, exit_code), (0, 0))
            self.assertEqual(client.iter_calls, [(21, 7, False)])
            self.assertEqual(len(client.requests), 1)
            self.assertIsInstance(client.requests[0], functions.messages.GetDialogFiltersRequest)
            self.assertEqual(payload["chats"][0]["folder_id"], [7])
            self.assertEqual(payload["chats"][0]["folder_name"], ["My groups"])
            self.assertEqual(payload["chats"][0]["chat_id"], -1000000000101)
            self.assertEqual(payload["chats"][0]["title"], "Actual API result")
            self.assertEqual(payload["chats"][0]["my_role"], "owner")
            self.assertEqual(payload["chats"][0]["participants_count"], 4)
            self.assertTrue(payload["chats"][0]["is_forum"])
            self.assertEqual(payload["chats"][0]["link"], "https://t.me/visible_group")
            folders_path = Path(payload["index_path"]).parent / "folder_chats.csv"
            self.assertTrue(folders_path.exists())
            with folders_path.open(encoding="utf-8-sig", newline="") as stream:
                csv_row = next(csv.DictReader(stream))
            self.assertEqual(json.loads(csv_row["folder_id"]), [7])

            listed = folders_forums._folder_record(folder)
            self.assertTrue(listed[1])
            self.assertTrue(listed[0]["rules"]["groups"])
            self.assertEqual(listed[0]["include_chat_ids"], [-1000000000101])
            self.assertEqual(listed[0]["exclude_chat_ids"], [-1000000000202])
            self.assertEqual(listed[0]["pinned_chat_ids"], [-1000000000303])

    def test_duplicate_folder_names_require_an_id(self):
        folders = [make_filter(1, "same"), make_filter(2, "same")]
        client = FolderClient(folders)
        with tempfile.TemporaryDirectory() as temp:
            tg = FakeTelegram(client, Path(temp))
            code = asyncio.run(
                folders_forums.command_folder_chats(
                    tg, ["same"], output_format="json", output_dir=temp, max_chats=10
                )
            )
            payload, exit_code = tg.output[-1]
        self.assertEqual((code, exit_code), (2, 2))
        self.assertEqual(payload["status"], "ambiguous_folder")
        self.assertEqual([choice["folder_id"] for choice in payload["choices"]], [1, 2])
        self.assertEqual(client.iter_calls, [])

    def test_multiple_folder_memberships_deduplicate_by_chat_id(self):
        first = make_filter(1, "groups", groups=True)
        second = make_filter(2, "channels", broadcasts=True)
        entity = SimpleNamespace(
            id=555,
            title="Shared chat",
            username=None,
            forum=False,
            participants_count=None,
            broadcast=False,
            creator=False,
            admin_rights=None,
        )
        row1 = SimpleNamespace(id=-100555, entity=entity, unread_count=0)
        row2 = SimpleNamespace(id=-100555, entity=entity, unread_count=0)
        client = FolderClient([first, second], {1: [row1], 2: [row2]})
        with tempfile.TemporaryDirectory() as temp:
            tg = FakeTelegram(client, Path(temp))
            code = asyncio.run(
                folders_forums.command_folder_chats(
                    tg, ["groups", "channels"], output_format="csv", output_dir=temp, max_chats=10
                )
            )
            payload, _ = tg.output[-1]
        self.assertEqual(code, 0)
        self.assertEqual(payload["unique_chats"], 1)
        row = payload["chats"][0]
        self.assertEqual(row["folder_id"], [1, 2])
        self.assertEqual(row["folder_name"], ["groups", "channels"])
        self.assertEqual(row["folder_memberships"], [
            {"folder_id": 1, "folder_name": "groups"},
            {"folder_id": 2, "folder_name": "channels"},
        ])

    def test_folder_api_failure_is_reported_without_guessing_explicit_lists(self):
        class FailedClient(FolderClient):
            async def __call__(self, request):
                raise RuntimeError("limited")

        client = FailedClient([])
        with tempfile.TemporaryDirectory() as temp:
            tg = FakeTelegram(client, Path(temp))
            code = asyncio.run(
                folders_forums.command_folder_chats(
                    tg, ["My groups"], output_format="json", output_dir=temp, max_chats=10
                )
            )
        payload, exit_code = tg.output[-1]
        self.assertEqual((code, exit_code), (1, 1))
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["chats"], [])
        self.assertEqual(payload["error"]["code"], "RuntimeError")
        self.assertEqual(client.iter_calls, [])

    def test_topics_paginate_using_last_message_fields_not_topic_id_as_message_id(self):
        topics1 = [make_topic(90, "Renamed", top_message=900, closed=True), make_topic(80, "Open", top_message=800)]
        topics2 = [make_topic(1, "General", top_message=1, hidden=True)]
        response1 = SimpleNamespace(
            count=3,
            topics=topics1,
            messages=[
                SimpleNamespace(id=900, date=datetime(2026, 3, 2, tzinfo=timezone.utc)),
                SimpleNamespace(id=800, date=datetime(2026, 3, 1, tzinfo=timezone.utc)),
            ],
        )
        response2 = SimpleNamespace(
            count=3,
            topics=topics2,
            messages=[SimpleNamespace(id=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc))],
        )

        class Client:
            def __init__(self):
                self.calls = []

            async def get_input_entity(self, entity):
                return "input-peer"

            async def __call__(self, request):
                self.calls.append(request)
                return (response1, response2)[len(self.calls) - 1]

        client = Client()
        chat = {"id": -100, "title": "Forum"}
        rows, count, complete, error = asyncio.run(
            folders_forums._fetch_topics(client, object(), chat=chat, max_topics=20)
        )
        self.assertTrue(complete)
        self.assertIsNone(error)
        self.assertEqual(count, 3)
        self.assertEqual([row["topic_id"] for row in rows], [1, 80, 90])
        self.assertEqual(next(row for row in rows if row["topic_id"] == 90)["status"], "closed")
        self.assertTrue(client.calls[1].offset_date == response1.messages[1].date)
        self.assertEqual(client.calls[1].offset_id, 800)
        self.assertEqual(client.calls[1].offset_topic, 80)
        self.assertNotEqual(client.calls[1].offset_id, client.calls[1].offset_topic)

    def test_topics_report_partial_when_server_omits_a_safe_pagination_cursor(self):
        response = SimpleNamespace(
            count=2,
            topics=[make_topic(90, "One", top_message=900)],
            messages=[],
        )

        class Client:
            async def get_input_entity(self, entity):
                return "peer"

            async def __call__(self, request):
                return response

        rows, count, complete, error = asyncio.run(
            folders_forums._fetch_topics(
                Client(), object(), chat={"id": 1, "title": "Forum"}, max_topics=10
            )
        )
        self.assertEqual(count, 2)
        self.assertFalse(complete)
        self.assertEqual(error["code"], "topic_pagination_offset_unavailable")
        self.assertTrue(any(row["topic_id"] == 1 for row in rows))

    def test_general_topic_export_filters_actual_thread_headers_and_preserves_topic_id(self):
        general_reply = SimpleNamespace(reply_to_msg_id=6, reply_to_top_id=None, forum_topic=True)
        other_reply = SimpleNamespace(reply_to_msg_id=4, reply_to_top_id=99, forum_topic=True)
        messages = [
            SimpleNamespace(id=10, reply_to=other_reply, action=None, message="other"),
            SimpleNamespace(id=9, reply_to=general_reply, action=None, message="general reply"),
            SimpleNamespace(id=8, reply_to=None, action=None, message="general message"),
            SimpleNamespace(
                id=7,
                reply_to=None,
                action=type("MessageActionTopicCreate", (), {})(),
                message="topic root",
            ),
        ]

        class Client:
            entity = SimpleNamespace(id=1, title="Forum", forum=True)

            async def get_messages(self, entity, ids):
                return None

            def iter_messages(self, entity, **kwargs):
                self.kwargs = kwargs

                async def generate():
                    for message in messages:
                        yield message

                return generate()

        with tempfile.TemporaryDirectory() as temp:
            client = Client()
            tg = FakeTelegram(client, Path(temp))
            code = asyncio.run(
                folders_forums.command_export_topic(
                    tg, "Forum", topic_id=1, max_messages=10, output_dir=temp
                )
            )
            payload, exit_code = tg.output[-1]
            rows = [
                json.loads(line)
                for line in Path(payload["messages_path"]).read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual((code, exit_code), (0, 0))
        self.assertNotIn("reply_to", client.kwargs)
        self.assertEqual([row["message_id"] for row in rows], [9, 8])
        self.assertTrue(all(row["topic_id"] == 1 for row in rows))

    def test_topic_export_resumes_from_saved_cursor_and_deduplicates(self):
        thread_reply = SimpleNamespace(reply_to_msg_id=40, reply_to_top_id=90, forum_topic=True)
        first_page = [
            SimpleNamespace(id=95, reply_to=thread_reply, action=None, message="five"),
            SimpleNamespace(id=94, reply_to=thread_reply, action=None, message="four"),
            SimpleNamespace(id=93, reply_to=thread_reply, action=None, message="three"),
        ]
        second_page = [first_page[2]]

        class Client:
            entity = SimpleNamespace(id=1, title="Forum", forum=True)

            def __init__(self):
                self.calls = []

            async def get_messages(self, entity, ids):
                return None

            def iter_messages(self, entity, **kwargs):
                self.calls.append(kwargs)
                page = first_page if len(self.calls) == 1 else second_page

                async def generate():
                    for message in page:
                        yield message

                return generate()

        with tempfile.TemporaryDirectory() as temp:
            client = Client()
            tg = FakeTelegram(client, Path(temp))
            first_code = asyncio.run(
                folders_forums.command_export_topic(
                    tg, "Forum", topic_id=90, max_messages=2, output_dir=temp
                )
            )
            first_report = tg.output[-1][0]
            second_code = asyncio.run(
                folders_forums.command_export_topic(
                    tg, "Forum", topic_id=90, max_messages=2, output_dir=temp
                )
            )
            second_report = tg.output[-1][0]
            rows = [
                json.loads(line)
                for line in Path(second_report["messages_path"]).read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(first_code, 1)
        self.assertFalse(first_report["complete"])
        self.assertEqual(second_code, 0)
        self.assertTrue(second_report["complete"])
        self.assertEqual(client.calls[0]["reply_to"], 90)
        self.assertEqual(client.calls[1]["reply_to"], 90)
        self.assertEqual(client.calls[1]["offset_id"], 94)
        self.assertEqual([row["message_id"] for row in rows], [95, 94, 93])
        self.assertTrue(all(row["topic_id"] == 90 for row in rows))

    def test_topic_export_retries_a_message_that_failed_to_save(self):
        thread_reply = SimpleNamespace(reply_to_msg_id=40, reply_to_top_id=90, forum_topic=True)
        messages = {
            95: SimpleNamespace(id=95, reply_to=thread_reply, action=None, message="five"),
            94: SimpleNamespace(id=94, reply_to=thread_reply, action=None, message="four"),
            93: SimpleNamespace(id=93, reply_to=thread_reply, action=None, message="three"),
        }

        class Client:
            entity = SimpleNamespace(id=1, title="Forum", forum=True)

            def __init__(self):
                self.calls = []

            async def get_messages(self, entity, ids):
                return None

            def iter_messages(self, entity, **kwargs):
                self.calls.append(kwargs)
                page = [messages[95], messages[94], messages[93]] if len(self.calls) == 1 else [
                    messages[94], messages[93]
                ]

                async def generate():
                    for message in page:
                        yield message

                return generate()

        with tempfile.TemporaryDirectory() as temp:
            client = Client()
            tg = FakeTelegram(client, Path(temp))
            append = tg.append_record
            failed_once = False

            def append_with_one_failure(path, record):
                nonlocal failed_once
                if record["message_id"] == 94 and not failed_once:
                    failed_once = True
                    raise OSError("simulated write failure")
                append(path, record)

            tg.append_record = append_with_one_failure
            first_code = asyncio.run(
                folders_forums.command_export_topic(
                    tg, "Forum", topic_id=90, max_messages=10, output_dir=temp
                )
            )
            first_report = tg.output[-1][0]
            metadata = json.loads(Path(first_report["metadata_path"]).read_text(encoding="utf-8"))
            second_code = asyncio.run(
                folders_forums.command_export_topic(
                    tg, "Forum", topic_id=90, max_messages=10, output_dir=temp
                )
            )
            second_report = tg.output[-1][0]
            rows = [
                json.loads(line)
                for line in Path(second_report["messages_path"]).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(first_code, 1)
        self.assertEqual(metadata["history_offset_id"], 95)
        self.assertEqual(second_code, 0)
        self.assertEqual(client.calls[1]["offset_id"], 95)
        self.assertEqual([row["message_id"] for row in rows], [95, 94, 93])

    def test_non_forum_chat_is_rejected_before_topic_history_is_read(self):
        class Client:
            entity = SimpleNamespace(id=1, title="Not a forum", forum=False)

            def iter_messages(self, *args, **kwargs):
                raise AssertionError("non-forum history must not be read")

        with tempfile.TemporaryDirectory() as temp:
            tg = FakeTelegram(Client(), Path(temp))
            code = asyncio.run(
                folders_forums.command_export_topic(
                    tg, "Not a forum", topic_id=1, max_messages=10, output_dir=temp
                )
            )
        self.assertEqual(code, 2)
        self.assertEqual(tg.output[-1][0]["status"], "not_a_forum")

    def test_folder_forum_helpers_do_not_call_telegram_mutation_methods(self):
        tree = ast.parse(Path(folders_forums.__file__).read_text(encoding="utf-8"))
        forbidden = {
            "send_message",
            "send_read_acknowledge",
            "edit_message",
            "delete_messages",
            "delete_topic_history",
            "join_channel",
            "leave_channel",
            "mark_read",
        }
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(forbidden.isdisjoint(calls), forbidden.intersection(calls))


if __name__ == "__main__":
    unittest.main()
