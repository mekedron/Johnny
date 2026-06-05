<script lang="ts">
	import { page } from '$app/state';
	import favicon from '$lib/assets/favicon.svg';

	let { children } = $props();

	const navItems = [
		{ href: '/calendar', label: 'Calendar' },
		{ href: '/templates', label: 'Templates' },
		{ href: '/providers', label: 'Providers' },
		{ href: '/history', label: 'History' },
		{ href: '/settings', label: 'Settings' }
	];

	let sidebarOpen = $state(false);

	function isActive(href: string): boolean {
		const path = page.url.pathname;
		return path === href || path.startsWith(`${href}/`);
	}

	function closeSidebar() {
		sidebarOpen = false;
	}
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<div class="app-shell">
	<header class="header">
		<button
			class="menu-toggle"
			type="button"
			aria-label="Toggle navigation"
			aria-expanded={sidebarOpen}
			onclick={() => (sidebarOpen = !sidebarOpen)}
		>
			<span aria-hidden="true">☰</span>
		</button>
		<a class="brand" href="/">Johnny</a>
		<div class="account" data-testid="account-indicator">
			<span class="account-label">Account</span>
			<span class="account-name">Not connected</span>
		</div>
	</header>

	{#if sidebarOpen}
		<button
			class="sidebar-backdrop"
			type="button"
			aria-label="Close navigation"
			onclick={closeSidebar}
		></button>
	{/if}

	<aside class="sidebar" class:open={sidebarOpen} aria-label="Primary">
		<nav>
			<ul>
				{#each navItems as item (item.href)}
					<li>
						<a
							href={item.href}
							class:active={isActive(item.href)}
							aria-current={isActive(item.href) ? 'page' : undefined}
							onclick={closeSidebar}
						>
							{item.label}
						</a>
					</li>
				{/each}
			</ul>
		</nav>
	</aside>

	<main class="content">
		{@render children()}
	</main>
</div>

<style>
	:global(html, body) {
		margin: 0;
		padding: 0;
	}
	:global(body) {
		font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
		color: #111827;
		background: #ffffff;
	}

	.app-shell {
		display: grid;
		grid-template-areas:
			'header header'
			'sidebar main';
		grid-template-columns: 240px 1fr;
		grid-template-rows: 56px 1fr;
		min-height: 100vh;
	}

	.header {
		grid-area: header;
		display: flex;
		align-items: center;
		gap: 1rem;
		padding: 0 1rem;
		background: #1f2937;
		color: #ffffff;
		position: sticky;
		top: 0;
		z-index: 20;
	}

	.menu-toggle {
		display: none;
		background: transparent;
		color: inherit;
		border: 0;
		font-size: 1.25rem;
		cursor: pointer;
		padding: 0.25rem 0.5rem;
		border-radius: 4px;
	}
	.menu-toggle:hover {
		background: rgba(255, 255, 255, 0.1);
	}

	.brand {
		font-weight: 700;
		font-size: 1.25rem;
		color: inherit;
		text-decoration: none;
	}

	.account {
		margin-left: auto;
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		line-height: 1.2;
	}
	.account-label {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		opacity: 0.7;
	}
	.account-name {
		font-size: 0.95rem;
		font-weight: 600;
	}

	.sidebar {
		grid-area: sidebar;
		background: #f3f4f6;
		border-right: 1px solid #e5e7eb;
	}
	.sidebar nav ul {
		list-style: none;
		margin: 0;
		padding: 0.75rem 0;
	}
	.sidebar nav a {
		display: block;
		padding: 0.75rem 1.25rem;
		color: #1f2937;
		text-decoration: none;
		border-left: 3px solid transparent;
	}
	.sidebar nav a:hover {
		background: #e5e7eb;
	}
	.sidebar nav a.active {
		background: #e0e7ff;
		border-left-color: #4f46e5;
		font-weight: 600;
		color: #312e81;
	}

	.sidebar-backdrop {
		display: none;
	}

	.content {
		grid-area: main;
		padding: 1.5rem 2rem;
		overflow-x: hidden;
	}

	@media (max-width: 720px) {
		.app-shell {
			grid-template-areas:
				'header'
				'main';
			grid-template-columns: 1fr;
		}
		.menu-toggle {
			display: inline-flex;
		}
		.sidebar {
			position: fixed;
			top: 56px;
			left: 0;
			bottom: 0;
			width: 240px;
			transform: translateX(-100%);
			transition: transform 0.2s ease-out;
			z-index: 30;
		}
		.sidebar.open {
			transform: translateX(0);
			box-shadow: 4px 0 12px rgba(0, 0, 0, 0.15);
		}
		.sidebar-backdrop {
			display: block;
			position: fixed;
			top: 56px;
			left: 0;
			right: 0;
			bottom: 0;
			background: rgba(0, 0, 0, 0.35);
			border: 0;
			padding: 0;
			cursor: pointer;
			z-index: 25;
		}
		.content {
			padding: 1rem;
		}
	}
</style>
