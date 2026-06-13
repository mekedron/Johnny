<script lang="ts">
	import { onMount } from 'svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { cn } from '$lib/utils.js';
	import {
		deletePolicy,
		describeDecision,
		effectivePolicy,
		findPolicyRow,
		formatPatterns,
		listCatalogTools,
		listPolicies,
		normalizePolicyDocument,
		parsePatterns,
		putPolicy,
		resolveCapability,
		toggleTool,
		TOOL_SOURCE_LABEL,
		type CatalogToolRead,
		type EffectiveOut,
		type PolicyCoordinates,
		type PolicyDocument,
		type PolicyRow,
		type PolicyScope
	} from '$lib/capabilities';

	// Embeddable by design (Johnny-wks.9): the workspace detail page passes
	// workspaceId and this edits that workspace's BASE policy layer; the agent
	// edit page (Johnny-trt.44) passes agentId and the SAME component edits
	// that agent's override layer. Exactly one is set — the standalone global
	// page (with its session-mode/session scope pills) was removed in wks.9.
	let {
		agentId = null,
		workspaceId = null
	}: { agentId?: number | null; workspaceId?: number | null } = $props();

	let rows = $state<PolicyRow[]>([]);
	let baselineSafeBins = $state<string[]>([]);
	let tools = $state<CatalogToolRead[]>([]);
	let effective = $state<EffectiveOut | null>(null);
	let loading = $state(false);
	let errorMessage = $state<string | null>(null);
	let statusMessage = $state<string | null>(null);

	// Editor state (textareas mirror the selected layer's stored document).
	let allowText = $state('');
	let alsoAllowText = $state('');
	let denyText = $state('');
	let binsDenyText = $state('');
	let safeBinsCustomized = $state(false);
	let safeBinsText = $state('');
	let saving = $state(false);
	let clearing = $state(false);
	let togglingKinds = $state<Set<string>>(new Set());
	let quickToggleNote = $state<string | null>(null);

	// Inspector state.
	let inspectKind = $state<'tool' | 'bin'>('tool');
	let inspectName = $state('');
	let inspectResult = $state<string | null>(null);
	let inspectAllowed = $state<boolean | null>(null);
	let inspecting = $state(false);

	const target = $derived.by<PolicyScope | null>(() => {
		if (agentId != null) return { scope: 'agent', agentId };
		if (workspaceId != null) return { scope: 'workspace', workspaceId };
		return null;
	});

	const coords = $derived.by<PolicyCoordinates>(() => {
		if (agentId != null) return { agentId };
		if (workspaceId != null) return { workspaceId };
		return {};
	});

	const isWorkspaceTarget = $derived(target?.scope === 'workspace');

	function scopeLabel(): string {
		if (agentId != null) return `agent #${agentId}`;
		if (workspaceId != null) return 'workspace';
		return 'scope';
	}

	function loadEditorFromRows() {
		if (target == null) return;
		const doc = normalizePolicyDocument(findPolicyRow(rows, target)?.document);
		allowText = formatPatterns(doc.tools_allow);
		alsoAllowText = formatPatterns(doc.tools_also_allow);
		denyText = formatPatterns(doc.tools_deny);
		binsDenyText = formatPatterns(doc.bins_deny);
		safeBinsCustomized = doc.safe_bins != null;
		safeBinsText = formatPatterns(doc.safe_bins ?? baselineSafeBins);
	}

	async function refreshAll() {
		loading = true;
		errorMessage = null;
		try {
			const policyList = await listPolicies();
			rows = policyList.rows;
			baselineSafeBins = policyList.baseline_safe_bins;
			const [catalog, eff] = await Promise.all([
				listCatalogTools(coords),
				effectivePolicy(coords)
			]);
			tools = catalog.tools;
			effective = eff;
			loadEditorFromRows();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to load capability policy';
		} finally {
			loading = false;
		}
	}

	function editorDocument(): PolicyDocument {
		return {
			tools_allow: parsePatterns(allowText),
			tools_also_allow: parsePatterns(alsoAllowText),
			tools_deny: parsePatterns(denyText),
			bins_deny: parsePatterns(binsDenyText),
			safe_bins: isWorkspaceTarget && safeBinsCustomized ? parsePatterns(safeBinsText) : null
		};
	}

	async function save() {
		if (target == null) return;
		saving = true;
		errorMessage = null;
		statusMessage = null;
		try {
			await putPolicy(target, editorDocument());
			statusMessage = `Saved the ${scopeLabel()} layer — applies from the next session start / delegated task, no restart.`;
			await refreshAll();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to save policy';
		} finally {
			saving = false;
		}
	}

	async function clearLayer() {
		if (target == null) return;
		clearing = true;
		errorMessage = null;
		statusMessage = null;
		try {
			const out = await deletePolicy(target);
			statusMessage = out.deleted
				? `Cleared the ${scopeLabel()} layer${isWorkspaceTarget ? ' (safe-bins back to the built-in baseline)' : ''}.`
				: `The ${scopeLabel()} layer had nothing stored.`;
			await refreshAll();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to clear policy layer';
		} finally {
			clearing = false;
		}
	}

	async function quickToggle(tool: CatalogToolRead) {
		const wantEnabled = !tool.allowed;
		togglingKinds = new Set([...togglingKinds, tool.kind]);
		quickToggleNote = null;
		try {
			const out = await toggleTool(tool.kind, wantEnabled, workspaceId);
			if (wantEnabled && !out.enabled) {
				quickToggleNote = `${tool.kind}: still denied by the ${out.layer} layer (rule "${out.rule}").`;
			}
			await refreshAll();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to toggle tool';
		} finally {
			const next = new Set(togglingKinds);
			next.delete(tool.kind);
			togglingKinds = next;
		}
	}

	async function inspect() {
		const name = inspectName.trim();
		if (name.length === 0) return;
		inspecting = true;
		inspectResult = null;
		inspectAllowed = null;
		try {
			const out = await resolveCapability(
				inspectKind === 'tool' ? { tool: name } : { bin: name },
				coords
			);
			inspectResult = describeDecision(out);
			inspectAllowed = out.allowed;
		} catch (err) {
			inspectResult = err instanceof Error ? err.message : 'Resolve failed';
			inspectAllowed = null;
		} finally {
			inspecting = false;
		}
	}

	function storedScopes(): string {
		if (rows.length === 0) return 'none';
		return rows
			.map((row) => {
				if (row.scope === 'workspace') return `workspace #${row.workspace_id}`;
				if (row.scope === 'agent') return `agent #${row.agent_id}`;
				if (row.scope === 'session_mode') return `${row.session_mode} mode`;
				if (row.scope === 'session') return `session #${row.bot_session_id}`;
				return row.scope;
			})
			.join(', ');
	}

	onMount(() => {
		void refreshAll();
	});

	const textareaClass =
		'border-input bg-background min-h-20 w-full rounded-md border px-3 py-2 font-mono text-xs shadow-xs outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]';
