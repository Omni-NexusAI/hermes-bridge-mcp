#!/usr/bin/env sh
# install-skills.sh — Cross-platform skill deployment for Hermes Bridge MCP.
#
# Works on Linux, macOS, Android/Termux. On Windows, use install-skills.ps1
# (or run this via Git Bash / MSYS).
#
# Copies all skills from the repo's skills/ directory into the agent's
# skills directory, then registers slash commands via register-commands.py.
# Detects HERMES_HOME automatically:
#   - Android/Termux: $HOME/.hermes/
#   - Linux/macOS:    $HOME/.hermes/
#   - Windows (MSYS): $LOCALAPPDATA/hermes/ or $HOME/AppData/Local/hermes/
#
# Usage:
#   sh scripts/install-skills.sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SKILLS_SRC="$REPO_ROOT/skills"

# Detect HERMES_HOME
if [ -n "${HERMES_HOME:-}" ]; then
    SKILLS_DEST="$HERMES_HOME/skills"
elif [ -n "${LOCALAPPDATA:-}" ] && [ -d "$LOCALAPPDATA/hermes" ]; then
    SKILLS_DEST="$LOCALAPPDATA/hermes/skills"
elif [ -d "$HOME/.hermes" ]; then
    SKILLS_DEST="$HOME/.hermes/skills"
else
    SKILLS_DEST="$HOME/.hermes/skills"
fi

echo "Source:   $SKILLS_SRC"
echo "Target:   $SKILLS_DEST"

if [ ! -d "$SKILLS_SRC" ]; then
    echo "Error: No skills/ directory found in repo root ($REPO_ROOT)" >&2
    exit 1
fi

mkdir -p "$SKILLS_DEST"

# Copy each skill directory, preserving structure
SKILL_COUNT=0
for skill_dir in "$SKILLS_SRC"/*/; do
    skill_name=$(basename "$skill_dir")
    dest_dir="$SKILLS_DEST/$skill_name"
    mkdir -p "$dest_dir"
    cp -r "$skill_dir"* "$dest_dir/"
    SKILL_COUNT=$((SKILL_COUNT + 1))
    echo "  Installed: $skill_name"
done

if [ "$SKILL_COUNT" -eq 0 ]; then
    echo "Warning: No skill directories found in $SKILLS_SRC" >&2
else
    echo "Installed $SKILL_COUNT skill(s) to $SKILLS_DEST"
fi

# Register slash commands (quick_commands aliases)
echo ""
echo "Registering slash commands..."
REGISTER_SCRIPT="$SKILLS_SRC/agent-bridge-pairing/scripts/register-commands.py"
if [ -f "$REGISTER_SCRIPT" ]; then
    PYTHON_BIN="${PYTHON:-python3}"
    if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        "$PYTHON_BIN" "$REGISTER_SCRIPT" || echo "  Warning: Command registration failed (non-fatal)"
    else
        echo "  Warning: Python not found — skipping command registration."
        echo "  Run manually: python $REGISTER_SCRIPT"
    fi
else
    echo "  Warning: register-commands.py not found — skipping command registration."
fi

echo ""
echo "Done. Reload skills in your agent with:"
echo "  hermes skills reload    (Hermes)"
echo "  /reload-skills          (in-session)"
