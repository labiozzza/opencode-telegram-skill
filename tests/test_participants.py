from __future__ import annotations

import asyncio
import ast
import csv
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acl_helpers import assert_owner_only_acl
from scripts import participants, telegram


def user(user_id: int, first_name: str):
    return SimpleNamespace(
        id=user_id,
        first_name=first_name,
        last_name=None,
        username=None,
        bot=False,
        participant=SimpleNamespace(),
    )


class Channel:
    def __init__(self, chat_id: int, title: str, *, owned: bool, participants_count: int | None):
        self.id = chat_id
        self.title = title
        self.username = None
        self.broadcast = True
        self.creator = owned
        self.admin_rights = None
        self.participants_count = participants_count


class UserEntity:
    def __init__(self, user_id: int, name: str):
        self.id = user_id
        self.first_name = name
        self.last_name = None
        self.username = None
        self.bot = False


def dialog(entity, *, unread_count: int = 0):
    return SimpleNamespace(
        id=entity.id,
        entity=entity,
        is_user=isinstance(entity, UserEntity),
        unread_count=unread_count,
    )


class FakeClient:
    def __init__(self, dialogs=None, participants=None):
        self.dialog_rows = dialogs or []
        self.participant_rows = participants or {}
        self.calls: list[str] = []

    def iter_dialogs(self, limit=None):
        async def generate():
            self.calls.append("iter_dialogs")
            for row in self.dialog_rows[:limit]:
                yield row

        return generate()

    def iter_participants(self, entity, limit=None):
        async def generate():
            self.calls.append("iter_participants")
            for row in self.participant_rows.get(entity.id, []):
                yield row

        return generate()


class FakeTelegram:
    def __init__(self, client):
        self.client = client
        self.output: list[tuple[dict, int]] = []

    async def authorized_client(self, *, preserve_exports=False):
        return self.client, None

    async def close_client(self, client):
        return None

    def safe_error(self, exc):
        return {"code": type(exc).__name__, "message": str(exc)}

    def emit(self, payload, exit_code=0):
        self.output.append((payload, exit_code))
        return exit_code

    def _set_private_permissions(self, path: Path, mode: int, *, directory=False):
        telegram._set_private_permissions(path, mode, directory=directory)

    def data_dir(self):
        return Path(tempfile.gettempdir()) / "unused-telegram-participant-tests"

    def entity_record(self, entity, dialog=None):
        if isinstance(entity, UserEntity):
            return {"id": entity.id, "title": entity.first_name, "username": None, "type": "private"}
        return {
            "id": entity.id,
            "title": entity.title,
            "username": entity.username,
            "type": "channel" if entity.broadcast else "group",
        }

    def iso_utc(self, value):
        return telegram.iso_utc(value)


