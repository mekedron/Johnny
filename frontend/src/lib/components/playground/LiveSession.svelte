<script lang="ts">
	import BotIcon from '@lucide/svelte/icons/bot';
	import ExternalLinkIcon from '@lucide/svelte/icons/external-link';
	import MicIcon from '@lucide/svelte/icons/mic';
	import MicOffIcon from '@lucide/svelte/icons/mic-off';
	import OctagonXIcon from '@lucide/svelte/icons/octagon-x';
	import SendIcon from '@lucide/svelte/icons/send-horizontal';
	import SquareIcon from '@lucide/svelte/icons/square';
	import UserIcon from '@lucide/svelte/icons/user';
	import Volume2Icon from '@lucide/svelte/icons/volume-2';
	import VolumeXIcon from '@lucide/svelte/icons/volume-x';
	import { Button } from '$lib/components/ui/button/index.js';
	import UtteranceAudioButton from '$lib/components/UtteranceAudioButton.svelte';
	import { BOT_MODE_LABEL, sessionAudioUrl, type BotMode } from '$lib/sessionDetail';
	import { type Provider, type ProviderKind } from '$lib/providers';
	import { readSessionAgent } from '$lib/agents';
	import type { LiveState, PlaygroundController } from '$lib/playground/playgroundSession.svelte';

	let { controller }: { controller: PlaygroundController } = $props();

	let transcriptEl = $state<HTMLDivElement | null>(null);

	// Auto-scroll the transcript to the bottom as lines arrive.
	$effect(() => {
		// Read length to register the dependency, then scroll post-render.
		void controller.transcript.length;
		if (transcriptEl) {
			transcriptEl.scrollTop = transcriptEl.scrollHeight;
		}
	});

	const liveStateLabel: Record<LiveState, string> = {
		idle: 'Idle',
		listening: 'Listening',
		thinking: 'Thinking',
		speaking: 'Speaking'
	};

	const activeChips = $derived.by(() => {
		const chips: { label: string; value: string }[] = [];
		// Multi-agent group (Johnny-trt.48): the roster is the configuration.
		if (controller.liveGroup) {
			chips.push({
				label: 'Agents',
				value: controller.groupMembers.map((m) => m.name).join(' · ')
			});
			const ctx = controller.context.trim();
			if (ctx) chips.push({ label: 'Context', value: ctx.slice(0, 32) });
			return chips;
		}
		const session = controller.liveSession;
		const overrides =
			session?.playground_overrides && typeof session.playground_overrides === 'object'
				? (session.playground_overrides as Record<string, unknown>)
				: {};
		// Johnny-trt.45: the AGENT is the session's configuration — lead with it.
		const liveAgent = session ? readSessionAgent(session).agentName : null;
		if (liveAgent) {
			chips.push({ label: 'Agent', value: liveAgent });
		}
		// Mode comes from the agent profile now; render the selected agent's
		// configured mode (the dispatched snapshot equals it since trt.45).
		const agentMode = controller.agents.find(
			(a) => a.id === (session ? readSessionAgent(session).agentId : null)
		)?.mode;
		if (agentMode) {
			chips.push({ label: 'Mode', value: BOT_MODE_LABEL[agentMode as BotMode] ?? agentMode });
		}
		const liveProviders =
			overrides.providers && typeof overrides.providers === 'object'
				? (overrides.providers as Record<string, { credentials_id?: number }>)
				: undefined;
		const providerEntries: Array<[ProviderKind, Provider[]]> = [
			['stt', controller.providers.stt],
			['llm', controller.providers.llm],
			['tts', controller.providers.tts]
		];
		for (const [kind, list] of providerEntries) {
			const id = liveProviders?.[kind]?.credentials_id ?? controller.providerOverrides[kind];
			if (id !== null && id !== undefined) {
				const p = list.find((x) => x.id === id);
				if (p) chips.push({ label: kind.toUpperCase(), value: p.display_name });
			} else {
				const def = list.find((x) => x.is_active);
				if (def) chips.push({ label: kind.toUpperCase(), value: `${def.display_name} (default)` });
			}
		}
		const liveContext = typeof overrides.context === 'string' ? overrides.context : '';
		if (liveContext.trim().length > 0) {
			chips.push({ label: 'Context', value: liveContext.trim().slice(0, 32) });
		}
		return chips;
	});

	function handleComposerKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
			e.preventDefault();
			void controller.sendText();
		}
	}
</script>

