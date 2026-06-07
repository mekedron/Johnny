// Minimal type stub for @novnc/novnc (Johnny-105). The package ships
// no .d.ts files, so this lets svelte-check resolve the default
// import in BotSigninModal.svelte without falling back to implicit
// `any`. Only the surface we actually use is typed.

declare module '@novnc/novnc' {
	interface RFBOptions {
		shared?: boolean;
		credentials?: {
			username?: string;
			password?: string;
			target?: string;
		};
		repeaterID?: string;
		wsProtocols?: string[];
	}

	export default class RFB {
		constructor(
			target: HTMLElement,
			urlOrSocket: string | WebSocket,
			options?: RFBOptions
		);

		viewOnly: boolean;
		focusOnClick: boolean;
		clipViewport: boolean;
		dragViewport: boolean;
		scaleViewport: boolean;
		resizeSession: boolean;
		showDotCursor: boolean;
		background: string;
		qualityLevel: number;
		compressionLevel: number;

		disconnect(): void;
		sendCredentials(credentials: { username?: string; password?: string }): void;
		sendKey(keysym: number, code: string, down?: boolean): void;
		focus(): void;
		blur(): void;
		machineShutdown(): void;
		machineReboot(): void;
		machineReset(): void;
		clipboardPasteFrom(text: string): void;

		addEventListener(
			type: string,
			listener: (event: Event) => void
		): void;
		removeEventListener(
			type: string,
			listener: (event: Event) => void
		): void;
	}
}
