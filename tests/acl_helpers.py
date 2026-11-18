from __future__ import annotations

import csv
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _current_user_sid() -> str:
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    )
    return next(csv.reader(result.stdout.splitlines()))[1]


def assert_owner_only_acl(test: Any, path: Path, *, directory: bool = False) -> None:
    with tempfile.TemporaryDirectory() as temp:
        saved_acl = Path(temp) / "acl.txt"
        subprocess.run(
            ["icacls", str(path), "/save", str(saved_acl)],
            check=True,
            capture_output=True,
            text=True,
        )
        acl_lines = [
            line.strip()
            for line in saved_acl.read_text(encoding="utf-16-le").splitlines()
            if line.strip().startswith("D:")
        ]

    test.assertEqual(len(acl_lines), 1, acl_lines)
    entries = re.findall(r"\(([^()]*)\)", acl_lines[0])
    test.assertEqual(len(entries), 1, acl_lines[0])
    ace = entries[0].split(";")
    test.assertEqual(len(ace), 6, acl_lines[0])
    test.assertEqual(ace[0], "A", acl_lines[0])
    test.assertEqual(ace[2], "FA", acl_lines[0])
    test.assertEqual(ace[3:5], ["", ""], acl_lines[0])
    current_sid = _current_user_sid()
    # icacls serializes the local Administrator SID (RID 500) as the SDDL
    # well-known alias LA instead of its machine-specific SID string.
    trustee_matches = ace[5] == current_sid or (
        ace[5] == "LA" and current_sid.rsplit("-", 1)[-1] == "500"
    )
    test.assertTrue(trustee_matches, acl_lines[0])

    if directory:
        inheritance_flags = re.findall(r"OI|CI", ace[1])
        test.assertEqual(set(inheritance_flags), {"OI", "CI"}, acl_lines[0])
        test.assertEqual(len(inheritance_flags), 2, acl_lines[0])
        test.assertEqual(re.sub(r"OI|CI", "", ace[1]), "", acl_lines[0])
    else:
        test.assertEqual(ace[1], "", acl_lines[0])
