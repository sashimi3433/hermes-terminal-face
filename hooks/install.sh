#!/usr/bin/env bash
#
# Install or uninstall the terminal-face hooks for Grok Build and/or the
# Antigravity CLI (agy). Pi and Hermes have their own in-process integrations;
# see README.md.
#
# Usage:
#   hooks/install.sh                 # install for grok and agy
#   hooks/install.sh grok            # only Grok Build
#   hooks/install.sh agy             # only Antigravity CLI
#   hooks/install.sh --uninstall     # remove both
#   hooks/install.sh --dry-run       # print what would happen
#
# Environment:
#   GROK_HOME                 default ~/.grok
#   ANTIGRAVITY_PLUGIN_DIR    default ~/.gemini/config/plugins/terminal-face
#
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hook="$root/hooks/terminal-face-hook.py"
grok_template="$root/hooks/grok/terminal-face.json"
agy_src="$root/hooks/antigravity"

grok_home="${GROK_HOME:-$HOME/.grok}"
grok_hook="$grok_home/hooks/terminal-face.json"
agy_dir="${ANTIGRAVITY_PLUGIN_DIR:-$HOME/.gemini/config/plugins/terminal-face}"

agents=()
uninstall=false
dry_run=false

usage() {
	sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
	case "$1" in
		grok | agy) agents+=("$1") ;;
		--all) agents=(grok agy) ;;
		--uninstall) uninstall=true ;;
		--dry-run) dry_run=true ;;
		-h | --help)
			usage
			exit 0
			;;
		*)
			echo "unknown argument: $1" >&2
			usage >&2
			exit 2
			;;
	esac
	shift
done

[ ${#agents[@]} -gt 0 ] || agents=(grok agy)

python="$(command -v python3 || true)"
if [ -z "$python" ]; then
	echo "python3 is required (the hook script runs on it)" >&2
	exit 1
fi
if [ ! -f "$hook" ]; then
	echo "missing hook script: $hook" >&2
	exit 1
fi

run() {
	if $dry_run; then
		echo "  would: $*"
	else
		echo "  $*"
		"$@"
	fi
}

install_grok() {
	echo "Grok Build → $grok_hook"
	run mkdir -p "$grok_home/hooks"
	if [ -f "$grok_hook" ] && ! grep -q "terminal-face-hook.py" "$grok_hook"; then
		run cp "$grok_hook" "$grok_hook.bak.$(date +%Y%m%d%H%M%S)"
	fi
	if $dry_run; then
		echo "  would: render $grok_template → $grok_hook"
		return
	fi
	"$python" - "$grok_template" "$grok_hook" "$python" "$hook" <<'PY'
import json
import pathlib
import sys

template, dest, python, hook = sys.argv[1:5]
rendered = pathlib.Path(template).read_text().replace("__COMMAND__", f"{python} {hook}")
json.loads(rendered)  # fail before writing a broken hook file
pathlib.Path(dest).write_text(rendered)
PY
	echo "  wrote $grok_hook"
	if grep -qs "claude-face-hook\|terminal-face-hook" "$HOME/.claude/settings.json"; then
		echo
		echo "  note: ~/.claude/settings.json already runs a face hook, and Grok"
		echo "        reads Claude hooks too. Both ramps send the same colors, but"
		echo "        to avoid duplicate work add to ~/.grok/config.toml:"
		echo "            [compat.claude]"
		echo "            hooks = false"
	fi
}

uninstall_grok() {
	echo "Grok Build → $grok_hook"
	if [ -f "$grok_hook" ]; then
		run rm -f "$grok_hook"
	else
		echo "  not installed"
	fi
}

install_agy() {
	echo "Antigravity CLI → $agy_dir"
	run mkdir -p "$agy_dir"
	run cp "$agy_src/plugin.json" "$agy_dir/plugin.json"
	run cp "$agy_src/hooks.json" "$agy_dir/hooks.json"
	run cp "$hook" "$agy_dir/terminal-face-hook.py"
	run chmod +x "$agy_dir/terminal-face-hook.py"
	if ! $dry_run && command -v agy >/dev/null 2>&1; then
		agy plugin validate "$agy_dir" 2>&1 | sed 's/^/  /' || true
	fi
}

uninstall_agy() {
	echo "Antigravity CLI → $agy_dir"
	if [ -f "$agy_dir/hooks.json" ] && grep -q "terminal-face-hook" "$agy_dir/hooks.json"; then
		run rm -rf "$agy_dir"
	else
		echo "  not installed (or modified by hand; leaving it alone)"
	fi
}

if $uninstall; then
	for agent in "${agents[@]}"; do
		"uninstall_$agent"
	done
	echo "Uninstalled. Restart the agent to drop the hooks."
else
	for agent in "${agents[@]}"; do
		"install_$agent"
	done
	echo
	echo "Installed. Restart the agent (or open a new session) to load the hooks."
fi
