"""
terminal-face — Hermes plugin that mirrors agent state to the terminal cursor
color (OSC 12) so isoden/claude-terminal-face's Ghostty shader can animate a
face on the terminal background.

Ported from claude-face-hook.sh. Maps Hermes plugin hooks to the 4 shader
states (idle/thinking/working/done/err) using the same 6-digit hex colors the
shader matches against:

    THINK = f0c14b   (pre_llm_call — user just submitted, agent is reasoning)
    WORK  = 3d6fe0   (terminal/write_file/patch/exec_code — mutating work)
    DONE  = 8bd346   (post_llm_call — turn finished)
    ERR   = ff5f6d   (post_tool_call with an error result)

CLI/TUI only: writes to the controlling TTY of the Hermes process. Gateway
sessions (Telegram/Discord/...) have no TTY and silently no-op.
"""

from __future__ import annotations

import json
import logging
import os
import sys

logger = logging.getLogger("plugins.terminal-face")

# --- State palette (must match the GLSL shader's palette exactly) -----------
THINK = "f0c14b"  # thinking — yellow
WORK = "3d6fe0"  # working  — blue
DONE = "8bd346"  # done     — green
ERR = "ff5f6d"  # error    — red

# Tools that represent "mutating work" (write/edit/exec) vs "reading/exploring"
# (which stays in the thinking state).
WORK_TOOLS = {
    "terminal",
    "write_file",
    "patch",
    "execute_code",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_navigate",
}

# Platforms where writing to a TTY makes sense. Gateway platforms (telegram,
# discord, slack, matrix, ...) run headless and should no-op.
TTY_PLATFORMS = {"cli", "tui"}


def _resolve_tty() -> str | None:
    """Return a writable TTY path for the current process tree, or None.

    Priority:
      1. $HERMES_FACE_TTY override (explicit user pin).
      2. sys.stdout's TTY if Hermes is running interactively in a terminal.
      3. Ancestor walk — find the nearest real TTY in the parent process chain
         (needed when Hermes spawns hooks in a subprocess without a ctty).
    """
    override = os.environ.get("HERMES_FACE_TTY")
    if override and os.path.exists(override):
        return override

    # CLI/TUI case — stdout is the user's terminal.
    try:
        if sys.stdout and sys.stdout.isatty():
            import tty as _tty  # noqa: F401  (only to confirm termios avail)

            # sys.__stdout__ can drift from the real fd; use the fd directly.
            fd = getattr(sys.stdout, "fileno", lambda: None)()
            if isinstance(fd, int) and os.isatty(fd):
                return os.ttyname(fd)
    except Exception:
        pass

    # Subprocess case — walk ancestors looking for a writable TTY.
    # (Hook callbacks always run in-process for plugin hooks, but we keep this
    # for robustness if a future Hermes version offloads them.)
    try:
        ppid = os.getppid()
        for _ in range(8):
            if not ppid or ppid == 1:
                break
            # ps -o tty= -p PID → e.g. "ttys011" or "??" if none
            import subprocess

            res = subprocess.run(
                ["ps", "-o", "tty=", "-p", str(ppid)],
                capture_output=True,
                text=True,
                timeout=1,
            )
            tty = res.stdout.strip()
            if tty and tty != "??":
                path = f"/dev/{tty}"
                if os.access(path, os.W_OK):
                    return path
            # Walk up
            res = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(ppid)],
                capture_output=True,
                text=True,
                timeout=1,
            )
            try:
                ppid = int(res.stdout.strip())
            except ValueError:
                break
    except Exception:
        pass

    return None


def _send(color_hex: str) -> None:
    """Write an OSC 12 cursor-color escape to the resolved TTY.

    Silently no-ops on gateway platforms or when no TTY can be found — this
    plugin must never break the agent loop.
    """
    # Gate on platform from env (Hermes sets HERMES_PLATFORM in sessions).
    platform = os.environ.get("HERMES_PLATFORM", "cli")
    if platform not in TTY_PLATFORMS:
        return

    tty = _resolve_tty()
    if not tty:
        return

    seq = f"\033]12;#{color_hex}\a"
    try:
        with open(tty, "w", buffering=1) as f:
            f.write(seq)
            f.flush()
    except OSError:
        # TTY disappeared between resolve and write — nothing to do.
        pass
    except Exception as e:
        logger.debug("terminal-face: failed to write OSC 12 to %s: %s", tty, e)


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------


def on_pre_llm_call(user_message=None, platform=None, **kwargs):
    """User submitted a prompt — agent is now thinking/planning."""
    del user_message, kwargs
    _send(THINK)


def on_pre_tool_call(tool_name=None, args=None, **kwargs):
    """A tool is about to run. Mutating tools → WORK, everything else → THINK."""
    del args, kwargs
    if tool_name in WORK_TOOLS:
        _send(WORK)
    else:
        _send(THINK)


def on_post_tool_call(tool_name=None, result=None, **kwargs):
    """Tool returned. Error → ERR. Mutating-tool success → WORK. Reads → THINK."""
    if isinstance(result, str):
        # Cheap error sniff without a full JSON parse — Hermes tool results are
        # JSON strings; errors typically carry {"error": ...} or exit_code != 0.
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                if parsed.get("error"):
                    _send(ERR)
                    return
                # terminal tool reports exit_code on the result envelope.
                exit_code = parsed.get("exit_code")
                if isinstance(exit_code, int) and exit_code != 0:
                    _send(ERR)
                    return
        except (json.JSONDecodeError, TypeError):
            pass
    # Successful or unparseable. Mutating tools stay in WORK; reading/exploring
    # tools return to THINK so the face reflects "reasoning again."
    _send(WORK if tool_name in WORK_TOOLS else THINK)


def on_post_llm_call(assistant_response=None, **kwargs):
    """Turn finished — show the 'done' face."""
    del assistant_response, kwargs
    _send(DONE)


def on_session_end(**kwargs):
    """Session ended — reset to idle (the shader's fallback)."""
    del kwargs
    # The shader falls back to idle when it sees any color far from the
    # palette. We reset the cursor to a neutral gray so the face goes idle.
    _send("888888")


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx):
    """Wire Hermes plugin hooks to OSC 12 cursor-color writes."""
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)

    logger.info(
        "terminal-face plugin registered "
        "(state → OSC 12 cursor color for Ghostty shader)"
    )
