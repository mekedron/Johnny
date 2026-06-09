import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// Standalone vitest config for the pure-function unit tests under src/**.
// These tests exercise extracted helpers (sessionTurns, personalities,
// providers validation, history) and deliberately do NOT mount Svelte
// components, so we skip the sveltekit()/svelte() plugins from vite.config.ts
// (vitest prefers this file over vite.config.ts when both exist) and only wire
// up what the tests need:
//   - the `$lib` path alias SvelteKit normally provides, resolved explicitly
//   - the Node environment (no jsdom — nothing touches the DOM)
// `pnpm test` runs `vitest run` against this config.
export default defineConfig({
	resolve: {
		alias: {
			$lib: fileURLToPath(new URL('./src/lib', import.meta.url))
		}
	},
	test: {
		environment: 'node',
		include: ['src/**/*.test.ts']
	}
});
