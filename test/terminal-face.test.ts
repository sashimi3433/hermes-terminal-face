/**
 * Tests for the Pi extension in extensions/terminal-face.ts.
 *
 * Run with: node --test test/terminal-face.test.ts
 *
 * The palette math is pure; the lifecycle test drives the extension through a
 * fake `pi` API, points TERMINAL_FACE_TTY at a scratch file and asserts on the
 * OSC 12 bytes that would have gone to a real terminal.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import terminalFace, { COLORS, planRamp } from "../extensions/terminal-face.ts";

const OSC12 = /\x1b\]12;#([0-9a-f]{6})\x07/g;

type Rgb = [number, number, number];

function parseHex(hex: string): Rgb {
	const value = Number.parseInt(hex, 16);
	return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff];
}

function normalize(hex: string): Rgb {
	return parseHex(hex).map((channel) => channel / 255) as Rgb;
}

function distanceSq(a: Rgb, b: Rgb): number {
	return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
}

/** Shader decode (claude-terminal-face-status.glsl): softmax over key colors. */
function decode(hex: string): Record<keyof typeof COLORS, number> {
	const color = normalize(hex);
	const distances = Object.entries(COLORS).map(
		([state, key]) => [state, distanceSq(color, normalize(key))] as const,
	);
	const min = Math.min(...distances.map(([, distance]) => distance));
	const weights = distances.map(
		([state, distance]) => [state, Math.exp(-(distance - min) / 0.03)] as const,
	);
	const total = weights.reduce((sum, [, weight]) => sum + weight, 0);
	return Object.fromEntries(weights.map(([state, weight]) => [state, weight / total])) as Record<
		keyof typeof COLORS,
		number
	>;
}

function readColors(file: string): string[] {
	return [...fs.readFileSync(file, "utf8").matchAll(OSC12)].map((match) => match[1]);
}

const wait = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Wait until the ramp stopped writing (no new colors for 60ms). */
async function waitForRamp(tty: string, timeoutMs = 2000): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	let lastCount = readColors(tty).length;
	let stableSince = Date.now();
	while (Date.now() < deadline) {
		await wait(10);
		const count = readColors(tty).length;
		if (count !== lastCount) {
			lastCount = count;
			stableSince = Date.now();
		} else if (Date.now() - stableSince > 60) {
			return;
		}
	}
	assert.fail("ramp did not settle");
}

test("planRamp sends the target as-is when there is no previous state", () => {
	assert.deepEqual(planRamp(null, COLORS.think), [COLORS.think]);
	assert.deepEqual(planRamp(undefined, COLORS.think), [COLORS.think]);
});

test("planRamp skips ramps between identical colors", () => {
	assert.deepEqual(planRamp(COLORS.work, COLORS.work), [COLORS.work]);
});

test("planRamp ends exactly on the key color", () => {
	const steps = planRamp(COLORS.think, COLORS.work);
	assert.equal(steps.length, 31);
	assert.equal(steps.at(-1), COLORS.work);
	assert.notEqual(steps[0], COLORS.work, "should start away from the target");
});

test("planRamp stays on the RGB line between the two key colors", () => {
	const from = parseHex(COLORS.think);
	const to = parseHex(COLORS.work);
	for (const step of planRamp(COLORS.think, COLORS.work)) {
		parseHex(step).forEach((channel, index) => {
			const low = Math.min(from[index], to[index]);
			const high = Math.max(from[index], to[index]);
			assert.ok(channel >= low && channel <= high, `${step} channel ${index} off the line`);
		});
	}
});

test("planRamp moves monotonically toward the target", () => {
	const target = normalize(COLORS.work);
	const steps = planRamp(COLORS.think, COLORS.work).map((step) =>
		distanceSq(normalize(step), target),
	);
	for (let i = 1; i < steps.length; i++) {
		assert.ok(steps[i] <= steps[i - 1] + 1e-9, `distance grew at step ${i}`);
	}
	assert.ok(steps.at(-1) === 0);
});

test("think → work ramp decodes as only think and work", () => {
	// Mid-ramp colors must not leak into other states, otherwise the face
	// morphs through a third expression (the reason for the upstream palette).
	for (const step of planRamp(COLORS.think, COLORS.work)) {
		const weights = decode(step);
		assert.ok(weights.think + weights.work > 0.99, `${step} leaks: ${JSON.stringify(weights)}`);
	}
});

test("ramp midpoint weight follows a smoothstep, not a snap", () => {
	const steps = planRamp(COLORS.think, COLORS.work).map((step) => decode(step).work);
	const midpoint = steps[Math.floor(steps.length / 2)];
	assert.ok(midpoint > 0.2 && midpoint < 0.8, `midpoint weight ${midpoint} is a snap`);
});

