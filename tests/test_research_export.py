from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import telegram


class ResearchExportTests(unittest.TestCase):
    def test_cli_parses_folder_topic_and_full_history_commands(self):
        parser = telegram.build_parser()
        folders = parser.parse_args(["folders", "chats", "--folder", "My groups", "--folder", "2", "--format", "csv"])
        self.assertEqual(folders.command, "folders")
        self.assertEqual(folders.folders_command, "chats")
        self.assertEqual(folders.folder, ["My groups", "2"])
        self.assertEqual(folders.format, "csv")

        topics = parser.parse_args(["topics", "--chat", "-100123"])
        self.assertEqual((topics.command, topics.chat), ("topics", "-100123"))

        export = parser.parse_args(["export", "--chat", "-100123", "--topic-id", "1", "--max-messages", "25"])
        self.assertEqual((export.topic_id, export.max_messages), (1, 25))
        all_history = parser.parse_args(["export", "--chat", "501", "--all-history"])
        self.assertTrue(all_history.all_history)

        media = parser.parse_args([
            "media", "export", "--chat", "-100123", "--since", "2026-01-01",
            "--until", "2026-01-31", "--topic-id", "90",
        ])
        self.assertEqual(media.topic_id, 90)

    def test_all_history_export_resumes_older_pages_then_syncs_new_ids(self):
        message_date = datetime(2026, 10, 1, tzinfo=timezone.utc)

        def message(message_id):
            return SimpleNamespace(
                id=message_id,
                sender_id=5,
                sender=None,
                date=message_date,
                message=f"message {message_id}",
                reply_to=None,
                edit_date=None,
                media=None,
            )

        class Client:
            def __init__(self):
                self.calls = []
                self.pages = [
                    [message(5), message(4), message(3)],
                    [message(3), message(2)],
                    [message(6)],
                ]

            def iter_messages(self, entity, **kwargs):
                self.calls.append(kwargs)
                page = self.pages[len(self.calls) - 1]

                async def generate():
                    for item in page:
                        yield item

                return generate()

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                client = Client()
                chat_folder = root / "chat_501"

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, _query):
                    return object(), None

                with patch.object(telegram, "authorized_client", side_effect=authorized), \
                        patch.object(telegram, "resolve_or_result", side_effect=resolve), \
                        patch.object(telegram, "entity_record", return_value={
                            "id": 501, "title": "Private chat", "username": None, "type": "private",
                        }), \
                        patch.object(telegram, "chat_dir", return_value=chat_folder), \
                        patch.object(telegram, "_set_private_permissions", return_value=None), \
                        patch.object(telegram, "close_client", new=AsyncMock()):
                    reports = []
                    for _ in range(3):
                        output = []
                        with patch.object(
                            telegram,
                            "emit",
                            side_effect=lambda payload, exit_code=0: output.append((payload, exit_code)) or exit_code,
                        ):
                            code = await telegram.command_all_history_export("501", limit=2)
                        reports.append((code, output[-1][0]))

                path = Path(reports[-1][1]["messages_path"])
                ids = [json.loads(line)["message_id"] for line in path.read_text(encoding="utf-8").splitlines()]
                metadata = json.loads(Path(reports[-1][1]["metadata_path"]).read_text(encoding="utf-8"))
                return client, reports, ids, metadata

        client, reports, ids, metadata = asyncio.run(run())
        self.assertEqual([report[0] for report in reports], [1, 0, 0])
        self.assertEqual([report[1]["mode"] for report in reports], ["initial", "resume", "incremental"])
        self.assertEqual(client.calls[1]["offset_id"], 4)
        self.assertEqual(client.calls[2]["min_id"], 5)
        self.assertTrue(client.calls[2]["reverse"])
        self.assertEqual(ids, [5, 4, 3, 2, 6])
        self.assertTrue(metadata["history_sweep_complete"])
        self.assertTrue(metadata["complete"])

    def test_all_history_export_retries_a_message_that_failed_to_save(self):
        message_date = datetime(2026, 10, 1, tzinfo=timezone.utc)

        def message(message_id):
            return SimpleNamespace(
                id=message_id,
                sender_id=5,
                sender=None,
                date=message_date,
                message=f"message {message_id}",
                reply_to=None,
                edit_date=None,
                media=None,
            )

        class Client:
            def __init__(self):
                self.calls = []
                self.pages = [[message(5), message(4), message(3)], [message(4), message(3)]]

            def iter_messages(self, entity, **kwargs):
                self.calls.append(kwargs)
                page = self.pages[len(self.calls) - 1]

                async def generate():
                    for item in page:
                        yield item

                return generate()

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                client = Client()
                chat_folder = root / "chat_501"
                failed_once = False
                append = telegram.append_record

                def append_with_one_failure(path, record):
                    nonlocal failed_once
                    if record["message_id"] == 4 and not failed_once:
                        failed_once = True
                        raise OSError("simulated write failure")
                    append(path, record)

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, _query):
                    return object(), None

                with patch.object(telegram, "authorized_client", side_effect=authorized), \
                        patch.object(telegram, "resolve_or_result", side_effect=resolve), \
                        patch.object(telegram, "entity_record", return_value={
                            "id": 501, "title": "Private chat", "username": None, "type": "private",
                        }), \
                        patch.object(telegram, "chat_dir", return_value=chat_folder), \
                        patch.object(telegram, "_set_private_permissions", return_value=None), \
                        patch.object(telegram, "append_record", side_effect=append_with_one_failure), \
                        patch.object(telegram, "close_client", new=AsyncMock()):
                    reports = []
                    metadata_after_failure = None
                    for index in range(2):
                        output = []
                        with patch.object(
                            telegram,
                            "emit",
                            side_effect=lambda payload, exit_code=0: output.append((payload, exit_code)) or exit_code,
                        ):
                            code = await telegram.command_all_history_export("501", limit=10)
                        reports.append((code, output[-1][0]))
                        if index == 0:
                            metadata_after_failure = json.loads(
                                Path(output[-1][0]["metadata_path"]).read_text(encoding="utf-8")
                            )

                path = Path(reports[-1][1]["messages_path"])
                ids = [json.loads(line)["message_id"] for line in path.read_text(encoding="utf-8").splitlines()]
                return client, reports, ids, metadata_after_failure

        client, reports, ids, metadata = asyncio.run(run())
        self.assertEqual(reports[0][0], 1)
        self.assertEqual(metadata["history_offset_id"], 5)
        self.assertEqual(reports[1][0], 0)
        self.assertEqual(client.calls[1]["offset_id"], 5)
        self.assertEqual(ids, [5, 4, 3])

    def test_media_export_scopes_by_topic_and_reuses_existing_downloader(self):
        thread = SimpleNamespace(reply_to_top_id=90, reply_to_msg_id=80, forum_topic=True)
        wrong_thread = SimpleNamespace(reply_to_top_id=91, reply_to_msg_id=81, forum_topic=True)
        date = datetime(2026, 1, 10, tzinfo=timezone.utc)
        messages = [
            SimpleNamespace(id=1, date=date, reply_to=thread, media=object()),
            SimpleNamespace(id=2, date=date, reply_to=wrong_thread, media=object()),
        ]

        class Client:
            entity = SimpleNamespace(id=123, title="Forum", forum=True)

            def iter_messages(self, entity, **kwargs):
                self.kwargs = kwargs

                async def generate():
                    for item in messages:
                        yield item

                return generate()

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = Client()
                store = SimpleNamespace(manifest_path=Path(temp) / "manifest.jsonl")
                downloads = AsyncMock(return_value=({"status": "downloaded", "message_id": 1}, 12))
                output = []

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, _query):
                    return client.entity, None

                with patch.object(telegram, "authorized_client", side_effect=authorized), \
                        patch.object(telegram, "resolve_or_result", side_effect=resolve), \
                        patch.object(telegram, "entity_record", return_value={
                            "id": -100123, "title": "Forum", "username": None, "type": "group",
                        }), \
                        patch.object(telegram, "_media_store", return_value=store), \
                        patch.object(telegram, "attachment_descriptor", return_value={
                            "attachment_type": "photo", "mime_type": "image/jpeg",
                            "original_file_name": None, "expected_size_bytes": 12,
                        }), \
                        patch.object(telegram, "download_attachment", downloads), \
                        patch.object(telegram, "close_client", new=AsyncMock()), \
                        patch.object(
                            telegram,
                            "emit",
                            side_effect=lambda payload, exit_code=0: output.append((payload, exit_code)) or exit_code,
                        ):
                    code = await telegram.command_media_export(
                        "Forum",
                        since_arg="2026-01-01",
                        until_arg="2026-01-31",
                        output_dir=temp,
                        max_files=10,
                        max_messages=10,
                        max_file_bytes=1024,
                        max_total_bytes=2048,
                        allowed_types=None,
                        topic_id=90,
                    )
                return code, output[-1], client, downloads

        code, (payload, exit_code), client, downloads = asyncio.run(run())
        self.assertEqual((code, exit_code), (0, 0))
        self.assertEqual(client.kwargs["reply_to"], 90)
        self.assertEqual(payload["topic_id"], 90)
        self.assertEqual(payload["messages_scanned"], 2)
        downloads.assert_awaited_once()
        self.assertEqual(downloads.await_args.kwargs["topic_id"], 90)


if __name__ == "__main__":
    unittest.main()
