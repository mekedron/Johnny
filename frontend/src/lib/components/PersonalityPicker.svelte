<script lang="ts">
	import type { Personality } from '$lib/personalities';
	import { personalityOptions } from '$lib/personalities';

	interface Props {
		personalities: Personality[];
		/** Current selection: `null` = blank (no personality override). */
		value: number | null;
		onChange: (value: number | null) => void;
		id?: string;
		label?: string;
		helpText?: string;
		disabled?: boolean;
		/** Show the "Manage" link to the personalities library page. */
		showManageLink?: boolean;
	}

	let {
		personalities,
		value,
		onChange,
		id = 'personality-select',
		label = 'Personality',
		helpText = '',
		disabled = false,
		showManageLink = true
	}: Props = $props();

	const options = $derived(personalityOptions(personalities));
	// HTML <option> values are strings; encode null (the blank option) as ''.
	const selected = $derived(value === null ? '' : String(value));

	function handleChange(event: Event): void {
		const raw = (event.currentTarget as HTMLSelectElement).value;
		onChange(raw === '' ? null : Number(raw));
	}
</script>

<div class="flex flex-col gap-2">
	<div class="flex items-center justify-between gap-2">
		<label for={id} class="text-foreground text-sm leading-none font-medium">{label}</label>
		{#if showManageLink}
			<a
				href="/personalities"
				class="text-muted-foreground hover:text-foreground text-xs hover:underline"
				data-testid="personality-manage-link"
			>
				Manage
			</a>
		{/if}
	</div>
	<select
		{id}
		value={selected}
		onchange={handleChange}
		{disabled}
		data-testid="personality-select"
		class="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 flex h-9 w-full rounded-md border px-3 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50"
	>
		{#each options as opt (opt.value ?? 'blank')}
			<option value={opt.value === null ? '' : String(opt.value)}>{opt.label}</option>
		{/each}
	</select>
	{#if helpText}
		<p class="text-muted-foreground m-0 text-xs">{helpText}</p>
	{/if}
</div>
