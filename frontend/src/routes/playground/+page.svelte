<script lang="ts">
	import { onDestroy } from 'svelte';
	import {
		audioWebSocketUrl,
		postBrowserText,
		startBrowserSession,
		stopBrowserSession,
		type BrowserSession
	} from '$lib/browserSessions';
	import {
		startBrowserAudioSession,
		type BrowserAudioSession
	} from '$lib/browserAudio';

	let persona = $state('Concise, friendly conversation partner.');
	let systemPrompt = $state('');
	let starting = $state(false);
	let stopping = $state(false);
	let micDenied = $state(false);
	let micUnsupported = $state(false);
	let errorMessage = $state<string | null>(null);
	let liveSession = $state<BrowserSession | null>(null);
	let audioSession = $state<BrowserAudioSession | null>(null);
	let audioReady = $state(false);
	let textInput = $state('');
	let textPending = $state(false);

	const isLive = $derived(liveSession !== null);

	async function startSession() {
		starting = true;
		errorMessage = null;
		micDenied = false;
		micUnsupported = false;

		// Detect mic support up front so we can warn before asking for
		// permission — denied falls back to text-only chat (AC #6).
		const supportsMic =
			typeof navigator !== 'undefined' &&
			navigator.mediaDevices !== undefined &&
			typeof navigator.mediaDevices.getUserMedia === 'function';
		if (!supportsMic) {
			micUnsupported = true;
		}

		try {
			const session = await startBrowserSession({
				persona: persona.trim() || undefined,
				system_prompt: systemPrompt.trim() || undefined
			});
			liveSession = session;

			if (supportsMic) {
				const audio = await startBrowserAudioSession({
					wsUrl: audioWebSocketUrl(session),
					onReady: () => {
						audioReady = true;
					},
					onEnded: () => {
						audioReady = false;
					},
					onError: (err) => {
						errorMessage = err.message;
					},
					onMicDenied: () => {
						micDenied = true;
					}
				});
				audioSession = audio;
			}
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : String(err);
		} finally {
			starting = false;
		}
	}

	async function endSession() {
		if (!liveSession) return;
		stopping = true;
		try {
			await audioSession?.stop();
			await stopBrowserSession(liveSession.id);
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : String(err);
		} finally {
			audioSession = null;
			audioReady = false;
			liveSession = null;
			stopping = false;
		}
	}

	async function sendText() {
		if (!liveSession || textInput.trim().length === 0) return;
		textPending = true;
		try {
			await postBrowserText(liveSession.id, textInput.trim());
			textInput = '';
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : String(err);
		} finally {
			textPending = false;
		}
	}

	onDestroy(() => {
		// Ensure we tear down the audio + ws on navigation away from the page
		// (AC #7 — no orphan WebRTC / WebSocket / mic indicator).
		void audioSession?.stop();
		if (liveSession) {
			void stopBrowserSession(liveSession.id).catch(() => undefined);
		}
	});
</script>