function createHarness(stepMs: number) {
	const handlers = new Map<string, ((event: unknown, ctx: unknown) => unknown)[]>();
	const pi = {
		getAllTools: () => [],
		on: (event: string, handler: (event: unknown, ctx: unknown) => unknown) => {
			handlers.set(event, [...(handlers.get(event) ?? []), handler]);
			return () => {};
		},
	};
	const ctx = { mode: "tui" };
	terminalFace(pi as never, { stepMs });

	return async (event: string, payload: Record<string, unknown> = {}): Promise<void> => {
		for (const handler of handlers.get(event) ?? []) {
			await handler({ type: event, ...payload }, ctx);
		}
	};
}

test("lifecycle events drive the cursor color", async () => {
	const tty = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "terminal-face-")), "tty");
	fs.writeFileSync(tty, "");
	process.env.TERMINAL_FACE_TTY = tty;

	try {
		const dispatch = createHarness(1);
		const settle = () => waitForRamp(tty);

		await dispatch("session_start");
		assert.equal(readColors(tty).at(-1), COLORS.idle, "session start should be idle");

		await dispatch("agent_start");
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.think, "agent start should be thinking");
		assert.ok(readColors(tty).length > 20, "state change should ramp, not jump");

		const beforeRepeat = readColors(tty).length;
		await dispatch("agent_start");
		await wait(20);
		assert.equal(readColors(tty).length, beforeRepeat, "same state must not rewrite");

		await dispatch("tool_execution_start", { toolCallId: "c1", toolName: "bash" });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.work, "bash is work");

		const beforeNested = readColors(tty).length;
		await dispatch("tool_execution_start", {
			toolCallId: "c1/1",
			toolName: "read",
			parentToolCallId: "c1",
		});
		await wait(20);
		assert.equal(readColors(tty).length, beforeNested, "nested calls must be ignored");

		await dispatch("tool_execution_end", { toolCallId: "c1", toolName: "bash", isError: true });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.err, "tool error is err");

		await dispatch("tool_execution_start", { toolCallId: "c2", toolName: "read" });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.think, "read stays in think");

		// Parallel batch: a finishing read must not pull the face out of work.
		await dispatch("tool_execution_start", { toolCallId: "c3", toolName: "bash" });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.work, "bash in a parallel batch is work");

		await dispatch("tool_execution_end", { toolCallId: "c2", toolName: "read", isError: false });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.work, "a sibling read end keeps work");

		await dispatch("tool_execution_end", { toolCallId: "c3", toolName: "bash", isError: false });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.think, "last tool end returns to think");

		await dispatch("ui_prompt_start");
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.idle, "a dialog means waiting on you");

		await dispatch("ui_prompt_end");
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.think, "dialog close resumes the state");

		await dispatch("agent_before_settle", { outcome: "completed" });
		await dispatch("agent_settled", { aborted: false });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.done, "settled run is done");

		await dispatch("agent_start");
		await dispatch("agent_before_settle", { outcome: "error" });
		await dispatch("agent_settled", { aborted: false });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.err, "failed run is err");

		await dispatch("agent_start");
		await dispatch("agent_settled", { aborted: true });
		await settle();
		assert.equal(readColors(tty).at(-1), COLORS.idle, "aborted run returns to idle");

		await dispatch("session_shutdown");
		assert.equal(readColors(tty).at(-1), COLORS.idle, "shutdown resets to idle");
	} finally {
		delete process.env.TERMINAL_FACE_TTY;
		fs.rmSync(path.dirname(tty), { recursive: true, force: true });
	}
});

test("warns once when pi hides the hardware cursor", async () => {
	const tty = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "terminal-face-")), "tty");
	fs.writeFileSync(tty, "");
	process.env.TERMINAL_FACE_TTY = tty;

	try {
		const makeHarness = (settings: { showHardwareCursor?: boolean }) => {
			const notifications: string[] = [];
			const handlers = new Map<string, ((event: unknown, ctx: unknown) => unknown)[]>();
			const pi = {
				getAllTools: () => [],
				getSettings: () => settings,
				on: (event: string, handler: (event: unknown, ctx: unknown) => unknown) => {
					handlers.set(event, [...(handlers.get(event) ?? []), handler]);
					return () => {};
				},
			};
			const ctx = { mode: "tui", ui: { notify: (message: string) => notifications.push(message) } };
			terminalFace(pi as never, { stepMs: 1 });
			return {
				notifications,
				dispatch: async (event: string) => {
					for (const handler of handlers.get(event) ?? []) {
						await handler({ type: event }, ctx);
					}
				},
			};
		};

		const enabled = makeHarness({ showHardwareCursor: true });
		await enabled.dispatch("session_start");
		assert.deepEqual(enabled.notifications, [], "no warning when the cursor is visible");

		const missing = makeHarness({});
		await missing.dispatch("session_start");
		await missing.dispatch("session_start");
		assert.equal(missing.notifications.length, 1, "warn exactly once");
		assert.match(missing.notifications[0], /showHardwareCursor/);
	} finally {
		delete process.env.TERMINAL_FACE_TTY;
		fs.rmSync(path.dirname(tty), { recursive: true, force: true });
	}
});
