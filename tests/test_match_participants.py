from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import match_participants


class MatchParticipantTests(unittest.TestCase):
    def test_matches_only_by_user_id_and_keeps_all_chat_memberships(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dialogs = root / "dialogs.csv"
            with dialogs.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=("id", "title", "username", "type", "user_id"),
                )
                writer.writeheader()
                writer.writerow({"id": "501", "title": "Alex", "username": "alex", "type": "private", "user_id": "501"})
                writer.writerow({"id": "502", "title": "Same display name", "username": "", "type": "private", "user_id": "502"})
                writer.writerow({"id": "-1001", "title": "Group", "type": "group", "user_id": ""})

            first = root / "chat_1.jsonl"
            first.write_text(
                json.dumps({"user_id": 501, "chat_id": -1001, "chat_title": "Group", "display_name": "different name"}) + "\n"
                + json.dumps({"user_id": 502, "chat_id": -1001, "chat_title": "Group", "display_name": "Alex"}) + "\n",
                encoding="utf-8",
            )
            second = root / "chat_2.csv"
            with second.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=("user_id", "chat_id", "chat_title", "display_name"),
                )
                writer.writeheader()
                writer.writerow({"user_id": 501, "chat_id": "-1002", "chat_title": "Another group", "display_name": "Alex"})

            output = root / "matches.csv"
            report = match_participants.match_exports(
                str(dialogs), [str(first), str(second)], str(output)
            )
            with output.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))

        self.assertEqual(report["matching_rule"], "numeric Telegram user_id only")
        self.assertEqual(report["matched_users"], 2)
        self.assertEqual([row["user_id"] for row in rows], ["501", "502"])
        user_501 = rows[0]
        self.assertEqual(user_501["dialog_title"], "Alex")
        self.assertEqual(json.loads(user_501["matched_chat_ids"]), [-1002, -1001])
        self.assertEqual(
            json.loads(user_501["matched_chat_titles"]),
            ["Another group", "Group"],
        )
        # Same display name never creates a match for user 502 beyond its ID.
        self.assertEqual(rows[1]["user_id"], "502")

    def test_streams_json_arrays_and_directory_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dialogs = root / "dialogs.json"
            dialogs.write_text(
                json.dumps(
                    [
                        {"id": "42", "title": "Private", "type": "private", "user_id": 42},
                        {"id": "43", "title": "Not private", "type": "group", "user_id": ""},
                    ]
                ),
                encoding="utf-8",
            )
            participants_dir = root / "participants"
            participants_dir.mkdir()
            (participants_dir / "chat_7.json").write_text(
                '[{"user_id":42,"chat_id":-1007,"chat_title":"Seven"},'
                '{"user_id":999,"chat_id":-1007,"chat_title":"Seven"}]',
                encoding="utf-8",
            )
            (participants_dir / "chat_7.state.json").write_text(
                '{"status":"complete","unique_participants_saved":2}',
                encoding="utf-8",
            )
            output = root / "matches.csv"
            report = match_participants.match_exports(
                str(dialogs), [str(participants_dir)], str(output)
            )
            with output.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(report["matched_users"], 1)
        self.assertEqual(rows[0]["user_id"], "42")
        self.assertEqual(json.loads(rows[0]["matched_chat_ids"]), [-1007])

    def test_rejects_boolean_and_fractional_ids_instead_of_matching(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dialogs = root / "dialogs.json"
            dialogs.write_text(
                json.dumps(
                    [
                        {"id": "1", "title": "One", "type": "private", "user_id": 1},
                        {"id": "501", "title": "Five hundred one", "type": "private", "user_id": 501},
                        {"id": "503", "title": "Five hundred three", "type": "private", "user_id": 503},
                    ]
                ),
                encoding="utf-8",
            )
            participants = root / "participants.jsonl"
            participants.write_text(
                '{"user_id":true,"chat_id":-1001}\n'
                '{"user_id":501.9,"chat_id":-1002}\n'
                '{"user_id":503,"chat_id":-1003.9}\n',
                encoding="utf-8",
            )
            output = root / "matches.csv"

            report = match_participants.match_exports(
                str(dialogs), [str(participants)], str(output)
            )
            with output.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))

        self.assertEqual(report["matched_users"], 0)
        self.assertEqual(report["participant_rows_indexed"], 0)
        self.assertEqual(rows, [])

    def test_rejects_malformed_or_unrecognized_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dialogs = root / "dialogs.json"
            dialogs.write_text('{"not":"an array"}', encoding="utf-8")
            participant = root / "participants.jsonl"
            participant.write_text('{"user_id":1,"chat_id":2}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                match_participants.match_exports(
                    str(dialogs), [str(participant)], str(root / "out.csv")
                )


if __name__ == "__main__":
    unittest.main()