<svelte:head>
	<title>Playground · Johnny</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<div>
			<h1>Playground</h1>
			<p class="lede">
				Talk to Johnny directly in the browser — no calendar event, no Google Meet.
				Useful for testing prompts, providers, and the interrupt path.
			</p>
		</div>
	</header>

	{#if errorMessage}
		<div class="alert error" role="alert" data-testid="playground-error">
			{errorMessage}
		</div>
	{/if}

	{#if !isLive}
		<section class="setup" aria-labelledby="setup-heading">
			<h2 id="setup-heading">Configure</h2>
			<label class="field">
				<span>Persona</span>
				<input
					type="text"
					bind:value={persona}
					placeholder="e.g. concise, friendly conversation partner"
					maxlength="200"
				/>
				<span class="hint">
					Short description that shapes the bot's tone. Used when no custom
					system prompt is supplied.
				</span>
			</label>
			<label class="field">
				<span>Custom system prompt (optional)</span>
				<textarea
					bind:value={systemPrompt}
					rows="4"
					placeholder="Leave blank to use the default playground prompt"
				></textarea>
				<span class="hint">
					When set, replaces the default playground instructions. Useful for
					testing prompt changes without editing a real meeting.
				</span>
			</label>
			<button
				type="button"
				class="primary"
				disabled={starting}
				onclick={startSession}
				data-testid="playground-start-button"
			>
				{starting ? 'Starting…' : 'Start session'}
			</button>
		</section>
	{:else if liveSession}
		<section class="live" aria-labelledby="live-heading">
			<h2 id="live-heading">Live session #{liveSession.id}</h2>
			<div class="status">
				<span class="badge browser" data-testid="playground-badge">browser</span>
				<span class="status-text">{liveSession.status}</span>
				{#if audioReady}
					<span class="status-text live" data-testid="audio-live">audio ready</span>
				{:else if micDenied}
					<span class="status-text muted" data-testid="audio-mic-denied">
						mic denied — using text input
					</span>
				{:else if micUnsupported}
					<span class="status-text muted">mic unavailable in this browser</span>
				{:else}
					<span class="status-text">audio starting…</span>
				{/if}
			</div>
			<div class="actions">
				<a class="secondary" href={`/sessions/${liveSession.id}`} target="_blank">
					Open live view in new tab
				</a>
				<button
					type="button"
					class="danger"
					disabled={stopping}
					onclick={endSession}
					data-testid="playground-end-button"
				>
					{stopping ? 'Ending…' : 'End session'}
				</button>
			</div>

			<form
				class="text-input"
				onsubmit={(e) => {
					e.preventDefault();
					void sendText();
				}}
			>
				<label class="field">
					<span>Text input (fallback when mic is denied or muted)</span>
					<textarea
						bind:value={textInput}
						rows="2"
						placeholder="Type to chat without a microphone"
						disabled={textPending}
					></textarea>
				</label>
				<button type="submit" class="secondary" disabled={textPending}>
					{textPending ? 'Sending…' : 'Send text'}
				</button>
			</form>
		</section>
	{/if}
</div>

<style>
	.page {
		max-width: 720px;
		margin: 0 auto;
		padding: 32px 16px;
	}

	.page-header h1 {
		margin: 0 0 8px;
	}

	.lede {
		margin: 0 0 24px;
		color: var(--muted, #555);
	}

	.setup,
	.live {
		display: flex;
		flex-direction: column;
		gap: 16px;
		padding: 24px;
		border: 1px solid var(--border, #ddd);
		border-radius: 8px;
		background: var(--surface, #fff);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.field span {
		font-weight: 600;
	}

	.hint {
		font-weight: 400;
		font-size: 0.875rem;
		color: var(--muted, #666);
	}

	.field input,
	.field textarea {
		padding: 8px;
		border: 1px solid var(--border, #ccc);
		border-radius: 4px;
		font: inherit;
	}

	button.primary,
	button.secondary,
	button.danger,
	a.secondary {
		padding: 8px 16px;
		border-radius: 4px;
		font: inherit;
		cursor: pointer;
		border: 1px solid transparent;
		text-decoration: none;
		display: inline-block;
		text-align: center;
	}

	button.primary {
		background: var(--primary, #2563eb);
		color: #fff;
	}

	button.secondary,
	a.secondary {
		background: transparent;
		color: var(--primary, #2563eb);
		border-color: currentColor;
	}

	button.danger {
		background: #dc2626;
		color: #fff;
	}

	button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.status {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.badge {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 999px;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		background: #ede9fe;
		color: #6d28d9;
	}

	.badge.browser {
		background: #ede9fe;
		color: #6d28d9;
	}

	.status-text {
		color: var(--muted, #555);
	}

	.status-text.live {
		color: #16a34a;
	}

	.status-text.muted {
		color: #b45309;
	}

	.actions {
		display: flex;
		gap: 12px;
	}

	.text-input {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.alert {
		padding: 12px 16px;
		border-radius: 6px;
		margin-bottom: 16px;
	}

	.alert.error {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}
</style>