{#if controller.liveSession || controller.liveGroup}
	<section
		class="flex flex-col rounded-md border border-border bg-card"
		aria-labelledby="live-heading"
	>
		<!-- Header -->
		<header
			class="flex flex-wrap items-start justify-between gap-3 border-b border-separator px-5 py-4"
		>
			<div class="flex min-w-0 flex-col gap-1.5">
				<div class="flex flex-wrap items-center gap-3">
					<h2
						id="live-heading"
						class="m-0 text-base leading-tight font-semibold tracking-tight text-foreground"
					>
						{#if controller.liveGroup}
							Group <span class="font-mono">#{controller.liveGroup.group_id}</span>
							<span class="font-normal text-muted-foreground">
								· {controller.groupMembers.length} agents</span
							>
						{:else if controller.liveSession}
							Session <span class="font-mono">#{controller.liveSession.id}</span>
						{/if}
					</h2>
					<span
						class="inline-flex items-center gap-1.5 text-xs"
						aria-live="polite"
						data-testid="live-state"
						data-state={controller.liveState}
					>
						<span aria-hidden="true" class="live-pulse h-2 w-2 rounded-full bg-primary"></span>
						<span class="font-medium text-foreground">{liveStateLabel[controller.liveState]}</span>
					</span>
					{#if controller.audioReady}
						<span class="text-xs text-success" data-testid="audio-live">Audio ready</span>
					{:else if controller.micDenied}
						<span class="text-xs text-warning" data-testid="audio-mic-denied">
							Mic denied — text only
						</span>
					{:else if controller.micUnsupported}
						<span class="text-xs text-warning">Mic unavailable in this browser</span>
					{:else}
						<span class="text-xs text-muted-foreground">Audio starting…</span>
					{/if}
				</div>
				{#if activeChips.length > 0}
					<div class="flex flex-wrap items-center gap-1.5" data-testid="live-chips">
						{#each activeChips as chip (chip.label + chip.value)}
							<span
								class="inline-flex items-center gap-1.5 rounded-xs border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[0.7rem] text-muted-foreground"
								title={`${chip.label}: ${chip.value}`}
							>
								<span class="font-semibold text-ink-subtle">{chip.label}</span>
								<span class="text-foreground">{chip.value}</span>
							</span>
						{/each}
					</div>
				{/if}
			</div>

			<div class="flex flex-wrap items-center gap-2">
				<Button
					variant="outline"
					size="sm"
					onclick={() => controller.interruptBot()}
					data-testid="playground-interrupt-button"
					title="Stop the bot immediately (voice barge-in also works while it's speaking)"
				>
					<OctagonXIcon /> Interrupt
				</Button>
				{#if controller.liveSession}
					<Button
						variant="ghost"
						size="sm"
						href={`/sessions/${controller.liveSession.id}`}
						target="_blank"
						rel="noopener"
					>
						<ExternalLinkIcon /> Open detail
					</Button>
				{/if}
				<Button
					variant="destructive"
					size="sm"
					disabled={controller.stopping}
					onclick={() => controller.endSession()}
					data-testid="playground-end-button"
				>
					<SquareIcon />
					{controller.stopping
						? 'Ending…'
						: controller.liveGroup
							? 'End group'
							: 'End session'}
				</Button>
			</div>
		</header>

		<!-- Per-agent state strip (Johnny-trt.48): who listens / thinks /
		     holds the floor / suppressed whom / lost a claim — driven by the
		     conversation-dynamics events on each member's feed. -->
		{#if controller.liveGroup}
			<div
				class="grid gap-2 border-b border-separator px-5 py-3 sm:grid-cols-2 lg:grid-cols-4"
				aria-label="Agent state strip"
				data-testid="agent-state-strip"
			>
				{#each controller.groupMembers as member (member.sessionId)}
					<div
						class="flex flex-col gap-1.5 rounded-md border px-3 py-2"
						class:border-border={!member.holdsFloor}
						class:border-foreground={member.holdsFloor}
						class:bg-surface-2={member.holdsFloor}
						class:opacity-60={member.status !== 'live'}
						data-testid={`agent-strip-${member.sessionId}`}
						data-state={member.status !== 'live' ? member.status : member.state}
						data-holds-floor={member.holdsFloor}
					>
						<div class="flex items-center gap-1.5">
							<BotIcon class="size-3.5 shrink-0" />
							<span class="min-w-0 flex-1 truncate text-sm font-semibold text-foreground">
								{member.name}
							</span>
							<a
								class="font-mono text-[0.7rem] text-ink-subtle hover:text-foreground"
								href={`/sessions/${member.sessionId}`}
								target="_blank"
								rel="noopener"
								title="Open this agent's session detail"
							>
								#{member.sessionId}
							</a>
							{#if member.status === 'live'}
								<button
									type="button"
									class="rounded-sm p-0.5 text-ink-subtle transition-colors hover:bg-surface-3 hover:text-destructive"
									onclick={() => controller.endGroupMember(member.sessionId)}
									aria-label={`End ${member.name}`}
									title={`End ${member.name} — the rest of the group keeps running`}
									data-testid={`agent-strip-end-${member.sessionId}`}
								>
									<SquareIcon class="size-3" />
								</button>
							{/if}
						</div>
						<div class="flex flex-wrap items-center gap-1.5 text-[0.7rem]">
							{#if member.status !== 'live'}
								<span
									class="rounded-xs px-1.5 py-0.5 font-medium"
									class:bg-surface-3={member.status === 'ended'}
									class:text-muted-foreground={member.status === 'ended'}
									class:bg-destructive={member.status === 'failed'}
									class:text-destructive-foreground={member.status === 'failed'}
								>
									{member.status === 'ended' ? 'Left' : 'Failed'}
								</span>
							{:else if member.holdsFloor}
								<span
									class="inline-flex items-center gap-1 rounded-xs bg-foreground px-1.5 py-0.5 font-medium text-background"
									data-testid={`agent-floor-${member.sessionId}`}
								>
									<span class="live-pulse h-1.5 w-1.5 rounded-full bg-background"></span>
									<span>Speaking · floor</span>
									{#if member.floorWaitMs !== null && member.floorWaitMs > 50}
										<span>waited {(member.floorWaitMs / 1000).toFixed(1)}s</span>
									{/if}
								</span>
							{:else if member.state === 'thinking'}
								<span class="rounded-xs bg-surface-3 px-1.5 py-0.5 font-medium text-foreground">
									Thinking…
								</span>
							{:else}
								<span class="rounded-xs bg-surface-2 px-1.5 py-0.5 text-muted-foreground">
									Listening
								</span>
							{/if}
							{#if member.heardPeer}
								<span
									class="rounded-xs bg-surface-2 px-1.5 py-0.5 italic text-muted-foreground"
									data-testid={`agent-heard-peer-${member.sessionId}`}
								>
									heard {member.heardPeer}
								</span>
							{/if}
							{#if member.suppressedCount > 0}
								<span
									class="rounded-xs border border-border px-1.5 py-0.5 text-muted-foreground"
									title={`Peer speech labeled + suppressed (never opened a turn)${member.lastSuppressedPeer ? ` — last: ${member.lastSuppressedPeer}` : ''}`}
									data-testid={`agent-suppressed-${member.sessionId}`}
								>
									suppressed ×{member.suppressedCount}
								</span>
							{/if}
							{#if member.claimsLost > 0 || member.claimsWon > 0}
								<span
									class="rounded-xs border border-border px-1.5 py-0.5 text-muted-foreground"
									title="Turn claims won/lost (Johnny-trt.47 arbitration)"
								>
									claims {member.claimsWon}W/{member.claimsLost}L
								</span>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}

		<!-- Voice controls -->
		<div
			class="grid gap-4 border-b border-separator px-5 py-3 sm:grid-cols-2"
			aria-label="Voice controls"
		>
			<div class="flex items-center gap-3">
				<button
					type="button"
					class="flex shrink-0 items-center justify-center rounded-sm p-1.5 text-ink-muted transition-colors hover:bg-surface-2 hover:text-foreground"
					onclick={() => controller.toggleSpeakerMute()}
					data-testid="toggle-speaker"
					aria-pressed={controller.speakerMuted}
					aria-label={controller.speakerMuted ? 'Unmute speaker' : 'Mute speaker'}
					title={controller.speakerMuted ? 'Unmute speaker' : 'Mute speaker'}
				>
					{#if controller.speakerMuted}
						<VolumeXIcon class="size-4 text-destructive" />
					{:else}
						<Volume2Icon class="size-4" />
					{/if}
				</button>
				<label class="flex min-w-0 flex-1 items-center gap-2 text-xs text-muted-foreground">
					<span class="shrink-0 font-medium text-foreground">Speaker</span>
					<input
						type="range"
						min="0"
						max="100"
						value={Math.round(controller.volume * 100)}
						oninput={(e) =>
							controller.setVolume(Number((e.target as HTMLInputElement).value) / 100)}
						disabled={controller.speakerMuted}
						class="h-1 min-w-0 flex-1 [accent-color:var(--color-foreground)] disabled:opacity-50"
						data-testid="volume-slider"
						aria-label="Speaker volume"
					/>
					<span
						class="w-9 shrink-0 text-right font-mono tabular-nums"
						class:opacity-50={controller.speakerMuted}
					>
						{Math.round(controller.volume * 100)}%
					</span>
				</label>
			</div>

			<div class="flex items-center gap-3" data-testid="mic-level">
				<button
					type="button"
					class="flex shrink-0 items-center justify-center rounded-sm p-1.5 text-ink-muted transition-colors hover:bg-surface-2 hover:text-foreground"
					onclick={() => controller.toggleMicMute()}
					data-testid="toggle-mic"
					aria-pressed={controller.micMuted}
					aria-label={controller.micMuted ? 'Unmute mic' : 'Mute mic'}
					title={controller.micMuted ? 'Unmute mic' : 'Mute mic'}
				>
					{#if controller.micMuted}
						<MicOffIcon class="size-4 text-destructive" />
					{:else}
						<MicIcon class="size-4" />
					{/if}
				</button>
				<div class="flex min-w-0 flex-1 items-center gap-2 text-xs text-muted-foreground">
					<span class="shrink-0 font-medium text-foreground">Mic</span>
					<div
						class="relative h-1 min-w-0 flex-1 overflow-hidden rounded-pill bg-surface-3"
						role="meter"
						aria-valuemin="0"
						aria-valuemax="100"
						aria-valuenow={Math.round(controller.micLevel * 100)}
						aria-label="Mic input level"
					>
						<div
							class="h-full rounded-pill transition-[width] duration-100"
							class:bg-foreground={!controller.micMuted}
							class:bg-ink-subtle={controller.micMuted}
							style:width={`${Math.round(controller.micLevel * 100)}%`}
						></div>
					</div>
					<span
						class="w-9 shrink-0 text-right font-mono tabular-nums"
						class:opacity-50={controller.micMuted}
					>
						{Math.round(controller.micLevel * 100)}%
					</span>
				</div>
			</div>

			<label
				class="flex items-center gap-2 text-xs text-muted-foreground sm:col-span-2"
				title="While the bot is speaking, talking over it cuts its audio immediately in the browser and tells the server to stop — no need to wait for it to finish"
			>
				<input
					type="checkbox"
					class="size-3.5 rounded-sm border border-border-strong bg-surface-3 [accent-color:var(--color-foreground)]"
					checked={controller.autoBargeIn}
					onchange={() => controller.toggleAutoBargeIn()}
					data-testid="toggle-auto-barge-in"
				/>
				<span class="font-medium text-foreground">Voice barge-in</span>
				<span>speaking over the bot cuts it off instantly</span>
			</label>
		</div>

		<!-- Transcript -->
		<div
			bind:this={transcriptEl}
			class="flex max-h-[55vh] min-h-[320px] flex-col gap-2 overflow-y-auto px-5 py-4"
			aria-label="Live transcript"
			data-testid="transcript-pane"
		>
			{#if controller.transcript.length === 0}
				<div class="m-auto flex max-w-[36ch] flex-col items-center gap-2 text-center">
					<MicIcon class="size-6 text-ink-subtle" />
					<p class="m-0 text-sm text-muted-foreground">
						Speak into the mic or type below to start the conversation.
					</p>
				</div>
			{:else}
				{#each controller.transcript as line (line.key)}
					{@const isBot = line.speaker === 'bot'}
					{@const isUser = line.speaker === 'user'}
					<div
						class="flex flex-col gap-1 rounded-md px-3 py-2"
						class:bg-surface-2={isBot}
						class:border={isBot && !line.isFinal}
						class:border-border={!line.isFinal}
						class:border-dashed={!line.isFinal}
						class:italic={!line.isFinal}
						data-testid={isBot
							? line.isFinal
								? 'bot-line'
								: 'bot-partial-line'
							: isUser
								? line.isFinal
									? 'user-line'
									: 'partial-line'
								: 'speaker-line'}
					>
						<div
							class="flex items-center gap-1.5 font-mono text-[0.7rem] font-semibold tracking-wide"
							class:text-ink-subtle={!isBot}
							class:text-foreground={isBot}
						>
							{#if isBot}
								<BotIcon class="size-3" />
								<span>{line.label ?? 'Johnny'}</span>
								{@const audioSessionId = line.sessionId ?? controller.liveSession?.id ?? null}
								{#if line.audioFile && audioSessionId !== null}
									<UtteranceAudioButton
										src={sessionAudioUrl(audioSessionId, line.audioFile)}
									/>
								{/if}
							{:else if isUser}
								<UserIcon class="size-3" />
								<span>You</span>
							{:else}
								<span class="font-sans font-normal italic">Speaker</span>
							{/if}
							{#if !line.isFinal}
								<span class="font-sans font-normal text-warning">· partial</span>
							{/if}
							{#if line.interrupted}
								<!-- Barge-in partial (Johnny-trt.58): the phrase was cut
								     mid-speech; the text below is what was delivered. -->
								<span class="font-sans font-normal text-warning" data-testid="interrupted-marker"
									>· interrupted</span
								>
							{/if}
						</div>
						<p
							class="m-0 text-sm leading-snug"
							class:text-foreground={line.isFinal}
							class:text-muted-foreground={!line.isFinal}
						>
							{line.text}
						</p>
					</div>
				{/each}
			{/if}
		</div>

		<!-- Composer -->
		<form
			class="flex flex-col gap-2 border-t border-separator px-5 py-4"
			onsubmit={(e) => {
				e.preventDefault();
				void controller.sendText();
			}}
		>
			<div class="flex items-start gap-2">
				<textarea
					bind:value={controller.textInput}
					rows={2}
					class="border-input bg-background flex w-full rounded-md border px-3 py-2 text-sm shadow-xs outline-none transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50 resize-none"
					class:!border-destructive={controller.dictationState === 'recording'}
					placeholder={controller.dictationState === 'recording'
						? 'Listening… speak now to dictate'
						: 'Type a message, or press the mic to dictate'}
					disabled={controller.textPending}
					onkeydown={handleComposerKeydown}
					data-testid="playground-text-input"
					data-partial-transcript={controller.dictationState === 'recording'
						? controller.dictationPartial
						: undefined}
					data-dictation-state={controller.dictationState}
				></textarea>
				<button
					type="button"
					class="flex h-9 shrink-0 items-center gap-1.5 rounded-md border px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50"
					class:border-border={controller.dictationState !== 'recording'}
					class:bg-background={controller.dictationState !== 'recording'}
					class:text-foreground={controller.dictationState !== 'recording'}
					class:hover:bg-accent={controller.dictationState === 'idle'}
					class:border-destructive={controller.dictationState === 'recording'}
					class:bg-destructive={controller.dictationState === 'recording'}
					class:text-destructive-foreground={controller.dictationState === 'recording'}
					onclick={() => controller.toggleDictation()}
					disabled={controller.dictationState === 'starting' ||
						controller.dictationState === 'stopping'}
					aria-pressed={controller.dictationState === 'recording'}
					aria-label={controller.dictationState === 'recording'
						? 'Stop dictation'
						: controller.dictationState === 'idle'
							? 'Start dictation'
							: 'Dictation transitioning'}
					title={controller.dictationState === 'recording'
						? 'Stop dictation'
						: 'Start dictation — speak to fill the chat input'}
					data-testid="playground-mic-button"
				>
					{#if controller.dictationState === 'recording'}
						<span
							class="h-2 w-2 shrink-0 rounded-full bg-destructive-foreground live-pulse"
							aria-hidden="true"
						></span>
						Rec
					{:else if controller.dictationState === 'starting'}
						<span class="italic text-ink-subtle">…</span>
					{:else if controller.dictationState === 'stopping'}
						<span class="italic text-ink-subtle">Stopping…</span>
					{:else}
						<MicIcon class="size-4" />
						Mic
					{/if}
				</button>
				<Button type="submit" variant="outline" size="default" disabled={controller.textPending}>
					<SendIcon />
					{controller.textPending ? 'Sending…' : 'Send'}
				</Button>
			</div>

			<div class="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
				<span>
					<kbd class="rounded-xs border border-border bg-surface-2 px-1 py-0.5 font-mono text-[0.7rem]"
						>Enter</kbd
					> sends ·
					<kbd class="rounded-xs border border-border bg-surface-2 px-1 py-0.5 font-mono text-[0.7rem]"
						>Shift+Enter</kbd
					> newline
				</span>
				{#if controller.dictationState === 'recording' && controller.dictationProviderLabel}
					<span data-testid="dictation-provider-label">
						Dictating via <span class="font-mono text-foreground"
							>{controller.dictationProviderLabel}</span
						>
					</span>
				{/if}
			</div>
		</form>
	</section>
{/if}
