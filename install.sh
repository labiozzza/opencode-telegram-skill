#!/bin/sh
set -eu

repo_url="https://github.com/labiozzza/opencode-telegram-skill.git"
skill_dir="${TELEGRAM_SKILL_DIR:-$HOME/.config/opencode/skills/telegram}"
data_dir="${TELEGRAM_SKILL_HOME:-$HOME/.local/share/telegram-skill}"
venv="$data_dir/.venv"
launcher_dir="${TELEGRAM_BIN_DIR:-$HOME/.local/bin}"

if [ -e "$skill_dir" ]; then
  if git -C "$skill_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Telegram skill is already installed at $skill_dir. Run 'telegram update-check' or 'telegram update'." >&2
  else
    echo "Refusing to overwrite existing non-Git skill directory: $skill_dir" >&2
    echo "Back it up and move it aside, then rerun this installer." >&2
  fi
  exit 1
fi

command -v git >/dev/null 2>&1 || { echo "Git is required to install/update the skill." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required to install the skill." >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10 or newer is required." >&2; exit 1; }
mkdir -p "$(dirname "$skill_dir")" "$data_dir" "$launcher_dir"
git clone "$repo_url" "$skill_dir"
python3 -m venv "$venv"
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -r "$skill_dir/requirements.txt"

cat > "$launcher_dir/telegram" <<'LAUNCHER'
#!/bin/sh
set -eu
skill_dir="${TELEGRAM_SKILL_DIR:-$HOME/.config/opencode/skills/telegram}"
data_dir="${TELEGRAM_SKILL_HOME:-$HOME/.local/share/telegram-skill}"
exec "$data_dir/.venv/bin/python" "$skill_dir/scripts/telegram.py" "$@"
LAUNCHER
chmod 700 "$launcher_dir/telegram"

echo "Installed Telegram skill at $skill_dir"
echo "Ensure $launcher_dir is on PATH, then run: telegram auth"