</script>

<section class="flex flex-col gap-6" data-testid="tools-panel">
	{#if errorMessage}
		<p class="text-destructive text-sm" role="alert" data-testid="tools-error">{errorMessage}</p>
	{/if}

	<!-- Catalog -->
	<div class="flex flex-col gap-2">
		<h2 class="text-foreground m-0 text-sm font-semibold">
			Tool catalog
			<span class="text-muted-foreground font-normal">
				— core + skill + MCP kinds as the router sees them at {scopeLabel()} scope
			</span>
		</h2>
		{#if quickToggleNote}
			<p class="text-warning m-0 text-xs" data-testid="tools-quicktoggle-note">{quickToggleNote}</p>
		{/if}
		{#if tools.length === 0 && !loading}
			<p class="text-muted-foreground text-sm italic">No catalog entries.</p>
		{/if}
		<ul class="m-0 flex list-none flex-col gap-1.5 p-0">
			{#each tools as tool (tool.kind)}
				<li
					class={cn(
						'border-border bg-card flex flex-wrap items-center gap-2 rounded-md border px-3 py-2',
						!tool.allowed && 'opacity-70'
					)}
					data-testid="tool-{tool.kind}"
				>
					<span class="text-foreground font-mono text-xs font-semibold">{tool.kind}</span>
					<Badge variant="outline" class="text-muted-foreground text-[10px] uppercase">
						{TOOL_SOURCE_LABEL[tool.source]}
					</Badge>
					{#if !tool.allowed}
						<Badge
							variant="outline"
							class="border-destructive/40 bg-destructive/10 text-destructive"
							data-testid="tool-{tool.kind}-denied"
						>
							denied · {tool.policy_layer}{tool.policy_rule ? ` · ${tool.policy_rule}` : ''}
						</Badge>
					{:else if !tool.available}
						<Badge
							variant="outline"
							class="border-warning/40 bg-warning/10 text-warning"
							title={tool.unavailable_reason}
						>
							unavailable
						</Badge>
					{/if}
					<span class="text-muted-foreground min-w-0 flex-1 truncate text-xs" title={tool.one_liner}>
						{tool.one_liner}
					</span>
					{#if workspaceId != null}
						<Button
							variant="ghost"
							size="sm"
							class="h-6 px-2 text-xs"
							disabled={togglingKinds.has(tool.kind) || (!tool.allowed && !tool.toggle_managed)}
							title={!tool.allowed && !tool.toggle_managed
								? 'Denied by a pattern rule — edit the layer document below'
								: undefined}
							onclick={() => void quickToggle(tool)}
							data-testid="tool-{tool.kind}-quicktoggle"
						>
							{togglingKinds.has(tool.kind) ? '…' : tool.allowed ? 'Deny' : 'Allow'}
						</Button>
					{/if}
				</li>
			{/each}
		</ul>
	</div>

	<!-- Policy editor -->
	<div class="border-border bg-card flex flex-col gap-4 rounded-lg border p-4">
		<div class="flex flex-wrap items-center justify-between gap-2">
			<h2 class="text-foreground m-0 text-sm font-semibold">Policy editor</h2>
			<Badge variant="outline" class="text-muted-foreground" data-testid="scope-badge">
				editing the {scopeLabel()} layer
			</Badge>
		</div>

		<p class="text-muted-foreground m-0 text-xs">
			Resolution order: workspace → per-agent → per-mode → per-session. Deny wins at every
			merge; a non-empty allow-list redefines what is allowed from that layer on; patterns are
			<span class="font-mono">fnmatch</span> globs (<span class="font-mono">mcp__shady__*</span>
			denies a whole server). Stored layers: {storedScopes()}.
		</p>

		{#if target == null}
			<p class="text-warning m-0 text-sm" data-testid="scope-empty-hint">
				Save the workspace or agent first to edit its policy layer.
			</p>
		{:else}
			<div class="grid gap-3 md:grid-cols-2">
				<label class="flex flex-col gap-1">
					<span class="text-muted-foreground text-xs font-medium">
						tools_allow <span class="font-normal">(non-empty = ONLY these kinds allowed)</span>
					</span>
					<textarea
						class={textareaClass}
						rows="3"
						placeholder="one glob per line"
						bind:value={allowText}
						data-testid="policy-allow"
					></textarea>
				</label>
				<label class="flex flex-col gap-1">
					<span class="text-muted-foreground text-xs font-medium">
						tools_also_allow <span class="font-normal">(extends an allow-list in force)</span>
					</span>
					<textarea
						class={textareaClass}
						rows="3"
						placeholder="one glob per line"
						bind:value={alsoAllowText}
						data-testid="policy-also-allow"
					></textarea>
				</label>
				<label class="flex flex-col gap-1">
					<span class="text-muted-foreground text-xs font-medium">
						tools_deny <span class="font-normal">(deny wins everywhere)</span>
					</span>
					<textarea
						class={textareaClass}
						rows="3"
						placeholder="one glob per line"
						bind:value={denyText}
						data-testid="policy-deny"
					></textarea>
				</label>
				<label class="flex flex-col gap-1">
					<span class="text-muted-foreground text-xs font-medium">
						bins_deny <span class="font-normal">(sandbox exec argv[0] basenames)</span>
					</span>
					<textarea
						class={textareaClass}
						rows="3"
						placeholder="one glob per line"
						bind:value={binsDenyText}
						data-testid="policy-bins-deny"
					></textarea>
				</label>
			</div>

			{#if isWorkspaceTarget}
				<div class="flex flex-col gap-2" data-testid="safe-bins-editor">
					<label class="flex items-center gap-2 text-xs font-medium">
						<input type="checkbox" bind:checked={safeBinsCustomized} data-testid="safe-bins-customize" />
						<span class="text-muted-foreground">
							Customize safe-bins (the guaranteed sandbox toolset; unchecked = built-in baseline of
							{baselineSafeBins.length} bins)
						</span>
					</label>
					{#if safeBinsCustomized}
						<textarea
							class={textareaClass}
							rows="4"
							placeholder="one binary per line"
							bind:value={safeBinsText}
							data-testid="safe-bins-text"
						></textarea>
						<div class="flex items-center gap-2">
							<Button
								variant="outline"
								size="sm"
								class="h-7 px-2 text-xs"
								onclick={() => (safeBinsText = formatPatterns(baselineSafeBins))}
								data-testid="safe-bins-fill-baseline"
							>
								Fill with baseline
							</Button>
							<span class="text-muted-foreground text-xs">
								Removing a baseline bin hard-denies it (beats skill requires). Uncheck to reset to default.
							</span>
						</div>
					{/if}
				</div>
			{/if}

			<div class="flex flex-wrap items-center gap-2">
				<Button size="sm" onclick={() => void save()} disabled={saving} data-testid="policy-save">
					{saving ? 'Saving…' : `Save ${scopeLabel()} layer`}
				</Button>
				<Button
					variant="outline"
					size="sm"
					onclick={() => void clearLayer()}
					disabled={clearing}
					data-testid="policy-clear"
				>
					{clearing ? 'Clearing…' : 'Clear layer'}
				</Button>
				{#if statusMessage}
					<span class="text-primary text-xs" data-testid="policy-status">{statusMessage}</span>
				{/if}
			</div>
		{/if}
	</div>

	<!-- Effective view + inspector -->
	<div class="grid gap-4 md:grid-cols-2">
		<div class="border-border bg-card flex flex-col gap-2 rounded-lg border p-4" data-testid="effective-summary">
			<h2 class="text-foreground m-0 text-sm font-semibold">Effective at {scopeLabel()} scope</h2>
			{#if effective}
				<dl class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
					<dt class="text-muted-foreground">Tools</dt>
					<dd class="m-0">
						{#if effective.tools_unrestricted}
							unrestricted (no deny rules, no allow-list)
						{:else if effective.allow_layer}
							allow-list in force from the
							<span class="font-mono">{effective.allow_layer}</span> layer
						{:else}
							deny rules active
						{/if}
					</dd>
					<dt class="text-muted-foreground">Safe-bins</dt>
					<dd class="m-0">
						{effective.safe_bins.length} bins{effective.safe_bins.length !==
						effective.baseline_safe_bins.length
							? ` (baseline: ${effective.baseline_safe_bins.length})`
							: ' (the built-in baseline)'}
					</dd>
					{#if effective.removed_baseline_bins.length > 0}
						<dt class="text-destructive">Removed bins</dt>
						<dd class="text-destructive m-0 font-mono" data-testid="effective-removed-bins">
							{effective.removed_baseline_bins.join(', ')}
						</dd>
					{/if}
					<dt class="text-muted-foreground">Layers</dt>
					<dd class="m-0">
						{effective.layers.length === 0
							? 'none stored for these coordinates'
							: effective.layers.map((l) => l.scope).join(' → ')}
					</dd>
				</dl>
			{:else if !loading}
				<p class="text-muted-foreground m-0 text-xs italic">Not loaded.</p>
			{/if}
		</div>

		<div class="border-border bg-card flex flex-col gap-2 rounded-lg border p-4">
			<h2 class="text-foreground m-0 text-sm font-semibold">
				Policy inspector
				<span class="text-muted-foreground font-normal">— which layer decides?</span>
			</h2>
			<div class="flex flex-wrap items-center gap-2">
				<div class="flex items-center gap-1" role="radiogroup" aria-label="Capability kind">
					<Button
						variant={inspectKind === 'tool' ? 'default' : 'outline'}
						size="sm"
						class="h-7 px-2 text-xs"
						onclick={() => (inspectKind = 'tool')}
						data-testid="inspector-kind-tool"
					>
						tool
					</Button>
					<Button
						variant={inspectKind === 'bin' ? 'default' : 'outline'}
						size="sm"
						class="h-7 px-2 text-xs"
						onclick={() => (inspectKind = 'bin')}
						data-testid="inspector-kind-bin"
					>
						bin
					</Button>
				</div>
				<Input
					placeholder={inspectKind === 'tool' ? 'kind, e.g. google-calendar' : 'binary, e.g. curl'}
					class="h-8 w-56 font-mono text-xs"
					bind:value={inspectName}
					onkeydown={(event: KeyboardEvent) => {
						if (event.key === 'Enter') void inspect();
					}}
					data-testid="inspector-input"
				/>
				<Button
					size="sm"
					class="h-8"
					onclick={() => void inspect()}
					disabled={inspecting || inspectName.trim().length === 0}
					data-testid="inspector-check"
				>
					{inspecting ? '…' : 'Check'}
				</Button>
			</div>
			{#if inspectResult}
				<p
					class={cn(
						'm-0 text-xs',
						inspectAllowed === true && 'text-primary',
						inspectAllowed === false && 'text-destructive',
						inspectAllowed === null && 'text-warning'
					)}
					data-testid="inspector-result"
				>
					{inspectResult}
				</p>
			{/if}
		</div>
	</div>
</section>
