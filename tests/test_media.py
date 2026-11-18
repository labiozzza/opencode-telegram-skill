from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acl_helpers import assert_owner_only_acl
from scripts import media, telegram


class DocumentAttributeFilename:
    def __init__(self, file_name: str):
        self.file_name = file_name


class DocumentAttributeVideo:
    round_message = False


class DocumentAttributeAudio:
    voice = True


def document_message(
    message_id: int,
    *,
    content_size: int | None = 5,
    filename: str = "report.pdf",
    mime_type: str = "application/pdf",
    date: datetime | None = None,
    topic_id: int | None = None,
    grouped_id: int | None = None,
) -> SimpleNamespace:
    document = SimpleNamespace(
        attributes=[DocumentAttributeFilename(filename)],
        mime_type=mime_type,
        size=content_size,
    )
    return SimpleNamespace(
        id=message_id,
        date=date or datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        media=SimpleNamespace(document=document, photo=None),
        file=SimpleNamespace(size=content_size),
        sender_id=77,
        sender=SimpleNamespace(first_name="Test", last_name="Sender", username="tester"),
        reply_to=SimpleNamespace(reply_to_top_id=topic_id) if topic_id else None,
        grouped_id=grouped_id,
        action=None,
    )


class FakeMediaClient:
    def __init__(self, content: bytes = b"hello", *, progress: int | None = None):
        self.content = content
        self.progress = progress
        self.calls: list[str] = []
        self.messages: dict[int, object] = {}
        self.history: list[object] = []

    async def get_messages(self, entity, *, ids):
        self.calls.append("get_messages")
        return self.messages.get(ids)

    async def iter_messages(self, entity, **kwargs):
        self.calls.append("iter_messages")
        self.iter_kwargs = kwargs
        for message in self.history:
            yield message

    async def download_media(self, message, *, file, progress_callback):
        self.calls.append("download_media")
        amount = len(self.content) if self.progress is None else self.progress
        progress_callback(amount, len(self.content))
        Path(file).write_bytes(self.content)
        return file

    async def disconnect(self):
        self.calls.append("disconnect")


