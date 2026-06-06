<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		ACCOUNT_ROLE_LABEL,
		ACCOUNT_ROLES,
		deleteBotSession,
		disconnectAccount,
		getBotSessionStatus,
		listAccounts,
		startOAuth,
		updateAccount,
		uploadBotSession,
		type Account,
		type AccountRole,
		type BotSessionStatus
	} from '$lib/accounts';

	let accounts = $state<Account[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let busyId = $state<number | null>(null);

	let showForm = $state(false);
	let formRole = $state<AccountRole>('user');
	let formIsDefault = $state(false);
	let formSubmitting = $state(false);
	let formError = $state<string | null>(null);
	let popupRef: Window | null = null;
	let lastAuthorizeUrl = $state<string | null>(null);
	let reconnectingId = $state<number | null>(null);

	// Bot-session storage_state (Johnny-4ph). Map account id → status so
	// the "Bot session connected" badge on each bot row reflects whether
	// a Playwright storage_state.json sits in the shared docker volume.
	let botSessions = $state<Record<number, BotSessionStatus>>({});
	let botBusyId = $state<number | null>(null);
	let showBotSessionForm = $state<{ account: Account } | null>(null);
	let botFormError = $state<string | null>(null);
	let botFormSubmitting = $state(false);
	let botSessionFile = $state<File | null>(null);

	async function loadAccounts() {
		loading = true;
		error = null;
		try {
			accounts = await listAccounts();
			await loadBotSessions();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function loadBotSessions() {
		// Bot-session status is a per-bot-account file check; only fetch
		// for rows with role=bot so we don't 400 the server on every user
		// row. Failures are non-fatal — the UI just falls back to "not
		// connected" rather than blocking the whole accounts list.
		const next: Record<number, BotSessionStatus> = {};
		const bots = accounts.filter((a) => a.role === 'bot');
		await Promise.all(
			bots.map(async (a) => {
				try {
					next[a.id] = await getBotSessionStatus(a.id);
				} catch {
					next[a.id] = {
						connected: false,
						saved_at: null,
						size_bytes: null,
						path: ''
					};
				}
			})
		);
		botSessions = next;
	}

	function openBotSessionForm(account: Account) {
		botSessionFile = null;
		botFormError = null;
		showBotSessionForm = { account };
	}

	function closeBotSessionForm() {
		showBotSessionForm = null;
		botFormError = null;
		botSessionFile = null;
	}

	async function submitBotSessionForm(event: Event) {
		event.preventDefault();
		const ctx = showBotSessionForm;
		if (!ctx || !botSessionFile) return;
		botFormSubmitting = true;
		botFormError = null;
		try {
			const text = await botSessionFile.text();
			botSessions[ctx.account.id] = await uploadBotSession(ctx.account.id, text);
			closeBotSessionForm();
		} catch (e) {
			botFormError = e instanceof Error ? e.message : String(e);
		} finally {
			botFormSubmitting = false;
		}
	}

	async function onDisconnectBotSession(account: Account) {
		const confirmed = confirm(
			`Disconnect the saved bot session for ${account.email}? ` +
				'The meet-worker will need a new storage_state.json before it can join meetings.'
		);
		if (!confirmed) return;
		botBusyId = account.id;
		try {
			botSessions[account.id] = await deleteBotSession(account.id);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			botBusyId = null;
		}
	}

	function onBotSessionFileChange(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		botSessionFile = input.files?.[0] ?? null;
		botFormError = null;
	}

	function formatBotSessionSize(bytes: number | null): string {
		if (bytes === null) return '';
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
	}

	onMount(() => {
		loadAccounts();
		window.addEventListener('message', handleOAuthMessage);
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('message', handleOAuthMessage);
		}
	});

	function handleOAuthMessage(event: MessageEvent) {
		const data = event.data;
		if (!data || typeof data !== 'object') return;
		if ((data as { type?: unknown }).type !== 'johnny:oauth') return;
		// A popup completed the OAuth round-trip. Reload the list so the
		// new row appears without the user having to click Refresh.
		loadAccounts();
		showForm = false;
		lastAuthorizeUrl = null;
		reconnectingId = null;
		if (popupRef && !popupRef.closed) {
			try {
				popupRef.close();
			} catch {
				// ignore — some browsers block closing windows we did not open
			}
		}
		popupRef = null;
	}

	function openAddForm() {
		formRole = 'user';
		formIsDefault = accounts.every((a) => !a.is_default_user);
		formError = null;
		lastAuthorizeUrl = null;
		showForm = true;
	}

	function closeForm() {
		showForm = false;
		formError = null;
	}

	async function submitForm(event: Event) {
		event.preventDefault();
		formSubmitting = true;
		formError = null;
		try {
			const resp = await startOAuth({
				role: formRole,
				is_default_user: formIsDefault
			});
			lastAuthorizeUrl = resp.authorize_url;
			popupRef = window.open(resp.authorize_url, 'johnny-oauth', 'width=520,height=720');
			if (!popupRef) {
				formError = 'Popup was blocked. Use the link below to continue in a new tab.';
			}
		} catch (e) {
			formError = e instanceof Error ? e.message : String(e);
		} finally {
			formSubmitting = false;
		}
	}

	async function onSetDefault(account: Account) {
		busyId = account.id;
		try {
			await updateAccount(account.id, { is_default_user: true });
			await loadAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	async function onChangeRole(account: Account, role: AccountRole) {
		if (role === account.role) return;
		busyId = account.id;
		try {
			await updateAccount(account.id, { role });
			await loadAccounts();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busyId = null;
		}
	}

	async function onReconnect(account: Account) {
		reconnectingId = account.id;
		error = null;
		try {
			const resp = await startOAuth({
				role: account.role,
				is_default_user: account.is_default_user
			});
			lastAuthorizeUrl = resp.authorize_url;
			popupRef = window.open(
				resp.authorize_url,
				'johnny-oauth-reconnect',
				'width=520,height=720'
			);
			if (!popupRef) {
				error =
					'Popup was blocked. Use the "Open consent in a new tab" link below to continue.';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			reconnectingId = null;
		}
	}

	async function onDisconnect(account: Account) {
		const confirmed = confirm(
			`Disconnect ${account.email}? This revokes the refresh token at Google and removes the local record.`
		);
		if (!confirmed) return;
		busyId = account.id;
		try {
			await disconnectAccount(account.id);
			await loadAccounts();
		} catch (e) {
			const err = e as Error & { status?: number; body?: unknown };
			if (err.status === 409) {
				const detail =
					err.body && typeof err.body === 'object' && 'detail' in err.body
						? (err.body as { detail: { meeting_config_count?: number } }).detail
						: null;
				const count = detail?.meeting_config_count ?? 0;
				const forceConfirmed = confirm(
					`${account.email} is the bot identity for ${count} meeting config(s). ` +
						'Disconnecting will also delete those configs. Continue?'
				);
				if (forceConfirmed) {
					try {
						await disconnectAccount(account.id, true);
						await loadAccounts();
					} catch (innerErr) {
						error = innerErr instanceof Error ? innerErr.message : String(innerErr);
					}
				}
			} else {
				error = err.message;
			}
		} finally {
			busyId = null;
		}
	}

	function formatExpiry(value: string | null): string {
		if (!value) return '—';
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return value;
		return d.toLocaleString();
	}
</script>

<svelte:head>
	<title>Settings · Johnny</title>
</svelte:head>

<div class="page">
	<header class="page-header">
		<div>
			<h1>Settings</h1>
			<p class="lede">
				Manage the Google accounts Johnny uses. Tag one as the default user identity and add
				bot identities for meetings you want Johnny to join under a different name.
			</p>
		</div>
		<div class="header-actions">
			<button type="button" onclick={loadAccounts} disabled={loading}>
				{loading ? 'Refreshing…' : 'Refresh'}
			</button>
			<button type="button" class="primary" onclick={openAddForm}>Add account</button>
		</div>
	</header>

	{#if error}
		<div class="alert error" role="alert">{error}</div>
	{/if}

	<section class="settings-shortcuts" aria-label="Settings shortcuts">
		<a
			class="shortcut-card"
			href="/settings/stt"
			data-testid="settings-shortcut-stt"
		>
			<strong>Speech-to-text providers</strong>
			<span>
				Browse installed STT adapters, configure credentials, and run a
				5-second mic test to compare transcript quality, latency, and
				cost.
			</span>
		</a>
	</section>

	<section class="accounts-section" data-testid="accounts-section">
		<h2>Connected accounts</h2>
		{#if accounts.length === 0}
			<p class="empty">
				No Google accounts connected yet. Click <strong>Add account</strong> to sign in.
			</p>
		{:else}
			<ul class="account-list">
				{#each accounts as account (account.id)}
					<li
						id={`account-${account.id}`}
						class="account"
						class:default={account.is_default_user}
						class:needs-reauth={account.token_health === 'needs_reauth'}
						data-testid={`account-row-${account.id}`}
					>
						<div class="account-main">
							<div class="account-title">
								<strong>{account.email}</strong>
								<span class={`role-badge role-${account.role}`}>
									{ACCOUNT_ROLE_LABEL[account.role]}
								</span>
								{#if account.is_default_user}
									<span class="badge default-badge">Default user</span>
								{/if}
								{#if account.token_health === 'needs_reauth'}
									<span
										class="badge reauth-badge"
										title="The stored refresh token can't be decrypted. Reconnect to restore this account."
										data-testid={`reauth-badge-${account.id}`}
									>
										Token unreadable — reconnect
									</span>
								{/if}
							</div>
							<dl class="account-meta">
								<dt>Token expires:</dt>
								<dd>{formatExpiry(account.token_expires_at)}</dd>
								<dt>Added:</dt>
								<dd>{formatExpiry(account.created_at)}</dd>
							</dl>
							{#if account.token_health === 'needs_reauth'}
								<p class="reauth-explainer">
									This account's stored refresh token can no longer be decrypted
									(usually because <code>FERNET_KEY</code> was rotated). Reconnecting
									runs the Google sign-in flow again and replaces the row in place.
								</p>
							{/if}
							{#if account.role === 'bot'}
								{@const session = botSessions[account.id]}
								<div
									class="bot-session"
									class:bot-session-connected={session?.connected}
									data-testid={`bot-session-${account.id}`}
								>
									<div class="bot-session-summary">
										<strong>Bot session:</strong>
										{#if session?.connected}
											<span class="bot-session-status connected">
												Connected
												{#if session.saved_at}
													<small>(saved {formatExpiry(session.saved_at)})</small>
												{/if}
												{#if session.size_bytes}
													<small>· {formatBotSessionSize(session.size_bytes)}</small>
												{/if}
											</span>
										{:else}
											<span class="bot-session-status missing">Not connected</span>
										{/if}
									</div>
									<p class="bot-session-help">
										Meet-worker needs a Playwright <code>storage_state.json</code> to
										sign into Google as this bot. Connect the session below; the file
										lands in the shared <code>google_auth_state</code> volume.
									</p>
								</div>
							{/if}
						</div>
						<div class="account-actions">
							{#if account.token_health === 'needs_reauth'}
								<button
									type="button"
									class="primary"
									onclick={() => onReconnect(account)}
									disabled={reconnectingId === account.id || busyId === account.id}
									data-testid={`reconnect-button-${account.id}`}
								>
									{reconnectingId === account.id ? 'Opening…' : 'Reconnect'}
								</button>
							{/if}
							<label class="role-select">
								<span class="visually-hidden">Role</span>
								<select
									value={account.role}
									disabled={busyId === account.id}
									onchange={(e) =>
										onChangeRole(account, (e.currentTarget as HTMLSelectElement).value as AccountRole)}
								>
									{#each ACCOUNT_ROLES as r (r)}
										<option value={r}>{ACCOUNT_ROLE_LABEL[r]}</option>
									{/each}
								</select>
							</label>
							{#if !account.is_default_user && account.role === 'user'}
								<button
									type="button"
									onclick={() => onSetDefault(account)}
									disabled={busyId === account.id}
								>
									Set as default
								</button>
							{/if}
							{#if account.role === 'bot'}
								{@const session = botSessions[account.id]}
								<button
									type="button"
									class={session?.connected ? '' : 'primary'}
									onclick={() => openBotSessionForm(account)}
									disabled={botBusyId === account.id}
									data-testid={`connect-bot-session-${account.id}`}
								>
									{session?.connected ? 'Replace bot session' : 'Connect bot session'}
								</button>
								{#if session?.connected}
									<button
										type="button"
										class="danger"
										onclick={() => onDisconnectBotSession(account)}
										disabled={botBusyId === account.id}
										data-testid={`disconnect-bot-session-${account.id}`}
									>
										Disconnect session
									</button>
								{/if}
							{/if}
							<button
								type="button"
								class="danger"
								onclick={() => onDisconnect(account)}
								disabled={busyId === account.id}
							>
								Disconnect
							</button>
						</div>
					</li>
				{/each}
			</ul>
			{#if lastAuthorizeUrl && reconnectingId !== null}
				<p class="reauth-fallback">
					Popup blocked or didn't open?
					<a href={lastAuthorizeUrl} target="_blank" rel="noopener noreferrer">
						Open consent in a new tab
					</a>
				</p>
			{/if}
		{/if}
	</section>
</div>

{#if showForm}
	<div
		class="modal-backdrop"
		role="dialog"
		aria-modal="true"
		aria-labelledby="add-account-heading"
	>
		<form class="modal" onsubmit={submitForm}>
			<h2 id="add-account-heading">Add Google account</h2>
			<p class="modal-lede">
				A new browser tab will open Google's consent screen. After you sign in, the popup will
				close and this list will refresh automatically.
			</p>
			<label>
				<span>Identity tag</span>
				<select bind:value={formRole} required>
					{#each ACCOUNT_ROLES as r (r)}
						<option value={r}>{ACCOUNT_ROLE_LABEL[r]}</option>
					{/each}
				</select>
				<small>
					Tag the account as your <strong>user</strong> identity (calendar source) or a
					<strong>bot</strong> identity that joins meetings on your behalf.
				</small>
			</label>
			{#if formRole === 'user'}
				<label class="checkbox">
					<input type="checkbox" bind:checked={formIsDefault} />
					<span>Use this as my default user identity</span>
				</label>
			{/if}
			{#if formError}
				<div class="alert error">{formError}</div>
			{/if}
			{#if lastAuthorizeUrl}
				<p class="fallback-hint">
					Popup blocked or didn't open?
					<a href={lastAuthorizeUrl} target="_blank" rel="noopener noreferrer">
						Open consent in a new tab
					</a>
				</p>
			{/if}
			<div class="modal-actions">
				<button type="button" onclick={closeForm} disabled={formSubmitting}>Cancel</button>
				<button type="submit" class="primary" disabled={formSubmitting}>
					{formSubmitting ? 'Opening…' : 'Continue to Google'}
				</button>
			</div>
		</form>
	</div>
{/if}

{#if showBotSessionForm}
	{@const ctx = showBotSessionForm}
	<div
		class="modal-backdrop"
		role="dialog"
		aria-modal="true"
		aria-labelledby="connect-bot-session-heading"
	>
		<form class="modal" onsubmit={submitBotSessionForm} data-testid="bot-session-modal">
			<h2 id="connect-bot-session-heading">Connect bot session</h2>
			<p class="modal-lede">
				Connect a Playwright sign-in session for <strong>{ctx.account.email}</strong> so
				the meet-worker can open Chromium straight into Google as this bot.
			</p>
			<details class="bot-session-howto">
				<summary>How to generate the sign-in file</summary>
				<p>
					Run the seed helper on this machine. It opens a Chromium window where you
					sign in to Google as the bot. The helper writes
					<code>storage_state.json</code> to the path you pass.
				</p>
				<pre><code
						>cd backend
uv sync --extra auth-seed
uv run playwright install chromium
uv run python -m johnny.tools.seed_auth_state \
  --account-id {ctx.account.id} \
  --email {ctx.account.email} \
  --keep-local /tmp/storage_state.json</code
					></pre>
				<p class="howto-note">
					The CLI helper also copies the file into the docker volume automatically
					(handy for operators). Uploading via this form is the alternative for
					anyone who can't run <code>docker cp</code> directly.
				</p>
			</details>
			<label>
				<span>storage_state.json</span>
				<input
					type="file"
					accept="application/json,.json"
					onchange={onBotSessionFileChange}
					required
					data-testid="bot-session-file-input"
				/>
				<small>
					Pick the JSON file the helper produced. Maximum 4 MiB; must contain a
					non-empty <code>cookies</code> array.
				</small>
			</label>
			{#if botSessionFile}
				<p class="file-summary">
					Selected: <strong>{botSessionFile.name}</strong>
					<small>({formatBotSessionSize(botSessionFile.size)})</small>
				</p>
			{/if}
			{#if botFormError}
				<div class="alert error" data-testid="bot-session-error">{botFormError}</div>
			{/if}
			<div class="modal-actions">
				<button type="button" onclick={closeBotSessionForm} disabled={botFormSubmitting}>
					Cancel
				</button>
				<button
					type="submit"
					class="primary"
					disabled={botFormSubmitting || !botSessionFile}
					data-testid="bot-session-submit"
				>
					{botFormSubmitting ? 'Uploading…' : 'Save bot session'}
				</button>
			</div>
		</form>
	</div>
{/if}

<style>
	.page {
		max-width: 960px;
	}
	.page-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1.5rem;
		flex-wrap: wrap;
	}
	.lede {
		max-width: 60ch;
		color: #4b5563;
		margin: 0.25rem 0 0;
	}
	.header-actions {
		display: flex;
		gap: 0.5rem;
		flex-shrink: 0;
	}

	button {
		padding: 0.45rem 0.9rem;
		border: 1px solid #d1d5db;
		background: #ffffff;
		color: #1f2937;
		border-radius: 6px;
		cursor: pointer;
		font-size: 0.9rem;
	}
	button:hover:not(:disabled) {
		background: #f9fafb;
	}
	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	button.primary {
		background: #4f46e5;
		color: #ffffff;
		border-color: #4f46e5;
	}
	button.primary:hover:not(:disabled) {
		background: #4338ca;
	}
	button.danger {
		color: #b91c1c;
		border-color: #fca5a5;
	}
	button.danger:hover:not(:disabled) {
		background: #fef2f2;
	}

	.alert {
		padding: 0.75rem 1rem;
		border-radius: 6px;
		margin: 1rem 0;
	}
	.alert.error {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}

	.settings-shortcuts {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
		gap: 0.75rem;
		margin-top: 1.5rem;
	}
	.shortcut-card {
		display: grid;
		gap: 0.3rem;
		padding: 0.9rem 1rem;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		background: #ffffff;
		text-decoration: none;
		color: inherit;
	}
	.shortcut-card:hover {
		border-color: #4f46e5;
		background: #eef2ff;
	}
	.shortcut-card strong {
		font-size: 0.95rem;
		color: #1f2937;
	}
	.shortcut-card span {
		font-size: 0.85rem;
		color: #4b5563;
	}

	.accounts-section {
		margin-top: 1.5rem;
	}
	.accounts-section h2 {
		margin: 0 0 0.5rem;
		font-size: 1.1rem;
	}
	.empty {
		color: #6b7280;
		font-style: italic;
		margin: 0.5rem 0 0;
	}

	.account-list {
		list-style: none;
		padding: 0;
		margin: 0.75rem 0 0;
		display: grid;
		gap: 0.75rem;
	}
	.account {
		display: flex;
		gap: 1rem;
		justify-content: space-between;
		padding: 1rem;
		border: 1px solid #e5e7eb;
		border-radius: 8px;
		background: #ffffff;
	}
	.account.default {
		border-color: #4f46e5;
		background: #eef2ff;
	}
	.account.needs-reauth {
		border-color: #f97316;
		background: #fff7ed;
	}
	.reauth-badge {
		background: #f97316;
		color: #ffffff;
	}
	.reauth-explainer {
		margin: 0.5rem 0 0;
		font-size: 0.85rem;
		color: #7c2d12;
		max-width: 60ch;
	}
	.reauth-explainer code {
		background: #fff7ed;
		border: 1px solid #fed7aa;
		padding: 0.05rem 0.3rem;
		border-radius: 4px;
		font-size: 0.8rem;
	}
	.reauth-fallback {
		margin: 0.75rem 0 0;
		font-size: 0.85rem;
		color: #4b5563;
	}
	.reauth-fallback a {
		color: #4f46e5;
		text-decoration: underline;
	}
	.account-main {
		flex: 1;
		min-width: 0;
	}
	.account-title {
		display: flex;
		gap: 0.6rem;
		align-items: baseline;
		flex-wrap: wrap;
	}
	.role-badge {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		padding: 0.1rem 0.45rem;
		border-radius: 999px;
		font-weight: 600;
	}
	.role-badge.role-user {
		background: #ecfeff;
		color: #155e75;
		border: 1px solid #a5f3fc;
	}
	.role-badge.role-bot {
		background: #fff7ed;
		color: #9a3412;
		border: 1px solid #fed7aa;
	}
	.badge {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		background: #4f46e5;
		color: #ffffff;
		padding: 0.1rem 0.45rem;
		border-radius: 999px;
	}
	.default-badge {
		background: #4f46e5;
	}
	.account-meta {
		display: grid;
		grid-template-columns: max-content 1fr;
		column-gap: 0.6rem;
		row-gap: 0.15rem;
		margin: 0.5rem 0 0;
		font-size: 0.85rem;
		color: #4b5563;
	}
	.account-meta dt {
		font-weight: 600;
	}
	.account-meta dd {
		margin: 0;
	}
	.account-actions {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		flex-shrink: 0;
	}
	.role-select select {
		padding: 0.35rem 0.5rem;
		border-radius: 6px;
		border: 1px solid #d1d5db;
		font: inherit;
		background: #ffffff;
	}
	.visually-hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}

	.modal-backdrop {
		position: fixed;
		inset: 0;
		background: rgba(17, 24, 39, 0.6);
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 1rem;
		z-index: 50;
	}
	.modal {
		background: #ffffff;
		padding: 1.5rem;
		border-radius: 10px;
		width: min(480px, 100%);
		max-height: 90vh;
		overflow: auto;
		display: grid;
		gap: 0.85rem;
	}
	.modal h2 {
		margin: 0;
		font-size: 1.1rem;
	}
	.modal-lede {
		margin: 0;
		color: #4b5563;
		font-size: 0.9rem;
	}
	.modal label {
		display: grid;
		gap: 0.3rem;
		font-size: 0.9rem;
	}
	.modal label > span {
		font-weight: 600;
	}
	.modal label.checkbox {
		grid-template-columns: max-content 1fr;
		gap: 0.5rem;
		align-items: center;
	}
	.modal label.checkbox > span {
		font-weight: 400;
	}
	.modal select {
		font: inherit;
		padding: 0.45rem 0.6rem;
		border: 1px solid #d1d5db;
		border-radius: 6px;
		background: #ffffff;
	}
	.modal small {
		color: #6b7280;
		font-size: 0.8rem;
	}
	.fallback-hint {
		margin: 0;
		font-size: 0.85rem;
		color: #4b5563;
	}
	.fallback-hint a {
		color: #4f46e5;
		text-decoration: underline;
	}
	.modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 0.25rem;
	}

	.bot-session {
		margin: 0.75rem 0 0;
		padding: 0.6rem 0.8rem;
		border: 1px solid #fed7aa;
		background: #fff7ed;
		border-radius: 6px;
		font-size: 0.85rem;
	}
	.bot-session.bot-session-connected {
		border-color: #86efac;
		background: #f0fdf4;
	}
	.bot-session-summary {
		display: flex;
		gap: 0.4rem;
		align-items: baseline;
		flex-wrap: wrap;
	}
	.bot-session-status {
		font-weight: 500;
	}
	.bot-session-status.connected {
		color: #166534;
	}
	.bot-session-status.missing {
		color: #9a3412;
	}
	.bot-session-status small {
		color: #6b7280;
		font-weight: 400;
		margin-left: 0.25rem;
	}
	.bot-session-help {
		margin: 0.3rem 0 0;
		color: #4b5563;
		font-size: 0.8rem;
	}
	.bot-session-help code {
		background: rgba(255, 255, 255, 0.6);
		padding: 0.05rem 0.3rem;
		border-radius: 4px;
		font-size: 0.75rem;
	}
	.bot-session-howto {
		background: #f9fafb;
		border: 1px solid #e5e7eb;
		border-radius: 6px;
		padding: 0.5rem 0.75rem;
		font-size: 0.85rem;
	}
	.bot-session-howto summary {
		cursor: pointer;
		font-weight: 600;
		color: #374151;
	}
	.bot-session-howto p {
		margin: 0.5rem 0;
		color: #4b5563;
	}
	.bot-session-howto pre {
		margin: 0.5rem 0;
		padding: 0.5rem;
		background: #1f2937;
		color: #f9fafb;
		border-radius: 4px;
		font-size: 0.75rem;
		overflow-x: auto;
		white-space: pre;
	}
	.bot-session-howto code {
		font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	}
	.bot-session-howto p code {
		background: #e5e7eb;
		padding: 0.05rem 0.3rem;
		border-radius: 4px;
	}
	.howto-note {
		font-size: 0.8rem;
		color: #6b7280;
	}
	.file-summary {
		margin: 0;
		font-size: 0.85rem;
		color: #374151;
	}
	.file-summary small {
		color: #6b7280;
	}

	@media (max-width: 640px) {
		.account {
			flex-direction: column;
		}
		.account-actions {
			flex-direction: row;
			flex-wrap: wrap;
		}
	}
</style>
