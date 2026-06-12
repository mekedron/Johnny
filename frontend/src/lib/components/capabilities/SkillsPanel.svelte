<script lang="ts">
	import { onMount } from 'svelte';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { cn } from '$lib/utils.js';
	import {
		listSkills,
		skillStatus,
		toggleTool,
		SKILL_STATUS_LABEL,
		type SkillRead,
		type SkillStatus
	} from '$lib/capabilities';

	// Inventory is keyed by sandbox (Phase-7 note): exactly one ('global')
	// today, so the panel takes no props yet — per-agent personal sandboxes
	// later select a different inventory, same component.

	let skills = $state<SkillRead[]>([]);
	let skillsDir = $state('');
	let loading = $state(false);
	let loaded = $state(false);
	let errorMessage = $state<string | null>(null);
	let togglingKinds = $state<Set<string>>(new Set());
	/** Kind → explanation when an enable left the kind denied elsewhere. */
	let toggleNotes = $state<Record<string, string>>({});

	const STATUS_BADGE_CLASS: Record<SkillStatus, string> = {
		available: 'border-transparent bg-primary/15 text-primary',
		unavailable: 'border-warning/40 bg-warning/10 text-warning',
		ineligible: 'border-transparent bg-surface-3 text-muted-foreground',
		disabled: 'border-destructive/40 bg-destructive/10 text-destructive'
	};

	async function refresh() {
		loading = true;
		errorMessage = null;
		try {
			const res = await listSkills();
			skills = res.skills;
			skillsDir = res.skills_dir;
			loaded = true;
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to load skills';
		} finally {
			loading = false;
		}
	}

	async function handleToggle(skill: SkillRead) {
		const wantEnabled = !skill.enabled;
		togglingKinds = new Set([...togglingKinds, skill.kind]);
		const remainingNotes = { ...toggleNotes };
		delete remainingNotes[skill.kind];
		toggleNotes = remainingNotes;
		try {
			const out = await toggleTool(skill.kind, wantEnabled);
			if (wantEnabled && !out.enabled) {
				toggleNotes = {
					...toggleNotes,
					[skill.kind]: `Still denied by the ${out.layer} layer (rule "${out.rule}") — edit that rule on the Tools tab.`
				};
			}
			await refresh();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to toggle skill';
		} finally {
			const next = new Set(togglingKinds);
			next.delete(skill.kind);
			togglingKinds = next;
		}
	}

	onMount(() => {
		void refresh();
	});
</script>

<section class="flex flex-col gap-4" data-testid="skills-panel">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<p class="text-muted-foreground m-0 text-sm">
			Skill packages discovered on the sandbox volume
			{#if skillsDir}<span class="font-mono text-xs">({skillsDir})</span>{/if}
			— rescanned on every refresh, exactly what the next session loads.
		</p>
		<Button
			variant="outline"
			size="sm"
			onclick={() => void refresh()}
			disabled={loading}
			data-testid="skills-refresh"
		>
			<RefreshCwIcon class={cn('size-3.5', loading && 'animate-spin')} />
			{loading ? 'Scanning…' : 'Refresh'}
		</Button>
	</div>

	{#if errorMessage}
		<p class="text-destructive text-sm" role="alert" data-testid="skills-error">
			{errorMessage}
		</p>
	{/if}

	{#if loaded && skills.length === 0 && !errorMessage}
		<p class="text-muted-foreground text-sm italic" data-testid="skills-empty">
			No skills on the volume yet — drop a <span class="font-mono">&lt;name&gt;/SKILL.md</span>
			package into the skills directory and refresh.
		</p>
	{/if}

	<ul class="m-0 flex list-none flex-col gap-3 p-0">
		{#each skills as skill (skill.kind)}
			{@const status = skillStatus(skill)}
			<li
				class="border-border bg-card rounded-lg border p-4"
				data-testid="skill-{skill.kind}"
			>
				<div class="flex flex-wrap items-start justify-between gap-3">
					<div class="flex min-w-0 flex-col gap-1">
						<div class="flex flex-wrap items-center gap-2">
							<span class="text-foreground font-mono text-sm font-semibold">
								{skill.kind}
							</span>
							<Badge
								variant="outline"
								class={STATUS_BADGE_CLASS[status]}
								data-testid="skill-{skill.kind}-status"
							>
								{SKILL_STATUS_LABEL[status]}
							</Badge>
						</div>
						<p class="text-muted-foreground m-0 text-sm">{skill.description}</p>
					</div>
					<Button
						variant={skill.enabled ? 'outline' : 'default'}
						size="sm"
						disabled={togglingKinds.has(skill.kind)}
						onclick={() => void handleToggle(skill)}
						data-testid="skill-{skill.kind}-toggle"
					>
						{togglingKinds.has(skill.kind) ? '…' : skill.enabled ? 'Disable' : 'Enable'}
					</Button>
				</div>

				{#if !skill.eligible && skill.reasons.length > 0}
					<ul
						class="text-muted-foreground m-0 mt-2 flex list-none flex-col gap-0.5 p-0 text-xs"
						data-testid="skill-{skill.kind}-reasons"
					>
						{#each skill.reasons as reason (reason)}
							<li>· {reason}</li>
						{/each}
					</ul>
				{/if}
				{#if skill.eligible && !skill.available && skill.unavailable_reason}
					<p class="text-warning m-0 mt-2 text-xs" data-testid="skill-{skill.kind}-unavailable">
						{skill.unavailable_reason}
					</p>
				{/if}
				{#if !skill.enabled}
					<p class="text-destructive/90 m-0 mt-2 text-xs" data-testid="skill-{skill.kind}-denied">
						Hidden from the router catalog — denied at the
						<span class="font-mono">{skill.policy_layer}</span> layer
						{#if skill.policy_rule}(rule <span class="font-mono">{skill.policy_rule}</span>){/if}
					</p>
				{/if}
				{#if toggleNotes[skill.kind]}
					<p class="text-warning m-0 mt-2 text-xs" data-testid="skill-{skill.kind}-toggle-note">
						{toggleNotes[skill.kind]}
					</p>
				{/if}

				{#if skill.body_preview}
					<details class="mt-2">
						<summary class="text-muted-foreground cursor-pointer text-xs select-none">
							Instructions preview
						</summary>
						<pre
							class="bg-surface-3 text-muted-foreground mt-1 max-h-48 overflow-auto rounded-md p-2 text-xs whitespace-pre-wrap">{skill.body_preview}</pre>
					</details>
				{/if}
			</li>
		{/each}
	</ul>
</section>
