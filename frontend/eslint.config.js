import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';
import globals from 'globals';
import ts from 'typescript-eslint';

export default ts.config(
	js.configs.recommended,
	...ts.configs.recommended,
	...svelte.configs['flat/recommended'],
	{
		languageOptions: {
			globals: {
				...globals.browser,
				...globals.node
			}
		}
	},
	{
		files: ['**/*.svelte'],
		languageOptions: {
			parserOptions: {
				parser: ts.parser
			}
		}
	},
	{
		files: ['src/lib/components/ui/**/*.svelte'],
		rules: {
			// shadcn-svelte primitives intentionally forward all unknown
			// attributes via `...restProps`. They are not built as custom
			// elements, so the customElement.props compiler warning is a
			// false positive here.
			'svelte/valid-compile': ['warn', { ignoreWarnings: true }]
		}
	},
	{
		ignores: [
			'.svelte-kit/',
			'build/',
			'dist/',
			'node_modules/',
			'package-lock.json',
			'pnpm-lock.yaml'
		]
	}
);