class MediaStorageTests(unittest.TestCase):
    def test_photo_video_voice_and_image_document_types_are_classified(self):
        photo = SimpleNamespace(
            media=SimpleNamespace(photo=object(), document=None),
            file=SimpleNamespace(size=123),
        )
        self.assertEqual(media.attachment_descriptor(photo)["attachment_type"], "photo")
        self.assertEqual(media.attachment_descriptor(photo)["expected_size_bytes"], 123)

        video = document_message(90, filename="movie.mp4", mime_type="application/octet-stream")
        video.media.document.attributes.append(DocumentAttributeVideo())
        self.assertEqual(media.attachment_descriptor(video)["attachment_type"], "video")

        voice = document_message(91, filename="voice.ogg", mime_type="audio/ogg")
        voice.media.document.attributes.append(DocumentAttributeAudio())
        self.assertEqual(media.attachment_descriptor(voice)["attachment_type"], "voice")

        image = document_message(92, filename="scan.png", mime_type="image/png")
        self.assertEqual(media.attachment_descriptor(image)["attachment_type"], "image")

    def test_dns_resolution_errors_are_reported_as_network_errors(self):
        error = socket.gaierror(socket.EAI_AGAIN, "temporary name resolution failure")
        self.assertEqual(telegram.safe_error(error)["code"], "network_error")
        self.assertEqual(media.media_error(error, telegram.safe_error)["code"], "network_error")

    def test_asyncio_timeout_errors_are_reported_as_network_errors(self):
        error = asyncio.TimeoutError("synthetic request timeout")
        self.assertEqual(telegram.safe_error(error)["code"], "network_error")
        self.assertEqual(media.media_error(error, telegram.safe_error)["code"], "network_error")

    def test_network_unreachable_os_errors_are_not_storage_failures(self):
        for error_number in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            with self.subTest(error_number=error_number):
                error = OSError(error_number, "synthetic network unreachable")
                self.assertEqual(telegram.safe_error(error)["code"], "network_error")
                self.assertEqual(
                    media.media_error(error, telegram.safe_error)["code"],
                    "network_error",
                )

    def test_download_writes_sha_manifest_without_trusting_original_filename(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "private-media"
                store = media.MediaStore(root, -100123)
                message = document_message(101, content_size=6, filename=r"..\..\outside.exe")
                client = FakeMediaClient(b"sample")
                record, byte_count = await media.download_attachment(
                    client,
                    message,
                    store=store,
                    chat_id=-100123,
                    chat_title="Selected group",
                    max_file_bytes=100,
                    remaining_total_bytes=100,
                    allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(record["status"], "downloaded")
                self.assertEqual(byte_count, 6)
                path = Path(record["path"])
                self.assertTrue(path.is_relative_to(root.resolve()))
                self.assertEqual(path.name, "101.bin")
                self.assertEqual(path.read_bytes(), b"sample")
                self.assertEqual(record["sha256"], hashlib.sha256(b"sample").hexdigest())
                self.assertEqual(record["original_file_name"], r"..\..\outside.exe")
                self.assertEqual(record["forum_topic_id"], None)
                manifest = [json.loads(line) for line in store.manifest_path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(manifest), 1)
                self.assertEqual(manifest[0]["source_id"], "-100123:101:0")
                self.assertEqual(manifest[0]["relative_path"], path.relative_to(root).as_posix())

        asyncio.run(run())

    # Regression: ISSUE-003 — Telegram-controlled executable suffixes must not survive publication.
    # Found by /qa on 2026-11-18
    # Report: .gstack/tmp/qa-reports/run-20261117T192404Z/qa-report-telegram-research-2026-11-18.md
    def test_telegram_executable_suffixes_are_not_published_as_executable_files(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                store = media.MediaStore(Path(temp) / "media", 42)
                client = FakeMediaClient(b"synthetic payload")
                for message_id, filename in enumerate(
                    ("payload.exe", "payload.lnk", "payload.bat", "payload.cmd", "payload.ps1"),
                    start=301,
                ):
                    record, _ = await media.download_attachment(
                        client,
                        document_message(
                            message_id,
                            content_size=len(b"synthetic payload"),
                            filename=filename,
                            mime_type="application/x-msdownload",
                        ),
                        store=store,
                        chat_id=42,
                        chat_title="Synthetic group",
                        max_file_bytes=100,
                        remaining_total_bytes=1000,
                        allowed_types=None,
                        safe_error=telegram.safe_error,
                    )
                    self.assertEqual(record["original_file_name"], filename)
                    self.assertEqual(Path(record["path"]).suffix.casefold(), ".bin")

        asyncio.run(run())

    # Regression: ISSUE-004 — reusing media must tighten stale ACLs on the owned tree and file.
    # Found by /qa on 2026-11-18
    # Report: .gstack/tmp/qa-reports/run-20261117T192404Z/qa-report-telegram-research-2026-11-18.md
    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    def test_existing_download_and_media_tree_are_hardened_before_reuse(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "media"
                store = media.MediaStore(
                    root,
                    42,
                    secure_path=telegram._set_private_permissions,
                )
                message = document_message(
                    401,
                    content_size=len(b"synthetic"),
                    filename="report.pdf",
                )
                client = FakeMediaClient(b"synthetic")
                downloaded, _ = await media.download_attachment(
                    client,
                    message,
                    store=store,
                    chat_id=42,
                    chat_title="Synthetic group",
                    max_file_bytes=100,
                    remaining_total_bytes=100,
                    allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                path = Path(downloaded["path"])
                original_dirs = [
                    root / "telegram",
                    store.chat_root,
                    path.parent,
                ]
                for directory in original_dirs:
                    subprocess.run(
                        ["icacls", str(directory), "/grant", "*S-1-1-0:(OI)(CI)F"],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                subprocess.run(
                    ["icacls", str(path), "/grant", "*S-1-1-0:F"],
                    check=True,
                    capture_output=True,
                    text=True,
                )

                reopened = media.MediaStore(
                    root,
                    42,
                    secure_path=telegram._set_private_permissions,
                )
                reopened.originals_dir(message)
                reused = await reopened.existing_download_async(downloaded["source_id"])

                self.assertIsNotNone(reused)
                for directory in original_dirs:
                    assert_owner_only_acl(self, directory, directory=True)
                assert_owner_only_acl(self, path)

        asyncio.run(run())

    # Regression: ISSUE-005 — failed staging cleanup must not leave a published orphan.
    # Found by /qa on 2026-11-18
    # Report: .gstack/tmp/qa-reports/run-20261117T192404Z/qa-report-telegram-research-2026-11-18.md
    def test_commit_temp_removes_published_target_when_staging_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            store = media.MediaStore(Path(temp) / "media", 42)
            directory = store.originals_dir(document_message(501))
            staging = directory / ".staging"
            staging.write_bytes(b"synthetic")
            target = directory / "501.pdf"
            original_unlink = Path.unlink

            def fail_staging_unlink(path, *args, **kwargs):
                if path == staging:
                    raise OSError("synthetic staging cleanup failure")
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=fail_staging_unlink):
                with self.assertRaises((OSError, media.MediaStorageError)):
                    store.commit_temp(staging, directory, 501, 0, ".pdf")

            self.assertFalse(target.exists())

    def test_never_overwrites_existing_file_and_deduplicates_same_source(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                store = media.MediaStore(Path(temp) / "media", 42)
                message = document_message(102, content_size=11, filename="same.bin")
                directory = store.originals_dir(message)
                original = directory / "102.bin"
                original.write_bytes(b"keep me")
                client = FakeMediaClient(b"new content")

                first, _ = await media.download_attachment(
                    client, message, store=store, chat_id=42, chat_title="A",
                    max_file_bytes=100, remaining_total_bytes=100, allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(original.read_bytes(), b"keep me")
                self.assertEqual(Path(first["path"]).name, "102-1.bin")

                second, transferred = await media.download_attachment(
                    client, message, store=store, chat_id=42, chat_title="A",
                    max_file_bytes=100, remaining_total_bytes=100, allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(second["status"], "already_present")
                self.assertEqual(transferred, 0)
                self.assertEqual(client.calls.count("download_media"), 1)

        asyncio.run(run())

    def test_content_hash_deduplicates_reposted_attachment(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                store = media.MediaStore(Path(temp) / "media", 42)
                client = FakeMediaClient(b"identical")
                first, _ = await media.download_attachment(
                    client, document_message(201, content_size=9), store=store, chat_id=42, chat_title="A",
                    max_file_bytes=100, remaining_total_bytes=100, allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                second, _ = await media.download_attachment(
                    client, document_message(202, content_size=9), store=store, chat_id=42, chat_title="A",
                    max_file_bytes=100, remaining_total_bytes=100, allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(second["status"], "downloaded")
                self.assertEqual(second["relative_path"], first["relative_path"])
                self.assertEqual(second["deduplicated_from_source_id"], first["source_id"])
                self.assertEqual(len(list(Path(temp).rglob("*.pdf"))), 1)

        asyncio.run(run())

    def test_skip_does_not_replace_a_successful_source_manifest_record(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                store = media.MediaStore(Path(temp) / "media", 42)
                message = document_message(203, content_size=5)
                client = FakeMediaClient(b"hello")
                first, transferred = await media.download_attachment(
                    client, message, store=store, chat_id=42, chat_title="A",
                    max_file_bytes=100, remaining_total_bytes=100, allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(first["status"], "downloaded")
                self.assertEqual(transferred, 5)

                filtered, filtered_bytes = await media.download_attachment(
                    client, message, store=store, chat_id=42, chat_title="A",
                    max_file_bytes=100, remaining_total_bytes=100, allowed_types={"photo"},
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(filtered["status"], "skipped")
                self.assertEqual(filtered["reason"], "type_filter")
                self.assertEqual(filtered_bytes, 0)
                self.assertEqual(store.existing_download(first["source_id"]), first)

                with patch.object(media.asyncio, "to_thread", wraps=asyncio.to_thread) as to_thread:
                    capped = await media.skipped_attachment(
                        message, store=store, chat_id=42, chat_title="A", reason="max_files_limit_exceeded",
                    )
                    retry, retry_bytes = await media.download_attachment(
                        client, message, store=store, chat_id=42, chat_title="A",
                        max_file_bytes=100, remaining_total_bytes=100, allowed_types=None,
                        safe_error=telegram.safe_error,
                    )
                self.assertGreaterEqual(to_thread.call_count, 2)
                self.assertEqual(capped["status"], "skipped")
                self.assertEqual(store.existing_download(first["source_id"]), first)

                self.assertEqual(retry["status"], "already_present")
                self.assertEqual(retry_bytes, 0)
                self.assertEqual(client.calls.count("download_media"), 1)
                self.assertEqual(len(list(Path(temp).rglob("*.pdf"))), 1)
                manifest = [
                    json.loads(line)
                    for line in store.manifest_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual([row["status"] for row in manifest], ["downloaded"])

        asyncio.run(run())

    def test_known_and_streamed_size_limits_are_reported_as_skipped(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                store = media.MediaStore(Path(temp) / "media", 42)
                oversized = document_message(301, content_size=11)
                client = FakeMediaClient(b"01234567890")
                result, _ = await media.download_attachment(
                    client, oversized, store=store, chat_id=42, chat_title="A",
                    max_file_bytes=10, remaining_total_bytes=100, allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["reason"], "file_size_limit_exceeded")
                self.assertNotIn("download_media", client.calls)

                unknown_size = document_message(302, content_size=None)
                streamed = FakeMediaClient(b"01234567890")
                result, _ = await media.download_attachment(
                    streamed, unknown_size, store=store, chat_id=42, chat_title="A",
                    max_file_bytes=10, remaining_total_bytes=100, allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["reason"], "download_size_limit_exceeded")
                self.assertEqual(list(Path(temp).rglob("*.part")), [])
                manifest = [json.loads(line) for line in store.manifest_path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual([row["status"] for row in manifest], ["skipped", "skipped"])

        asyncio.run(run())

    def test_total_size_budget_is_reported_as_skipped(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                store = media.MediaStore(Path(temp) / "media", 42)
                result, _ = await media.download_attachment(
                    FakeMediaClient(b"hello"),
                    document_message(303, content_size=5),
                    store=store,
                    chat_id=42,
                    chat_title="A",
                    max_file_bytes=100,
                    remaining_total_bytes=4,
                    allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["reason"], "total_size_limit_exceeded")

        asyncio.run(run())

    def test_interruption_removes_staging_file_and_allows_resume(self):
        class InterruptOnce(FakeMediaClient):
            def __init__(self):
                super().__init__(b"resumable")
                self.interrupt = True

            async def download_media(self, message, *, file, progress_callback):
                if self.interrupt:
                    self.interrupt = False
                    raise asyncio.CancelledError
                return await super().download_media(message, file=file, progress_callback=progress_callback)

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "media"
                store = media.MediaStore(root, 42)
                message = document_message(304, content_size=9)
                client = InterruptOnce()
                with self.assertRaises(asyncio.CancelledError):
                    await media.download_attachment(
                        client, message, store=store, chat_id=42, chat_title="A",
                        max_file_bytes=100, remaining_total_bytes=100, allowed_types=None,
                        safe_error=telegram.safe_error,
                    )
                self.assertEqual(list(root.rglob("*.part")), [])
                self.assertEqual(list(root.rglob("*.pdf")), [])
                result, _ = await media.download_attachment(
                    client, message, store=store, chat_id=42, chat_title="A",
                    max_file_bytes=100, remaining_total_bytes=100, allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(Path(result["path"]).read_bytes(), b"resumable")

        asyncio.run(run())

    def test_size_mismatch_does_not_publish_original(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                store = media.MediaStore(Path(temp) / "media", 42)
                result, _ = await media.download_attachment(
                    FakeMediaClient(b"short"),
                    document_message(401, content_size=20),
                    store=store,
                    chat_id=42,
                    chat_title="A",
                    max_file_bytes=100,
                    remaining_total_bytes=100,
                    allowed_types=None,
                    safe_error=telegram.safe_error,
                )
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "download_size_mismatch")
                self.assertEqual(list(Path(temp).rglob("*.pdf")), [])
                self.assertEqual(list(Path(temp).rglob("*.part")), [])

        asyncio.run(run())

    def test_forum_topics_have_independent_originals_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            store = media.MediaStore(Path(temp) / "media", 42)
            first = store.originals_dir(document_message(501, topic_id=10))
            second = store.originals_dir(document_message(502, topic_id=20))
            self.assertIn("topic_10", first.parts)
            self.assertIn("topic_20", second.parts)
            self.assertNotEqual(first, second)
            self.assertIsNone(media.forum_topic_id(document_message(503)))
            general = document_message(504)
            general.reply_to = SimpleNamespace(forum_topic=True, reply_to_top_id=None)
            self.assertEqual(media.forum_topic_id(general), "general")

    def test_manifest_repairs_only_an_incomplete_trailing_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "media"
            store = media.MediaStore(root, 42)
            store.manifest_path.write_bytes(b'{"source_id":"42:1:0","status":"skipped"}\n{"source_id":"42:2')
            resumed = media.MediaStore(root, 42)
            self.assertEqual(
                resumed.manifest_path.read_bytes(),
                b'{"source_id":"42:1:0","status":"skipped"}\n',
            )

    def test_existing_manifest_acl_is_resecured_before_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "media"
            initial = media.MediaStore(root, 42)
            initial.manifest_path.write_text(
                '{"source_id":"42:1:0","status":"skipped"}\n',
                encoding="utf-8",
            )
            secured: list[tuple[Path, int, bool]] = []

            def secure_path(path, mode, *, directory=False):
                secured.append((Path(path), mode, directory))

            resumed = media.MediaStore(root, 42, secure_path=secure_path)
            self.assertEqual(resumed._latest["42:1:0"]["status"], "skipped")
            self.assertIn((resumed.manifest_path, 0o600, False), secured)

    def test_symlink_output_component_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "media"
            outside = Path(temp) / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "telegram").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is not permitted in this environment.")
            with self.assertRaises(media.MediaStorageError):
                media.MediaStore(root, 42)

    def test_hardlinked_manifest_is_rejected_without_mutating_external_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "media"
            chat_root = root / "telegram" / "chat_42"
            chat_root.mkdir(parents=True)
            external = Path(temp) / "external.jsonl"
            external.write_text("keep unchanged\n", encoding="utf-8")
            try:
                os.link(external, chat_root / "manifest.jsonl")
            except OSError:
                self.skipTest("Hard links are not supported in this environment.")
            with self.assertRaises(media.MediaStorageError):
                media.MediaStore(root, 42)
            self.assertEqual(external.read_text(encoding="utf-8"), "keep unchanged\n")


class MediaCommandTests(unittest.TestCase):
    def test_default_media_store_does_not_touch_existing_message_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp) / "skill-data"
            with (
                patch.object(telegram, "data_dir", return_value=data_root),
                patch.object(telegram, "ensure_data_dir", side_effect=AssertionError("must not touch exports")),
                patch.object(telegram, "_set_private_permissions"),
            ):
                store = telegram._media_store(None, 42)
            self.assertEqual(store.root, (data_root / "media").resolve())
            self.assertEqual(store.manifest_path.name, "manifest.jsonl")

    def test_media_client_preparation_does_not_rewrite_exports_acl(self):
        fake_client = SimpleNamespace()
        with (
            patch.object(telegram, "ensure_session_data_dir") as ensure_root,
            patch.object(telegram, "ensure_data_dir", side_effect=AssertionError("must preserve exports")),
            patch.object(telegram, "TelegramClient", return_value=fake_client),
            patch.object(telegram, "session_base", return_value=Path("account")),
            patch.object(telegram, "session_path", return_value=Path("account.session")),
        ):
            result = telegram.client_from_credentials({"api_id": 1, "api_hash": "test"}, preserve_exports=True)
        self.assertIs(result, fake_client)
        ensure_root.assert_called_once_with()

    def test_media_cli_requires_explicit_chat_and_accepts_bounded_options(self):
        parser = telegram.build_parser()
        download = parser.parse_args([
            "media", "download", "--chat", "-100123", "--message-id", "55",
            "--type", "photo", "--type", "video", "--max-file-bytes", "2048",
        ])
        self.assertEqual(download.command, "media")
        self.assertEqual(download.media_command, "download")
        self.assertEqual(download.chat, "-100123")
        self.assertEqual(download.type, ["photo", "video"])
        export = parser.parse_args([
            "media", "export", "--chat", "@selected", "--since", "2026-01-01",
            "--until", "2026-01-31", "--max-files", "3",
        ])
        self.assertEqual(export.chat, "@selected")
        self.assertEqual(export.max_files, 3)
        with self.assertRaises(SystemExit):
            parser.parse_args(["media", "download", "--message-id", "1"])

    def test_invalid_limits_are_rejected_before_authorization(self):
        async def run():
            args = telegram.build_parser().parse_args([
                "media", "export", "--chat", "42", "--since", "2026-01-01",
                "--until", "2026-01-31", "--max-files", "0",
            ])
            with patch.object(telegram, "authorized_client", side_effect=AssertionError("must not connect")):
                output = StringIO()
                with redirect_stdout(output):
                    code = await telegram.dispatch(args)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_limit")

        asyncio.run(run())

    def test_download_command_only_reads_the_selected_message_and_downloads_media(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = FakeMediaClient(b"payload")
                message = document_message(601, content_size=7)
                client.messages[601] = message
                entity = object()
                store = media.MediaStore(Path(temp) / "media", 42)

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, query):
                    self.assertEqual(query, "42")
                    return entity, None

                output = StringIO()
                with (
                    patch.object(telegram, "authorized_client", authorized),
                    patch.object(telegram, "resolve_or_result", resolve),
                    patch.object(telegram, "entity_record", return_value={"id": 42, "title": "Only selected chat"}),
                    patch.object(telegram, "_media_store", return_value=store),
                    patch.object(telegram, "secure_auth_files"),
                    redirect_stdout(output),
                ):
                    code = await telegram.command_media_download(
                        "42", message_id=601, output_dir=None, allowed_types=None,
                        max_file_bytes=100, max_total_bytes=100,
                    )
                result = json.loads(output.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(client.calls, ["get_messages", "download_media", "disconnect"])
                self.assertFalse({"send_message", "send_file", "send_read_acknowledge", "join_channel"} & set(client.calls))

        asyncio.run(run())

    # Added by /ship Step 7 coverage audit (2026-11-18).
    # Value: protects=missing single-message requests terminate without downloading; fails_when=a missing result reaches download_media or reports success; why_new=the existing command test covers only a present attachment; seam=none; no_claim=Telegram API behavior
    def test_download_command_reports_missing_message_without_downloading(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = FakeMediaClient()

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, _query):
                    return object(), None

                output = StringIO()
                with (
                    patch.object(telegram, "authorized_client", authorized),
                    patch.object(telegram, "resolve_or_result", resolve),
                    patch.object(
                        telegram,
                        "entity_record",
                        return_value={"id": 42, "title": "Selected chat"},
                    ),
                    patch.object(
                        telegram,
                        "_media_store",
                        side_effect=AssertionError("a missing message must not create storage"),
                    ),
                    patch.object(telegram, "secure_auth_files"),
                    redirect_stdout(output),
                ):
                    code = await telegram.command_media_download(
                        "42",
                        message_id=999,
                        output_dir=temp,
                        allowed_types=None,
                        max_file_bytes=100,
                        max_total_bytes=100,
                    )

                result = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "message_not_found")
                self.assertEqual((result["chat_id"], result["message_id"]), (42, 999))
                self.assertEqual(client.calls, ["get_messages", "disconnect"])

        asyncio.run(run())

    def test_media_export_respects_date_chat_and_file_caps(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = FakeMediaClient(b"hello")
                client.history = [
                    document_message(701, content_size=5, date=datetime(2026, 2, 1, tzinfo=timezone.utc)),
                    document_message(702, content_size=5, date=datetime(2026, 1, 20, tzinfo=timezone.utc)),
                    document_message(703, content_size=5, date=datetime(2026, 1, 19, tzinfo=timezone.utc)),
                    document_message(704, content_size=5, date=datetime(2025, 12, 31, tzinfo=timezone.utc)),
                ]
                store = media.MediaStore(Path(temp) / "media", 42)

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, query):
                    self.assertEqual(query, "chosen group")
                    return object(), None

                output = StringIO()
                with (
                    patch.object(telegram, "authorized_client", authorized),
                    patch.object(telegram, "resolve_or_result", resolve),
                    patch.object(telegram, "entity_record", return_value={"id": 42, "title": "Chosen group"}),
                    patch.object(telegram, "_media_store", return_value=store),
                    patch.object(telegram, "secure_auth_files"),
                    redirect_stdout(output),
                ):
                    code = await telegram.command_media_export(
                        "chosen group",
                        since_arg="2026-01-01",
                        until_arg="2026-01-31",
                        output_dir=None,
                        max_files=1,
                        max_messages=10,
                        max_file_bytes=100,
                        max_total_bytes=100,
                        allowed_types=None,
                    )
                result = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["chat_id"], 42)
                self.assertEqual(result["counts"]["downloaded"], 1)
                self.assertEqual(result["counts"]["skipped"], 1)
                self.assertEqual(result["results"][1]["reason"], "max_files_limit_exceeded")
                self.assertEqual(result["max_files_reached"], True)
                self.assertEqual(client.calls.count("download_media"), 1)
                self.assertEqual(client.iter_kwargs["limit"], 11)
                self.assertTrue(all(row["message_id"] in {702, 703} for row in result["results"]))

        asyncio.run(run())

    def test_media_export_charges_aborted_transfer_to_total_budget(self):
        class PartialOversizeClient(FakeMediaClient):
            def __init__(self):
                super().__init__(b"")
                self.history = [
                    document_message(801, content_size=None),
                    document_message(802, content_size=None),
                ]
                self.attempted_message_ids: list[int] = []
                self.transferred_bytes = 0

            async def download_media(self, message, *, file, progress_callback):
                self.attempted_message_ids.append(message.id)
                if message.id == 801:
                    data = b"123456"
                else:
                    data = b"12345"
                Path(file).write_bytes(data)
                self.transferred_bytes += len(data)
                progress_callback(len(data), None)
                return file

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = PartialOversizeClient()
                store = media.MediaStore(Path(temp) / "media", 42)
                output = StringIO()

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, query):
                    self.assertEqual(query, "42")
                    return object(), None

                with (
                    patch.object(telegram, "authorized_client", authorized),
                    patch.object(telegram, "resolve_or_result", resolve),
                    patch.object(telegram, "entity_record", return_value={"id": 42, "title": "A"}),
                    patch.object(telegram, "_media_store", return_value=store),
                    patch.object(telegram, "close_client"),
                    redirect_stdout(output),
                ):
                    code = await telegram.command_media_export(
                        "42", since_arg="2026-01-01", until_arg="2026-01-31",
                        output_dir=None, max_files=10, max_messages=10,
                        max_file_bytes=100, max_total_bytes=5, allowed_types=None,
                    )

                result = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(client.attempted_message_ids, [801])
                self.assertEqual(client.transferred_bytes, 6)
                self.assertEqual(result["bytes_downloaded"], 6)
                self.assertEqual(
                    [item["reason"] for item in result["results"]],
                    ["download_size_limit_exceeded", "total_size_limit_exceeded"],
                )

        asyncio.run(run())

    def test_media_export_charges_partial_network_transfer_to_total_budget(self):
        class InterruptedTransferClient(FakeMediaClient):
            def __init__(self):
                super().__init__(b"")
                self.history = [
                    document_message(811, content_size=None),
                    document_message(812, content_size=None),
                ]
                self.attempted_message_ids: list[int] = []
                self.transferred_bytes = 0

            async def download_media(self, message, *, file, progress_callback):
                self.attempted_message_ids.append(message.id)
                if message.id == 811:
                    data = b"12345"
                    Path(file).write_bytes(data)
                    self.transferred_bytes += len(data)
                    progress_callback(len(data), None)
                    raise ConnectionError("synthetic transfer interruption")
                data = b"12345"
                Path(file).write_bytes(data)
                self.transferred_bytes += len(data)
                progress_callback(len(data), None)
                return file

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = InterruptedTransferClient()
                store = media.MediaStore(Path(temp) / "media", 42)
                output = StringIO()

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, query):
                    self.assertEqual(query, "42")
                    return object(), None

                with (
                    patch.object(telegram, "authorized_client", authorized),
                    patch.object(telegram, "resolve_or_result", resolve),
                    patch.object(telegram, "entity_record", return_value={"id": 42, "title": "A"}),
                    patch.object(telegram, "_media_store", return_value=store),
                    patch.object(telegram, "close_client"),
                    redirect_stdout(output),
                ):
                    code = await telegram.command_media_export(
                        "42", since_arg="2026-01-01", until_arg="2026-01-31",
                        output_dir=None, max_files=10, max_messages=10,
                        max_file_bytes=100, max_total_bytes=5, allowed_types=None,
                    )

                result = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(client.attempted_message_ids, [811])
                self.assertEqual(client.transferred_bytes, 5)
                self.assertEqual(result["bytes_downloaded"], 5)
                self.assertEqual(result["results"][0]["status"], "error")
                self.assertEqual(result["results"][0]["error"]["code"], "network_error")
                self.assertEqual(result["results"][1]["reason"], "total_size_limit_exceeded")

        asyncio.run(run())

    def test_media_export_reports_progress_when_message_iteration_fails(self):
        class FailingIteratorClient(FakeMediaClient):
            async def iter_messages(self, entity, **kwargs):
                self.calls.append("iter_messages")
                self.iter_kwargs = kwargs
                yield self.history[0]
                raise ConnectionError("synthetic network interruption")

        async def run():
            with tempfile.TemporaryDirectory() as temp:
                client = FailingIteratorClient(b"hello")
                client.history = [document_message(901, content_size=5)]
                store = media.MediaStore(Path(temp) / "media", 42)
                output = StringIO()

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, query):
                    self.assertEqual(query, "42")
                    return object(), None

                with (
                    patch.object(telegram, "authorized_client", authorized),
                    patch.object(telegram, "resolve_or_result", resolve),
                    patch.object(telegram, "entity_record", return_value={"id": 42, "title": "A"}),
                    patch.object(telegram, "_media_store", return_value=store),
                    patch.object(telegram, "close_client"),
                    redirect_stdout(output),
                ):
                    code = await telegram.command_media_export(
                        "42", since_arg="2026-01-01", until_arg="2026-01-31",
                        output_dir=None, max_files=10, max_messages=10,
                        max_file_bytes=100, max_total_bytes=100, allowed_types=None,
                    )

                result = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["error"]["code"], "network_error")
                self.assertIn("retry", result["error"]["message"])
                self.assertEqual(result["counts"]["downloaded"], 1)
                self.assertEqual(result["bytes_downloaded"], 5)
                self.assertEqual([item["message_id"] for item in result["results"]], [901])
                self.assertTrue(Path(result["results"][0]["path"]).is_file())

        asyncio.run(run())

    def test_media_export_reports_message_cap_only_when_more_in_range_messages_exist(self):
        async def run_case(history, expected_cap):
            with tempfile.TemporaryDirectory() as temp:
                client = FakeMediaClient(b"hello")
                client.history = history
                store = media.MediaStore(Path(temp) / "media", 42)
                output = StringIO()

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, query):
                    self.assertEqual(query, "42")
                    return object(), None

                with (
                    patch.object(telegram, "authorized_client", authorized),
                    patch.object(telegram, "resolve_or_result", resolve),
                    patch.object(telegram, "entity_record", return_value={"id": 42, "title": "A"}),
                    patch.object(telegram, "_media_store", return_value=store),
                    patch.object(telegram, "close_client"),
                    redirect_stdout(output),
                ):
                    code = await telegram.command_media_export(
                        "42", since_arg="2026-01-01", until_arg="2026-01-31",
                        output_dir=None, max_files=10, max_messages=1,
                        max_file_bytes=100, max_total_bytes=100, allowed_types=None,
                    )
                return code, json.loads(output.getvalue()), client

        async def run():
            only_message = document_message(821, content_size=5)
            code, result, client = await run_case([only_message], False)
            self.assertEqual(code, 0)
            self.assertFalse(result["max_messages_reached"])
            self.assertEqual(result["messages_scanned"], 1)
            self.assertEqual(client.iter_kwargs["limit"], 2)

            extra_in_range = document_message(822, content_size=5)
            code, result, _ = await run_case([only_message, extra_in_range], True)
            self.assertEqual(code, 1)
            self.assertTrue(result["max_messages_reached"])
            self.assertEqual(result["messages_scanned"], 1)
            self.assertEqual(len(result["results"]), 1)

            outside_range = document_message(
                823,
                content_size=5,
                date=datetime(2025, 12, 31, tzinfo=timezone.utc),
            )
            code, result, _ = await run_case([only_message, outside_range], False)
            self.assertEqual(code, 0)
            self.assertFalse(result["max_messages_reached"])
            self.assertEqual(result["messages_scanned"], 1)

        asyncio.run(run())

    # Added by /ship Step 7 coverage audit (2026-11-18).
    # Value: protects=undated records make bounded media export visibly incomplete; fails_when=the attachment is downloaded or the result is complete; why_new=existing export tests cover dated bounds and iterator failures only; seam=none; no_claim=Telegram timestamp completeness
    def test_media_export_marks_undated_messages_partial_and_skips_them(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                undated = document_message(741, content_size=5)
                undated.date = None
                dated = document_message(
                    742,
                    content_size=5,
                    date=datetime(2026, 1, 15, tzinfo=timezone.utc),
                )
                client = FakeMediaClient(b"hello")
                client.history = [undated, dated]
                store = media.MediaStore(Path(temp) / "media", 42)
                output = StringIO()

                async def authorized(**_kwargs):
                    return client, None

                async def resolve(_client, _query):
                    return object(), None

                with (
                    patch.object(telegram, "authorized_client", authorized),
                    patch.object(telegram, "resolve_or_result", resolve),
                    patch.object(
                        telegram,
                        "entity_record",
                        return_value={"id": 42, "title": "Selected chat"},
                    ),
                    patch.object(telegram, "_media_store", return_value=store),
                    patch.object(telegram, "close_client"),
                    redirect_stdout(output),
                ):
                    code = await telegram.command_media_export(
                        "42",
                        since_arg="2026-01-01",
                        until_arg="2026-01-31",
                        output_dir=None,
                        max_files=10,
                        max_messages=10,
                        max_file_bytes=100,
                        max_total_bytes=100,
                        allowed_types=None,
                    )

                result = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["undated_messages_skipped"], 1)
                self.assertEqual(result["messages_scanned"], 2)
                self.assertEqual(result["counts"]["downloaded"], 1)
                self.assertEqual([row["message_id"] for row in result["results"]], [742])
                self.assertEqual(client.calls.count("download_media"), 1)
                self.assertEqual(result["limits"][0]["limit"], "date_range")

        asyncio.run(run())

    def test_date_range_is_validated_before_authorization(self):
        async def run():
            output = StringIO()
            with patch.object(telegram, "authorized_client", side_effect=AssertionError("must not connect")), redirect_stdout(output):
                code = await telegram.command_media_export(
                    "42",
                    since_arg="2026-02-01",
                    until_arg="2026-01-01",
                    output_dir=None,
                    max_files=10,
                    max_messages=100,
                    max_file_bytes=100,
                    max_total_bytes=100,
                    allowed_types=None,
                )
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_range")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
