/**
 * Unit tests for the client-side auto barge-in gate (Johnny-trt.9).
 *
 * The gate is the pure state machine `startBrowserAudioSession` runs on
 * every 20 ms capture frame while the bot is speaking: RMS >= 0.02 AND
 * peak >= 0.08 for 2+ consecutive frames fires the local interrupt. Pin
 * the thresholds, the consecutive-frame persistence, and the reset
 * semantics so a refactor can't silently turn the gate trigger-happy
 * (self-interrupts on room noise) or deaf (no barge-in). Run via
 * `pnpm test` (vitest).
 */

import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import {
	BARGE_IN_PEAK_THRESHOLD,
	BARGE_IN_RMS_THRESHOLD,
	BARGE_IN_TRIGGER_FRAMES,
	createBargeInGate,
	pcm16FrameLevels
} from '$lib/browserAudio';

/** A frame comfortably above both thresholds. */
const LOUD = { rms: 0.05, peak: 0.2 };
/** A frame comfortably below both thresholds (room noise / silence). */
const QUIET = { rms: 0.005, peak: 0.02 };

describe('createBargeInGate', () => {
	it('pins the bead-specified defaults', () => {
		assert.equal(BARGE_IN_RMS_THRESHOLD, 0.02);
		assert.equal(BARGE_IN_PEAK_THRESHOLD, 0.08);
		assert.equal(BARGE_IN_TRIGGER_FRAMES, 2);
	});

	it('fires on the second consecutive qualifying frame, not the first', () => {
		const gate = createBargeInGate();
		assert.equal(gate.push(LOUD.rms, LOUD.peak), false);
		assert.equal(gate.push(LOUD.rms, LOUD.peak), true);
	});

	it('treats thresholds as inclusive (>=)', () => {
		const gate = createBargeInGate();
		assert.equal(gate.push(BARGE_IN_RMS_THRESHOLD, BARGE_IN_PEAK_THRESHOLD), false);
		assert.equal(gate.push(BARGE_IN_RMS_THRESHOLD, BARGE_IN_PEAK_THRESHOLD), true);
	});

	it('requires BOTH thresholds — high RMS with low peak never fires', () => {
		const gate = createBargeInGate();
		for (let i = 0; i < 10; i++) {
			assert.equal(gate.push(0.5, BARGE_IN_PEAK_THRESHOLD - 0.001), false);
		}
	});

	it('requires BOTH thresholds — high peak with low RMS never fires', () => {
		const gate = createBargeInGate();
		for (let i = 0; i < 10; i++) {
			assert.equal(gate.push(BARGE_IN_RMS_THRESHOLD - 0.001, 0.9), false);
		}
	});

	it('a quiet frame resets the consecutive run', () => {
		const gate = createBargeInGate();
		assert.equal(gate.push(LOUD.rms, LOUD.peak), false);
		assert.equal(gate.push(QUIET.rms, QUIET.peak), false);
		// The earlier loud frame must not count any more: one more loud
		// frame is a fresh run of 1, not a completed run of 2.
		assert.equal(gate.push(LOUD.rms, LOUD.peak), false);
		assert.equal(gate.push(LOUD.rms, LOUD.peak), true);
	});

	it('sustained sub-threshold noise never fires (60 s at 20 ms frames)', () => {
		const gate = createBargeInGate();
		for (let i = 0; i < 3000; i++) {
			assert.equal(gate.push(QUIET.rms, QUIET.peak), false);
		}
	});

	it('after firing, the count restarts — continued speech fires again only after a fresh run', () => {
		const gate = createBargeInGate();
		gate.push(LOUD.rms, LOUD.peak);
		assert.equal(gate.push(LOUD.rms, LOUD.peak), true);
		assert.equal(gate.push(LOUD.rms, LOUD.peak), false);
		assert.equal(gate.push(LOUD.rms, LOUD.peak), true);
	});

	it('reset() drops accumulated progress', () => {
		const gate = createBargeInGate();
		gate.push(LOUD.rms, LOUD.peak);
		gate.reset();
		assert.equal(gate.push(LOUD.rms, LOUD.peak), false);
		assert.equal(gate.push(LOUD.rms, LOUD.peak), true);
	});

	it('honors custom thresholds and frame counts', () => {
		const gate = createBargeInGate({ rmsThreshold: 0.1, peakThreshold: 0.3, triggerFrames: 3 });
		// Default-loud is below the custom thresholds.
		for (let i = 0; i < 5; i++) {
			assert.equal(gate.push(LOUD.rms, LOUD.peak), false);
		}
		assert.equal(gate.push(0.2, 0.5), false);
		assert.equal(gate.push(0.2, 0.5), false);
		assert.equal(gate.push(0.2, 0.5), true);
	});

	it('clamps triggerFrames to at least 1', () => {
		const gate = createBargeInGate({ triggerFrames: 0 });
		assert.equal(gate.push(LOUD.rms, LOUD.peak), true);
	});
});

describe('pcm16FrameLevels', () => {
	it('returns zeros for an empty frame', () => {
		assert.deepEqual(pcm16FrameLevels(new Int16Array(0)), { rms: 0, peak: 0 });
	});

	it('returns zeros for digital silence', () => {
		assert.deepEqual(pcm16FrameLevels(new Int16Array(320)), { rms: 0, peak: 0 });
	});

	it('full-scale DC measures rms = peak = 1', () => {
		const pcm = new Int16Array(320).fill(0x7fff);
		const { rms, peak } = pcm16FrameLevels(pcm);
		assert.ok(Math.abs(rms - 1) < 1e-6);
		assert.ok(Math.abs(peak - 1) < 1e-6);
	});

	it('normalizes negative samples against 0x8000', () => {
		const pcm = new Int16Array(320).fill(-0x8000);
		const { rms, peak } = pcm16FrameLevels(pcm);
		assert.ok(Math.abs(rms - 1) < 1e-6);
		assert.ok(Math.abs(peak - 1) < 1e-6);
	});

	it('a half-scale sine measures rms ≈ amplitude/√2 and peak ≈ amplitude', () => {
		const amplitude = 0.5;
		const pcm = new Int16Array(320);
		for (let i = 0; i < pcm.length; i++) {
			// 8 full cycles over the frame so the RMS estimate is exact-ish.
			pcm[i] = Math.round(Math.sin((2 * Math.PI * 8 * i) / pcm.length) * amplitude * 0x7fff);
		}
		const { rms, peak } = pcm16FrameLevels(pcm);
		assert.ok(Math.abs(rms - amplitude / Math.SQRT2) < 0.01, `rms ${rms}`);
		assert.ok(Math.abs(peak - amplitude) < 0.01, `peak ${peak}`);
	});

	it('a typical speech-level frame clears both gate thresholds', () => {
		// ~0.15 amplitude sine — quiet speech close to the mic.
		const pcm = new Int16Array(320);
		for (let i = 0; i < pcm.length; i++) {
			pcm[i] = Math.round(Math.sin((2 * Math.PI * 8 * i) / pcm.length) * 0.15 * 0x7fff);
		}
		const { rms, peak } = pcm16FrameLevels(pcm);
		assert.ok(rms >= BARGE_IN_RMS_THRESHOLD);
		assert.ok(peak >= BARGE_IN_PEAK_THRESHOLD);
	});
});
