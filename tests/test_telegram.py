from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import telegram
from scripts import updater


class TelegramSkillTests(unittest.TestCase):
    def test_update_check_compares_release_and_caches_for_a_day(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version_file = root / "VERSION"
            version_file.write_text("0.1.0", encoding="utf-8")
            with patch.object(updater, "VERSION_PATH", version_file), patch.object(updater, "CACHE_PATH", root / "cache.json"), \
                    patch.object(updater, "latest_release", return_value={"tag": "v0.2.0", "url": "https://github.com/example/repo/releases/tag/v0.2.0"}):
                first = updater.check_for_update(force=True)
                self.assertEqual(first["status"], "update_available")
                self.assertFalse(first["cached"])
            with patch.object(updater, "VERSION_PATH", version_file), patch.object(updater, "CACHE_PATH", root / "cache.json"), \
                    patch.object(updater, "latest_release", side_effect=AssertionError("cache should avoid network")):
                second = updater.check_for_update()
                self.assertEqual(second["status"], "update_available")
                self.assertTrue(second["cached"])

    def test_update_refuses_non_git_install(self):
        with patch.object(updater, "__file__", str(Path(tempfile.gettempdir()) / "no-git-root" / "scripts" / "updater.py")):
            result = updater.apply_update()
        self.assertEqual(result["status"], "not_git_install")

    def test_missing_session_status_does_not_connect(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"TELEGRAM_SKILL_HOME": temp}):
            output = []
            with patch.object(telegram, "emit", side_effect=lambda value, exit_code=0: output.append((value, exit_code)) or exit_code):
                result = telegram.main(["status"])
            self.assertEqual(result, 0)
            self.assertEqual(output[0][0]["status"], "needs_auth")

    def test_credentials_written_private_and_secret_not_printed(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"TELEGRAM_SKILL_HOME": temp}):
            root = telegram.ensure_data_dir()
            telegram.save_credentials(123, "private-api-hash")
            path = telegram.credentials_path()
            if os.name == "nt":
                assert_owner_only_acl(self, root, directory=True)
                assert_owner_only_acl(self, path)
            else:
                self.assertEqual(stat_mode(root), 0o700)
                self.assertEqual(stat_mode(path), 0o600)
            self.assertEqual(json.loads(path.read_text())["api_hash"], "private-api-hash")
            self.assertEqual(telegram.load_credentials(), {"api_id": 123, "api_hash": "private-api-hash"})

    def test_atomic_credentials_roundtrip_without_fchmod(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"TELEGRAM_SKILL_HOME": temp}), \
                patch.dict(telegram.os.__dict__):
            telegram.os.__dict__.pop("fchmod", None)
            telegram.ensure_data_dir()
            telegram.save_credentials(456, "windows-python-312-regression")
            self.assertEqual(
                telegram.load_credentials(),
                {"api_id": 456, "api_hash": "windows-python-312-regression"},
            )
            self.assertEqual(list(Path(temp).glob(".credentials.json.*")), [])

    def test_session_file_is_restricted_to_owner(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"TELEGRAM_SKILL_HOME": temp}):
            telegram.ensure_data_dir()
            telegram.save_credentials(123, "hash")
            session = telegram.session_path()
            session.write_bytes(b"mock sqlite session")
            session.chmod(0o644)
            telegram.secure_auth_files()
            if os.name == "nt":
                assert_owner_only_acl(self, session)
            else:
                self.assertEqual(stat_mode(session), 0o600)

    def test_serializes_message_with_utc_and_attachment_metadata(self):
        message = SimpleNamespace(
            id=44,
            sender_id=7,
            sender=SimpleNamespace(first_name="Иван", last_name="Тест", username="ivan"),
            date=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
            message="Привет",
            reply_to=SimpleNamespace(reply_to_msg_id=40),
            edit_date=None,
            media=SimpleNamespace(photo=SimpleNamespace(size=2048), document=None),
        )
        record = telegram.serialize_message(message, -100123)
        self.assertEqual(record["message_id"], 44)
        self.assertEqual(record["date"], "2026-01-02T03:04:00Z")
        self.assertEqual(record["sender_name"], "Иван Тест")
        self.assertEqual(record["type"], "text_with_attachment")
        self.assertEqual(record["attachments"][0]["type"], "photo")

    def test_searches_dialog_metadata_and_reports_ambiguous_names(self):
        class FakeClient:
            def __init__(self):
                self.message_history_requested = False

            async def iter_dialogs(self):
                for chat_id, title, username in (
                    (-100101, "Иван Петров", "ivan_p"),
                    (-100202, "Иван Сидоров", "ivan_s"),
                ):
                    entity = SimpleNamespace(id=abs(chat_id), title=title, username=username, broadcast=False)
                    yield SimpleNamespace(id=chat_id, entity=entity)

            async def iter_messages(self, *args, **kwargs):
                self.message_history_requested = True
                if False:
                    yield None

        async def run():
            client = FakeClient()
            matches = await telegram.find_chats(client, "Иван")
            self.assertEqual([item["id"] for item in matches], [-100101, -100202])
            exact = await telegram.find_chats(client, "@ivan_p")
            self.assertEqual(len(exact), 1)
            self.assertEqual(exact[0]["id"], -100101)
            self.assertFalse(client.message_history_requested)

        import asyncio

        asyncio.run(run())

    def test_telegram_links_normalize_without_joining_invite_chats(self):
        marked_id = telegram.normalize_query("https://t.me/c/123456/99")
        self.assertEqual(marked_id, str(telegram.utils.get_peer_id(telegram.types.PeerChannel(123456))))
        self.assertEqual(telegram.normalize_query("https://t.me/ivan_p/42"), "ivan_p")
        self.assertEqual(telegram.normalize_query("https://t.me/+privateInvite"), "https://t.me/+privateInvite")

    def test_private_invite_is_not_resolved_or_joined(self):
        class FakeClient:
            async def iter_dialogs(self):
                if False:
                    yield None

            async def get_entity(self, query):
                raise AssertionError("private invite must not be resolved")

        async def run():
            entity, choices = await telegram.resolve_chat(FakeClient(), "https://t.me/+privateInvite")
            self.assertIsNone(entity)
            self.assertEqual(choices, [])

        import asyncio

        asyncio.run(run())

    def test_incremental_append_has_no_duplicate_and_repairs_partial_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "messages.jsonl"
            record = {"message_id": 9, "text": "ok"}
            telegram.append_record(path, record)
            with path.open("ab") as stream:
                stream.write(b'{"message_id":')
            count, last_id = telegram.repair_jsonl(path)
            self.assertEqual((count, last_id), (1, 9))
            self.assertEqual(telegram.existing_ids(path), {9})
            telegram.append_record(path, {"message_id": 10, "text": "new"})
            count, last_id = telegram.repair_jsonl(path)
            self.assertEqual((count, last_id), (2, 10))
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_repair_adds_newline_after_valid_interrupted_record(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "messages.jsonl"
            path.write_bytes(b'{"message_id":9}')
            self.assertEqual(telegram.repair_jsonl(path), (1, 9))
            self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_repair_streams_large_jsonl_without_reading_whole_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "messages.jsonl"
            path.write_text('{"message_id":9}\n', encoding="utf-8")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
                self.assertEqual(telegram.repair_jsonl(path), (1, 9))

    def test_export_then_sync_persists_only_new_ids(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
                self.round = 0

            async def iter_messages(self, entity, **kwargs):
                self.calls.append(kwargs)
                self.round += 1
                ids = (1, 2) if self.round == 1 else (2, 3)
                for message_id in ids:
                    yield SimpleNamespace(
                        id=message_id,
                        sender_id=5,
                        sender=None,
                        date=datetime.now(timezone.utc).replace(microsecond=0),
                        message=f"message {message_id}",
                        reply_to=None,
                        edit_date=None,
                        media=None,
                    )

            async def disconnect(self):
                return None

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = FakeClient()
                entity = object()
                async def fake_authorized():
                    return client, None
                async def fake_resolve(_client, _query):
                    return entity, None
                with patch.object(telegram, "authorized_client", fake_authorized), patch.object(telegram, "resolve_or_result", fake_resolve), patch.object(telegram, "entity_record", return_value={"id": 99, "title": "Test chat", "username": None, "type": "group"}), patch.object(telegram, "chat_dir", side_effect=lambda chat_id: Path(temp) / f"chat_{chat_id}"):
                    output = StringIO()
                    with redirect_stdout(output):
                        await telegram.command_export("Test chat", days=None, since_arg=None, until_arg=None, limit=5, sync_only=False)
                    first = json.loads(output.getvalue())
                    output = StringIO()
                    with redirect_stdout(output):
                        await telegram.command_export("Test chat", days=None, since_arg=None, until_arg=None, limit=5, sync_only=False)
                    second = json.loads(output.getvalue())
                self.assertEqual(first["messages_processed"], 2)
                self.assertEqual(second["mode"], "incremental")
                self.assertEqual(second["messages_processed"], 1)
                self.assertEqual(client.calls[1]["min_id"], 2)
                lines = (Path(temp) / "chat_99" / "messages.jsonl").read_text().splitlines()
                self.assertEqual([json.loads(line)["message_id"] for line in lines], [1, 2, 3])

        import asyncio

        asyncio.run(run())

    def test_interrupted_initial_range_resumes_and_deduplicates(self):
        class FakeClient:
            async def iter_messages(self, entity, **kwargs):
                for message_id in (1, 2):
                    yield SimpleNamespace(
                        id=message_id,
                        sender_id=5,
                        sender=None,
                        date=datetime(2026, 10, 15, tzinfo=timezone.utc),
                        message=f"message {message_id}",
                        reply_to=None,
                        edit_date=None,
                        media=None,
                    )

            async def disconnect(self):
                return None

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                folder = Path(temp) / "chat_99"
                folder.mkdir()
                messages = folder / "messages.jsonl"
                telegram.append_record(messages, {"message_id": 1, "chat_id": 99, "text": "partial"})
                start = "2026-10-01T00:00:00Z"
                end = "2026-10-31T23:59:59Z"
                telegram.atomic_json(folder / "metadata.json", {
                    "chat": {"id": 99, "title": "Test chat"},
                    "period_start": start,
                    "period_end": end,
                    "export_in_progress": True,
                    "last_saved_message_id": 1,
                    "message_count": 1,
                })
                async def fake_authorized():
                    return FakeClient(), None
                async def fake_resolve(_client, _query):
                    return object(), None
                with patch.object(telegram, "authorized_client", fake_authorized), patch.object(telegram, "resolve_or_result", fake_resolve), patch.object(telegram, "entity_record", return_value={"id": 99, "title": "Test chat", "username": None, "type": "group"}), patch.object(telegram, "chat_dir", side_effect=lambda chat_id: folder):
                    output = StringIO()
                    with redirect_stdout(output):
                        await telegram.command_export("Test chat", days=None, since_arg=None, until_arg=None, limit=5, sync_only=True)
                    result = json.loads(output.getvalue())
                self.assertEqual(result["mode"], "range")
                self.assertEqual(result["messages_processed"], 1)
                self.assertEqual([json.loads(line)["message_id"] for line in messages.read_text().splitlines()], [1, 2])
                self.assertFalse(json.loads((folder / "metadata.json").read_text())["export_in_progress"])

        import asyncio

        asyncio.run(run())

    def test_incremental_query_uses_min_id_and_never_refetches_history(self):
        class FakeClient:
            def __init__(self):
                self.kwargs = None

            async def iter_messages(self, entity, **kwargs):
                self.kwargs = kwargs
                for message_id in (11, 12):
                    yield SimpleNamespace(id=message_id, date=datetime.now(timezone.utc))

        async def run():
            client = FakeClient()
            items, capped = await telegram.consume_messages(client, object(), limit=5, min_id=10)
            self.assertEqual([item.id for item in items], [11, 12])
            self.assertFalse(capped)
            self.assertEqual(client.kwargs["min_id"], 10)
            self.assertTrue(client.kwargs["reverse"])
            self.assertNotIn("offset_date", client.kwargs)

        import asyncio

        asyncio.run(run())

    def test_message_limit_reports_truncated_result(self):
        class FakeClient:
            async def iter_messages(self, entity, **kwargs):
                for message_id in (1, 2, 3):
                    yield SimpleNamespace(id=message_id, date=datetime.now(timezone.utc))

        async def run():
            messages, capped = await telegram.consume_messages(FakeClient(), object(), limit=2)
            self.assertEqual([message.id for message in messages], [1, 2])
            self.assertTrue(capped)

        import asyncio

        asyncio.run(run())

    def test_invalid_auth_errors_are_user_actionable(self):
        from telethon.errors import PhoneCodeInvalidError
        from telethon.errors import PhoneCodeExpiredError
        from telethon.errors import ApiIdInvalidError
        from telethon.errors import AuthKeyUnregisteredError
        from telethon.errors import FloodWaitError

        error = telegram.safe_error(PhoneCodeInvalidError(request=None))
        self.assertEqual(error["code"], "invalid_code")
        self.assertIn("Terminal", error["message"])
        expired_code = telegram.safe_error(PhoneCodeExpiredError(request=None))
        self.assertEqual(expired_code["code"], "expired_code")
        invalid_api = telegram.safe_error(ApiIdInvalidError(request=None))
        self.assertEqual(invalid_api["code"], "invalid_api_credentials")
        self.assertIn("--reset-api", invalid_api["message"])
        invalid_session = telegram.safe_error(AuthKeyUnregisteredError(request=None))
        self.assertEqual(invalid_session["code"], "invalid_session")
        self.assertIn("telegram auth", invalid_session["message"])
        flood_wait = telegram.safe_error(FloodWaitError(request=None, capture=42))
        self.assertEqual(flood_wait["code"], "flood_wait")
        self.assertEqual(flood_wait["retry_after_seconds"], 42)

    def test_auth_without_tty_returns_guidance_without_network(self):
        output = []
        with patch("sys.stdin.isatty", return_value=False), patch.object(telegram, "emit", side_effect=lambda value, exit_code=0: output.append((value, exit_code)) or exit_code):
            result = telegram.main(["auth"])
        self.assertEqual(result, 2)
        self.assertEqual(output[0][0]["error"]["code"], "interactive_terminal_required")

    def test_cli_sync_does_not_require_export_period_arguments(self):
        args = telegram.build_parser().parse_args(["sync", "--chat", "123"])
        self.assertFalse(hasattr(args, "days"))
        self.assertEqual(args.max_messages, telegram.DEFAULT_LIMIT)

    def test_delete_channel_dry_run_and_confirmation_id_gate(self):
        channel = type("Channel", (), {"id": 123, "broadcast": True, "username": "test_channel"})()
        target = {"id": -100123, "title": "Test channel", "username": "test_channel", "type": "channel"}
        client = AsyncMock()
        request = object()
        output = []

        async def run():
            with patch.object(telegram, "authorized_client", new=AsyncMock(return_value=(client, None))), \
                    patch.object(telegram, "resolve_or_result", new=AsyncMock(return_value=(channel, None))), \
                    patch.object(telegram, "entity_record", return_value=target), \
                    patch.object(telegram, "channel_link", new=AsyncMock(return_value="https://t.me/test_channel")), \
                    patch.object(telegram, "DeleteChannelRequest", return_value=request), \
                    patch.object(telegram, "close_client", new=AsyncMock()), \
                    patch.object(telegram, "emit", side_effect=lambda value, exit_code=0: output.append((value, exit_code)) or exit_code):
                result = await telegram.command_delete_channel("@test_channel", confirm_id=None)
                self.assertEqual(result, 0)
                self.assertEqual(output[-1][0]["status"], "confirmation_required")
                self.assertEqual(output[-1][0]["link"], "https://t.me/test_channel")
                self.assertEqual(client.await_count, 0)

                result = await telegram.command_delete_channel("@test_channel", confirm_id=-100999)
                self.assertEqual(result, 2)
                self.assertEqual(output[-1][0]["status"], "confirmation_mismatch")
                self.assertEqual(client.await_count, 0)

                result = await telegram.command_delete_channel("@test_channel", confirm_id=-100123)
                self.assertEqual(result, 0)
                self.assertEqual(output[-1][0]["status"], "deleted")
                client.assert_awaited_once_with(request)

        import asyncio

        asyncio.run(run())

    def test_delete_channel_parser_requires_target_and_accepts_exact_id(self):
        args = telegram.build_parser().parse_args([
            "delete-channel", "--chat", "@test_channel", "--confirm-id", "-100123"
        ])
        self.assertEqual(args.command, "delete-channel")
        self.assertEqual(args.confirm_id, -100123)

    def test_bot_click_requires_confirmation_and_matching_button_label(self):
        import asyncio

        bot = type("User", (), {"id": 99, "bot": True})()
        button = SimpleNamespace(text="Proceed", button=SimpleNamespace(data=b"callback", url=None))
        message = SimpleNamespace(id=123, buttons=[[button]], message="Choose an action", click=AsyncMock())
        message.click.return_value = None
        client = AsyncMock()
        client.get_messages.return_value = message
        output = []
        target = {"id": 99, "title": "Test bot", "username": "test_bot", "type": "private"}

        async def run():
            with patch.object(telegram, "authorized_client", new=AsyncMock(return_value=(client, None))), \
                    patch.object(telegram, "resolve_or_result", new=AsyncMock(return_value=(bot, None))), \
                    patch.object(telegram, "entity_record", return_value=target), \
                    patch.object(telegram, "close_client", new=AsyncMock()), \
                    patch.object(telegram, "emit", side_effect=lambda value, exit_code=0: output.append((value, exit_code)) or exit_code):
                self.assertEqual(await telegram.bot_click("@test_bot", message_id=123, row=0, column=0, confirm=False, confirm_label=None), 0)
                self.assertEqual(output[-1][0]["status"], "confirmation_required")
                message.click.assert_not_awaited()
                self.assertEqual(await telegram.bot_click("@test_bot", message_id=123, row=0, column=0, confirm=True, confirm_label="Wrong"), 2)
                self.assertEqual(output[-1][0]["status"], "confirmation_mismatch")
                message.click.assert_not_awaited()
                self.assertEqual(await telegram.bot_click("@test_bot", message_id=123, row=0, column=0, confirm=True, confirm_label="Proceed"), 0)
                message.click.assert_awaited_once_with(i=0, j=0)

        asyncio.run(run())

    def test_bot_click_never_activates_url_buttons(self):
        import asyncio

        bot = type("User", (), {"id": 99, "bot": True})()
        button = SimpleNamespace(text="Open site", button=SimpleNamespace(data=None, url="https://example.com"))
        message = SimpleNamespace(id=123, buttons=[[button]], message="Visit", click=AsyncMock())
        client = AsyncMock()
        client.get_messages.return_value = message
        output = []

        async def run():
            with patch.object(telegram, "authorized_client", new=AsyncMock(return_value=(client, None))), \
                    patch.object(telegram, "resolve_or_result", new=AsyncMock(return_value=(bot, None))), \
                    patch.object(telegram, "entity_record", return_value={"id": 99, "title": "Test bot", "type": "private"}), \
                    patch.object(telegram, "close_client", new=AsyncMock()), \
                    patch.object(telegram, "emit", side_effect=lambda value, exit_code=0: output.append((value, exit_code)) or exit_code):
                self.assertEqual(await telegram.bot_click("@test_bot", message_id=123, row=0, column=0, confirm=True, confirm_label="Open site"), 2)
                self.assertEqual(output[-1][0]["status"], "unsupported_button")
                message.click.assert_not_awaited()

        asyncio.run(run())

    def test_bot_cli_parsers(self):
        parser = telegram.build_parser()
        read_args = parser.parse_args(["bot-read", "--chat", "@bot", "--limit", "5"])
        self.assertEqual(read_args.limit, 5)
        click_args = parser.parse_args(["bot-click", "--chat", "@bot", "--message-id", "123", "--row", "0", "--column", "1"])
        self.assertEqual((click_args.message_id, click_args.row, click_args.column), (123, 0, 1))

    def test_bot_send_only_targets_bot_and_requires_confirm_to_send(self):
        import asyncio

        bot = type("User", (), {"id": 99, "bot": True})()
        person = type("User", (), {"id": 100, "bot": False})()
        client = AsyncMock()
        client.send_message.return_value = SimpleNamespace(id=456)
        target = {"id": 99, "title": "Test bot", "username": "test_bot", "type": "private"}
        output = []

        async def run():
            with patch.object(telegram, "authorized_client", new=AsyncMock(return_value=(client, None))), \
                    patch.object(telegram, "collect_dialogs", new=AsyncMock(return_value=[])), \
                    patch.object(telegram, "resolve_cached", new=AsyncMock(side_effect=[(person, []), (bot, []), (bot, [])])), \
                    patch.object(telegram, "entity_record", return_value=target), \
                    patch.object(telegram, "close_client", new=AsyncMock()), \
                    patch.object(telegram, "append_send_log"), \
                    patch.object(telegram, "emit", side_effect=lambda value, exit_code=0: output.append((value, exit_code)) or exit_code):
                await telegram.send_batch([{"chat": "@someone", "text": "/status"}], confirm=True, bots_only=True)
                self.assertEqual(output[-1][0]["results"][0]["reason"], "not_a_bot")
                client.send_message.assert_not_awaited()
                await telegram.send_batch([{"chat": "@test_bot", "text": "/status"}], confirm=False, bots_only=True)
                self.assertEqual(output[-1][0]["results"][0]["status"], "dry_run")
                client.send_message.assert_not_awaited()
                await telegram.send_batch([{"chat": "@test_bot", "text": "/status"}], confirm=True, bots_only=True)
                client.send_message.assert_awaited_once_with(bot, "/status")

        asyncio.run(run())

    def test_script_does_not_join_chats_or_delete_messages(self):
        script_path = Path(telegram.__file__)
        tree = ast.parse(script_path.read_text(encoding="utf-8"))
        forbidden = {"edit_message", "delete_messages", "join_channel", "leave_channel"}
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(forbidden.isdisjoint(calls), forbidden.intersection(calls))


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def assert_owner_only_acl(test: unittest.TestCase, path: Path, *, directory: bool = False) -> None:
    result = subprocess.run(
        ["icacls", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    acl_line = result.stdout.splitlines()[0]
    test.assertEqual(acl_line.count(":("), 1, acl_line)
    if directory:
        test.assertIn(":(OI)(CI)(F)", acl_line)
    else:
        test.assertIn(":(F)", acl_line)


if __name__ == "__main__":
    unittest.main()
