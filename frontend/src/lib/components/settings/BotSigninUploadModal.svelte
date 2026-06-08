<!--
  BotSigninUploadModal (Johnny-ckz.23) — CLI helper + storage_state
  upload pane. The "Upload" branch of the sign-in method picker.

  Three pieces visible at the same time:
    1. The exact `python -m johnny.tools.seed_auth_state` invocation
       the operator runs on their workstation, with a copy button.
       Inline (not "see docs") because the whole point of this surface
       is to keep the CLI path first-class.
    2. An email field (only for the New-bot variant — Replace and
       Attach already know the account row's email and pin it).
    3. A file picker that reads the storage_state.json into memory and
       POSTs/PUTs it through the typed client. Validation errors
       surface inline as a destructive alert; success closes back to
       the settings page with the chosen / created Account row so the
       picker can persist the user's last-used method per account.
-->
<script lang="ts">
	import { untrack } from 'svelte';
	import XIcon from '@lucide/svelte/icons/x';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import CheckCircle2Icon from '@lucide/svelte/icons/check-circle-2';
	import CopyIcon from '@lucide/svelte/icons/copy';
	import UploadIcon from '@lucide/svelte/icons/upload';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import {
		buildSeedAuthStateCommand,
		uploadBotSessionForNew,
		uploadBotSessionToAccount
	} from '$lib/bot-signin';
	import type { Account } from '$lib/accounts';

	type Props = {
		account?: Account | null;
		title?: string | null;
		// When provided the email field is hidden and pinned to this
		// value (Attach to existing row from a calendar-only candidate).
		emailLock?: string | null;
		onClose: (result: Account | null) => void;
	};

	let {
		account = null,
		title = null,
		emailLock = null,
		onClose
	}: Props = $props();

	// The modal mounts fresh each time the user picks "Upload", so the
	// props don't change after mount — `email`'s starting value is a
	// one-shot read. `untrack` documents that and silences the warning.
	let email = $state(
		untrack(() => account?.email ?? emailLock ?? '')
	);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let copyOk = $state(false);
	let successAccount = $state<Account | null>(null);
	let fileInput: HTMLInputElement | null = $state(null);

	const heading = $derived(
		title ??
			(account
				? `Upload session for ${account.email}`
				: 'Upload a bot session')
	);
	const emailReadonly = $derived(
		account !== null || (emailLock !== null && emailLock !== '')
	);
	const command = $derived(
		buildSeedAuthStateCommand({
			email: email || account?.email || null,
			accountId: account?.id ?? null
		})
	);
	const canSubmit = $derived(
		!busy && !!email.trim() && email.includes('@')
	);

	async function copyCommand() {
		try {
			await navigator.clipboard.writeText(command);
			copyOk = true;
			setTimeout(() => (copyOk = false), 1800);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	function pickFile() {
		fileInput?.click();
	}

	async function onFileChosen(event: Event) {
		const target = event.target as HTMLInputElement;
		const file = target.files?.[0];
		target.value = '';
		if (!file) return;
		await uploadFile(file);
	}

	async function uploadFile(file: File) {
		busy = true;
		error = null;
		try {
			const buf = await file.arrayBuffer();
			const result = account
				? await uploadBotSessionToAccount(account.id, buf)
				: await uploadBotSessionForNew(email.trim().toLowerCase(), buf);
			successAccount = result;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}

	function close() {
		onClose(successAccount);
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		close();
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<div
	class="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center bg-background/40 backdrop-blur-sm"
	data-testid="bot-signin-upload-overlay"
>
	<div
		class="m-4 flex w-full max-w-[40rem] flex-col gap-4 rounded-md border border-border bg-card p-6 shadow-lg"
		role="dialog"
		aria-modal="true"
		aria-labelledby="bot-signin-upload-heading"
		data-testid="bot-signin-upload-dialog"
	>
		<div class="flex items-start justify-between gap-3">
			<div class="flex flex-col gap-1">
				<h3
					id="bot-signin-upload-heading"
					class="m-0 text-base font-semibold tracking-tight"
				>
					{heading}
				</h3>
				<p class="m-0 text-xs text-muted-foreground">
					Run the seeder on your workstation, then upload the resulting
					<code class="rounded bg-surface-1 px-1 py-0.5 text-[0.7rem]"
						>storage_state.json</code
					>. Lands in the same shared volume the noVNC path writes to, so
					the meet-worker and /playground see no difference.
				</p>
			</div>
			<button
				type="button"
				class="rounded-md p-1 text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
				onclick={close}
				aria-label="Close"
				data-testid="bot-signin-upload-close"
			>
				<XIcon class="size-4" />
			</button>
		</div>

		<!-- Email field — only when no account row is pinned. -->
		{#if !emailReadonly}
			<label class="flex flex-col gap-1" for="bot-signin-upload-email">
				<span class="text-xs font-medium text-foreground">Bot email</span>
				<input
					id="bot-signin-upload-email"
					type="email"
					bind:value={email}
					placeholder="bot@example.com"
					class="border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 font-mono text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]"
					required
					data-testid="bot-signin-upload-email-input"
				/>
				<span class="text-[0.7rem] text-muted-foreground">
					The Google account this storage_state.json was signed in as. If a
					row with this address already exists, it gains the bot capability;
					otherwise a new bot-only row is created.
				</span>
			</label>
		{:else}
			<div class="flex flex-col gap-1">
				<span class="text-xs font-medium text-foreground">Bot email</span>
				<span
					class="font-mono text-sm text-foreground"
					data-testid="bot-signin-upload-email-pinned">{email}</span
				>
			</div>
		{/if}

		<!-- CLI command block with copy. -->
		<div class="flex flex-col gap-2">
			<div class="flex items-center justify-between">
				<span class="text-xs font-medium text-foreground"
					>1. Run the seeder on your workstation</span
				>
				<Button
					variant="outline"
					size="sm"
					onclick={copyCommand}
					data-testid="bot-signin-upload-copy"
				>
					<CopyIcon /> {copyOk ? 'Copied' : 'Copy'}
				</Button>
			</div>
			<pre
				class="m-0 overflow-x-auto rounded-md border border-border bg-surface-1 p-3 font-mono text-[0.75rem] leading-relaxed text-foreground"
				data-testid="bot-signin-upload-command">{command}</pre>
			<p class="m-0 text-[0.7rem] text-muted-foreground">
				A headed Chromium opens — sign in to Google as this email. The
				script writes <code>storage_state.json</code> into a temp dir; download
				it (or pull from the docker volume if your stack is co-located) and
				upload below.
			</p>
		</div>

		<!-- File picker. -->
		<div class="flex flex-col gap-2">
			<span class="text-xs font-medium text-foreground"
				>2. Upload the generated file</span
			>
			<input
				bind:this={fileInput}
				type="file"
				accept="application/json,.json"
				class="hidden"
				onchange={onFileChosen}
				data-testid="bot-signin-upload-file-input"
			/>
			<Button
				variant="outline"
				onclick={pickFile}
				disabled={!canSubmit}
				data-testid="bot-signin-upload-pick-file"
			>
				<UploadIcon /> {busy ? 'Uploading…' : 'Choose storage_state.json'}
			</Button>
		</div>

		{#if error}
			<Alert.Root variant="destructive" data-testid="bot-signin-upload-error">
				<CircleAlertIcon />
				<Alert.Title>Upload failed</Alert.Title>
				<Alert.Description>{error}</Alert.Description>
			</Alert.Root>
		{/if}

		{#if successAccount}
			<Alert.Root data-testid="bot-signin-upload-success">
				<CheckCircle2Icon />
				<Alert.Title>Uploaded</Alert.Title>
				<Alert.Description>
					Stored a Playwright session for
					<strong>{successAccount.email}</strong>. Johnny will reuse it on
					the next Meet join.
				</Alert.Description>
			</Alert.Root>
		{/if}

		<div class="flex items-center justify-end gap-2">
			{#if successAccount}
				<Button onclick={close} data-testid="bot-signin-upload-done"
					>Done</Button
				>
			{:else}
				<Button variant="ghost" onclick={close}>Cancel</Button>
			{/if}
		</div>
	</div>
</div>
