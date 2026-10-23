#!/usr/bin/env python3
"""
terminal-face hook — mirrors an agent's working state to the terminal cursor
color (OSC 12) so isoden/claude-terminal-face's Ghostty custom shader can
animate a robot face on the terminal background.

This is the *external hook* counterpart of the in-process integrations in this
repository (Pi extension, Hermes plugin). Agents that run hooks as short-lived
commands use this script:

    grok    Grok Build   ~/.grok/hooks/*.json       (Claude Code-compatible)
    agy     Antigravity  ~/.gemini/config/plugins/<name>/hooks.json
    claude  Claude Code  ~/.claude/settings.json    (same dialect as grok)

Interface:
  * State comes from argv (`--event`, see below) plus the hook payload on
    stdin (JSON). Tool-based events read the tool name and result from it.
  * The target color is written to the terminal via OSC 12. Hooks may run many
    times per turn and are killed when the command exits, so the ~1s color ramp
    is handed to a detached child process (new session) and the hook returns
    immediately.
  * Nothing is ever printed to stdout: grok reads hook stdout as a reply for
    some events, and a stray line must never change agent behaviour.

Events (`--event`):
    reset       jump to idle immediately (session start/end)
    idle        ramp to idle (cancelled turn, waiting on you)
    thinking    ramp to thinking (prompt submitted, model invocation)
    pre-tool    ramp to working/thinking by tool name
    post-tool   ramp to err on failure, else working/thinking by tool name
    error       ramp to err
    done        ramp to done (turn completed)

Palette and ramp constants must match claude-terminal-face-status.glsl
(IDLE_KEY … ERR_KEY, STATE_SOFT) and the other integrations in this repo.

Only macOS/Linux: it resolves a TTY device path (/dev/ttysNNN) and forks.
Set TERMINAL_FACE_TTY to pin the target (e.g. /dev/ttys011).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sys
import time

# --- State palette (must match the GLSL shader's keys exactly) --------------
IDLE = "5ce0c9"
THINK = "f7b81f"
WORK = "41419c"
DONE = "0a9900"
ERR = "e00000"

# --- Ramp shape (mirrors claude-face-ramp.sh in the upstream repo) -----------
STATE_SOFT = 0.03  # shader constant: softmax sharpness
RAMP_HEAD = 5  # color-only head (face holds)
RAMP_BODY = 21  # morphing body
RAMP_TAIL = 5  # color-only tail
RAMP_STEP_SECONDS = 0.03  # ≈30Hz, matching the bash ramp's effective spacing

# Tools whose effect is a mutation of the environment. Names are taken from
# the agents' own tool sets plus common aliases (Claude Code style names reach
# grok matchers/payloads too).
MUTATING_TOOLS = {
    # grok
    "run_terminal_command",
    "search_replace",
    "hashline_edit",
    "write",
    # claude code
    "bash",
    "powershell",
    "edit",
    "multiedit",
    "notebookedit",
    # antigravity (agy)
    "run_command",
    "run_shell_command",
    "write_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "apply_patch",
    "create_file",
    "delete_file",
    "move_file",
    "browser_click_element",
    "browser_type",
    "browser_press_key",
    "browser_navigate",
    "browser_select_option",
    "browser_resize_window",
    # generic aliases
    "terminal",
    "execute_code",
    "patch",
}

READ_TOOLS = {
    "read_file",
    "view_file",
    "read_url_content",
    "list_dir",
    "list_directory",
    "grep",
    "grep_search",
    "glob",
    "find",
    "ls",
    "web_search",
    "web_fetch",
    "read",
    "view",
}

# Fallbacks for MCP / third-party tool names that are in neither set.
READ_NAME_RE = re.compile(
    r"(read|view|list|grep|glob|search|find|fetch|url|status|inspect|get[_-])",
    re.IGNORECASE,
)
MUTATING_NAME_RE = re.compile(
    r"(write|edit|replace|patch|create|delete|remove|move|rename|shell|command|exec|terminal|apply|install|click|type)",
    re.IGNORECASE,
)

SESSION_END_STOP_REASONS = {"shutdown", "channel_closed"}


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    value = int(hex_color.lstrip("#"), 16)
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "".join(
        f"{max(0, min(255, int(channel + 0.5))):02x}" for channel in rgb
    )


def plan_ramp(source_hex: str | None, target_hex: str) -> list[str]:
    """Colors to send, in order, when moving the cursor to `target_hex`.

    `source_hex` is the last *target* color. Without one (first state of a
    session) the target is sent as-is — interpolating from an unknown color
    looks worse than a clean cut. Same math as the Pi extension and the Hermes
    plugin, so all three produce identical ramps.
    """
    if not source_hex or source_hex.lower() == target_hex.lower():
        return [target_hex]

    source = hex_to_rgb(source_hex)
    target = hex_to_rgb(target_hex)

    distance_sq = sum(((t - s) / 255.0) ** 2 for s, t in zip(source, target))
    k = max(distance_sq / STATE_SOFT, 1e-6)

    w_min = 0.01
    logit_min = math.log(w_min / (1 - w_min))
    s_lo = clamp01(0.5 + logit_min / (2 * k))
    s_hi = clamp01(0.5 - logit_min / (2 * k))

    def mix(position: float) -> str:
        clamped = clamp01(position)
        return rgb_to_hex(
            tuple(s + (t - s) * clamped for s, t in zip(source, target))
        )

    colors: list[str] = []
    for i in range(1, RAMP_HEAD + 1):
        colors.append(mix(s_lo * i / RAMP_HEAD))
    for i in range(1, RAMP_BODY + 1):
        p = i / RAMP_BODY
        w = w_min + (1 - 2 * w_min) * (p * p * (3 - 2 * p))
        colors.append(mix(0.5 + math.log(w / (1 - w)) / (2 * k)))
    for i in range(1, RAMP_TAIL):
        colors.append(mix(s_hi + (1 - s_hi) * i / RAMP_TAIL))
    colors.append(target_hex)
    return colors


# ---------------------------------------------------------------------------
# Terminal resolution
# ---------------------------------------------------------------------------


def _ps_field(pid: int, field: str) -> str:
    try:
        import subprocess

        res = subprocess.run(
            ["ps", "-o", field, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def _tty_from_ps(pid: int) -> str | None:
    """Resolve a PID's controlling terminal to an openable device path."""
    tty = _ps_field(pid, "tty=")
    if not tty or tty in {"??", "?", "-"}:
        return None
    path = tty if tty.startswith("/") else f"/dev/{tty}"
    return path if os.access(path, os.W_OK) else None


