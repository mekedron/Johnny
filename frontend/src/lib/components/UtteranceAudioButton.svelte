<script lang="ts">
	/**
	 * Play/stop toggle for one captured reply WAV (Johnny-od1).
	 *
	 * Rendered next to Johnny's chat lines on the live session view and the
	 * History detail page. The audio element is created lazily on first play
	 * (a history page can hold hundreds of utterances — no point fetching
	 * audio for rows the operator never plays) and torn down on destroy.
	 */
	import PlayIcon from '@lucide/svelte/icons/play';
	import SquareIcon from '@lucide/svelte/icons/square';
	import { onDestroy } from 'svelte';

	interface Props {
		/** Full playback URL — see sessionAudioUrl() in $lib/sessionDetail. */
		src: string;
		/** Accessible name; the default fits Johnny's bot lines. */
		label?: string;
	}

	let { src, label = "Play Johnny's audio" }: Props = $props();

	let audio: HTMLAudioElement | null = null;
	let playing = $state(false);
	let failed = $state(false);

	function stop(): void {
		if (audio) {
			audio.pause();
			audio.currentTime = 0;
		}
		playing = false;
	}

	async function toggle(): Promise<void> {
		if (playing) {
			stop();
			return;
		}
		failed = false;
		if (!audio) {
			audio = new Audio(src);
			audio.addEventListener('ended', () => {
				playing = false;
			});
			audio.addEventListener('error', () => {
				playing = false;
				failed = true;
			});
		}
		try {
			await audio.play();
			playing = true;
		} catch {
			playing = false;
			failed = true;
		}
	}

	onDestroy(() => {
		if (audio) {
			audio.pause();
			audio = null;
		}
	});
</script>

<button
	type="button"
	class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-border text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground disabled:opacity-50"
	class:text-destructive={failed}
	data-testid="utterance-audio-button"
	aria-label={playing ? 'Stop playback' : label}
	title={failed ? 'Audio unavailable' : playing ? 'Stop' : label}
	onclick={toggle}
>
	{#if playing}
		<SquareIcon class="h-3 w-3" />
	{:else}
		<PlayIcon class="h-3 w-3" />
	{/if}
</button>
