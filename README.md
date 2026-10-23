# hermes-terminal-face

Agent plugins that mirror the agent's working state to the terminal cursor color (OSC 12), so [isoden/claude-terminal-face](https://github.com/isoden/claude-terminal-face)'s Ghostty custom shader can animate a robot face on the terminal background.

It started as a port of the original Claude Code integration to **[Hermes Agent](https://github.com/NousResearch/hermes-agent)**; the repository name is historical. It now ships the same integration for:

| Agent | Integration |
|---|---|
| **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** | in-process plugin (`__init__.py`) |
| **[Pi](https://pi.dev)** | Pi package (`extensions/terminal-face.ts`) |
| **Grok Build** (`grok`) | hook script + `~/.grok/hooks/*.json` |
| **Antigravity CLI** (`agy`) | hook script packaged as an `agy` plugin |
| **Claude Code** | the same hook script, if you are not using upstream's own hooks |

The shader itself is unchanged — only the hook layer is implemented per agent.

## How it works

```
Hermes plugin hooks ─┐
Pi extension events ─┼─▶ OSC 12 (cursor color) ─▶ Ghostty custom shader
Agent hooks (grok/agy/claude) ─┘
```

1. **Ghostty's custom shader draws the face.** You reference the original `claude-terminal-face-status.glsl` in `~/.config/ghostty/config`. This is pure terminal-side rendering; the agent is not involved.
2. **The plugin/extension/hooks fire on lifecycle events** and write a state-specific color to the terminal cursor via the OSC 12 escape sequence.
3. **The shader reads `iCurrentCursorColor`** and matches it against a 5-color palette (`idle` / `thinking` / `working` / `done` / `err`) to pick the face animation.

State changes are **ramped**: the shader is stateless and decodes the state from the current color with a distance-based softmax, so a direct jump between two key colors snaps the face. Every implementation sends intermediate colors over ~1s, spaced by the inverse of that softmax, so the expression morphs smoothly (the same technique as upstream's `claude-face-ramp.sh`).

## Palette

The palette must match the shader's `IDLE_KEY` / `THINK_KEY` / `WORK_KEY` / `DONE_KEY` / `ERR_KEY` constants (redesigned upstream on 2026-07-18). If you set this up with an older shader clone, `git pull` it — the previous palette no longer decodes.

| State | Color | Meaning |
|---|---|---|
| `idle` | `#5ce0c9` teal | nothing in flight, waiting for you |
| `thinking` | `#f7b81f` yellow | prompt submitted, model turn, read-only tool |
| `working` | `#41419c` blue | mutating tool (write / edit / bash / code) |
| `done` | `#0a9900` green | run settled successfully |
| `err` | `#e00000` red | tool error or failed run |

## Event → state mapping

### Pi (`extensions/terminal-face.ts`)

| Pi event | Color | Face state |
|---|---|---|
| `input`, `agent_start`, `turn_start` | `#f7b81f` yellow | thinking (user submitted, reasoning) |
| `tool_execution_start` (write/edit/bash/powershell/codemode) | `#41419c` blue | working (mutating) |
| `tool_execution_start` (read/grep/find/ls, read-only MCP tools) | `#f7b81f` yellow | thinking (investigating) |
| `tool_execution_end` (error) | `#e00000` red | err |
| `tool_execution_end` (success) | matches tool | working or thinking |
| `agent_settled` (`agent_before_settle` outcome `error`) | `#e00000` red | err |
| `agent_settled` (completed) | `#0a9900` green | done |
| `agent_settled` (aborted, e.g. Escape) | `#5ce0c9` teal | idle |
| `ui_prompt_start` / `ui_prompt_end` | `#5ce0c9` teal → back | idle while a dialog waits on you |
| `session_start`, `session_shutdown` | `#5ce0c9` teal | idle (reset) |

Nested tool calls (a codemode script calling tools) are ignored; the parent tool already describes the state.

### Hermes (`__init__.py`)

| Hermes hook | Color | Face state |
|---|---|---|
| `pre_llm_call` | `#f7b81f` yellow | thinking (user just submitted, reasoning) |
| `pre_tool_call` (write/terminal/exec) | `#41419c` blue | working (mutating) |
| `pre_tool_call` (read/search) | `#f7b81f` yellow | thinking (investigating) |
| `post_tool_call` (error) | `#e00000` red | err |
| `post_tool_call` (success) | matches tool | working or thinking |
| `post_llm_call` | `#0a9900` green | done (response complete) |
| `on_session_end` | `#5ce0c9` teal | idle (reset) |

Gateway platforms (Telegram, Discord, Slack, Matrix, …) have no controlling TTY and silently no-op. This integration is meaningful only in **CLI/TUI sessions** running inside a Ghostty terminal.

### Grok Build (`hooks/terminal-face-hook.py` + `~/.grok/hooks/terminal-face.json`)

| Grok hook | Color | Face state |
|---|---|---|
| `SessionStart`, `SessionEnd` | `#5ce0c9` teal | idle (reset) |
| `UserPromptSubmit` | `#f7b81f` yellow | thinking |
| `PreToolUse` (`run_terminal_command`, `search_replace`, `write`, …) | `#41419c` blue | working |
| `PreToolUse` (`read_file`, `grep`, `list_dir`, `web_search`, …) | `#f7b81f` yellow | thinking |
| `PostToolUse` with a non-zero `exit_code` / error | `#e00000` red | err |
| `PostToolUse` success | matches tool | working or thinking |
| `PostToolUseFailure`, `StopFailure` | `#e00000` red | err |
| `Stop` | `#0a9900` green | done (session-end `reason` is ignored) |
| `StopCancelled` | `#5ce0c9` teal | idle (interrupted) |
| `Notification` (`permission_prompt`) | `#5ce0c9` teal | idle (waiting on you) |

Subagent turn-end events are ignored, so a subagent finishing does not overwrite the main session's face.

### Antigravity CLI (`hooks/terminal-face-hook.py` as an `agy` plugin)

| `agy` hook | Color | Face state |
|---|---|---|
| `SessionStart` | `#5ce0c9` teal | idle (reset) |
| `PreInvocation` | `#f7b81f` yellow | thinking (model round starts) |
| `PreToolUse` (`run_command`, `write_file`, `replace_file_content`, …) | `#41419c` blue | working |
| `PreToolUse` (`read_file`, `view_file`, `grep_search`, …) | `#f7b81f` yellow | thinking |
| `PostToolUse` with an `error` | `#e00000` red | err |
| `PostToolUse` success | matches tool | working or thinking |
| `Stop` | `#0a9900` green | done |

`agy` does not pass command exit codes to `PostToolUse`, so a failing shell command stays in `working`; only tool-level errors turn the face red. There is no session-end hook either — the face keeps the last state until the next session's `SessionStart` resets it.

## Setup

### 1. Get the shader

Clone the upstream repo (the shader is not bundled here):

```bash
git clone https://github.com/isoden/claude-terminal-face.git ~/src/claude-terminal-face
```

### 2. Configure Ghostty

Append to `~/.config/ghostty/config`:

```ini
custom-shader = /Users/YOU/src/claude-terminal-face/claude-terminal-face-status.glsl
custom-shader-animation = always
```

Requires **Ghostty 1.2.0+** (custom shader support).

### 3. Install the integration for your agent

**Pi** — install the package (git source) or load it for one run:

```bash
pi install git:github.com/sashimi3433/hermes-terminal-face
# or, without touching settings:
pi --extension /path/to/hermes-terminal-face
```

`package.json` declares the extension under the `pi` key, so the next session picks it up. The extension lives in `extensions/terminal-face.ts`.

Then make pi show the hardware cursor. Ghostty only refreshes the shader's cursor-color uniform while the viewport has a cursor glyph, and pi hides the hardware cursor by default, so the face would never react:

```jsonc
// ~/.pi/agent/settings.json
"showHardwareCursor": true
```

or export `PI_HARDWARE_CURSOR=1` before starting pi. The extension shows a one-time warning when this is missing. The visible caret simply takes the current state color.

**Hermes** — clone into the plugins directory and enable it:

```bash
mkdir -p ~/.hermes/plugins
git clone https://github.com/sashimi3433/hermes-terminal-face.git ~/.hermes/plugins/terminal-face
hermes plugins enable terminal-face
```

**Grok Build** — install the hook file (it runs the script from this repository, so keep the clone around):

```bash
hooks/install.sh grok
```

That renders `~/.grok/hooks/terminal-face.json` with the absolute path to `hooks/terminal-face-hook.py`. Grok reloads hooks per session; `grok inspect` lists them (14 in the example below means this file plus upstream's Claude hooks).

Grok also reads `~/.claude/settings.json`, so if you already run the upstream Claude Code hooks, both fire. They send the same colors, but if you want a single writer, add to `~/.grok/config.toml`:

```toml
[compat.claude]
hooks = false
```

**Antigravity CLI** — install the plugin:

```bash
hooks/install.sh agy
```

That creates `~/.gemini/config/plugins/terminal-face/` (`plugin.json`, `hooks.json`, and the hook script). Verify with `agy plugin validate ~/.gemini/config/plugins/terminal-face`; `agy plugin list` then shows `terminal-face`.

Installing both at once: `hooks/install.sh` (or `hooks/install.sh --all`). `hooks/install.sh --uninstall` removes them again.

### 4. Run inside Ghostty

```bash
pi        # or: hermes, grok, agy
```

Start a new session (`/new`, `/reset`, or restart) and the face appears on the terminal background, reacting to the agent's state.

## Troubleshooting

- **Pi: the face never changes.** Pi hides the hardware cursor by default and Ghostty only updates the shader's cursor-color uniform while a cursor glyph exists. Set `"showHardwareCursor": true` in `~/.pi/agent/settings.json` (or `export PI_HARDWARE_CURSOR=1`) and restart pi. The extension warns once per process when this is missing.
- **Grok: colours double up.** Grok reads Claude Code hooks by default. If `~/.claude/settings.json` already runs upstream's `claude-face-hook.sh`, disable one source (`[compat.claude] hooks = false` in `~/.grok/config.toml`). The ramps are identical, so doubling is only wasted work.
- **Grok / Antigravity: nothing happens.** Run `TERMINAL_FACE_DEBUG=1` in the agent's environment to log hook resolutions to `$TMPDIR/terminal-face/hook.log` (Grok hooks may not inherit arbitrary env vars; set `env` on the hook if needed). The hook finds the terminal by walking parent processes, so the agent UI must own a TTY.
- **Override the write target.** All implementations write to the controlling TTY (`/dev/tty`). If that is not the terminal you are looking at (unusual wrappers, custom harnesses), pin it:
  - Pi: `export TERMINAL_FACE_TTY=/dev/ttys011`
  - Hermes: `export HERMES_FACE_TTY=/dev/ttys011` (or `TERMINAL_FACE_TTY`)
  - Grok / Antigravity / Claude hooks: `export TERMINAL_FACE_TTY=/dev/ttys011` (or set it in the hook's `env`)
  With the override set, Pi also stops gating on TUI mode, which helps in custom front-ends.
- **Old face colours / no reaction.** Update the shader clone; the palette changed upstream on 2026-07-18.
- **SSH.** OSC 12 tunnels transparently through an SSH PTY, so run the agent on the remote host and see the face in your local Ghostty. Only the *local* Ghostty needs the shader.
- **tmux / screen / multiplexers.** Whether OSC 12 reaches the outer terminal depends on the multiplexer's passthrough. If the face never changes, try running without it.

## Files

- `extensions/terminal-face.ts` — Pi extension (events → OSC 12, with ramped transitions)
- `package.json` — Pi package manifest (`pi.extensions`)
- `plugin.yaml` — Hermes plugin manifest
- `__init__.py` — Hermes hook callbacks → OSC 12 cursor color writes
- `hooks/terminal-face-hook.py` — external hook script for Grok Build / Antigravity CLI / Claude Code
- `hooks/install.sh` — installs and removes the grok hook file and the `agy` plugin
- `hooks/grok/`, `hooks/antigravity/` — hook manifests used by the installer
- `test/terminal-face.test.ts` — Pi palette/ramp and lifecycle tests (`npm test`)
- `test/test_terminal_face_hook.py` — hook script tests (`python3 -m unittest discover -s test -p 'test_*.py'`)

The shader (`claude-terminal-face-status.glsl`) is **not** included — get it from the upstream repo.

## Credits

- **Shader and original concept:** [@isoden](https://github.com/isoden) — [claude-terminal-face](https://github.com/isoden/claude-terminal-face)
- **Hermes plugin port:** this repo
- **Pi extension and shared ramp implementation:** this repo
- **Grok Build / Antigravity CLI / Claude Code hooks:** this repo

## License

MIT — same as the upstream project.