def resolve_tty() -> str | None:
    """Return a writable TTY device path, or None.

    Priority:
      1. $TERMINAL_FACE_TTY (explicit pin).
      2. Our own controlling terminal, from ps (`os.ttyname` on macOS returns
         the useless `/dev/tty` for a controlling-terminal fd). The detached
         ramp runs in a new session, where `/dev/tty` is gone, so it needs the
         real device path.
      3. `/dev/tty`, when it resolves to a real device.
      4. Ancestor walk — hooks are often spawned without a controlling
         terminal even though the agent UI owns one.
    """
    override = os.environ.get("TERMINAL_FACE_TTY")
    if override:
        if os.path.exists(override) and os.access(override, os.W_OK):
            return override

    path = _tty_from_ps(os.getpid())
    if path:
        return path

    try:
        fd = os.open("/dev/tty", os.O_WRONLY)
        try:
            name = os.ttyname(fd)
            if name and name != "/dev/tty" and os.access(name, os.W_OK):
                return name
        finally:
            os.close(fd)
    except OSError:
        pass

    pid = os.getppid()
    for _ in range(8):
        if not pid or pid <= 1:
            break
        path = _tty_from_ps(pid)
        if path:
            return path
        try:
            pid = int(_ps_field(pid, "ppid=") or 0)
        except ValueError:
            break

    return None


def write_osc(tty: str, color_hex: str) -> None:
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, f"\033]12;#{color_hex}\a".encode())
        finally:
            os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Cross-process state (one directory per TTY)
# ---------------------------------------------------------------------------


def state_dir(tty: str) -> str:
    base = os.environ.get("TMPDIR") or "/tmp"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", tty) or "tty"
    path = os.path.join(base, "terminal-face", slug)
    os.makedirs(path, exist_ok=True)
    return path


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _write(path: str, text: str) -> None:
    try:
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


def _debug(message: str) -> None:
    if not os.environ.get("TERMINAL_FACE_DEBUG"):
        return
    try:
        base = os.environ.get("TMPDIR") or "/tmp"
        path = os.path.join(base, "terminal-face")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "hook.log"), "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


class _Lock:
    """Advisory lock so parallel tool hooks cannot tear the state."""

    def __init__(self, directory: str):
        self.path = os.path.join(directory, "lock")
        self.fd: int | None = None

    def __enter__(self):
        try:
            import fcntl

            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except Exception:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
        return self

    def __exit__(self, *_exc):
        if self.fd is not None:
            try:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(self.fd)
            self.fd = None
        return False


