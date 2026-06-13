<!--
  Per-workspace capability inventory (Johnny-wks.5) — the trt.37
  sandbox-keyed views projected onto ONE workspace: its skill packages with
  bin/eligibility verdicts probed against ITS sandbox, and the merged tool
  catalog that workspace's agents render into their prompts.

  Read-only by design: policy editing (enable/disable, allow/deny layers)
  lives in the Capability policy section on this same page (Johnny-wks.9) —
  this panel is inventory, not governance.

  The capabilities GET is the refresh (wks.3) and lazily STARTS a
  non-default workspace's container, so when the parent says the container
  is not running the panel withholds the auto-fetch behind an explicit
  "Probe inventory" button — otherwise merely opening the page would undo a
  Stop. After any probe `onProbed` lets the parent re-read container state.
-->
<script lang="ts">
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { cn } from '$lib/utils.js';
	import {
		listCatalogTools,
		listSkills,
		skillStatus,
		SKILL_STATUS_LABEL,
		TOOL_SOURCE_LABEL,
		type CatalogToolRead,
		type SkillRead,
		type SkillStatus
	} from '$lib/capabilities';
	import type { WorkspaceDisplayState } from '$lib/workspaces';

	let {
		workspaceId,
		displayState = null,
		onProbed = undefined
	}: {
		workspaceId: number;
		/** Container display state from the parent — gates the auto-fetch. */
		displayState?: WorkspaceDisplayState | null;
		/** Called after a probe completed (it may have started the container). */
		onProbed?: () => void;
	} = $props();

	let skills = $state<SkillRead[]>([]);
	let skillsDir = $state('');
	let sandboxKey = $state('');
	let tools = $state<CatalogToolRead[]>([]);
	let loading = $state(false);
	let loaded = $state(false);
	let errorMessage = $state<string | null>(null);

	// null = state unknown (docker not driven — the GET still answers with
	// honest verdicts). Only a known-idle container ('stopped' /
	// 'never-started') withholds the auto-fetch, because fetching would start
	// it — true for EVERY workspace now, the default included (Johnny-etu.5).
	const probeStartsContainer = $derived(
		displayState === 'stopped' || displayState === 'never-started'
	);

	const STATUS_BADGE_CLASS: Record<SkillStatus, string> = {
		available: 'border-transparent bg-primary/15 text-primary',
		unavailable: 'border-warning/40 bg-warning/10 text-warning',
		ineligible: 'border-transparent bg-surface-3 text-muted-foreground',
		disabled: 'border-destructive/40 bg-destructive/10 text-destructive'
	};

	const availableCount = $derived(skills.filter((s) => skillStatus(s) === 'available').length);

	async function refresh() {
		loading = true;
		errorMessage = null;
		try {
			const [skillsRes, toolsRes] = await Promise.all([
				listSkills(workspaceId),
				listCatalogTools({ workspaceId })
			]);
			skills = skillsRes.skills;
			skillsDir = skillsRes.skills_dir;
			sandboxKey = skillsRes.sandbox;
			tools = toolsRes.tools;
			loaded = true;
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to load the inventory';
		} finally {
			loading = false;
			onProbed?.();
		}
	}

	// Auto-fetch per workspace, unless that would start an idle container.
	$effect(() => {
		void workspaceId;
		skills = [];
		tools = [];
		loaded = false;
		errorMessage = null;
		if (!probeStartsContainer) {
			void refresh();
		}
	});
</script>

