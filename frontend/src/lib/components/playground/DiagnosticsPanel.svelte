<script lang="ts">
	import CircleAlertIcon from '@lucide/svelte/icons/circle-alert';
	import WifiOffIcon from '@lucide/svelte/icons/wifi-off';
	import * as Alert from '$lib/components/ui/alert/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import type { PlaygroundController } from '$lib/playground/playgroundSession.svelte';

	let { controller }: { controller: PlaygroundController } = $props();
</script>

<!-- Single consolidated diagnostics surface (Johnny-8zv.3): 409/Resume,
     connection health, STT/LLM/TTS failures, and the clean-end notice. -->

{#if controller.activeConflict}
	<Alert.Root data-testid="playground-conflict">
		<CircleAlertIcon />
		<Alert.Title>You already have a live session</Alert.Title>
		<Alert.Description>
			<div class="flex flex-col gap-2">
				<p class="m-0 text-sm">{controller.activeConflict.message}</p>
				<div class="flex flex-wrap gap-2">
					<Button size="sm" onclick={() => controller.resumeConflict()} data-testid="conflict-resume">
						Resume session{controller.activeConflict.id > 0
							? ` #${controller.activeConflict.id}`
							: ''}
					</Button>
					<Button
						variant="destructive"
						size="sm"
						onclick={() => controller.endConflictAndStart()}
						data-testid="conflict-replace"
					>
						End it & start new
					</Button>
					<Button variant="ghost" size="sm" onclick={() => controller.dismissConflict()}>
						Cancel
					</Button>
				</div>
			</div>
		</Alert.Description>
	</Alert.Root>
{/if}

{#if controller.isLive && controller.connection !== 'open'}
	<Alert.Root variant="destructive" data-testid="playground-connection">
		<WifiOffIcon />
		<Alert.Title>
			{controller.connection === 'reconnecting'
				? 'Backend connection lost — reconnecting…'
				: 'Connecting to the backend…'}
		</Alert.Title>
		<Alert.Description>
			Live transcript and audio are paused until the connection is restored.
		</Alert.Description>
	</Alert.Root>
{/if}

{#each controller.diagnostics as diag (diag.kind)}
	<Alert.Root
		variant={diag.severity === 'error' ? 'destructive' : 'default'}
		data-testid={`playground-diagnostic-${diag.kind}`}
	>
		<CircleAlertIcon />
		<Alert.Title>{diag.title}</Alert.Title>
		<Alert.Description>
			<div class="flex flex-col gap-1">
				<p class="m-0 text-sm">{diag.message}</p>
				{#if diag.hint}
					<p class="m-0 text-xs text-muted-foreground">{diag.hint}</p>
				{/if}
			</div>
		</Alert.Description>
	</Alert.Root>
{/each}

{#if controller.sessionNotice}
	<Alert.Root data-testid="playground-info">
		<CircleAlertIcon />
		<Alert.Title>Session ended</Alert.Title>
		<Alert.Description>{controller.sessionNotice}</Alert.Description>
	</Alert.Root>
{/if}
