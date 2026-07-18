# hermes-terminal-face

Hermes Agent plugin that mirrors the agent's working state to the terminal cursor color (OSC 12), so [isoden/claude-terminal-face](https://github.com/isoden/claude-terminal-face)'s Ghostty custom shader can animate a robot face on the terminal background.

This is a port of the original Claude Code integration to **[Hermes Agent](https://github.com/NousResearch/hermes-agent)**. The shader itself is unchanged — only the hook layer is re-implemented as a Hermes plugin.

## How it works

```
Hermes plugin hooks ──▶ OSC 12 (cursor color) ──▶ Ghostty custom shader
```

1. **Ghostty's custom shader draws the face.** You reference the original `claude-terminal-face-status.glsl` in `~/.config/ghostty/config`. This is pure terminal-side rendering; Hermes is not involved.
2. **Hermes plugin hooks fire on lifecycle events** and write a state-specific color to the terminal cursor via the OSC 12 escape sequence.
3. **The shader reads `iCurrentCursorColor`** and matches it against a 5-color palette (`idle` / `thinking` / `working` / `done` / `err`) to pick the face animation.

## Hook → state mapping

| Hermes hook | Color | Face state |
|---|---|---|
| `pre_llm_call` | `#f0c14b` yellow | thinking (user just submitted, reasoning) |
| `pre_tool_call` (write/terminal/exec) | `#3d6fe0` blue | working (mutating) |
| `pre_tool_call` (read/search) | `#f0c14b` yellow | thinking (investigating) |
| `post_tool_call` (error) | `#ff5f6d` red | err |
| `post_tool_call` (success) | matches tool | working or thinking |
| `post_llm_call` | `#8bd346` green | done (response complete) |
| `on_session_end` | `#888888` gray | idle (reset) |

Gateway platforms (Telegram, Discord, Slack, Matrix, …) have no controlling TTY and silently no-op. This plugin is meaningful only in **CLI/TUI sessions** running inside a Ghostty terminal.

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

### 3. Install this plugin

```bash
mkdir -p ~/.hermes/plugins
git clone https://github.com/sashimi3433/hermes-terminal-face.git ~/.hermes/plugins/terminal-face
```

### 4. Enable

```bash
hermes plugins enable terminal-face
```

Start a new session (`/reset` in an existing one) and run Hermes inside Ghostty:

```bash
hermes
```

The face appears on the terminal background and reacts to the agent's state.

## SSH usage

OSC 12 escape sequences tunnel transparently through SSH PTY, so you can run Hermes on a remote machine and see the face on your local Ghostty:

```bash
# From your local Ghostty window
ssh user@remote-host
hermes
```

The face renders locally; only the Hermes plugin needs to run on the remote. Just make sure your *local* Ghostty has the shader configured.

## Files

- `plugin.yaml` — Hermes plugin manifest
- `__init__.py` — Hook callbacks → OSC 12 cursor color writes

The shader (`claude-terminal-face-status.glsl`) is **not** included — get it from the upstream repo.

## Credits

- **Shader and original concept:** [@isoden](https://github.com/isoden) — [claude-terminal-face](https://github.com/isoden/claude-terminal-face)
- **Hermes plugin port:** this repo

## License

MIT — same as the upstream project.
