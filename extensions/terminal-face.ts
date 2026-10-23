/**
 * terminal-face — Pi extension that mirrors the agent's working state to the
 * terminal cursor color (OSC 12), so isoden/claude-terminal-face's Ghostty
 * custom shader can animate a robot face on the terminal background.
 *
 * Sister implementation of the Hermes plugin in this repository (`__init__.py`
 * + `plugin.yaml`). The shader is unchanged; this file only supplies the
 * state → color side channel.
 *
 *   THINK  #f7b81f  prompt submitted, model turn started, read-only tool
 *   WORK   #41419c  mutating tool (bash / write / edit / codemode / ...)
 *   DONE   #0a9900  run settled successfully
 *   ERR    #e00000  tool error, or the run ended with an error
 *   IDLE   #5ce0c9  nothing in flight, waiting for you, session ended
 *
 * The palette must match IDLE_KEY / THINK_KEY / WORK_KEY / DONE_KEY / ERR_KEY
 * in claude-terminal-face-status.glsl (and claude-face-hook.sh).
 *
 * Why ramps: the shader is stateless and decodes the state from the *current*
 * cursor color with a distance-based softmax, so jumping straight from one key
 * color to another snaps the face. Sending intermediate colors over ~1s gives
 * the shader a time axis. Their positions on the RGB line are the inverse of
 * the softmax, so the decoded weight follows a smoothstep (same easing as the
 * in-state morph). Mirrors claude-face-ramp.sh from the upstream repo.
 *
 * Notes:
 *   - Writes go to the controlling TTY (`/dev/tty`), never stdout, so piped or
 *     JSON/print/RPC runs cannot pollute their output stream.
 *   - TUI only: the shader only exists where a Ghostty window is attached.
 *   - Nested tool calls (codemode scripts calling tools) are ignored; the
 *     parent tool already owns the state.
 *
 * Load with `pi --extension ./extensions/terminal-face.ts`, or install the
 * package with `pi install git:github.com/sashimi3433/hermes-terminal-face`.
 */

import fs from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// --- State palette (must match the GLSL shader's keys exactly) --------------
const IDLE = "5ce0c9";
const THINK = "f7b81f";
const WORK = "41419c";
const DONE = "0a9900";
const ERR = "e00000";

export const COLORS = { idle: IDLE, think: THINK, work: WORK, done: DONE, err: ERR } as const;

/** Shader constant: softmax sharpness used to decode color → state weights. */
const STATE_SOFT = 0.03;
/** Ramp shape from claude-face-ramp.sh: color-only head, morphing body, tail. */
const RAMP_HEAD = 5;
const RAMP_BODY = 21;
const RAMP_TAIL = 5;
/** Effective step spacing in the upstream bash ramp (≈30Hz). */
const RAMP_STEP_MS = 30;

/** Built-in tools whose effect is a mutation of the environment. */
const MUTATING_TOOLS = new Set(["bash", "codemode", "edit", "powershell", "write"]);

type Rgb = [number, number, number];

function clamp01(value: number): number {
	return value < 0 ? 0 : value > 1 ? 1 : value;
}

