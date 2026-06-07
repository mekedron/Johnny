<!--
  BotSigninMethodPicker (Johnny-ckz.23) — choose between the noVNC
  in-browser sign-in and the CLI helper + storage_state.json upload.

  Both paths are first-class and intentionally surfaced together:
  - noVNC is the zero-CLI default but requires the helper container
    and websockify to work (port 6080, x11vnc, Xvfb). In hardened
    deployments any of those can be unavailable.
  - The upload path stays operational whenever the user can SSH or run
    Playwright on a workstation. It's the proven fallback if the noVNC
    helper container fails (image build / port conflict / etc.).

  The user picks explicitly. We remember the last choice so re-signing
  the same account defaults to its previous method; new bots default to
  whichever method was used most recently anywhere.
-->
<script lang="ts">
	import { untrack } from 'svelte';
	import XIcon from '@lucide/svelte/icons/x';
	import MonitorIcon from '@lucide/svelte/icons/monitor';
	import UploadIcon from '@lucide/svelte/icons/upload';
	import { Button } from '$lib/components/ui/button/index.js';

	export type SigninMethod = 'novnc' | 'upload';

	type Props = {
		title: string;
		subtitle?: string | null;
		defaultMethod: SigninMethod;
		onPick: (method: SigninMethod) => void;
		onClose: () => void;
	};

	let {
		title,
		subtitle = null,
		defaultMethod,
		onPick,
		onClose
	}: Props = $props();

	// `defaultMethod` is a snapshot of the user's last-used choice at
	// open time — we use it as the INITIAL `selected` value but never
	// re-read it (the picker is mounted fresh each open, so the prop
	// won't change). `untrack` documents that intent and silences the
	// $state-references-prop warning.
	let selected = $state<SigninMethod>(untrack(() => defaultMethod));

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.preventDefault();
			onClose();
			return;
		}
		if (event.key === 'Enter') {
			event.preventDefault();
			onPick(selected);
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<div
	class="fixed inset-0 z-50 flex items-center justify-center bg-background/40 backdrop-blur-sm"
	data-testid="bot-signin-method-picker"
>
	<div
		class="m-4 flex w-full max-w-[36rem] flex-col gap-4 rounded-md border border-border bg-card p-6 shadow-lg"
		role="dialog"
		aria-modal="true"
		aria-labelledby="bot-signin-method-heading"
	>
		<div class="flex items-start justify-between gap-3">
			<div class="flex min-w-0 flex-col gap-1">
				<h3
					id="bot-signin-method-heading"
					class="m-0 text-base font-semibold tracking-tight"
				>
					{title}
				</h3>
				{#if subtitle}
					<p class="m-0 text-xs text-muted-foreground">{subtitle}</p>
				{/if}
				<p class="m-0 text-xs text-muted-foreground">
					Pick a sign-in method. Both produce the same bot session — the
					meet-worker and /playground are indistinguishable from there on.
				</p>
			</div>
			<button
				type="button"
				class="rounded-md p-1 text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
				onclick={onClose}
				aria-label="Close"
				data-testid="bot-signin-method-close"
			>
				<XIcon class="size-4" />
			</button>
		</div>

		<div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
			<button
				type="button"
				class="flex flex-col items-start gap-2 rounded-md border bg-card p-4 text-left transition-colors hover:border-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
				class:border-foreground={selected === 'novnc'}
				class:border-border={selected !== 'novnc'}
				onclick={() => (selected = 'novnc')}
				onkeydown={(e) => {
					if (e.key === ' ') {
						e.preventDefault();
						selected = 'novnc';
					}
				}}
				aria-pressed={selected === 'novnc'}
				data-testid="bot-signin-method-novnc"
			>
				<div class="flex items-center gap-2">
					<MonitorIcon class="size-4 text-foreground" aria-hidden="true" />
					<span class="font-medium text-foreground">Sign in here in the browser</span>
				</div>
				<span class="text-xs text-muted-foreground">
					Opens an embedded Chromium window via noVNC. Zero CLI required —
					you sign in to Google and Johnny captures the session.
				</span>
				{#if defaultMethod === 'novnc'}
					<span
						class="text-[0.65rem] font-medium tracking-wide text-muted-foreground uppercase"
					>
						Last used
					</span>
				{/if}
			</button>

			<button
				type="button"
				class="flex flex-col items-start gap-2 rounded-md border bg-card p-4 text-left transition-colors hover:border-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
				class:border-foreground={selected === 'upload'}
				class:border-border={selected !== 'upload'}
				onclick={() => (selected = 'upload')}
				onkeydown={(e) => {
					if (e.key === ' ') {
						e.preventDefault();
						selected = 'upload';
					}
				}}
				aria-pressed={selected === 'upload'}
				data-testid="bot-signin-method-upload"
			>
				<div class="flex items-center gap-2">
					<UploadIcon class="size-4 text-foreground" aria-hidden="true" />
					<span class="font-medium text-foreground">Upload storage_state.json</span>
				</div>
				<span class="text-xs text-muted-foreground">
					Run the seed_auth_state CLI on your workstation, then upload the
					generated JSON file. Works without the noVNC container.
				</span>
				{#if defaultMethod === 'upload'}
					<span
						class="text-[0.65rem] font-medium tracking-wide text-muted-foreground uppercase"
					>
						Last used
					</span>
				{/if}
			</button>
		</div>

		<div class="flex items-center justify-end gap-2">
			<Button variant="ghost" onclick={onClose}>Cancel</Button>
			<Button
				onclick={() => onPick(selected)}
				data-testid="bot-signin-method-continue"
			>
				Continue
			</Button>
		</div>
	</div>
</div>