class ParticipantExportTests(unittest.TestCase):
    def test_cli_exposes_dialogs_and_participants_but_not_account_wide_research(self):
        parser = telegram.build_parser()
        dialogs = parser.parse_args(["dialogs", "--format", "csv", "--max-dialogs", "20"])
        self.assertEqual((dialogs.command, dialogs.format, dialogs.max_dialogs), ("dialogs", "csv", 20))
        args = parser.parse_args(["participants", "--chat", "100", "--max-participants", "25"])
        self.assertEqual((args.command, args.chat, args.max_participants), ("participants", "100", 25))
        with self.assertRaises(SystemExit):
            parser.parse_args(["research", "--product", "Example"])

    def test_role_listing_and_dialog_export_include_only_accessible_metadata(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                owner = Channel(100, "Owner room", owned=True, participants_count=2)
                person = UserEntity(501, "Member")
                owner_record = participants._dialog_record(FakeTelegram(FakeClient()), dialog(owner))
                private_record = participants._dialog_record(FakeTelegram(FakeClient()), dialog(person))
                self.assertEqual(owner_record["account_role"], "owner")
                self.assertIs(owner_record["is_managed"], True)
                self.assertEqual(private_record["account_role"], "private_dialog")
                self.assertNotIn("phone", owner_record)
                self.assertNotIn("phone", private_record)

                client = FakeClient(dialogs=[dialog(owner), dialog(person)])
                tg = FakeTelegram(client)
                self.assertEqual(
                    await participants.command_dialogs(
                        tg, output_format="csv", output_dir=temp, max_dialogs=10
                    ),
                    0,
                )
                payload, exit_code = tg.output[-1]
                self.assertEqual(exit_code, 0)
                self.assertTrue(Path(payload["path"]).exists())
                records = json.loads(Path(payload["index_path"]).read_text(encoding="utf-8"))
                self.assertEqual([row["account_role"] for row in records], ["owner", "private_dialog"])

        asyncio.run(run())

    def test_partial_dialog_iteration_replaces_stale_index_with_partial_records(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                entity = Channel(100, "Partial room", owned=True, participants_count=1)

                class PartialDialogClient(FakeClient):
                    def iter_dialogs(self, limit=None):
                        async def generate():
                            yield dialog(entity)
                            raise RuntimeError("interrupted")

                        return generate()

                stale_index = Path(temp) / "index" / "dialogs.json"
                stale_index.parent.mkdir()
                stale_index.write_text('[{"id":999,"title":"stale"}]', encoding="utf-8")
                tg = FakeTelegram(PartialDialogClient())
                self.assertEqual(
                    await participants.command_dialogs(
                        tg, output_format="json", output_dir=temp, max_dialogs=10
                    ),
                    1,
                )
                payload, exit_code = tg.output[-1]
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["status"], "partial")
                records = json.loads(Path(payload["index_path"]).read_text(encoding="utf-8"))
                self.assertEqual([row["id"] for row in records], [100])

        asyncio.run(run())

    # Added by /ship Step 7 coverage audit (2026-11-18).
    # Value: protects=dialog limits report a capped index instead of silent completeness; fails_when=an extra accessible dialog is omitted without partial status; why_new=existing dialog test covers iteration failure but not the limit sentinel; seam=none; no_claim=Telegram dialog visibility
    def test_dialog_export_marks_limit_reached_and_writes_only_the_bound(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                rows = [
                    dialog(Channel(100 + index, f"Room {index}", owned=False, participants_count=1))
                    for index in range(3)
                ]
                tg = FakeTelegram(FakeClient(dialogs=rows))
                code = await participants.command_dialogs(
                    tg, output_format="json", output_dir=temp, max_dialogs=2
                )

                payload, exit_code = tg.output[-1]
                exported = json.loads(Path(payload["index_path"]).read_text(encoding="utf-8"))
                self.assertEqual(code, 1)
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["status"], "partial")
                self.assertTrue(payload["limit_reached"])
                self.assertEqual(payload["dialogs"], 2)
                self.assertEqual([row["id"] for row in exported], [100, 101])

        asyncio.run(run())

    def test_participant_export_is_bounded_private_and_resumable(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                entity = Channel(100, "Room", owned=False, participants_count=3)
                chat = {"id": 100, "title": "Room", "type": "channel"}
                client = FakeClient(
                    participants={100: [user(1, "One"), user(2, "Two"), user(3, "Three")]}
                )
                tg = FakeTelegram(client)

                first = await participants._scan_participants(
                    tg, client, entity, chat, max_new=2, output_format="json", root=Path(temp)
                )
                self.assertEqual(first["status"], "partial")
                self.assertEqual(first["participants_added_this_run"], 2)
                self.assertEqual(first["limitation_reason"], "participant_limit_reached")

                second = await participants._scan_participants(
                    tg, client, entity, chat, max_new=2, output_format="csv", root=Path(temp)
                )
                self.assertEqual(second["status"], "complete")
                self.assertEqual(second["participants_added_this_run"], 1)
                self.assertEqual(second["resume_replayed_existing"], 2)
                rows = [
                    json.loads(line)
                    for line in Path(second["participants_path"]).read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual([row["user_id"] for row in rows], [1, 2, 3])
                self.assertEqual(Path(second["export_path"]).suffix, ".csv")
                if os.name == "nt":
                    assert_owner_only_acl(self, Path(second["participants_path"]))
                else:
                    self.assertEqual(Path(second["participants_path"]).stat().st_mode & 0o777, 0o600)
                self.assertTrue(all("phone" not in row for row in rows))

        asyncio.run(run())

    # Added by /ship Step 7 coverage audit (2026-11-18).
    # Value: protects=participant exports reject private dialogs before roster iteration; fails_when=a private recipient is passed to iter_participants; why_new=parser and report tests do not exercise the command's target-type guard; seam=none; no_claim=Telegram API permissions
    def test_participant_command_rejects_private_dialog_without_scanning(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                output_dir = Path(temp) / "exports"
                client = FakeClient()
                tg = FakeTelegram(client)
                person = UserEntity(501, "Private dialog")

                async def resolve(_client, _query):
                    return person, None

                tg.resolve_or_result = resolve
                with patch.object(
                    participants,
                    "_scan_participants",
                    side_effect=AssertionError("private dialogs must not be scanned"),
                ) as scan:
                    code = await participants.command_participants(
                        tg,
                        "501",
                        max_participants=10,
                        output_format="json",
                        output_dir=str(output_dir),
                    )

                payload, exit_code = tg.output[-1]
                self.assertEqual(code, 2)
                self.assertEqual(exit_code, 2)
                self.assertEqual(payload["status"], "unsupported_chat_type")
                self.assertFalse(output_dir.exists())
                scan.assert_not_awaited()
                self.assertNotIn("iter_participants", client.calls)

        asyncio.run(run())

    # Added by /ship Step 7 coverage audit (2026-11-18).
    # Value: protects=dialog and roster records preserve distinct Telegram roles; fails_when=admin, inferred member, restricted or left roles collapse to the wrong exported label; why_new=existing export tests check owner, private dialog and ordinary member only; seam=none; no_claim=Telethon role objects beyond the synthetic variants
    def test_role_metadata_distinguishes_admin_member_and_restricted_states(self):
        tg = FakeTelegram(FakeClient())
        admin = Channel(110, "Admin room", owned=False, participants_count=4)
        admin.admin_rights = object()
        admin_record = participants._dialog_record(tg, dialog(admin))
        member = Channel(111, "Member room", owned=False, participants_count=4)
        member_record = participants._dialog_record(tg, dialog(member))

        self.assertEqual(admin_record["account_role"], "admin")
        self.assertTrue(admin_record["is_managed"])
        self.assertEqual(member_record["account_role"], "member_or_subscriber")
        self.assertFalse(member_record["is_managed"])

        for type_name, expected in (
            ("ChannelParticipantCreator", "creator"),
            ("ChannelParticipantAdmin", "admin"),
            ("ChannelParticipantBanned", "restricted_or_removed"),
            ("ChannelParticipantLeft", "left"),
        ):
            with self.subTest(type_name=type_name):
                participant_user = user(900, "Role user")
                participant_user.participant = type(type_name, (), {})()
                self.assertEqual(participants._participant_role(participant_user), expected)
        self.assertEqual(
            participants._participant_role(SimpleNamespace(id=901, participant=None)),
            "unknown",
        )

    def test_missing_count_and_api_error_are_reported_as_incomplete(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                entity = Channel(100, "Unknown count", owned=False, participants_count=None)
                client = FakeClient(participants={100: [user(1, "One")]})
                state = await participants._scan_participants(
                    FakeTelegram(client),
                    client,
                    entity,
                    {"id": 100, "title": "Unknown count", "type": "channel"},
                    max_new=10,
                    output_format="json",
                    root=Path(temp),
                )
                self.assertEqual(state["status"], "unknown")
                self.assertIn("did not expose", state["limitation_reason"])

                class FailingClient(FakeClient):
                    def iter_participants(self, entity, limit=None):
                        async def generate():
                            raise RuntimeError("restricted")
                            yield None

                        return generate()

                failed = await participants._scan_participants(
                    FakeTelegram(FailingClient()),
                    FailingClient(),
                    entity,
                    {"id": 101, "title": "Restricted", "type": "channel"},
                    max_new=10,
                    output_format="json",
                    root=Path(temp),
                )
                self.assertEqual(failed["status"], "partial")
                self.assertEqual(failed["error"]["code"], "RuntimeError")

        asyncio.run(run())

    def test_coverage_is_based_on_ids_observed_in_the_current_scan(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                participant_file = root / "participants" / "chat_100.jsonl"
                participant_file.parent.mkdir()
                participant_file.write_text(
                    "".join(json.dumps({"user_id": i, "chat_id": 100}) + "\n" for i in (1, 2, 3)),
                    encoding="utf-8",
                )
                entity = Channel(100, "Room", owned=True, participants_count=3)
                client = FakeClient(participants={100: [user(1, "One"), user(2, "Two")]})
                state = await participants._scan_participants(
                    FakeTelegram(client),
                    client,
                    entity,
                    {"id": 100, "title": "Room", "type": "channel"},
                    max_new=10,
                    output_format="json",
                    root=root,
                )
                self.assertEqual(state["status"], "partial")
                self.assertFalse(state["complete_vs_reported_count"])
                self.assertEqual(state["unique_participants_saved"], 3)
                self.assertEqual(state["resume_replayed_existing"], 2)

        asyncio.run(run())

    def test_interrupted_jsonl_tail_is_repaired_before_resuming(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                participant_file = root / "participants" / "chat_100.jsonl"
                participant_file.parent.mkdir()
                participant_file.write_text(
                    '{"user_id":1,"chat_id":100}\n{"user_id":2,"first_name":"partial',
                    encoding="utf-8",
                )
                entity = Channel(100, "Room", owned=True, participants_count=2)
                client = FakeClient(participants={100: [user(1, "One"), user(2, "Two")]})
                state = await participants._scan_participants(
                    FakeTelegram(client),
                    client,
                    entity,
                    {"id": 100, "title": "Room", "type": "channel"},
                    max_new=10,
                    output_format="json",
                    root=root,
                )
                rows = [
                    json.loads(line)
                    for line in Path(state["participants_path"]).read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual([row["user_id"] for row in rows], [1, 2])
                self.assertEqual(state["participants_added_this_run"], 1)
                self.assertEqual(state["resume_replayed_existing"], 1)

        asyncio.run(run())

    def test_terminated_malformed_jsonl_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "participants.jsonl"
            path.write_text('{"user_id":1}\n{"user_id":\n', encoding="utf-8")
            with self.assertRaises(OSError):
                list(participants._iter_jsonl(FakeTelegram(FakeClient()), path))

    def test_participant_checkpoint_is_written_once_off_the_event_loop(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = FakeClient(participants={100: [user(1, "One"), user(2, "Two")]})
                tg = FakeTelegram(client)
                caller_thread = threading.get_ident()
                writer_threads: list[int] = []
                append_batch = participants._append_jsonl_batch

                def capture_writer(*args, **kwargs):
                    writer_threads.append(threading.get_ident())
                    return append_batch(*args, **kwargs)

                with patch.object(participants, "_append_jsonl_batch", side_effect=capture_writer) as mocked:
                    state = await participants._scan_participants(
                        tg,
                        client,
                        Channel(100, "Room", owned=True, participants_count=2),
                        {"id": 100, "title": "Room", "type": "channel"},
                        max_new=10,
                        output_format="json",
                        root=Path(temp),
                    )
                self.assertEqual(state["participants_added_this_run"], 2)
                self.assertEqual(mocked.call_count, 1)
                self.assertEqual(len(writer_threads), 1)
                self.assertNotEqual(writer_threads[0], caller_thread)

        asyncio.run(run())

    def test_csv_formula_cells_are_escaped(self):
        with tempfile.TemporaryDirectory() as temp:
            values = ["=1+1", "  =1+1", "\t=1+1", "\x00=1+1", "ordinary"]
            path = Path(temp) / "safe.csv"
            participants._write_csv(
                FakeTelegram(FakeClient()), path, [{"value": value} for value in values], ("value",)
            )
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual([row["value"] for row in rows], ["'" + value for value in values[:3]] + ["\\x00=1+1", "ordinary"])

    def test_participant_module_never_mutates_telegram_state(self):
        tree = ast.parse(Path(participants.__file__).read_text(encoding="utf-8"))
        forbidden = {
            "send_message",
            "send_read_acknowledge",
            "edit_message",
            "delete_messages",
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
