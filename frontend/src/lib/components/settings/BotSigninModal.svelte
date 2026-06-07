<!--
  BotSigninModal (Johnny-105) — noVNC-embedded Google sign-in.

  Lifecycle:
    1. onMount: POST /start to spawn the bot-signin container.
    2. Build the RFB canvas pointed at the proxy WS, with the bearer
       token in the query string.
    3. Poll /status every 1.5 s until the supervisor reports a
       terminal state. On signed_in, show success + an optional inline
       rename for placeholder emails. On any other terminal state,
       surface the error and let the user close.
    4. On cancel / unmount: best-effort POST /cancel so an abandoned
       Chromium doesn't sit waiting until the TTL fires.

  The noVNC viewer is loaded via dynamic import so a missing package
  surfaces as a friendly inline error instead of breaking the whole
  settings page build.
-->
<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import XIcon from '@lucide/svelte/icons/x';
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import CheckCircle2Icon from '@lucide/svelte/icons/check-circle-2';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import LoaderIcon from '@lucide/svelte/icons/loader';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import {
		buildProxyWsUrl,
		cancelBotSignin,
		getBotSigninStatus,
		renameAccount,
		startBotSignin,
		type BotSigninStatusResponse
	} from '$lib/bot-signin';
	import type { Account } from '$lib/accounts';

	type Props = {
		account?: Account | null;
		emailHint?: string | null;
		title?: string | null;
		onClose: (result: BotSigninStatusResponse | null) => void;
	};

	let {
		account = null,
		emailHint = null,
		title = null,
		onClose
	}: Props = $props();

	const POLL_INTERVAL_MS = 1500;

	let canvasContainer = $state<HTMLDivElement | null>(null);
	let signinId = $state<string | null>(null);
	let statusResponse = $state<BotSigninStatusResponse | null>(null);
	let phase = $state<
		'starting' | 'connecting' | 'awaiting' | 'finalizing' | 'done' | 'error'
	>('starting');
	let errorMessage = $state<string | null>(null);
	let viewerError = $state<string | null>(null);
	let pollHandle: ReturnType<typeof setTimeout> | null = null;
	let rfb: { disconnect: () => void } | null = null;
	let renameValue = $state('');
	let renameBusy = $state(false);
	let renameError = $state<string | null>(null);
	let renamedAccount = $state<Account | null>(null);

	const heading = $derived(
		title ??
			(account
				? `Sign in as ${account.email}`
				: 'Sign in to Google as the meeting bot')
	);

	const placeholderActive = $derived(
		statusResponse?.account
			? statusResponse.account.email.startsWith('unknown-') &&
				statusResponse.account.email.endsWith('@johnny.local')
			: false
	);

	const finalAccount = $derived(
		renamedAccount ?? statusResponse?.account ?? null
	);

	onMount(() => {
		void start();
	});

	onDestroy(() => {
		teardownPoll();
		teardownRfb();
		// Best-effort cancel if the supervisor is still running.
		if (signinId && phase !== 'done' && phase !== 'error') {
			void cancelBotSignin(signinId).catch(() => {
				// nothing useful to do here — the worker sweep will clean it up
			});
		}
	});

	function teardownPoll() {
		if (pollHandle !== null) {
			clearTimeout(pollHandle);
			pollHandle = null;
		}
	}

	function teardownRfb() {
		if (rfb) {
			try {
				rfb.disconnect();
			} catch {
				// disconnect throws if the socket was already closed; ignore
			}
			rfb = null;
		}
	}

	async function start() {
		try {
			const resp = await startBotSignin({
				account_id: account?.id ?? null,
				email_hint: emailHint ?? account?.email ?? null
			});
			signinId = resp.signin_session_id;
			phase = 'connecting';
			await attachViewer(resp.proxy_ws_path, resp.token);
			phase = 'awaiting';
			schedulePoll();
		} catch (e) {
			errorMessage = e instanceof Error ? e.message : String(e);
			phase = 'error';
		}
	}

	async function attachViewer(proxyWsPath: string, token: string) {
		if (!canvasContainer) {
			throw new Error('noVNC canvas container failed to mount');
		}
		const wsUrl = buildProxyWsUrl(proxyWsPath, token);
		try {
			// Dynamic import keeps the page bundle small and degrades
			// gracefully if @novnc/novnc isn't installed yet. The
			// package's exports field is a bare string ("./core/rfb.js")
			// so the bare module name resolves directly to RFB.
			const RFB = (await import('@novnc/novnc')).default;
			rfb = new RFB(canvasContainer, wsUrl, {
				wsProtocols: ['binary']
			});
			// Scale the framebuffer to fit our 1280×720 canvas slot so the
			// signed-in browser is always fully visible.
			Object.assign(rfb as unknown as { scaleViewport?: boolean }, {
				scaleViewport: true,
				resizeSession: false
			});
		} catch (e) {
			viewerError =
				e instanceof Error
					? `Could not load noVNC viewer: ${e.message}`
					: 'Could not load noVNC viewer';
			throw e;
		}
	}

	function schedulePoll() {
		teardownPoll();
		pollHandle = setTimeout(() => {
			void pollOnce();
		}, POLL_INTERVAL_MS);
	}

	async function pollOnce() {
		if (!signinId) return;
		try {
			const resp = await getBotSigninStatus(signinId);
			statusResponse = resp;
			if (resp.status === 'pending') {
				schedulePoll();
				return;
			}
			if (resp.status === 'signed_in') {
				phase = 'done';
				teardownRfb();
				if (resp.account) {
					renameValue = resp.account.email;
				}
				return;
			}
			phase = 'error';
			errorMessage = resp.error ?? `Sign-in ${resp.status}`;
			teardownRfb();
		} catch (e) {
			errorMessage = e instanceof Error ? e.message : String(e);
			phase = 'error';
			teardownRfb();
		}
	}

	function close(result: BotSigninStatusResponse | null) {
		teardownPoll();
		teardownRfb();
		onClose(result);
	}

	async function userCancel() {
		teardownPoll();
		teardownRfb();
		if (signinId) {
			try {
				await cancelBotSignin(signinId);
			} catch {
				// Even if cancel fails the user still wants out — the worker
				// sweep will collect the orphan.
			}
		}
		onClose(null);
	}

	async function submitRename(event: Event) {
		event.preventDefault();
		const target = statusResponse?.account;
		if (!target) return;
		const next = renameValue.trim().toLowerCase();
		if (!next || next === target.email) {
			return;
		}
		renameBusy = true;
		renameError = null;
		try {
			const updated = await renameAccount(target.id, next);
			renamedAccount = updated;
		} catch (e) {
			renameError = e instanceof Error ? e.message : String(e);
		} finally {
			renameBusy = false;
		}
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		if (phase === 'done') {
			close(statusResponse);
		} else {
			void userCancel();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<div
	class="fixed inset-0 z-50 flex items-center justify-center bg-background/40 backdrop-blur-sm"
	data-testid="bot-signin-overlay"
>
	<div
		class="m-4 flex w-full max-w-[64rem] flex-col gap-4 rounded-md border border-border bg-card p-6 shadow-lg"
		role="dialog"
		aria-modal="true"
		aria-labelledby="bot-signin-heading"
		data-testid="bot-signin-dialog"
	>
		<div class="flex items-start justify-between gap-3">
			<div class="flex flex-col gap-1">
				<h3
					id="bot-signin-heading"
					class="m-0 text-base font-semibold tracking-tight"
				>
					{heading}
				</h3>
				<p class="m-0 text-xs text-muted-foreground">
					Sign in to Google in the embedded browser below. Johnny captures
					the resulting session cookies and uses them to join Meet calls
					as this identity.
				</p>
			</div>
			<button
				type="button"
				class="rounded-md p-1 text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
				onclick={() => {
					if (phase === 'done') {
						close(statusResponse);
					} else {
						void userCancel();
					}
				}}
				aria-label="Close"
				data-testid="bot-signin-close"
			>
				<XIcon class="size-4" />
			</button>
		</div>

		{#if phase === 'error'}
			<Alert.Root variant="destructive" data-testid="bot-signin-error">
				<CircleAlertIcon />
				<Alert.Title>Sign-in failed</Alert.Title>
				<Alert.Description
					>{errorMessage ?? 'Unknown error'}</Alert.Description
				>
			</Alert.Root>
		{:else if phase === 'done'}
			<Alert.Root data-testid="bot-signin-success">
				<CheckCircle2Icon />
				<Alert.Title>Signed in</Alert.Title>
				<Alert.Description>
					Stored a Playwright session for
					<strong>{finalAccount?.email}</strong>. Johnny will reuse it on
					the next Meet join.
				</Alert.Description>
			</Alert.Root>

			{#if placeholderActive}
				<form
					class="flex flex-col gap-2 rounded-md border border-border bg-surface-1 p-3"
					onsubmit={submitRename}
					data-testid="bot-signin-rename-form"
				>
					<label
						for="bot-signin-rename"
						class="text-xs font-medium text-foreground"
					>
						Couldn't read the email automatically. Update it so the
						settings page shows the right address:
					</label>
					<input
						id="bot-signin-rename"
						type="email"
						bind:value={renameValue}
						class="border-input flex h-9 w-full rounded-md border bg-background px-3 py-1 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]"
						data-testid="bot-signin-rename-input"
						required
					/>
					{#if renameError}
						<p
							class="text-xs text-destructive"
							data-testid="bot-signin-rename-error"
						>
							{renameError}
						</p>
					{/if}
					<div class="flex items-center justify-end gap-2 pt-1">
						<Button
							type="submit"
							size="sm"
							disabled={renameBusy ||
								!renameValue.trim() ||
								renameValue.trim() === statusResponse?.account?.email}
							data-testid="bot-signin-rename-submit"
						>
							{renameBusy ? 'Saving…' : 'Save email'}
						</Button>
					</div>
				</form>
			{/if}
		{/if}

		<div
			class="relative flex aspect-[16/9] w-full overflow-hidden rounded-md border border-border bg-black"
		>
			<div
				bind:this={canvasContainer}
				class="absolute inset-0"
				data-testid="bot-signin-canvas"
			></div>
			{#if phase === 'starting' || phase === 'connecting'}
				<div
					class="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-black/70 text-white"
					data-testid="bot-signin-loading"
				>
					<LoaderIcon class="size-6 animate-spin" />
					<p class="text-sm">
						{phase === 'starting'
							? 'Spawning sign-in container…'
							: 'Connecting to embedded browser…'}
					</p>
				</div>
			{:else if phase === 'done'}
				<div
					class="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-black/70 text-white"
				>
					<CheckCircle2Icon class="size-8" />
					<p class="text-sm">Signed in. You can close this dialog.</p>
				</div>
			{/if}
			{#if viewerError}
				<div
					class="absolute inset-0 z-20 flex items-center justify-center bg-black/80 p-4 text-center text-xs text-white"
					data-testid="bot-signin-viewer-error"
				>
					<div class="flex flex-col gap-2">
						<CircleAlertIcon class="mx-auto size-6 text-warning" />
						<p>{viewerError}</p>
						<p class="text-[0.7rem] text-muted-foreground">
							If @novnc/novnc was just added, rebuild the frontend
							image and reload this page.
						</p>
					</div>
				</div>
			{/if}
		</div>

		<div
			class="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"
		>
			<span data-testid="bot-signin-phase">
				{#if phase === 'awaiting'}
					Waiting for sign-in…
				{:else if phase === 'starting'}
					Starting…
				{:else if phase === 'connecting'}
					Connecting…
				{:else if phase === 'done'}
					Done.
				{:else if phase === 'error'}
					Aborted.
				{/if}
				{#if statusResponse?.expires_at && phase === 'awaiting'}
					<span class="font-mono">
						(times out at
						{new Date(statusResponse.expires_at).toLocaleTimeString()})
					</span>
				{/if}
			</span>
			<div class="flex items-center gap-2">
				{#if phase === 'awaiting'}
					<Button
						variant="ghost"
						size="sm"
						onclick={() => void pollOnce()}
						data-testid="bot-signin-refresh"
					>
						<RefreshCwIcon /> Check now
					</Button>
				{/if}
				{#if phase === 'done' || phase === 'error'}
					<Button
						onclick={() => close(statusResponse)}
						data-testid="bot-signin-done-close"
					>
						Done
					</Button>
				{:else}
					<Button
						variant="ghost"
						onclick={() => void userCancel()}
						data-testid="bot-signin-cancel"
					>
						Cancel
					</Button>
				{/if}
			</div>
		</div>
	</div>
</div>