def _spawn_ramp(tty: str, colors: list[str], generation: str, gen_path: str) -> None:
    """Run the ramp in a detached child, then return to the agent at once."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()

    if pid:
        # Parent: wait until the child left our process group, then exit.
        os.close(write_fd)
        try:
            os.read(read_fd, 1)
        except OSError:
            pass
        os.close(read_fd)
        os._exit(0)  # noqa: SLF001 — must not run atexit/flush in a hook

    # Child
    os.close(read_fd)
    try:
        os.setsid()
    except OSError:
        pass
    try:
        os.write(write_fd, b"1")
    except OSError:
        pass
    os.close(write_fd)

    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass
    if devnull > 2:
        os.close(devnull)

    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    for color in colors:
        if _read(gen_path) != generation:
            os._exit(0)  # noqa: SLF001 — superseded by a newer transition
        write_osc(tty, color)
        time.sleep(RAMP_STEP_SECONDS)
    os._exit(0)  # noqa: SLF001


def set_state(target: str, *, immediate: bool = False) -> None:
    """Move the cursor color toward `target`, or jump when `immediate`."""
    tty = resolve_tty()
    if not tty:
        _debug(f"no tty (target={target})")
        return

    directory = state_dir(tty)
    color_path = os.path.join(directory, "color")
    gen_path = os.path.join(directory, "gen")

    with _Lock(directory):
        previous = _read(color_path) or None
        if previous == target:
            _debug(f"{target} already set on {tty}")
            return
        generation = str(int(_read(gen_path) or 0) + 1)
        _write(gen_path, generation)
        _write(color_path, target)

    if immediate or not previous:
        # Bumping the generation cancels any ramp still running.
        write_osc(tty, target)
        _debug(f"{tty}: {previous} -> {target} (immediate)")
        return

    colors = plan_ramp(previous, target)
    _spawn_ramp(tty, colors, generation, gen_path)
    _debug(f"{tty}: {previous} -> {target} ({len(colors)} steps)")


# ---------------------------------------------------------------------------
# Payload → state
# ---------------------------------------------------------------------------


def load_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def tool_name(payload: dict) -> str:
    call = payload.get("toolCall") or payload.get("tool_call")
    if isinstance(call, dict):
        name = call.get("name") or call.get("toolName") or ""
        if isinstance(name, str) and name:
            return name
    for key in ("tool_name", "toolName"):
        name = payload.get(key)
        if isinstance(name, str) and name:
            return name
    return ""


def tool_result(payload: dict) -> dict:
    for key in ("tool_response", "toolResponse", "tool_result", "toolResult", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def looks_like_error(payload: dict) -> bool:
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return True
    if isinstance(error, dict) and error:
        return True

    result = tool_result(payload)
    for key in ("error", "is_error", "isError"):
        value = result.get(key)
        if value:
            return True
    for key in ("exit_code", "exitCode"):
        value = result.get(key)
        if isinstance(value, int) and value != 0:
            return True
    for key in ("timed_out", "timedOut"):
        if result.get(key) is True:
            return True
    return result.get("success") is False


def is_mutating(name: str) -> bool:
    key = name.lower()
    if key in MUTATING_TOOLS:
        return True
    if key in READ_TOOLS:
        return False
    # MCP names arrive as `server__tool` or `mcp__server__tool`.
    short = key.rsplit("__", 1)[-1]
    if short in MUTATING_TOOLS:
        return True
    if short in READ_TOOLS:
        return False
    if READ_NAME_RE.search(short):
        return False
    if MUTATING_NAME_RE.search(short):
        return True
    return False


def target_for(args: argparse.Namespace, payload: dict) -> str | None:
    event = args.event

    if event == "reset":
        return IDLE
    if event == "idle":
        return IDLE
    if event == "thinking":
        return THINK
    if event == "error":
        return ERR
    if event == "done":
        # A subagent finishing, or grok's session-end Stop, is not the turn
        # the user is looking at.
        if payload.get("subagentType") or payload.get("subagent_type"):
            return None
        if payload.get("reason") in SESSION_END_STOP_REASONS:
            return None
        return DONE
    if event in {"pre-tool", "post-tool"}:
        name = tool_name(payload)
        if event == "post-tool" and looks_like_error(payload):
            return ERR
        return WORK if is_mutating(name) else THINK
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="terminal-face-hook",
        description="Agent hook → OSC 12 cursor color for the terminal face shader.",
    )
    parser.add_argument(
        "--source",
        choices=("grok", "claude", "agy"),
        default="grok",
        help="Agent dialect (affects payload field names and tool sets).",
    )
    parser.add_argument(
        "--event",
        required=True,
        choices=(
            "reset",
            "idle",
            "thinking",
            "pre-tool",
            "post-tool",
            "error",
            "done",
        ),
        help="Hook event, mapped to a state by this script.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    payload = load_payload()
    target = target_for(args, payload)
    if target is None:
        return 0
    set_state(target, immediate=args.event == "reset")
    _debug(
        f"{args.source}/{args.event} tool={tool_name(payload)!r} -> {target}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never break the agent loop
        _debug(f"error: {exc!r}")
        sys.exit(0)
