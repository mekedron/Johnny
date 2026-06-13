<!--
  MeetingBotAccountPicker (Johnny-wks.7) — the agent edit page's control for
  the agent's MEETING-BOT identity: the Google account the agent JOINS Meet
  calls as (the Playwright storage_state the meet-worker mounts). This is the
  ONLY place an agent's meeting-bot identity is managed — the Settings page no
  longer carries a Meeting Bots section.

  Two ways to set it, both first-class:
    • SELECT an existing bot-capable google_accounts row from the dropdown.
    • CONNECT a new bot session via the existing sign-in flow (noVNC or
      storage_state upload), reusing the Settings modals verbatim; on success
      the freshly-connected account is auto-selected.

  Controlled component: `value` is the draft's meeting_bot_account_id and
  `onChange` writes back to the draft, so the agent edit page's existing
  dirty / diff / Save machinery persists the binding like any other field.
  Connecting a new bot creates the GLOBAL google_accounts row immediately
  (same as Settings ever did); Save only persists the agent → account binding.

  google_accounts rows are global and dual-capability — a calendar account can
  also be a bot identity, and two agents MAY point at the same row (opt-in
  shared identity).
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import BotIcon from '@lucide/svelte/icons/bot';
	import TriangleAlertIcon from '@lucide/svelte/icons/triangle-alert';
	import { Button } from '$lib/components/ui/button/index.js';
	import BotSigninMethodPicker, {
		type SigninMethod
	} from '$lib/components/settings/BotSigninMethodPicker.svelte';
	import BotSigninModal from '$lib/components/settings/BotSigninModal.svelte';
	import BotSigninUploadModal from '$lib/components/settings/BotSigninUploadModal.svelte';
	import { listAccounts, type Account } from '$lib/accounts';

	type Props = {
		/** The draft's meeting_bot_account_id (null = no agent-level identity). */
		value: number | null;
		/** Write the new selection back to the draft. */
		onChange: (accountId: number | null) => void;
		/** id for the <label for> association. */
		selectId?: string;
	};

	let { value, onChange, selectId = 'agent-meeting-bot' }: Props = $props();

	let accounts = $state<Account[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Sign-in flow modal state (mirrors the Settings page orchestration).
	let methodPicker = $state(false);
	let novnc = $state(false);
	let upload = $state(false);

	const botAccounts = $derived(accounts.filter((a) => a.bot_session.connected));
	const selected = $derived(accounts.find((a) => a.id === value) ?? null);
	// A selection pointing at a row with no bot session on disk: legal (the FK
	// only needs the row to exist), but the bot would hit Google's sign-in
	// screen — warn so the operator connects a session.
	const selectedMissingSession = $derived(
		selected !== null && !selected.bot_session.connected
	);

	function health(account: Account): 'fresh' | 'aging' | 'expiring' {
		const saved = account.bot_session.saved_at;
		if (!saved) return 'expiring';
		const ageDays = (Date.now() - new Date(saved).getTime()) / 86_400_000;
		if (ageDays > 75) return 'expiring';
		if (ageDays > 30) return 'aging';
		return 'fresh';
	}

	async function loadAccounts(): Promise<void> {
		loading = true;
		error = null;
		try {
			accounts = await listAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load accounts';
		} finally {
			loading = false;
		}
	}

	onMount(loadAccounts);

	function handleSelect(raw: string) {
		onChange(raw === '' ? null : Number.parseInt(raw, 10));
	}

	function openConnect() {
		methodPicker = true;
	}

	function onPickMethod(method: SigninMethod) {
		methodPicker = false;
		if (method === 'novnc') novnc = true;
		else upload = true;
	}

	// On a successful connect: refresh the list so the new row shows its
	// session, then auto-select it (the operator's intent in opening Connect).
	async function afterConnect(account: Account | null) {
		novnc = false;
		upload = false;
		await loadAccounts();
		if (account) onChange(account.id);
	}
</script>

<div class="flex flex-col gap-1.5" data-testid="meeting-bot-picker">
	<label class="text-foreground text-xs font-medium" for={selectId}>
		Meeting bot account
	</label>
	<div class="flex max-w-md flex-wrap items-center gap-2">
		<select
			id={selectId}
			class="border-border-strong bg-surface-3 text-foreground focus-visible:border-ring flex-1 rounded-sm border px-2 py-1.5 text-sm outline-none"
			value={value === null ? '' : String(value)}
			onchange={(e) => handleSelect(e.currentTarget.value)}
			disabled={loading}
			data-testid="meeting-bot-select"
		>
			<option value="">
				{loading ? 'Loading accounts…' : 'None — resolve per meeting'}
			</option>
			{#each botAccounts as account (account.id)}
				<option value={String(account.id)}>{account.email}</option>
			{/each}
			{#if selected !== null && !selected.bot_session.connected}
				<!-- Keep an out-of-band selection (calendar-only / session removed)
				     visible rather than silently snapping to None. -->
				<option value={String(selected.id)}>{selected.email} (no session)</option>
			{/if}
		</select>
		<Button
			variant="outline"
			onclick={openConnect}
			data-testid="meeting-bot-connect"
		>
			<BotIcon /> Connect a meeting bot
		</Button>
	</div>

	{#if error}
		<p class="text-warning m-0 flex items-center gap-1 text-xs" data-testid="meeting-bot-error">
			<TriangleAlertIcon class="size-3" />
			{error}
		</p>
	{:else if selected !== null}
		<p class="text-muted-foreground m-0 flex flex-wrap items-center gap-1.5 text-xs" data-testid="meeting-bot-status">
			{#if selectedMissingSession}
				<TriangleAlertIcon class="text-warning size-3" />
				<span class="text-warning"
					>No bot session on disk — connect one or the bot lands on Google's sign-in
					screen.</span
				>
			{:else if health(selected) === 'fresh'}
				<span class="bg-success size-2 rounded-full" aria-hidden="true"></span>
				Joins meetings as <span class="text-foreground font-medium">{selected.email}</span>
			{:else}
				<span class="bg-warning size-2 rounded-full" aria-hidden="true"></span>
				<span class="text-warning"
					>Cookies aging for <span class="font-medium">{selected.email}</span> — re-sign in soon.</span
				>
			{/if}
		</p>
	{:else}
		<p class="text-muted-foreground m-0 text-xs">
			The Google identity this agent joins meetings as. Leave as <em>None</em> to keep the
			per-meeting default. Two agents with different accounts appear as two participants in the
			same Meet; pointing two agents at one account shares the identity.
		</p>
	{/if}
</div>

{#if methodPicker}
	<BotSigninMethodPicker
		title="Connect a meeting bot"
		subtitle="Sign in to Google as the account this agent should join meetings as."
		defaultMethod="novnc"
		onPick={onPickMethod}
		onClose={() => (methodPicker = false)}
	/>
{/if}

{#if novnc}
	<BotSigninModal
		account={null}
		emailHint={null}
		title={null}
		onClose={(result) => afterConnect(result?.account ?? null)}
	/>
{/if}

{#if upload}
	<BotSigninUploadModal
		account={null}
		title={null}
		emailLock={null}
		onClose={(result) => afterConnect(result)}
	/>
{/if}