function parseHex(hex: string): Rgb {
	const value = Number.parseInt(hex.replace(/^#/, ""), 16);
	return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff];
}

function formatHex(rgb: Rgb): string {
	return rgb.map((channel) => Math.round(channel).toString(16).padStart(2, "0")).join("");
}

/**
 * Colors to send, in order, when moving the cursor from `from` to `to`.
 *
 * `from` is the last *target* color. Without one (first state of a session)
 * the target is sent as-is — interpolating from an unknown color looks worse
 * than a clean cut.
 */
export function planRamp(from: string | null | undefined, to: string): string[] {
	if (!from || from.toLowerCase() === to.toLowerCase()) {
		return [to];
	}

	const source = parseHex(from);
	const target = parseHex(to);

	// Shader: w_to(s) = sigmoid(K * (2s - 1)), K = |key distance|² / STATE_SOFT.
	const distanceSq = source.reduce(
		(sum, channel, index) => sum + ((target[index] - channel) / 255) ** 2,
		0,
	);
	const k = Math.max(distanceSq / STATE_SOFT, 1e-6);

	// Weight range where the face is still (nearly) the source / target state.
	const wMin = 0.01;
	const logitMin = Math.log(wMin / (1 - wMin));
	const sLo = clamp01(0.5 + logitMin / (2 * k));
	const sHi = clamp01(0.5 - logitMin / (2 * k));

	const mix = (position: number): string =>
		formatHex([
			source[0] + (target[0] - source[0]) * clamp01(position),
			source[1] + (target[1] - source[1]) * clamp01(position),
			source[2] + (target[2] - source[2]) * clamp01(position),
		]);

	const steps: string[] = [];
	// Head: move the visible cursor color toward the body range; the face holds.
	for (let i = 1; i <= RAMP_HEAD; i++) {
		steps.push(mix((sLo * i) / RAMP_HEAD));
	}
	// Body: invert the softmax so the decoded weight follows a smoothstep.
	for (let i = 1; i <= RAMP_BODY; i++) {
		const p = i / RAMP_BODY;
		const w = wMin + (1 - 2 * wMin) * (p * p * (3 - 2 * p));
		steps.push(mix(0.5 + Math.log(w / (1 - w)) / (2 * k)));
	}
	// Tail: close the remaining color gap to the exact key color.
	for (let i = 1; i < RAMP_TAIL; i++) {
		steps.push(mix(sHi + ((1 - sHi) * i) / RAMP_TAIL));
	}
	// End on the key color exactly, so the next ramp starts where we recorded it.
	steps.push(to);

	return steps;
}

export interface TerminalFaceOptions {
	/** Ramp step spacing. Overridden in tests; production uses RAMP_STEP_MS. */
	stepMs?: number;
}

export default function terminalFace(pi: ExtensionAPI, options: TerminalFaceOptions = {}): void {
	const stepMs = options.stepMs ?? RAMP_STEP_MS;

	let fd: number | null = null;
	let ownsFd = false;
	let enabled = false;
	let target: string | null = null;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let generation = 0;
	let resumeState: string | null = null;
	let outcome: "completed" | "aborted" | "error" | undefined;
	// toolCallId → whether that call mutates the environment. Parallel tool
	// calls are common, so the state is "work while any mutating call runs".
	const activeTools = new Map<string, boolean>();

	const ttyOverride = (): string | undefined =>
		process.env.TERMINAL_FACE_TTY || process.env.HERMES_FACE_TTY || undefined;

	/** Write target: override → controlling TTY → stdout when it is a TTY. */
	function ttyFd(): number | null {
		if (fd !== null) return fd;

		const override = ttyOverride();
		if (override) {
			try {
				fd = fs.openSync(override, "w");
				ownsFd = true;
				return fd;
			} catch {
				// Fall through to the controlling TTY.
			}
		}

		if (process.platform !== "win32") {
			try {
				fd = fs.openSync("/dev/tty", "w");
				ownsFd = true;
				return fd;
			} catch {
				// No controlling terminal (RPC/JSON/print run without one).
			}
		}

		if (process.stdout?.isTTY) {
			fd = process.stdout.fd;
			ownsFd = false;
			return fd;
		}

		return null;
	}

	function write(hex: string): void {
		const handle = ttyFd();
		if (handle === null) return;
		try {
			fs.writeSync(handle, `\x1b]12;#${hex}\x07`);
		} catch {
			// The terminal disappeared between resolve and write.
		}
	}

	function cancelRamp(): void {
		if (timer) {
			clearTimeout(timer);
			timer = null;
		}
		generation++;
	}

	/** Move toward `next` with the upstream ramp; no-op when already there. */
	function rampTo(next: string): void {
		if (!enabled || next === target) return;
		if (ttyFd() === null) return;

		const previous = target;
		target = next;
		cancelRamp();
		const myGeneration = generation;
		const steps = planRamp(previous, next);
		let index = 0;

		const step = (): void => {
			if (myGeneration !== generation) return;
			write(steps[index++]);
			if (index < steps.length) {
				timer = setTimeout(step, stepMs);
				(timer as { unref?: () => void }).unref?.();
			} else {
				timer = null;
			}
		};

		step();
	}

	/** Jump straight to `next` (session boundaries), without a ramp. */
	function settle(next: string): void {
		if (!enabled) return;
		cancelRamp();
		target = next;
		write(next);
	}

	function isMutating(toolName: string): boolean {
		if (MUTATING_TOOLS.has(toolName)) return true;

		let annotations: { readOnlyHint?: boolean } | undefined;
		try {
			annotations = pi.getAllTools().find((tool) => tool.name === toolName)?.annotations;
		} catch {
			return false;
		}
		// Explicit non-read-only hints mean work; unknown read-only-ish tools
		// (read/grep/find/ls, unannotated MCP tools) stay in the thinking state.
		return annotations ? annotations.readOnlyHint !== true : false;
	}

	/** Work while any mutating call is in flight, otherwise thinking. */
	function toolState(): string {
		for (const mutates of activeTools.values()) {
			if (mutates) return WORK;
		}
		return THINK;
	}

	function resetRunState(): void {
		outcome = undefined;
		activeTools.clear();
	}

	// --- Lifecycle → state ---------------------------------------------------

	pi.on("session_start", (_event, ctx) => {
		enabled = ctx.mode === "tui" || Boolean(ttyOverride());
		resetRunState();
		resumeState = null;
		settle(IDLE);
	});

	pi.on("input", () => rampTo(toolState()));
	pi.on("agent_start", () => {
		resetRunState();
		rampTo(THINK);
	});
	pi.on("turn_start", () => rampTo(toolState()));

	pi.on("tool_execution_start", (event) => {
		// Nested calls run inside a parent tool (e.g. a codemode script); the
		// parent's own start/end already describe the state.
		if (event.parentToolCallId) return;
		activeTools.set(event.toolCallId, isMutating(event.toolName));
		rampTo(toolState());
	});

	pi.on("tool_execution_end", (event) => {
		if (event.parentToolCallId) return;
		activeTools.delete(event.toolCallId);
		rampTo(event.isError ? ERR : toolState());
	});

	pi.on("agent_before_settle", (event) => {
		outcome = event.outcome;
	});

	pi.on("agent_settled", (event) => {
		if (event.aborted) {
			rampTo(IDLE);
		} else if (outcome === "error") {
			rampTo(ERR);
		} else {
			rampTo(DONE);
		}
		outcome = undefined;
	});

	// A dialog (permission gate, model picker, …) means you are being waited on.
	pi.on("ui_prompt_start", () => {
		if (resumeState === null) resumeState = target ?? THINK;
		rampTo(IDLE);
	});

	pi.on("ui_prompt_end", () => {
		const resume = resumeState;
		resumeState = null;
		if (resume) rampTo(resume);
	});

	pi.on("session_shutdown", () => {
		settle(IDLE);
		if (fd !== null && ownsFd) {
			try {
				fs.closeSync(fd);
			} catch {
				// Already closed.
			}
		}
		fd = null;
		ownsFd = false;
		enabled = false;
		resetRunState();
	});
}
