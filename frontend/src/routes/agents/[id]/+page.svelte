<!--
  Agent edit page (Johnny-trt.44) — the sectioned admin surface. Five
  sections, each owning one aspect of the agent:

    IDENTITY                      name / avatar glyph / human description
    CHARACTER & COMMUNICATION     the character prompt (IDENTITY layer of
                                  the LLM system prompt, verbatim)
    BEHAVIOR                      mode + allowed-replies + confidence knob
    VOICE & BRAIN                 three per-stage model pickers (triage /
                                  answer / reasoning, trt.42 role slots) +
                                  TTS provider, voice picker, Test button
    CAPABILITIES                  the trt.37 ToolsPanel pinned to THIS
                                  agent's policy layer

  Split-only by design (the S2S reversal): there is no pipeline-mode
  switch — every agent gets LLM + TTS + voice. `/agents/new` renders the
  same page in create mode (Capabilities unlocks after the first save).
  Validation mirrors the API rules inline, with the API's own messages.
-->
<script lang="ts">
	import { onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import PlayIcon from '@lucide/svelte/icons/play';
	import TriangleAlertIcon from '@lucide/svelte/icons/triangle-alert';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Separator } from '$lib/components/ui/separator/index.js';
	import Page from '$lib/components/page.svelte';
	import PageHeader from '$lib/components/page-header.svelte';
	import ToolsPanel from '$lib/components/capabilities/ToolsPanel.svelte';
	import VoicePicker from '$lib/components/settings/VoicePicker.svelte';
	import MeetingBotAccountPicker from '$lib/components/agents/MeetingBotAccountPicker.svelte';
	import { BOT_MODES, BOT_MODE_LABEL } from '$lib/sessionDetail';
	import { listProviders, playSample, type ProviderList } from '$lib/providers';
	import { listSkills, skillStatus } from '$lib/capabilities';
	import {
		listWorkspaces,
		workspaceAttachmentValue,
		type Workspace
	} from '$lib/workspaces';
	import {
		agentGlyph,
		BOT_MODE_HINT,
		createAgent,
		diffAgentPayload,
		draftFromAgent,
		draftToCreatePayload,
		fallbackLabel,
		getAgent,
		providerOptionLabel,
		repliesToText,
		testAgentVoice,
		textToReplies,
		updateAgent,
		validateAgentDraft,
		type Agent,
		type AgentDraft
	} from '$lib/agents';

	const idParam = $derived(page.params.id ?? '');
	const isNew = $derived(idParam === 'new');

	let agent = $state<Agent | null>(null);
	let draft = $state<AgentDraft>(draftFromAgent(null));
	let repliesText = $state('');
	let providers = $state<ProviderList | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let saving = $state(false);
	let serverError = $state<string | null>(null);
	let savedNote = $state<string | null>(null);

	// Voice test playback (the VoicePicker pattern: own Audio + object URL).
	let testing = $state(false);
	let testError = $state<string | null>(null);
	let testWarning = $state<string | null>(null);
	let testNote = $state<string | null>(null);
	let testAudio: HTMLAudioElement | null = null;
	let testAudioUrl: string | null = null;

	const effectiveDraft = $derived<AgentDraft>({
		...draft,
		allowed_replies: textToReplies(repliesText)
	});
	const fieldErrors = $derived(validateAgentDraft(effectiveDraft));
	const patch = $derived(agent !== null ? diffAgentPayload(agent, effectiveDraft) : {});
	const dirty = $derived(isNew || Object.keys(patch).length > 0);
	const canSave = $derived(dirty && Object.keys(fieldErrors).length === 0 && !saving);

	const llmChoices = $derived(providers?.llm ?? []);
	const ttsChoices = $derived(providers?.tts ?? []);
	const selectedTts = $derived(
		ttsChoices.find((row) => row.id === draft.tts_provider_id) ?? null
	);
	// The saved voice config is untouched by this draft — the Test button can
	// honor the agent's EXACT saved provider+voice via the test_voice endpoint.
	const voiceClean = $derived(
		agent !== null && !('tts_provider_id' in patch) && !('tts_voice_id' in patch)
	);
	const canTestVoice = $derived(voiceClean || draft.tts_provider_id !== null);

	function resetFromAgent(row: Agent | null) {
		agent = row;
		draft = draftFromAgent(row);
		repliesText = repliesToText(row?.allowed_replies);
	}

	async function load(id: string) {
		loading = true;
		loadError = null;
		serverError = null;
		savedNote = null;
		stopTestAudio();
		try {
			resetFromAgent(id === 'new' ? null : await getAgent(Number.parseInt(id, 10)));
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load the agent';
		} finally {
			loading = false;
		}
	}

	// Reload when the route param changes (create → saved id, list → edit).
	$effect(() => {
		void load(idParam);
	});
	// Workspace attachment picker (Johnny-wks.5). The picked workspace is
	// part of the draft — Save applies it like any other field.
	let workspaces = $state<Workspace[]>([]);
	let workspacesLoaded = $state(false);
	let workspaceSkillsSummary = $state<{ available: number; total: number } | null>(null);

	const defaultWorkspace = $derived(workspaces.find((ws) => ws.is_default) ?? null);
	/** The draft's effective workspace row — null draft value = the default. */
	const pickedWorkspace = $derived(
		draft.workspace_id === null
			? defaultWorkspace
			: (workspaces.find((ws) => ws.id === draft.workspace_id) ?? null)
	);

	function handleWorkspaceChange(raw: string) {
		const id = Number.parseInt(raw, 10);
		const row = workspaces.find((ws) => ws.id === id) ?? null;
		// Picking the default stores null (the NULL-inherits-default convention).
		draft.workspace_id = row === null ? null : workspaceAttachmentValue(row);
	}

	// Capability summary for the PICKED workspace: skill availability probed
	// against ITS sandbox (the GET lazily ensures the container — the same
	// refresh the accounts panel below already triggers).
	$effect(() => {
		const picked = pickedWorkspace;
		workspaceSkillsSummary = null;
		if (picked === null) return;
		let cancelled = false;
		listSkills(picked.id)
			.then((res) => {
				if (cancelled) return;
				workspaceSkillsSummary = {
					available: res.skills.filter((s) => skillStatus(s) === 'available').length,
					total: res.skills.length
				};
			})
			.catch(() => {
				// summary is decoration — the detail page has the full story
			});
		return () => {
			cancelled = true;
		};
	});

	$effect(() => {
		if (providers === null) {
			listProviders()
				.then((list) => (providers = list))
				.catch(() => (providers = null));
		}
		if (!workspacesLoaded) {
			workspacesLoaded = true;
			listWorkspaces()
				.then((rows) => (workspaces = rows))
				.catch(() => (workspaces = []));
		}
	});

	function handleTtsProviderChange(value: string) {
		const id = value === '' ? null : Number.parseInt(value, 10);
		if (id !== draft.tts_provider_id) {
			// A voice id is provider-specific — switching (or unpinning) the
			// provider invalidates the old catalog selection.
			draft.tts_voice_id = null;
		}
		draft.tts_provider_id = id;
	}

	function handleLlmSlotChange(
		slot: 'router_llm_provider_id' | 'answer_llm_provider_id' | 'reasoning_llm_provider_id',
		value: string
	) {
		draft[slot] = value === '' ? null : Number.parseInt(value, 10);
	}

	async function handleSave() {
		if (!canSave) return;
		saving = true;
		serverError = null;
		savedNote = null;
		try {
			if (isNew) {
				const created = await createAgent(draftToCreatePayload(effectiveDraft));
				resetFromAgent(created);
				savedNote = 'Agent created.';
				await goto(`/agents/${created.id}`, { replaceState: true });
			} else if (agent !== null) {
				const updated = await updateAgent(agent.id, patch);
				resetFromAgent(updated);
				savedNote = 'Changes saved.';
			}
			// A save may have (re)attached this agent — refresh the picker's
			// agent counts (the $effect refetches when the flag flips).
			workspacesLoaded = false;
		} catch (err) {
			// The API's own message (409 name conflict, 422 cross-field rule) —
			// rendered verbatim so the inline copy matches the server's.
			serverError = err instanceof Error ? err.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	function stopTestAudio() {
		if (testAudio) {
			try {
				testAudio.pause();
			} catch {
				// already torn down
			}
		}
		if (testAudioUrl) {
			URL.revokeObjectURL(testAudioUrl);
			testAudioUrl = null;
		}
		testAudio = null;
		testing = false;
	}

	async function handleTestVoice() {
		if (testing) {
			stopTestAudio();
			return;
		}
		stopTestAudio();
		testError = null;
		testWarning = null;
		testNote = null;
		testing = true;
		try {
			let blob: Blob;
			if (voiceClean && agent !== null) {
				// Saved state — the per-agent endpoint speaks the EXACT saved
				// provider + voice (+ tts_options), 409ing on a broken pin.
				const sample = await testAgentVoice(agent.id);
				blob = sample.blob;
				testNote = `Played ${sample.provider}${sample.voice ? ` · ${sample.voice}` : ' · provider default voice'}`;
				if (!sample.audible) {
					testWarning = `No audible audio${sample.audibleReason ? ` (${sample.audibleReason})` : ''}.`;
				}
			} else if (draft.tts_provider_id !== null) {
				// Unsaved picker selection — preview without mutating the row.
				const sample = await playSample(
					draft.tts_provider_id,
					draft.tts_voice_id ?? undefined
				);
				blob = sample.blob;
				const name = selectedTts?.display_name ?? `provider #${draft.tts_provider_id}`;
				testNote = `Played ${name}${draft.tts_voice_id ? ` · ${draft.tts_voice_id}` : ' · provider default voice'} (unsaved selection)`;
				if (!sample.audible) {
					testWarning = `No audible audio${sample.audibleReason ? ` (${sample.audibleReason})` : ''}.`;
				}
			} else {
				testing = false;
				return;
			}
			testAudioUrl = URL.createObjectURL(blob);
			testAudio = new Audio(testAudioUrl);
			testAudio.addEventListener('ended', () => stopTestAudio());
			await testAudio.play();
		} catch (err) {
			testError = err instanceof Error ? err.message : 'Voice test failed';
			stopTestAudio();
		}
	}

	onDestroy(() => stopTestAudio());

	const SECTIONS = [
		{ id: 'identity', label: 'Identity' },
		{ id: 'meeting-bot', label: 'Meeting bot' },
		{ id: 'character', label: 'Character' },
		{ id: 'behavior', label: 'Behavior' },
		{ id: 'voice-brain', label: 'Voice & brain' },
		{ id: 'capabilities', label: 'Capabilities' }
	];

	const LLM_SLOTS = [
		{
			key: 'router_llm_provider_id' as const,
			label: 'Triage model',
			hint: 'Decides fast whether to stay silent, speak, or delegate — a cheap local model is recommended.'
		},
		{
			key: 'answer_llm_provider_id' as const,
			label: 'Answer model',
			hint: 'What the agent says — generates the conversational replies.'
		},
		{
			key: 'reasoning_llm_provider_id' as const,
			label: 'Reasoning model',
			hint: 'Used for delegated complex tasks the agent works on asynchronously.'
		}
	];

	const inputClass =
		'rounded-sm border border-border-strong bg-surface-3 px-2 py-1.5 text-sm text-foreground outline-none focus-visible:border-ring';
</script>

<svelte:head>
	<title>{isNew ? 'New agent' : (agent?.name ?? 'Agent')} · Johnny</title>
</svelte:head>

<Page testId="agent-edit-page">
	<nav class="text-muted-foreground -mb-4 text-xs" aria-label="Breadcrumb">
		<a href="/agents" class="hover:text-foreground hover:underline">← Agents</a>
	</nav>

	<PageHeader
		title={isNew ? 'New agent' : (agent?.name ?? 'Agent')}
		description={isNew
			? 'Define a new character for the bot. Capabilities unlock after the first save.'
			: 'Every aspect of this agent, one section per concern. Changes apply to the next session it joins.'}
	>
		{#snippet meta()}
			{#if agent?.is_default}
				<Badge variant="secondary" data-testid="edit-default-badge">default</Badge>
			{/if}
			{#if dirty && !loading && !loadError}
				<Badge variant="outline" data-testid="dirty-badge">unsaved changes</Badge>
			{/if}
		{/snippet}
		{#snippet actions()}
			<Button
				disabled={!canSave}
				onclick={handleSave}
				data-testid="save-agent"
				title={Object.keys(fieldErrors).length > 0
					? 'Fix the highlighted fields first'
					: undefined}
			>
				{saving ? 'Saving…' : isNew ? 'Create agent' : 'Save changes'}
			</Button>
		{/snippet}
	</PageHeader>

	{#if loadError}
		<Alert.Root variant="destructive" data-testid="agent-load-error">
			<CircleAlertIcon />
			<Alert.Description>
				{loadError} — <a href="/agents" class="underline">back to the agent list</a>
			</Alert.Description>
		</Alert.Root>
	{:else if loading}
		<p class="text-muted-foreground text-sm">Loading agent…</p>
	{:else}
		{#if serverError}
			<Alert.Root variant="destructive" data-testid="agent-save-error">
				<CircleAlertIcon />
				<Alert.Description>{serverError}</Alert.Description>
			</Alert.Root>
		{/if}
		{#if savedNote && !dirty}
			<p class="text-success m-0 -my-4 text-xs" data-testid="agent-saved-note">{savedNote}</p>
		{/if}

		<nav
			class="border-border flex flex-wrap items-center gap-1 border-b pb-2"
			aria-label="Agent sections"
		>
			{#each SECTIONS as section (section.id)}
				<a
					href={`#${section.id}`}
					class="text-muted-foreground hover:text-foreground hover:bg-surface-3 rounded-md px-2.5 py-1 text-xs font-medium transition-colors"
				>
					{section.label}
				</a>
			{/each}
		</nav>

		<!-- ─── IDENTITY ─────────────────────────────────────────────────── -->
		<section
			id="identity"
			class="border-border bg-surface-2 flex scroll-mt-6 flex-col gap-4 rounded-md border p-5"
			data-testid="section-identity"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Identity
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					Who this agent is in the room — participants address it by this name.
				</p>
			</header>
			<div class="grid gap-4 sm:grid-cols-[1fr_8rem]">
				<div class="flex flex-col gap-1.5">
					<label class="text-foreground text-xs font-medium" for="agent-name">Name</label>
					<Input
						id="agent-name"
						placeholder="e.g. Johnny"
						bind:value={draft.name}
						data-testid="identity-name"
						aria-invalid={fieldErrors.name !== undefined}
					/>
					{#if fieldErrors.name}
						<p class="text-destructive m-0 text-xs" data-testid="error-name">
							{fieldErrors.name}
						</p>
					{/if}
				</div>
				<div class="flex flex-col gap-1.5">
					<label class="text-foreground text-xs font-medium" for="agent-avatar">
						Avatar / emoji
					</label>
					<div class="flex items-center gap-2">
						<Input
							id="agent-avatar"
							placeholder="🤖"
							bind:value={draft.avatar}
							data-testid="identity-avatar"
						/>
						<span
							class="bg-surface-3 flex size-9 shrink-0 items-center justify-center rounded-md text-base"
							aria-hidden="true"
							data-testid="identity-glyph-preview"
						>
							{agentGlyph({ name: draft.name, avatar: draft.avatar })}
						</span>
					</div>
				</div>
			</div>
			<div class="flex flex-col gap-1.5">
				<label class="text-foreground text-xs font-medium" for="agent-description">
					Description
				</label>
				<textarea
					id="agent-description"
					class={`${inputClass} min-h-16 resize-y`}
					placeholder="Library note for humans — what this agent is for. Never injected into any prompt."
					bind:value={draft.description}
					data-testid="identity-description"
				></textarea>
			</div>
		</section>

		<!-- ─── MEETING BOT ──────────────────────────────────────────────── -->
		<!-- The Google identity this agent JOINS Meet calls as (Johnny-wks.7).
		     The ONLY place an agent's meeting-bot identity is managed — the
		     Settings page no longer carries a Meeting Bots section. Distinct
		     from the workspace's gog keyring (Capabilities, below): that is
		     container-only CLI tooling; this is the meeting join identity. -->
		<section
			id="meeting-bot"
			class="border-border bg-surface-2 flex scroll-mt-6 flex-col gap-4 rounded-md border p-5"
			data-testid="section-meeting-bot"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Meeting bot
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					The Google account this agent signs in as when it joins a Meet call. Each agent
					carries its own identity, so two agents in one meeting are two distinct
					participants. Connect a new bot session or pick an existing one.
				</p>
			</header>
			<div class="flex items-start gap-2">
				<div class="min-w-0 flex-1">
					<MeetingBotAccountPicker
						value={draft.meeting_bot_account_id}
						onChange={(id) => (draft.meeting_bot_account_id = id)}
					/>
				</div>
				{#if agent !== null && patch.meeting_bot_account_id !== undefined}
					<Badge variant="outline" data-testid="meeting-bot-unsaved-badge">applies after save</Badge>
				{/if}
			</div>
		</section>

		<!-- ─── CHARACTER & COMMUNICATION STYLE ──────────────────────────── -->
		<section
			id="character"
			class="border-border bg-surface-2 flex scroll-mt-6 flex-col gap-4 rounded-md border p-5"
			data-testid="section-character"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Character &amp; communication style
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					Injected verbatim as the identity layer of the system prompt — personality, tone,
					and how the agent communicates.
				</p>
			</header>
			<div class="flex flex-col gap-1.5">
				<textarea
					id="agent-character"
					class={`${inputClass} min-h-40 resize-y font-mono text-xs leading-relaxed`}
					placeholder={'You are a concise, dry-witted meeting assistant. Answer in at most two sentences…'}
					bind:value={draft.character_prompt}
					data-testid="character-prompt"
					aria-invalid={fieldErrors.character_prompt !== undefined}
				></textarea>
				{#if fieldErrors.character_prompt}
					<p class="text-destructive m-0 text-xs" data-testid="error-character-prompt">
						{fieldErrors.character_prompt}
					</p>
				{/if}
			</div>
		</section>

		<!-- ─── BEHAVIOR ─────────────────────────────────────────────────── -->
		<section
			id="behavior"
			class="border-border bg-surface-2 flex scroll-mt-6 flex-col gap-4 rounded-md border p-5"
			data-testid="section-behavior"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Behavior
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					When the agent is allowed to speak and how confident the router must be.
				</p>
			</header>
			<div class="flex flex-col gap-1.5">
				<label class="text-foreground text-xs font-medium" for="agent-mode">Mode</label>
				<select
					id="agent-mode"
					class={inputClass}
					bind:value={draft.mode}
					data-testid="behavior-mode"
				>
					{#each BOT_MODES as mode (mode)}
						<option value={mode}>{BOT_MODE_LABEL[mode]}</option>
					{/each}
				</select>
				<p class="text-muted-foreground m-0 text-xs" data-testid="behavior-mode-hint">
					{BOT_MODE_HINT[draft.mode]}
				</p>
			</div>
			<div class="flex flex-col gap-1.5">
				<label class="text-foreground text-xs font-medium" for="agent-replies">
					Allowed replies
					<span class="text-muted-foreground font-normal">— one per line</span>
				</label>
				<textarea
					id="agent-replies"
					class={`${inputClass} min-h-24 resize-y font-mono text-xs leading-relaxed`}
					placeholder={'Understood.\nOn it — give me a moment.\nI will follow up after the meeting.'}
					bind:value={repliesText}
					data-testid="behavior-replies"
					aria-invalid={fieldErrors.allowed_replies !== undefined}
				></textarea>
				{#if fieldErrors.allowed_replies}
					<p class="text-destructive m-0 text-xs" data-testid="error-allowed-replies">
						{fieldErrors.allowed_replies}
					</p>
				{:else}
					<p class="text-muted-foreground m-0 text-xs">
						The safe phrases “Limited auto-speak” may pick from. Other modes ignore the
						list.
					</p>
				{/if}
			</div>
			<div class="flex flex-col gap-1.5">
				<label class="text-foreground text-xs font-medium" for="agent-confidence">
					Confidence threshold
					<span
						class="text-foreground bg-surface-3 ml-1 rounded-sm px-1.5 py-0.5 font-mono text-[11px]"
						data-testid="behavior-confidence-value"
					>
						{draft.confidence_threshold.toFixed(2)}
					</span>
				</label>
				<input
					id="agent-confidence"
					type="range"
					min="0"
					max="1"
					step="0.05"
					bind:value={draft.confidence_threshold}
					data-testid="behavior-confidence"
					class="accent-primary w-full max-w-sm"
				/>
				<p class="text-muted-foreground m-0 text-xs">
					Router knob: replies scored below this confidence are dropped instead of spoken
					(0 lets everything through, 1 keeps only the most certain).
				</p>
			</div>
		</section>

		<!-- ─── VOICE & BRAIN ────────────────────────────────────────────── -->
		<section
			id="voice-brain"
			class="border-border bg-surface-2 flex scroll-mt-6 flex-col gap-4 rounded-md border p-5"
			data-testid="section-voice-brain"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Voice &amp; brain
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					Per-stage model pins and the speaking voice. Unset pickers inherit the global
					default provider; pickers list only configured providers of the right kind.
				</p>
			</header>

			<div class="grid gap-4 lg:grid-cols-3">
				{#each LLM_SLOTS as slot (slot.key)}
					<div class="flex flex-col gap-1.5">
						<label class="text-foreground text-xs font-medium" for={`agent-${slot.key}`}>
							{slot.label}
						</label>
						<select
							id={`agent-${slot.key}`}
							class={inputClass}
							value={draft[slot.key] === null ? '' : String(draft[slot.key])}
							onchange={(e) => handleLlmSlotChange(slot.key, e.currentTarget.value)}
							data-testid={`brain-${slot.key}`}
						>
							<option value="">Inherit global default</option>
							{#each llmChoices as row (row.id)}
								<option value={String(row.id)}>{providerOptionLabel(row)}</option>
							{/each}
						</select>
						<p class="text-muted-foreground m-0 text-xs">{slot.hint}</p>
						{#if draft[slot.key] === null}
							<p
								class="text-ink-subtle m-0 text-[11px] italic"
								data-testid={`fallback-${slot.key}`}
							>
								{fallbackLabel(llmChoices)}
							</p>
						{/if}
					</div>
				{/each}
			</div>
			{#if llmChoices.length === 0}
				<p class="text-muted-foreground m-0 text-xs">
					No LLM providers configured yet — add one on the
					<a href="/providers" class="underline">Providers</a> page to pin per-stage models.
				</p>
			{/if}

			<Separator />

			<div class="flex flex-col gap-4">
				<div class="flex max-w-md flex-col gap-1.5">
					<label class="text-foreground text-xs font-medium" for="agent-tts-provider">
						Voice provider (TTS)
					</label>
					<select
						id="agent-tts-provider"
						class={inputClass}
						value={draft.tts_provider_id === null ? '' : String(draft.tts_provider_id)}
						onchange={(e) => handleTtsProviderChange(e.currentTarget.value)}
						data-testid="voice-tts-provider"
					>
						<option value="">Inherit global default</option>
						{#each ttsChoices as row (row.id)}
							<option value={String(row.id)}>{providerOptionLabel(row)}</option>
						{/each}
					</select>
					{#if draft.tts_provider_id === null}
						<p class="text-ink-subtle m-0 text-[11px] italic" data-testid="fallback-tts">
							{fallbackLabel(ttsChoices)}
						</p>
					{/if}
					{#if fieldErrors.tts_voice_id}
						<p class="text-destructive m-0 text-xs" data-testid="error-tts-voice">
							{fieldErrors.tts_voice_id}
						</p>
					{/if}
				</div>

				{#if selectedTts !== null}
					<div class="flex flex-col gap-2">
						<div class="flex flex-wrap items-center gap-2 text-xs">
							<span class="text-foreground font-medium">Voice</span>
							<span class="text-muted-foreground">
								{#if draft.tts_voice_id}
									<span class="font-mono">{draft.tts_voice_id}</span>
								{:else}
									provider default
								{/if}
							</span>
							{#if draft.tts_voice_id}
								<Button
									variant="ghost"
									size="sm"
									class="h-6 px-2 text-xs"
									onclick={() => (draft.tts_voice_id = null)}
									data-testid="voice-clear"
								>
									Use provider default
								</Button>
							{/if}
						</div>
						<VoicePicker
							kind="tts"
							providerName={selectedTts.provider_name}
							providerId={selectedTts.id}
							values={{}}
							value={draft.tts_voice_id ?? ''}
							onSelect={(id) => (draft.tts_voice_id = id)}
						/>
					</div>
				{:else}
					<p class="text-muted-foreground m-0 text-xs">
						Pin a TTS provider to browse its voice catalog — voice ids are
						provider-specific.
					</p>
				{/if}

				<div class="flex flex-col gap-2">
					<div class="flex flex-wrap items-center gap-2">
						<Button
							variant="outline"
							size="sm"
							disabled={!canTestVoice && !testing}
							onclick={handleTestVoice}
							data-testid="voice-test"
							title={canTestVoice
								? 'Synthesize a sample with this agent’s provider + voice'
								: 'Pin a TTS provider (or save the agent) to test the voice'}
						>
							<PlayIcon class="size-3" />
							{testing ? 'Stop' : 'Test voice'}
						</Button>
						{#if testNote && !testError}
							<span class="text-muted-foreground text-xs" data-testid="voice-test-note">
								{testNote}
							</span>
						{/if}
					</div>
					{#if testError}
						<Alert.Root variant="destructive" data-testid="voice-test-error">
							<CircleAlertIcon />
							<Alert.Description>{testError}</Alert.Description>
						</Alert.Root>
					{/if}
					{#if testWarning}
						<Alert.Root data-testid="voice-test-warning">
							<TriangleAlertIcon />
							<Alert.Description>{testWarning}</Alert.Description>
						</Alert.Root>
					{/if}
				</div>
			</div>
		</section>

		<!-- ─── CAPABILITIES ─────────────────────────────────────────────── -->
		<section
			id="capabilities"
			class="border-border bg-surface-2 flex scroll-mt-6 flex-col gap-4 rounded-md border p-5"
			data-testid="section-capabilities"
		>
			<header class="flex flex-col gap-0.5">
				<h2 class="text-foreground m-0 text-xs font-semibold tracking-widest uppercase">
					Capabilities
				</h2>
				<p class="text-muted-foreground m-0 text-xs">
					This agent's tool scope in the layered policy engine. Rules here apply to
					<em>this agent only</em>, on top of its workspace's base policy — deny a tool here
					and other agents on the same workspace keep it. The catalog, skills, MCP servers,
					and base policy all live on the agent's <strong>workspace</strong> (set below).
				</p>
			</header>

			<!-- Workspace attachment (Johnny-wks.5): WHERE delegated work runs —
			     which sandbox container and skill packages. Orthogonal to the
			     policy layers below (what it MAY run). -->
			<div class="flex flex-col gap-1.5" data-testid="workspace-attachment">
				<label class="text-foreground text-xs font-medium" for="agent-workspace">
					Workspace
				</label>
				<div class="flex max-w-md items-center gap-2">
					<select
						id="agent-workspace"
						class={`${inputClass} flex-1`}
						value={pickedWorkspace === null ? '' : String(pickedWorkspace.id)}
						onchange={(e) => handleWorkspaceChange(e.currentTarget.value)}
						data-testid="agent-workspace-select"
					>
						{#if pickedWorkspace === null}
							<option value="">
								{workspaces.length === 0 ? 'Loading workspaces…' : 'Unknown workspace'}
							</option>
						{/if}
						{#each workspaces as ws (ws.id)}
							<option value={String(ws.id)}>{ws.name}{ws.is_default ? ' (default)' : ''}</option>
						{/each}
					</select>
					{#if agent !== null && (patch.workspace_id !== undefined)}
						<Badge variant="outline" data-testid="workspace-unsaved-badge">applies after save</Badge>
					{/if}
				</div>
				<p class="text-muted-foreground m-0 text-xs">
					The execution environment for this agent's delegated tasks — its skills, sandbox
					container, and tool state. Shared by every agent attached to it.
				</p>
				{#if pickedWorkspace !== null}
					<p class="text-muted-foreground m-0 text-xs" data-testid="workspace-summary">
						<span class="text-foreground font-medium">{pickedWorkspace.name}</span>
						· {pickedWorkspace.agent_count} agent{pickedWorkspace.agent_count === 1 ? '' : 's'}
						{#if workspaceSkillsSummary !== null}
							· {workspaceSkillsSummary.available} of {workspaceSkillsSummary.total} skill{workspaceSkillsSummary.total ===
							1
								? ''
								: 's'} available
						{/if}
						·
						<a href={`/workspaces/${pickedWorkspace.id}`} class="underline" data-testid="workspace-open-link">
							Open workspace
						</a>
					</p>
				{/if}
			</div>

			{#if agent !== null}
				<Separator />
				<ToolsPanel agentId={agent.id} />
			{:else}
				<p class="text-muted-foreground m-0 text-sm" data-testid="capabilities-locked">
					Save the agent first — the capability policy attaches to the saved agent.
				</p>
			{/if}
		</section>
	{/if}
</Page>
