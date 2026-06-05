<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { page } from '$app/state';
	import {
		BOT_SESSION_STATUS_LABEL,
		stopSession,
		type BotSession,
		type BotSessionStatus
	} from '$lib/sessions';
	import {
		DECISION_OUTCOME_LABEL,
		getSessionDetail,
		type AgentDecisionRecord,
		type AgentUtteranceRecord,
		type DecisionOutcome,
		type SessionDetail,
		type TranscriptChunk
	} from '$lib/sessionDetail';
	import {
		subscribeToSession,
		type AgentSpokeEvent,
		type ApprovalPendingEvent,
		type ApprovalResolvedEvent,
		type RouterDecisionEvent,
		type SessionEvent,
		type SessionStatusChangeEvent,
		type Subscription,
		type TranscriptFinalEvent,
		type TranscriptPartialEvent
	} from '$lib/sessionEvents';
	import { approveDecision, rejectDecision } from '$lib/decisions';

	interface TranscriptLine {
		key: string;
		text: string;
		speaker: string | null;
		isFinal: boolean;
		timestampMs: number;
	}

	interface DecisionEntry {
		key: string;
		decisionId: number | null;
		shouldSpeak: boolean;
		confidence: number;
		reason: string;
		replyType: string | null;
		suggestedReply: string | null;
		outcome: DecisionOutcome | 'spoken';
		matchedReply: string | null;
		timestampMs: number;
	}

	interface PendingApproval {
		decisionId: number;
		suggestedReply: string;
		reason: string;
		replyType: string | null;
		expiresAt: number;
		now: number;
	}

	const sessionIdStr = $derived(page.params.id);
	const sessionId = $derived(Number(sessionIdStr));

	let session = $state<BotSession | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let stopping = $state(false);
	let stopError = $state<string | null>(null);

	// Three feeds.
	let transcripts = $state<TranscriptLine[]>([]);
	let partial = $state<TranscriptLine | null>(null);
	let decisions = $state<DecisionEntry[]>([]);
	let pendingApprovals = $state<PendingApproval[]>([]);
	let resolvingDecisionIds = $state<Set<number>>(new Set());
	let approvalErrorMessage = $state<string | null>(null);

	// Connection state.
	let connected = $state(false);
	let connectError = $state<string | null>(null);

	// Resources.
	let subscription: Subscription | null = null;
	let approvalTimers: Map<number, ReturnType<typeof setInterval>> = new Map();
	let transcriptEl: HTMLDivElement | null = $state(null);

	const isTerminal = $derived(
		session !== null &&
			(session.status === 'ended' || session.status === 'failed')
	);

	function statusClass(status: BotSessionStatus): string {
		return `status-pill-${status}`;
	}

	function formatTimestamp(ms: number): string {
		if (!Number.isFinite(ms) || ms <= 0) return '';
		const date = new Date(ms);
		return date.toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
	}

	function relativeTimestamp(ms: number): string {
		if (!Number.isFinite(ms) || ms <= 0) return '';
		return formatTimestamp(ms);
	}

	function countdownSeconds(approval: PendingApproval): number {
		const remaining = Math.max(0, approval.expiresAt - approval.now);
		return Math.ceil(remaining / 1000);
	}

	async function loadInitialDetail() {
		loading = true;
		loadError = null;
		try {
			const detail = await getSessionDetail(sessionId);
			applySessionDetail(detail);
		} catch (err) {
			loadError = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	function applySessionDetail(detail: SessionDetail) {
		session = detail.session;
		transcripts = detail.transcripts.map(transcriptToLine);
		const utteranceMap = new Map<number, AgentUtteranceRecord>();
		for (const u of detail.utterances) {
			if (u.agent_decision_id !== null) {
				utteranceMap.set(u.agent_decision_id, u);
			}
		}
		decisions = detail.decisions.map((d) => decisionRecordToEntry(d, utteranceMap.get(d.id) ?? null));
		// Pending approvals from server-side state; live events refine
		// them. We don't have a timeout on these, so we display them
		// without a countdown until the live event fires (which carries
		// `timeout_s`).
		pendingApprovals = detail.pending_decisions.map((d) => ({
			decisionId: d.id,
			suggestedReply: d.suggested_reply ?? '',
			reason: d.reason,
			replyType: d.reply_type,
			// 0 hides the countdown.
			expiresAt: 0,
			now: Date.now()
		}));
	}

	function transcriptToLine(t: TranscriptChunk): TranscriptLine {
		return {
			key: `db-${t.id}`,
			text: t.text,
			speaker: t.speaker,
			isFinal: true,
			timestampMs: Date.parse(t.created_at) || 0
		};
	}

	function decisionRecordToEntry(
		d: AgentDecisionRecord,
		matchedUtterance: AgentUtteranceRecord | null
	): DecisionEntry {
		return {
			key: `db-d-${d.id}`,
			decisionId: d.id,
			shouldSpeak: d.should_speak,
			confidence: d.confidence,
			reason: d.reason,
			replyType: d.reply_type,
			suggestedReply: d.suggested_reply,
			outcome: d.outcome,
			matchedReply: matchedUtterance?.matched_allowed_reply ?? null,
			timestampMs: Date.parse(d.created_at) || 0
		};
	}

	function startSubscription() {
		if (!Number.isFinite(sessionId)) return;
		subscription = subscribeToSession(String(sessionId), {
			onEvent: (event) => handleEvent(event),
			onOpen: () => {
				connected = true;
				connectError = null;
			},
			onClose: () => {
				connected = false;
			},
			onError: (err) => {
				connectError = err.message;
			}
		});
	}

	function handleEvent(event: SessionEvent) {
		switch (event.type) {
			case 'transcript_partial':
				return handlePartial(event);
			case 'transcript_final':
				return handleFinal(event);
			case 'router_decision':
				return handleDecision(event);
			case 'approval_pending':
				return handleApprovalPending(event);
			case 'approval_resolved':
				return handleApprovalResolved(event);
			case 'agent_spoke':
				return handleAgentSpoke(event);
			case 'session_status_change':
				return handleStatus(event);
		}
	}

	function handlePartial(ev: TranscriptPartialEvent) {
		partial = {
			key: `partial-${ev.seq}`,
			text: ev.text,
			speaker: ev.speaker ?? null,
			isFinal: false,
			timestampMs: Number(ev.timestamp_ms) || Date.now()
		};
		void autoScrollTranscript();
	}

	function handleFinal(ev: TranscriptFinalEvent) {
		const line: TranscriptLine = {
			key: `live-${ev.seq}`,
			text: ev.text,
			speaker: ev.speaker ?? null,
			isFinal: true,
			timestampMs: Number(ev.timestamp_ms) || Date.now()
		};
		transcripts = [...transcripts, line];
		partial = null;
		void autoScrollTranscript();
	}

	function handleDecision(ev: RouterDecisionEvent) {
		const entry: DecisionEntry = {
			key: `live-d-${ev.seq}`,
			decisionId:
				typeof ev.decision_id === 'number' ? ev.decision_id : null,
			shouldSpeak: ev.should_speak,
			confidence: ev.confidence,
			reason: ev.reason,
			replyType: ev.reply_type ?? null,
			suggestedReply: ev.suggested_reply ?? null,
			outcome: ev.should_speak ? 'pending' : 'suppressed',
			matchedReply: null,
			timestampMs: Number(ev.timestamp_ms) || Date.now()
		};
		decisions = [entry, ...decisions];
	}

	function handleApprovalPending(ev: ApprovalPendingEvent) {
		const timeoutS =
			typeof ev.timeout_s === 'number' && ev.timeout_s > 0
				? ev.timeout_s
				: 15;
		const expiresAt = Date.now() + timeoutS * 1000;
		const incoming: PendingApproval = {
			decisionId: ev.decision_id,
			suggestedReply: ev.suggested_reply ?? '',
			reason: ev.reason ?? '',
			replyType: ev.reply_type ?? null,
			expiresAt,
			now: Date.now()
		};
		const idx = pendingApprovals.findIndex(
			(p) => p.decisionId === ev.decision_id
		);
		if (idx >= 0) {
			const next = [...pendingApprovals];
			next[idx] = incoming;
			pendingApprovals = next;
		} else {
			pendingApprovals = [...pendingApprovals, incoming];
		}
		startCountdown(ev.decision_id);
	}

	function handleApprovalResolved(ev: ApprovalResolvedEvent) {
		removePendingApproval(ev.decision_id);
		// Reflect resolution in the decision feed entry too.
		decisions = decisions.map((d) =>
			d.decisionId === ev.decision_id
				? {
						...d,
						outcome:
							ev.resolution === 'approved'
								? 'pending'
								: ev.resolution === 'rejected'
									? 'rejected'
									: 'suppressed'
					}
				: d
		);
	}

	function handleAgentSpoke(ev: AgentSpokeEvent) {
		// We don't always have the decision_id on this event; if a
		// pending approval was just satisfied, we expect an
		// approval_resolved to clear it. The decision row outcome flips
		// to 'spoken' once that happens.
		const matched =
			typeof ev.matched_allowed_reply === 'string'
				? ev.matched_allowed_reply
				: null;
		// Promote the most recent pending decision to spoken if any.
		const idx = decisions.findIndex((d) => d.outcome === 'pending');
		if (idx >= 0) {
			const next = [...decisions];
			next[idx] = { ...next[idx], outcome: 'spoken', matchedReply: matched };
			decisions = next;
		}
	}

	function handleStatus(ev: SessionStatusChangeEvent) {
		if (session !== null) {
			session = { ...session, status: ev.status };
		}
	}

	function startCountdown(decisionId: number) {
		const existing = approvalTimers.get(decisionId);
		if (existing !== undefined) clearInterval(existing);
		const timer = setInterval(() => {
			let removed = false;
			pendingApprovals = pendingApprovals.map((p) => {
				if (p.decisionId !== decisionId) return p;
				const now = Date.now();
				if (now >= p.expiresAt) {
					removed = true;
					return p;
				}
				return { ...p, now };
			});
			if (removed) {
				removePendingApproval(decisionId);
			}
		}, 250);
		approvalTimers.set(decisionId, timer);
	}

	function removePendingApproval(decisionId: number) {
		const timer = approvalTimers.get(decisionId);
		if (timer !== undefined) {
			clearInterval(timer);
			approvalTimers.delete(decisionId);
		}
		pendingApprovals = pendingApprovals.filter(
			(p) => p.decisionId !== decisionId
		);
	}

	async function approvePending(decisionId: number) {
		if (session === null) return;
		resolvingDecisionIds = new Set([...resolvingDecisionIds, decisionId]);
		approvalErrorMessage = null;
		try {
			await approveDecision(session.id, decisionId);
			removePendingApproval(decisionId);
		} catch (err) {
			approvalErrorMessage =
				err instanceof Error ? err.message : 'Failed to approve';
		} finally {
			const next = new Set(resolvingDecisionIds);
			next.delete(decisionId);
			resolvingDecisionIds = next;
		}
	}

	async function rejectPending(decisionId: number) {
		if (session === null) return;
		resolvingDecisionIds = new Set([...resolvingDecisionIds, decisionId]);
		approvalErrorMessage = null;
		try {
			await rejectDecision(session.id, decisionId);
			removePendingApproval(decisionId);
		} catch (err) {
			approvalErrorMessage =
				err instanceof Error ? err.message : 'Failed to reject';
		} finally {
			const next = new Set(resolvingDecisionIds);
			next.delete(decisionId);
			resolvingDecisionIds = next;
		}
	}

	async function handleEndSession() {
		if (session === null) return;
		stopping = true;
		stopError = null;
		try {
			const updated = await stopSession(session.id);
			session = updated;
		} catch (err) {
			stopError = err instanceof Error ? err.message : String(err);
		} finally {
			stopping = false;
		}
	}

	async function autoScrollTranscript() {
		// Defer to after the DOM update so scrollTop reflects the new content.
		await tick();
		if (transcriptEl !== null) {
			transcriptEl.scrollTop = transcriptEl.scrollHeight;
		}
	}

	onMount(() => {
		void loadInitialDetail().then(() => {
			void autoScrollTranscript();
		});
		startSubscription();
	});

	onDestroy(() => {
		if (subscription !== null) {
			subscription.close();
			subscription = null;
		}
		for (const timer of approvalTimers.values()) {
			clearInterval(timer);
		}
		approvalTimers.clear();
	});
</script>

<svelte:head>
	<title>Session #{sessionIdStr} · Johnny</title>
</svelte:head>

<div class="page" data-testid="session-page">
	<header class="page-header">
		<div class="title-row">
			<h1>Session #{sessionIdStr}</h1>
			{#if session !== null}
				<span class="status-pill {statusClass(session.status)}" data-testid="session-status">
					{BOT_SESSION_STATUS_LABEL[session.status]}
				</span>
			{/if}
			<span class="connection" class:connected aria-live="polite">
				{connected ? 'Live' : 'Connecting…'}
			</span>
		</div>
		<div class="header-actions">
			<a href="/calendar" class="ghost">Back to calendar</a>
			<button
				type="button"
				class="end-session danger"
				onclick={handleEndSession}
				disabled={stopping || isTerminal}
				data-testid="end-session-button"
			>
				{stopping ? 'Ending…' : 'End session'}
			</button>
		</div>
	</header>

	{#if loadError}
		<div class="alert error" role="alert">{loadError}</div>
	{/if}
	{#if connectError}
		<div class="alert warn" role="status" data-testid="connect-warn">
			Live updates paused: {connectError}
		</div>
	{/if}
	{#if stopError}
		<div class="alert error" role="alert" data-testid="stop-error">{stopError}</div>
	{/if}

	{#if loading}
		<p class="empty">Loading session…</p>
	{:else if session === null}
		<p class="empty">No session found.</p>
	{:else}
		<div class="panes">
			<section class="pane transcript-pane" aria-label="Transcript" data-testid="transcript-pane">
				<header class="pane-header">
					<h2>Transcript</h2>
					<span class="pane-count" data-testid="transcript-count">
						{transcripts.length}
					</span>
				</header>
				<div class="transcript-scroll" bind:this={transcriptEl} data-testid="transcript-scroll">
					{#if transcripts.length === 0 && partial === null}
						<p class="empty">Nothing transcribed yet.</p>
					{:else}
						<ul class="transcript-list">
							{#each transcripts as line (line.key)}
								<li
									class="transcript-line"
									class:partial={!line.isFinal}
									data-testid="transcript-line"
								>
									<div class="transcript-meta">
										{#if line.speaker}
											<span class="speaker">{line.speaker}</span>
										{:else}
											<span class="speaker unknown">Speaker</span>
										{/if}
										<time class="ts">{relativeTimestamp(line.timestampMs)}</time>
									</div>
									<p class="transcript-text">{line.text}</p>
								</li>
							{/each}
							{#if partial !== null}
								<li
									class="transcript-line partial"
									data-testid="transcript-partial"
								>
									<div class="transcript-meta">
										{#if partial.speaker}
											<span class="speaker">{partial.speaker}</span>
										{:else}
											<span class="speaker unknown">Speaker</span>
										{/if}
										<span class="ts partial-tag">…partial</span>
									</div>
									<p class="transcript-text partial-text">{partial.text}</p>
								</li>
							{/if}
						</ul>
					{/if}
				</div>
			</section>

			<section class="pane decisions-pane" aria-label="Decision feed" data-testid="decisions-pane">
				<header class="pane-header">
					<h2>Decisions</h2>
					<span class="pane-count" data-testid="decisions-count">
						{decisions.length}
					</span>
				</header>
				{#if decisions.length === 0}
					<p class="empty">No decisions yet.</p>
				{:else}
					<ul class="decision-list">
						{#each decisions as d (d.key)}
							<li class="decision" data-testid="decision-row">
								<header class="decision-header">
									<span class="decision-outcome outcome-{d.outcome}">
										{DECISION_OUTCOME_LABEL[d.outcome as DecisionOutcome] ?? d.outcome}
									</span>
									<span class="decision-confidence" title="Router confidence">
										{(d.confidence * 100).toFixed(0)}%
									</span>
									<time class="ts">{relativeTimestamp(d.timestampMs)}</time>
								</header>
								<p class="decision-reason">{d.reason}</p>
								{#if d.suggestedReply}
									<p class="decision-suggested">
										<span class="muted">Suggested:</span>
										<span>"{d.suggestedReply}"</span>
									</p>
								{/if}
								{#if d.replyType}
									<p class="decision-meta">
										<span class="muted">Type:</span>
										<span>{d.replyType}</span>
									</p>
								{/if}
								{#if d.matchedReply}
									<p class="decision-meta">
										<span class="muted">Matched reply:</span>
										<span>"{d.matchedReply}"</span>
									</p>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section
				class="pane approvals-pane"
				aria-label="Pending approvals"
				data-testid="approvals-pane"
			>
				<header class="pane-header">
					<h2>Pending approvals</h2>
					<span class="pane-count" data-testid="approvals-count">
						{pendingApprovals.length}
					</span>
				</header>
				{#if approvalErrorMessage}
					<div class="alert error" role="alert" data-testid="approval-error">
						{approvalErrorMessage}
					</div>
				{/if}
				{#if pendingApprovals.length === 0}
					<p class="empty">Nothing waiting for you.</p>
				{:else}
					<ul class="approval-list">
						{#each pendingApprovals as approval (approval.decisionId)}
							<li class="approval" data-testid="approval-row">
								<header class="approval-header">
									<span class="approval-id">Decision #{approval.decisionId}</span>
									{#if approval.expiresAt > 0}
										<span
											class="approval-countdown"
											aria-label="Auto-reject in {countdownSeconds(approval)} seconds"
											data-testid="approval-countdown"
										>
											{countdownSeconds(approval)}s
										</span>
									{/if}
								</header>
								<p class="approval-reply">"{approval.suggestedReply}"</p>
								{#if approval.reason}
									<p class="approval-reason">{approval.reason}</p>
								{/if}
								<div class="approval-actions">
									<button
										type="button"
										class="approve"
										disabled={resolvingDecisionIds.has(approval.decisionId)}
										onclick={() => approvePending(approval.decisionId)}
										data-testid="approve-button"
									>
										{resolvingDecisionIds.has(approval.decisionId)
											? '…'
											: 'Approve'}
									</button>
									<button
										type="button"
										class="reject"
										disabled={resolvingDecisionIds.has(approval.decisionId)}
										onclick={() => rejectPending(approval.decisionId)}
										data-testid="reject-button"
									>
										{resolvingDecisionIds.has(approval.decisionId)
											? '…'
											: 'Reject'}
									</button>
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		</div>
	{/if}
</div>

<style>
	.page {
		max-width: 1280px;
	}
	.page-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1rem;
		flex-wrap: wrap;
		margin-bottom: 1rem;
	}
	.title-row {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex-wrap: wrap;
	}
	.title-row h1 {
		margin: 0;
		font-size: 1.5rem;
	}
	.header-actions {
		display: flex;
		gap: 0.6rem;
		align-items: center;
	}
	a.ghost {
		color: #4b5563;
		text-decoration: none;
		font-size: 0.85rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		padding: 0.4rem 0.7rem;
	}
	a.ghost:hover {
		background: #f3f4f6;
	}
	.status-pill {
		font-size: 0.72rem;
		font-weight: 600;
		padding: 0.18rem 0.55rem;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	.status-pill-scheduled {
		background: #fef3c7;
		color: #92400e;
	}
	.status-pill-joining {
		background: #dbeafe;
		color: #1e40af;
	}
	.status-pill-joined {
		background: #d1fae5;
		color: #065f46;
	}
	.status-pill-ended,
	.status-pill-failed {
		background: #fee2e2;
		color: #991b1b;
	}
	.connection {
		font-size: 0.75rem;
		color: #6b7280;
		padding: 0.2rem 0.5rem;
		background: #f3f4f6;
		border-radius: 6px;
	}
	.connection.connected {
		background: #ecfdf5;
		color: #047857;
	}
	.end-session {
		appearance: none;
		border: 0;
		border-radius: 6px;
		font-weight: 600;
		font-size: 0.85rem;
		padding: 0.5rem 0.9rem;
		cursor: pointer;
	}
	.danger {
		background: #b91c1c;
		color: #ffffff;
	}
	.danger:hover:not(:disabled) {
		background: #991b1b;
	}
	.danger:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.alert {
		padding: 0.6rem 0.85rem;
		border-radius: 6px;
		margin: 0 0 0.75rem;
		font-size: 0.85rem;
	}
	.alert.error {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}
	.alert.warn {
		background: #fef3c7;
		color: #92400e;
		border: 1px solid #fde68a;
	}
	.empty {
		color: #6b7280;
		font-style: italic;
		margin: 1rem 0;
	}

	.panes {
		display: grid;
		grid-template-columns: 2fr 1.2fr 1.2fr;
		gap: 1rem;
		min-height: 60vh;
	}
	.pane {
		background: #ffffff;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		padding: 0.75rem 0.9rem;
		display: flex;
		flex-direction: column;
		min-height: 60vh;
	}
	.pane-header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
		margin-bottom: 0.6rem;
	}
	.pane-header h2 {
		margin: 0;
		font-size: 0.95rem;
		color: #111827;
	}
	.pane-count {
		font-size: 0.75rem;
		font-weight: 600;
		color: #ffffff;
		background: #1f2937;
		border-radius: 9999px;
		padding: 0.1rem 0.55rem;
	}

	.transcript-scroll {
		overflow-y: auto;
		flex: 1;
		min-height: 0;
		max-height: 65vh;
		padding-right: 0.25rem;
	}
	.transcript-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.transcript-line {
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		padding: 0.45rem 0.6rem;
	}
	.transcript-line.partial {
		background: #fffbeb;
		border-color: #fde68a;
		border-style: dashed;
	}
	.transcript-meta {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
		font-size: 0.7rem;
		color: #6b7280;
		margin-bottom: 0.2rem;
	}
	.speaker {
		font-weight: 600;
		color: #1f2937;
	}
	.speaker.unknown {
		color: #6b7280;
		font-weight: 500;
		font-style: italic;
	}
	.partial-tag {
		color: #92400e;
		font-weight: 600;
	}
	.transcript-text {
		margin: 0;
		color: #111827;
		font-size: 0.9rem;
		white-space: pre-wrap;
	}
	.partial-text {
		color: #92400e;
		font-style: italic;
	}

	.decision-list,
	.approval-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		overflow-y: auto;
		flex: 1;
		max-height: 65vh;
	}
	.decision {
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		padding: 0.5rem 0.65rem;
	}
	.decision-header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
		margin-bottom: 0.25rem;
	}
	.decision-outcome {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0.1rem 0.5rem;
		border-radius: 9999px;
	}
	.outcome-spoken {
		background: #dcfce7;
		color: #166534;
	}
	.outcome-suppressed {
		background: #f3f4f6;
		color: #4b5563;
	}
	.outcome-pending {
		background: #fef3c7;
		color: #92400e;
	}
	.outcome-rejected {
		background: #fee2e2;
		color: #991b1b;
	}
	.decision-confidence {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.75rem;
		color: #4b5563;
	}
	.ts {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.7rem;
		color: #6b7280;
	}
	.decision-reason {
		margin: 0 0 0.25rem;
		font-size: 0.85rem;
		color: #1f2937;
	}
	.decision-suggested,
	.decision-meta {
		margin: 0.15rem 0;
		font-size: 0.8rem;
		color: #374151;
	}
	.muted {
		color: #6b7280;
		margin-right: 0.25rem;
		font-weight: 600;
	}

	.approval {
		background: #fff7ed;
		border: 1px solid #fdba74;
		border-radius: 6px;
		padding: 0.55rem 0.7rem;
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.approval-header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.5rem;
		font-size: 0.75rem;
	}
	.approval-id {
		font-weight: 600;
		color: #9a3412;
	}
	.approval-countdown {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-weight: 600;
		color: #b91c1c;
	}
	.approval-reply {
		margin: 0;
		font-weight: 600;
		color: #1f2937;
	}
	.approval-reason {
		margin: 0;
		color: #6b7280;
		font-style: italic;
		font-size: 0.8rem;
	}
	.approval-actions {
		display: flex;
		gap: 0.4rem;
		margin-top: 0.2rem;
	}
	.approve,
	.reject {
		flex: 1;
		appearance: none;
		border: 0;
		border-radius: 4px;
		padding: 0.35rem 0.5rem;
		font-size: 0.78rem;
		font-weight: 600;
		cursor: pointer;
	}
	.approve {
		background: #16a34a;
		color: #ffffff;
	}
	.approve:hover:not(:disabled) {
		background: #15803d;
	}
	.reject {
		background: #fee2e2;
		color: #991b1b;
	}
	.reject:hover:not(:disabled) {
		background: #fecaca;
	}
	.approve:disabled,
	.reject:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	@media (max-width: 1100px) {
		.panes {
			grid-template-columns: 1fr;
		}
		.pane {
			min-height: 40vh;
		}
	}
</style>
