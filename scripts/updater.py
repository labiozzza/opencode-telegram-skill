"""Check and apply stable GitHub Releases for the installed Telegram skill."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPOSITORY = os.environ.get("TELEGRAM_SKILL_GITHUB_REPO", "labiozzza/opencode-telegram-skill")
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
CACHE_PATH = Path(os.environ.get("TELEGRAM_SKILL_HOME", "~/.local/share/telegram-skill")).expanduser() / "update-check.json"
VERSION_PATH = Path(__file__).resolve().parents[1] / "VERSION"
TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def current_version() -> str:
    return VERSION_PATH.read_text(encoding="utf-8").strip()


def version_tuple(version: str) -> tuple[int, int, int] | None:
    match = TAG_RE.fullmatch(version)
    return tuple(int(part) for part in match.groups()) if match else None


def latest_release() -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "opencode-telegram-skill"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        release = json.load(response)
    tag = release.get("tag_name", "")
    if not version_tuple(tag):
        raise ValueError("Latest GitHub release must use a stable semantic version tag, e.g. v1.2.3.")
    return {"tag": tag, "url": release.get("html_url")}


def _read_cache() -> dict[str, Any] | None:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - float(payload["checked_at"]) < CHECK_INTERVAL_SECONDS:
            return payload
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return None


def _write_cache(payload: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CACHE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(temp_path, 0o600)
    temp_path.replace(CACHE_PATH)


def check_for_update(*, force: bool = False) -> dict[str, Any]:
    installed = current_version()
    if not force:
        cached = _read_cache()
        if cached:
            return {**cached, "cached": True}
    checked_at = int(time.time())
    try:
        release = latest_release()
        latest = release["tag"].removeprefix("v")
        installed_tuple, latest_tuple = version_tuple(installed), version_tuple(latest)
        if installed_tuple is None:
            raise ValueError(f"Installed VERSION is not semantic version: {installed}")
        payload = {
            "status": "update_available" if latest_tuple > installed_tuple else "up_to_date",
            "installed_version": installed,
            "latest_version": latest,
            "release_url": release["url"],
            "checked_at": checked_at,
        }
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        payload = {"status": "check_failed", "installed_version": installed, "message": str(exc), "checked_at": checked_at}
    try:
        _write_cache(payload)
    except OSError:
        payload["cache_warning"] = "Could not write the 24-hour update-check cache."
    return {**payload, "cached": False}


def apply_update() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        return {"status": "not_git_install", "message": "Install this skill with install.sh before using its in-place updater."}
    try:
        origin = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        expected = f"https://github.com/{REPOSITORY}.git"
        if origin.rstrip("/") not in {expected, f"https://github.com/{REPOSITORY}", f"git@github.com:{REPOSITORY}.git"}:
            return {"status": "wrong_origin", "message": "The install's origin does not match the configured public repository."}
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], check=True, capture_output=True, text=True, timeout=10).stdout
        if dirty:
            return {"status": "local_changes", "message": "Update blocked to preserve local changes. Commit or back them up first."}
        release = latest_release()
        tag = release["tag"]
        latest = tag.removeprefix("v")
        installed = current_version()
        installed_tuple, latest_tuple = version_tuple(installed), version_tuple(latest)
        if installed_tuple is None:
            return {"status": "invalid_installed_version", "installed_version": installed}
        if latest_tuple <= installed_tuple:
            return {"status": "up_to_date", "installed_version": installed, "latest_version": latest}
        subprocess.run(["git", "-C", str(root), "fetch", "--tags", "origin"], check=True, capture_output=True, text=True, timeout=60)
        subprocess.run(["git", "-C", str(root), "checkout", "--detach", tag], check=True, capture_output=True, text=True, timeout=30)
        try:
            _write_cache({"status": "up_to_date", "installed_version": latest, "latest_version": latest, "release_url": release["url"], "checked_at": int(time.time())})
        except OSError:
            pass
        return {"status": "updated", "previous_version": installed, "installed_version": latest, "release_url": release["url"]}
    except (OSError, subprocess.SubprocessError, urllib.error.URLError, ValueError) as exc:
        return {"status": "update_failed", "message": str(exc)}
