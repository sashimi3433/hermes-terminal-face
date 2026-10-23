"""
terminal-face — Hermes plugin that mirrors agent state to the terminal cursor
color (OSC 12) so isoden/claude-terminal-face's Ghostty shader can animate a
face on the terminal background.

Maps Hermes plugin hooks to the shader's five states (idle/thinking/working/
done/err) using the same key colors the shader matches against:

    IDLE  = 5ce0c9   (session end — the shader's resting face)
    THINK = f7b81f   (pre_llm_call — user just submitted, agent is reasoning)
    WORK  = 41419c   (terminal/write_file/patch/exec_code — mutating work)
    DONE  = 0a9900   (post_llm_call — turn finished)
    ERR   = e00000   (post_tool_call with an error result)

Transitions are ramped: the shader is stateless and decodes the state from the
*current* cursor color with a distance-based softmax, so jumping straight from
one key color to another snaps the face. Sending intermediate colors over ~1s
gives the shader a time axis; their positions on the RGB line are the inverse
of the softmax, so the decoded weight follows a smoothstep (same easing as the
in-state morph). Mirrors claude-face-ramp.sh from the upstream repo.

CLI/TUI only: writes to the controlling TTY of the Hermes process. Gateway
sessions (Telegram/Discord/...) have no TTY and silently no-op.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time

logger = logging.getLogger("plugins.terminal-face")

# --- State palette (must match the GLSL shader's keys exactly) --------------
IDLE = "5ce0c9"  # idle     — resting face
THINK = "f7b81f"  # thinking — yellow
WORK = "41419c"  # working  — blue
DONE = "0a9900"  # done     — green
ERR = "e00000"  # error    — red

# --- Ramp shape (mirrors claude-face-ramp.sh in the upstream repo) -----------
STATE_SOFT = 0.03  # shader constant: softmax sharpness
RAMP_HEAD = 5  # color-only head (face holds)
RAMP_BODY = 21  # morphing body
RAMP_TAIL = 5  # color-only tail
RAMP_STEP_SECONDS = 0.03  # ≈30Hz, matching the bash ramp's effective spacing

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

# Ramp coordination. `_target` is the last color we committed to (not
# necessarily what is on screen yet); a newer transition bumps `_generation`
# and older ramp threads retire at their next step.
_lock = threading.Lock()
_generation = 0
_target: str | None = None


def _clamp(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    value = int(hex_color.lstrip("#"), 16)
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "".join(
        f"{max(0, min(255, math.floor(channel + 0.5))):02x}" for channel in rgb
    )


def _plan_ramp(source_hex: str | None, target_hex: str) -> list[str]:
    """Colors to send, in order, when moving the cursor to `target_hex`.

    `source_hex` is the last *target* color. Without one (first state of a
    session) the target is sent as-is — interpolating from an unknown color
    looks worse than a clean cut.
    """
    if not source_hex or source_hex.lower() == target_hex.lower():
        return [target_hex]

    source = _hex_to_rgb(source_hex)
    target = _hex_to_rgb(target_hex)

    # Shader: w_to(s) = sigmoid(K * (2s - 1)), K = |key distance|² / STATE_SOFT.
    distance_sq = sum(((t - s) / 255.0) ** 2 for s, t in zip(source, target))
    k = max(distance_sq / STATE_SOFT, 1e-6)

    # Weight range where the face is still (nearly) the source / target state.
    w_min = 0.01
    logit_min = math.log(w_min / (1 - w_min))
    s_lo = _clamp(0.5 + logit_min / (2 * k))
    s_hi = _clamp(0.5 - logit_min / (2 * k))

    def mix(position: float) -> str:
        clamped = _clamp(position)
        return _rgb_to_hex(
            tuple(s + (t - s) * clamped for s, t in zip(source, target))
        )

    colors: list[str] = []
    # Head: move the visible cursor color toward the body range; the face holds.
    for i in range(1, RAMP_HEAD + 1):
        colors.append(mix(s_lo * i / RAMP_HEAD))
    # Body: invert the softmax so the decoded weight follows a smoothstep.
    for i in range(1, RAMP_BODY + 1):
        p = i / RAMP_BODY
        w = w_min + (1 - 2 * w_min) * (p * p * (3 - 2 * p))
        colors.append(mix(0.5 + math.log(w / (1 - w)) / (2 * k)))
    # Tail: close the remaining color gap to the exact key color.
    for i in range(1, RAMP_TAIL):
        colors.append(mix(s_hi + (1 - s_hi) * i / RAMP_TAIL))
    # End on the key color exactly, so the next ramp starts where we recorded it.
    colors.append(target_hex)

    return colors


def _resolve_tty() -> str | None:
    """Return a writable TTY path for the current process tree, or None.

    Priority:
      1. $HERMES_FACE_TTY / $TERMINAL_FACE_TTY override (explicit user pin).
      2. sys.stdout's TTY if Hermes is running interactively in a terminal.
      3. Ancestor walk — find the nearest real TTY in the parent process chain
         (needed when Hermes spawns hooks in a subprocess without a ctty).
    """
    override = os.environ.get("HERMES_FACE_TTY") or os.environ.get("TERMINAL_FACE_TTY")
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


def _write_osc(tty: str, color_hex: str) -> None:
    """Write an OSC 12 cursor-color escape to the resolved TTY."""
    try:
        # O_APPEND keeps regular-file test sinks intact; for a char device it
        # is a no-op. One open per write matches the upstream bash ramp.
        fd = os.open(tty, os.O_WRONLY | os.O_APPEND)
    except OSError:
        # TTY disappeared between resolve and write — nothing to do.
        return
    try:
        os.write(fd, f"\033]12;#{color_hex}\a".encode())
    except OSError as e:
        logger.debug("terminal-face: failed to write OSC 12 to %s: %s", tty, e)
    finally:
        os.close(fd)


def _run_ramp(tty: str, colors: list[str], generation: int) -> None:
    for color in colors:
        with _lock:
            if generation != _generation:
                return
        _write_osc(tty, color)
        time.sleep(RAMP_STEP_SECONDS)


def _set_state(color_hex: str) -> None:
    """Ramp the cursor color toward `color_hex`.

    Silently no-ops on gateway platforms or when no TTY can be found — this
    plugin must never break the agent loop.
    """
    global _generation, _target

    # Gate on platform from env (Hermes sets HERMES_PLATFORM in sessions).
    platform = os.environ.get("HERMES_PLATFORM", "cli")
    if platform not in TTY_PLATFORMS:
        return

    tty = _resolve_tty()
    if not tty:
        return

    with _lock:
        if color_hex == _target:
            return
        previous = _target
        _target = color_hex
        _generation += 1
        generation = _generation

    colors = _plan_ramp(previous, color_hex)
    if len(colors) == 1:
        _write_osc(tty, colors[0])
        return

    threading.Thread(
        target=_run_ramp,
        args=(tty, colors, generation),
        name="terminal-face-ramp",
        daemon=True,
    ).start()


def _settle(color_hex: str) -> None:
    """Jump to `color_hex` without a ramp (session boundaries)."""
    global _generation, _target

    # Same platform gate as _set_state: gateway sessions have no TTY to write to.
    platform = os.environ.get("HERMES_PLATFORM", "cli")
    if platform not in TTY_PLATFORMS:
        return

    with _lock:
        _generation += 1
        _target = color_hex

    tty = _resolve_tty()
    if tty:
        _write_osc(tty, color_hex)


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------


def on_pre_llm_call(user_message=None, platform=None, **kwargs):
    """User submitted a prompt — agent is now thinking/planning."""
    del user_message, kwargs
    _set_state(THINK)


def on_pre_tool_call(tool_name=None, args=None, **kwargs):
    """A tool is about to run. Mutating tools → WORK, everything else → THINK."""
    del args, kwargs
    _set_state(WORK if tool_name in WORK_TOOLS else THINK)


def on_post_tool_call(tool_name=None, result=None, **kwargs):
    """Tool returned. Error → ERR. Mutating-tool success → WORK. Reads → THINK."""
    if isinstance(result, str):
        # Cheap error sniff without a full JSON parse — Hermes tool results are
        # JSON strings; errors typically carry {"error": ...} or exit_code != 0.
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                if parsed.get("error"):
                    _set_state(ERR)
                    return
                # terminal tool reports exit_code on the result envelope.
                exit_code = parsed.get("exit_code")
                if isinstance(exit_code, int) and exit_code != 0:
                    _set_state(ERR)
                    return
        except (json.JSONDecodeError, TypeError):
            pass
    # Successful or unparseable. Mutating tools stay in WORK; reading/exploring
    # tools return to THINK so the face reflects "reasoning again."
    _set_state(WORK if tool_name in WORK_TOOLS else THINK)


def on_post_llm_call(assistant_response=None, **kwargs):
    """Turn finished — show the 'done' face."""
    del assistant_response, kwargs
    _set_state(DONE)


def on_session_end(**kwargs):
    """Session ended — reset to idle (the shader's resting face)."""
    del kwargs
    _settle(IDLE)


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
