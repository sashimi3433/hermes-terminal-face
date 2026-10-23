# hermes-terminal-face

Agent plugins that mirror the agent's working state to the terminal cursor color (OSC 12), so [isoden/claude-terminal-face](https://github.com/isoden/claude-terminal-face)'s Ghostty custom shader can animate a robot face on the terminal background.

It started as a port of the original Claude Code integration to **[Hermes Agent](https://github.com/NousResearch/hermes-agent)**; the repository name is historical. It now ships the same integration for **[Pi](https://pi.dev)** as a Pi package. The shader itself is unchanged — only the hook layer is implemented per agent.

## How it works

```
Hermes plugin hooks ─┐
                     ├─▶ OSC 12 (cursor color) ─▶ Ghostty custom shader
Pi extension events ─┘
```

1. **Ghostty's custom shader draws the face.** You reference the original `claude-terminal-face-status.glsl` in `~/.config/ghostty/config`. This is pure terminal-side rendering; the agent is not involved.
2. **The plugin/extension hooks fire on lifecycle events** and write a state-specific color to the terminal cursor via the OSC 12 escape sequence.
3. **The shader reads `iCurrentCursorColor`** and matches it against a 5-color palette (`idle` / `thinking` / `working` / `done` / `err`) to pick the face animation.

State changes are **ramped**: the shader is stateless and decodes the state from the current color with a distance-based softmax, so a direct jump between two key colors snaps the face. Both implementations send intermediate colors over ~1s, spaced by the inverse of that softmax, so the expression morphs smoothly (the same technique as upstream's `claude-face-ramp.sh`).

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

### 4. Run inside Ghostty

```bash
pi        # or: hermes
```

Start a new session (`/new`, `/reset`, or restart) and the face appears on the terminal background, reacting to the agent's state.

## Troubleshooting

- **Pi: the face never changes.** Pi hides the hardware cursor by default and Ghostty only updates the shader's cursor-color uniform while a cursor glyph exists. Set `"showHardwareCursor": true` in `~/.pi/agent/settings.json` (or `export PI_HARDWARE_CURSOR=1`) and restart pi. The extension warns once per process when this is missing.
- **Override the write target.** Both implementations write to the controlling TTY (`/dev/tty`). If that is not the terminal you are looking at (unusual wrappers, custom harnesses), pin it:
  - Pi: `export TERMINAL_FACE_TTY=/dev/ttys011`
  - Hermes: `export HERMES_FACE_TTY=/dev/ttys011` (or `TERMINAL_FACE_TTY`)
  With the override set, Pi also stops gating on TUI mode, which helps in custom front-ends.
- **Old face colours / no reaction.** Update the shader clone; the palette changed upstream on 2026-07-18.
- **SSH.** OSC 12 tunnels transparently through an SSH PTY, so run the agent on the remote host and see the face in your local Ghostty. Only the *local* Ghostty needs the shader.
- **tmux / screen / multiplexers.** Whether OSC 12 reaches the outer terminal depends on the multiplexer's passthrough. If the face never changes, try running without it.

## Files

- `extensions/terminal-face.ts` — Pi extension (events → OSC 12, with ramped transitions)
- `package.json` — Pi package manifest (`pi.extensions`)
- `plugin.yaml` — Hermes plugin manifest
- `__init__.py` — Hermes hook callbacks → OSC 12 cursor color writes
- `test/terminal-face.test.ts` — palette/ramp and lifecycle tests (`npm test`)

The shader (`claude-terminal-face-status.glsl`) is **not** included — get it from the upstream repo.

## Credits

- **Shader and original concept:** [@isoden](https://github.com/isoden) — [claude-terminal-face](https://github.com/isoden/claude-terminal-face)
- **Hermes plugin port:** this repo
- **Pi extension and shared ramp implementation:** this repo

## License

MIT — same as the upstream project.