<section class="flex flex-col gap-4" data-testid="workspace-inventory">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<p class="text-muted-foreground m-0 text-sm">
			What this workspace's sandbox offers — skill packages on its volume
			{#if skillsDir}<span class="font-mono text-xs">({skillsDir})</span>{/if}
			and the tool catalog its agents see. Probed live
			{#if sandboxKey}against <span class="font-mono text-xs">{sandboxKey}</span>{/if}
			— allow/deny it in the Capability policy section below.
		</p>
		<Button
			variant="outline"
			size="sm"
			onclick={() => void refresh()}
			disabled={loading}
			data-testid="inventory-refresh"
		>
			<RefreshCwIcon class={cn('size-3.5', loading && 'animate-spin')} />
			{loading ? 'Probing…' : loaded ? 'Refresh' : 'Probe inventory'}
		</Button>
	</div>

	{#if errorMessage}
		<p class="text-destructive text-sm" role="alert" data-testid="inventory-error">
			{errorMessage}
		</p>
	{/if}

	{#if !loaded && !loading && probeStartsContainer}
		<p
			class="border-border bg-surface-1 text-muted-foreground m-0 rounded-md border border-dashed px-4 py-3 text-sm"
			data-testid="inventory-idle-note"
		>
			The container is not running, so the inventory hasn't been probed —
			<span class="text-foreground font-medium">Probe inventory</span> starts the container
			on demand and verifies every skill against it.
		</p>
	{:else if loaded}
		<div class="flex flex-col gap-5">
			<section class="flex flex-col gap-2" aria-label="Skills" data-testid="inventory-skills">
				<h3 class="text-foreground m-0 text-sm font-semibold">
					Skills
					<span class="text-muted-foreground font-normal">
						· {availableCount} of {skills.length} available
					</span>
				</h3>
				{#if skills.length === 0}
					<p class="text-muted-foreground m-0 text-sm italic" data-testid="inventory-skills-empty">
						No skill packages on this workspace's volume yet — install some from a
						delegated task or drop a <span class="font-mono">&lt;name&gt;/SKILL.md</span>
						into its skills directory.
					</p>
				{:else}
					<ul class="m-0 flex list-none flex-col gap-2 p-0">
						{#each skills as skill (skill.kind)}
							{@const status = skillStatus(skill)}
							<li
								class="border-border bg-card rounded-md border px-3 py-2.5"
								data-testid="inventory-skill-{skill.kind}"
							>
								<div class="flex flex-wrap items-center gap-2">
									<span class="text-foreground font-mono text-sm font-semibold">{skill.kind}</span>
									<Badge
										variant="outline"
										class={STATUS_BADGE_CLASS[status]}
										data-testid="inventory-skill-{skill.kind}-status"
									>
										{SKILL_STATUS_LABEL[status]}
									</Badge>
								</div>
								<p class="text-muted-foreground m-0 mt-1 text-xs">{skill.description}</p>
								{#if skill.missing_bins.length > 0}
									<p class="text-warning m-0 mt-1 text-xs">
										Missing binaries: <span class="font-mono">{skill.missing_bins.join(', ')}</span>
									</p>
								{/if}
								{#if !skill.eligible && skill.reasons.length > 0}
									<ul class="text-muted-foreground m-0 mt-1 flex list-none flex-col gap-0.5 p-0 text-xs">
										{#each skill.reasons as reason (reason)}
											<li>· {reason}</li>
										{/each}
									</ul>
								{:else if skill.eligible && !skill.available && skill.unavailable_reason}
									<p class="text-warning m-0 mt-1 text-xs">{skill.unavailable_reason}</p>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class="flex flex-col gap-2" aria-label="Tool catalog" data-testid="inventory-tools">
				<h3 class="text-foreground m-0 text-sm font-semibold">
					Tool catalog
					<span class="text-muted-foreground font-normal">· {tools.length} kinds</span>
				</h3>
				<ul class="m-0 flex list-none flex-col gap-1 p-0">
					{#each tools as tool (tool.kind)}
						<li
							class="border-border bg-card flex flex-wrap items-center gap-2 rounded-md border px-3 py-2"
							data-testid="inventory-tool-{tool.kind}"
						>
							<span class="text-foreground font-mono text-xs font-medium">{tool.kind}</span>
							<Badge variant="outline" class="text-muted-foreground text-[0.65rem]">
								{TOOL_SOURCE_LABEL[tool.source]}
							</Badge>
							{#if !tool.allowed}
								<Badge variant="outline" class="border-destructive/40 bg-destructive/10 text-destructive text-[0.65rem]">
									denied by policy
								</Badge>
							{:else if !tool.available}
								<Badge variant="outline" class="border-warning/40 bg-warning/10 text-warning text-[0.65rem]">
									unavailable
								</Badge>
							{/if}
							<span class="text-muted-foreground min-w-0 flex-1 truncate text-xs">
								{tool.one_liner}
							</span>
						</li>
					{/each}
				</ul>
			</section>
		</div>
	{/if}
</section>
