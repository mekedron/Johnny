<!--
  Per-workspace MCP connectors (Johnny-wks.8). An MCP server is OWNED by this
  workspace — its tools (`mcp__<server>__<tool>`) are available only to agents
  attached here. The flow is add → probe → enable; a server that has never
  been probed contributes nothing. Lives on the workspace detail page (there
  is no global MCP registry).
-->
<script lang="ts">
	import PlusIcon from '@lucide/svelte/icons/plus';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { cn } from '$lib/utils.js';
	import {
		createMcpServer,
		deleteMcpServer,
		isValidMcpName,
		listMcpServers,
		parseArgsLines,
		parseKeyValueLines,
		probeMcpServer,
		probeState,
		probeSummary,
		toolCount,
		updateMcpServer,
		type McpProbeOut,
		type McpServerRead,
		type McpServerUpdate,
		type McpTransport
	} from '$lib/mcpServers';

	let { workspaceId }: { workspaceId: number } = $props();

	let servers = $state<McpServerRead[]>([]);
	let loading = $state(false);
	let errorMessage = $state<string | null>(null);
	let probing = $state<Set<number>>(new Set());
	let probeResults = $state<Record<number, McpProbeOut>>({});
	let togglingIds = $state<Set<number>>(new Set());
	let deleteArmedId = $state<number | null>(null);
	let deletingIds = $state<Set<number>>(new Set());

	// --- add/edit form ----------------------------------------------------
	let formOpen = $state(false);
	/** null = creating; otherwise the row being edited. */
	let editing = $state<McpServerRead | null>(null);
	let formError = $state<string | null>(null);
	let submitting = $state(false);

	let nameText = $state('');
	let transport = $state<McpTransport>('stdio');
	let commandText = $state('');
	let argsText = $state('');
	let urlText = $state('');
	let envText = $state('');
	let headersText = $state('');
	let replaceSecrets = $state(false);
	let includeText = $state('');
	let excludeText = $state('');
	let connectTimeoutText = $state('10');
	let callTimeoutText = $state('60');
	let idleTtlText = $state('300');

	async function refresh() {
		loading = true;
		errorMessage = null;
		try {
			const res = await listMcpServers(workspaceId);
			servers = res.servers;
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to load MCP servers';
		} finally {
			loading = false;
		}
	}

	// Reload whenever the workspace changes (and on mount). Closing the form
	// keeps stale edit state from leaking across a workspace switch.
	$effect(() => {
		workspaceId;
		closeForm();
		void refresh();
	});

	function openCreate() {
		editing = null;
		formError = null;
		nameText = '';
		transport = 'stdio';
		commandText = '';
		argsText = '';
		urlText = '';
		envText = '';
		headersText = '';
		replaceSecrets = true; // creating: whatever is typed IS the secret set
		includeText = '';
		excludeText = '';
		connectTimeoutText = '10';
		callTimeoutText = '60';
		idleTtlText = '300';
		formOpen = true;
	}

	function openEdit(server: McpServerRead) {
		editing = server;
		formError = null;
		nameText = server.name;
		transport = server.transport;
		commandText = server.command;
		argsText = server.args.join('\n');
		urlText = server.url;
		envText = '';
		headersText = '';
		replaceSecrets = false; // values are write-only; keep unless replaced
		includeText = (server.tool_include ?? []).join('\n');
		excludeText = server.tool_exclude.join('\n');
		connectTimeoutText = String(server.connect_timeout_s);
		callTimeoutText = String(server.call_timeout_s);
		idleTtlText = String(server.idle_ttl_s);
		formOpen = true;
	}

	function closeForm() {
		formOpen = false;
		editing = null;
		formError = null;
	}

	function parseTimeout(text: string, label: string): number {
		const value = Number.parseFloat(text);
		if (!Number.isFinite(value) || value <= 0) {
			throw new Error(`${label} must be a positive number`);
		}
		return value;
	}

	async function submitForm() {
		formError = null;
		const name = nameText.trim();
		if (!isValidMcpName(name)) {
			formError =
				'Name must be a lowercase slug (a-z, 0-9, hyphens; no underscores) — it prefixes every tool kind as mcp__<name>__<tool>.';
			return;
		}
		const env = parseKeyValueLines(envText);
		const headers = parseKeyValueLines(headersText);
		const badLines = [...env.invalid, ...headers.invalid];
		if (replaceSecrets && badLines.length > 0) {
			formError = `These lines are not KEY=value: ${badLines.join(' · ')}`;
			return;
		}
		submitting = true;
		try {
			const include = parseArgsLines(includeText);
			const exclude = parseArgsLines(excludeText);
			const timeouts = {
				connect_timeout_s: parseTimeout(connectTimeoutText, 'Connect timeout'),
				call_timeout_s: parseTimeout(callTimeoutText, 'Call timeout'),
				idle_ttl_s: parseTimeout(idleTtlText, 'Idle TTL')
			};
			if (editing == null) {
				await createMcpServer(workspaceId, {
					name,
					transport,
					command: commandText.trim(),
					args: parseArgsLines(argsText),
					url: urlText.trim(),
					env: env.values,
					headers: headers.values,
					tool_include: include.length > 0 ? include : null,
					tool_exclude: exclude,
					...timeouts
				});
			} else {
				const payload: McpServerUpdate = {
					name,
					transport,
					command: commandText.trim(),
					args: parseArgsLines(argsText),
					url: urlText.trim(),
					tool_exclude: exclude,
					...timeouts
				};
				if (include.length > 0) {
					payload.tool_include = include;
				} else if (editing.tool_include != null) {
					payload.clear_tool_include = true;
				}
				if (replaceSecrets) {
					payload.env = env.values;
					payload.headers = headers.values;
				}
				await updateMcpServer(workspaceId, editing.id, payload);
			}
			closeForm();
			await refresh();
		} catch (err) {
			formError = err instanceof Error ? err.message : 'Failed to save MCP server';
		} finally {
			submitting = false;
		}
	}

	async function probe(server: McpServerRead) {
		probing = new Set([...probing, server.id]);
		errorMessage = null;
		try {
			probeResults = { ...probeResults, [server.id]: await probeMcpServer(workspaceId, server.id) };
			await refresh();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Probe failed';
		} finally {
			const next = new Set(probing);
			next.delete(server.id);
			probing = next;
		}
	}

	async function toggleEnabled(server: McpServerRead) {
		togglingIds = new Set([...togglingIds, server.id]);
		errorMessage = null;
		try {
			await updateMcpServer(workspaceId, server.id, { enabled: !server.enabled });
			await refresh();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to toggle server';
		} finally {
			const next = new Set(togglingIds);
			next.delete(server.id);
			togglingIds = next;
		}
	}

	async function removeServer(server: McpServerRead) {
		if (deleteArmedId !== server.id) {
			deleteArmedId = server.id;
			return;
		}
		deleteArmedId = null;
		deletingIds = new Set([...deletingIds, server.id]);
		try {
			await deleteMcpServer(workspaceId, server.id);
			const remainingResults = { ...probeResults };
			delete remainingResults[server.id];
			probeResults = remainingResults;
			await refresh();
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to delete server';
		} finally {
			const next = new Set(deletingIds);
			next.delete(server.id);
			deletingIds = next;
		}
	}

	const textareaClass =
		'border-input bg-background min-h-16 w-full rounded-md border px-3 py-2 font-mono text-xs shadow-xs outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]';
</script>

<section class="flex flex-col gap-4" data-testid="mcp-panel">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<p class="text-muted-foreground m-0 text-sm">
			MCP connectors contribute tools as
			<span class="font-mono">mcp__&lt;server&gt;__&lt;tool&gt;</span> kinds to agents in this
			workspace. The flow is add → probe → enable: a server that has never been probed contributes
			nothing.
		</p>
		<Button size="sm" onclick={openCreate} data-testid="mcp-add">
			<PlusIcon class="size-3.5" />
			Add server
		</Button>
	</div>

	{#if errorMessage}
		<p class="text-destructive text-sm" role="alert" data-testid="mcp-error">{errorMessage}</p>
	{/if}

	{#if formOpen}
		<form
			class="border-border bg-card flex flex-col gap-3 rounded-lg border p-4"
			onsubmit={(event) => {
				event.preventDefault();
				void submitForm();
			}}
			data-testid="mcp-form"
		>
			<h2 class="text-foreground m-0 text-sm font-semibold">
				{editing == null ? 'Add MCP server' : `Edit ${editing.name}`}
			</h2>
			<div class="grid gap-3 md:grid-cols-2">
				<div class="flex flex-col gap-1">
					<label for="mcp-name" class="text-muted-foreground text-xs font-medium">Name (slug)</label>
					<Input
						id="mcp-name"
						placeholder="my-connector"
						class="h-8 font-mono text-xs"
						bind:value={nameText}
						data-testid="mcp-form-name"
					/>
				</div>
				<div class="flex flex-col gap-1">
					<label for="mcp-transport" class="text-muted-foreground text-xs font-medium">Transport</label>
					<select
						id="mcp-transport"
						class="border-input bg-background h-8 rounded-md border px-2 text-xs shadow-xs outline-none"
						bind:value={transport}
						data-testid="mcp-form-transport"
					>
						<option value="stdio">stdio (spawned in this workspace's sandbox)</option>
						<option value="http">http (dialed directly)</option>
					</select>
				</div>
				{#if transport === 'stdio'}
					<div class="flex flex-col gap-1">
						<label for="mcp-command" class="text-muted-foreground text-xs font-medium">Command</label>
						<Input
							id="mcp-command"
							placeholder="python3"
							class="h-8 font-mono text-xs"
							bind:value={commandText}
							data-testid="mcp-form-command"
						/>
					</div>
					<div class="flex flex-col gap-1">
						<label for="mcp-args" class="text-muted-foreground text-xs font-medium">Args (one per line)</label>
						<textarea
							id="mcp-args"
							class={textareaClass}
							rows="2"
							placeholder="/opt/sandbox/mcp_fixture_server.py"
							bind:value={argsText}
							data-testid="mcp-form-args"
						></textarea>
					</div>
					<div class="flex flex-col gap-1 md:col-span-2">
						<label for="mcp-env" class="text-muted-foreground text-xs font-medium">
							Env (KEY=value per line{editing != null && editing.env_keys.length > 0
								? ` — stored: ${editing.env_keys.join(', ')}`
								: ''})
						</label>
						{#if editing != null}
							<label class="text-muted-foreground flex items-center gap-2 text-xs">
								<input
									type="checkbox"
									bind:checked={replaceSecrets}
									data-testid="mcp-form-replace-secrets"
								/>
								Replace stored values (unchecked keeps them; values are write-only and never shown)
							</label>
						{/if}
						<textarea
							id="mcp-env"
							class={textareaClass}
							rows="2"
							placeholder="API_TOKEN=…"
							disabled={editing != null && !replaceSecrets}
							bind:value={envText}
							data-testid="mcp-form-env"
						></textarea>
					</div>
				{:else}
					<div class="flex flex-col gap-1 md:col-span-2">
						<label for="mcp-url" class="text-muted-foreground text-xs font-medium">URL</label>
						<Input
							id="mcp-url"
							placeholder="https://mcp.example.com/sse"
							class="h-8 font-mono text-xs"
							bind:value={urlText}
							data-testid="mcp-form-url"
						/>
					</div>
					<div class="flex flex-col gap-1 md:col-span-2">
						<label for="mcp-headers" class="text-muted-foreground text-xs font-medium">
							Headers (KEY=value per line{editing != null && editing.header_keys.length > 0
								? ` — stored: ${editing.header_keys.join(', ')}`
								: ''})
						</label>
						{#if editing != null}
							<label class="text-muted-foreground flex items-center gap-2 text-xs">
								<input
									type="checkbox"
									bind:checked={replaceSecrets}
									data-testid="mcp-form-replace-secrets"
								/>
								Replace stored values (unchecked keeps them; values are write-only and never shown)
							</label>
						{/if}
						<textarea
							id="mcp-headers"
							class={textareaClass}
							rows="2"
							placeholder="Authorization=Bearer …"
							disabled={editing != null && !replaceSecrets}
							bind:value={headersText}
							data-testid="mcp-form-headers"
						></textarea>
					</div>
				{/if}
				<div class="flex flex-col gap-1">
					<label for="mcp-include" class="text-muted-foreground text-xs font-medium">Tool include globs (empty = all tools)</label>
					<textarea
						id="mcp-include"
						class={textareaClass}
						rows="2"
						placeholder="echo&#10;get-*"
						bind:value={includeText}
						data-testid="mcp-form-include"
					></textarea>
				</div>
				<div class="flex flex-col gap-1">
					<label for="mcp-exclude" class="text-muted-foreground text-xs font-medium">Tool exclude globs (win over include)</label>
					<textarea
						id="mcp-exclude"
						class={textareaClass}
						rows="2"
						placeholder="delete-*"
						bind:value={excludeText}
						data-testid="mcp-form-exclude"
					></textarea>
				</div>
				<div class="grid grid-cols-3 gap-2 md:col-span-2">
					<div class="flex flex-col gap-1">
						<label for="mcp-connect-timeout" class="text-muted-foreground text-xs font-medium">Connect timeout (s)</label>
						<Input
							id="mcp-connect-timeout"
							type="number"
							min="1"
							step="any"
							class="h-8 text-xs"
							bind:value={connectTimeoutText}
						/>
					</div>
					<div class="flex flex-col gap-1">
						<label for="mcp-call-timeout" class="text-muted-foreground text-xs font-medium">Call timeout (s)</label>
						<Input
							id="mcp-call-timeout"
							type="number"
							min="1"
							step="any"
							class="h-8 text-xs"
							bind:value={callTimeoutText}
						/>
					</div>
					<div class="flex flex-col gap-1">
						<label for="mcp-idle-ttl" class="text-muted-foreground text-xs font-medium">Idle TTL (s)</label>
						<Input
							id="mcp-idle-ttl"
							type="number"
							min="10"
							step="any"
							class="h-8 text-xs"
							bind:value={idleTtlText}
						/>
					</div>
				</div>
			</div>
			{#if formError}
				<p class="text-destructive m-0 text-xs" role="alert" data-testid="mcp-form-error">
					{formError}
				</p>
			{/if}
			<div class="flex items-center gap-2">
				<Button type="submit" size="sm" disabled={submitting} data-testid="mcp-form-submit">
					{submitting ? 'Saving…' : editing == null ? 'Create' : 'Save changes'}
				</Button>
				<Button type="button" variant="ghost" size="sm" onclick={closeForm}>Cancel</Button>
			</div>
		</form>
	{/if}

	{#if !loading && servers.length === 0}
		<p class="text-muted-foreground text-sm italic" data-testid="mcp-empty">
			No MCP servers configured for this workspace.
		</p>
	{/if}

	<ul class="m-0 flex list-none flex-col gap-3 p-0">
		{#each servers as server (server.id)}
			{@const state = probeState(server)}
			{@const liveProbe = probeResults[server.id]}
			<li
				class="border-border bg-card rounded-lg border p-4"
				data-testid="mcp-server-{server.name}"
			>
				<div class="flex flex-wrap items-center gap-2">
					<span class="text-foreground font-mono text-sm font-semibold">{server.name}</span>
					<Badge variant="outline" class="text-muted-foreground text-[10px] uppercase">
						{server.transport}
					</Badge>
					<Badge
						variant="outline"
						class={server.enabled
							? 'border-transparent bg-primary/15 text-primary'
							: 'border-transparent bg-surface-3 text-muted-foreground'}
						data-testid="mcp-server-{server.name}-enabled"
					>
						{server.enabled ? 'enabled' : 'disabled'}
					</Badge>
					<span
						class={cn(
							'text-xs',
							state === 'ok' && 'text-primary',
							state === 'failed' && 'text-destructive',
							state === 'never' && 'text-muted-foreground italic'
						)}
						data-testid="mcp-server-{server.name}-probe"
					>
						{probeSummary(server)}
					</span>
					<div class="ml-auto flex items-center gap-1.5">
						<Button
							variant="outline"
							size="sm"
							class="h-7 px-2 text-xs"
							disabled={probing.has(server.id)}
							onclick={() => void probe(server)}
							data-testid="mcp-server-{server.name}-probe-btn"
						>
							{probing.has(server.id) ? 'Probing…' : 'Probe'}
						</Button>
						<Button
							variant="outline"
							size="sm"
							class="h-7 px-2 text-xs"
							disabled={togglingIds.has(server.id)}
							onclick={() => void toggleEnabled(server)}
							data-testid="mcp-server-{server.name}-toggle"
						>
							{togglingIds.has(server.id) ? '…' : server.enabled ? 'Disable' : 'Enable'}
						</Button>
						<Button
							variant="ghost"
							size="sm"
							class="h-7 px-2 text-xs"
							onclick={() => openEdit(server)}
							data-testid="mcp-server-{server.name}-edit"
						>
							Edit
						</Button>
						<Button
							variant="ghost"
							size="sm"
							class="text-destructive hover:bg-destructive/10 hover:text-destructive h-7 px-2 text-xs"
							disabled={deletingIds.has(server.id)}
							onclick={() => void removeServer(server)}
							data-testid="mcp-server-{server.name}-delete"
						>
							{deletingIds.has(server.id)
								? '…'
								: deleteArmedId === server.id
									? 'Really delete?'
									: 'Delete'}
						</Button>
					</div>
				</div>

				{#if server.transport === 'stdio'}
					<p class="text-muted-foreground m-0 mt-1 font-mono text-xs">
						{server.command} {server.args.join(' ')}
					</p>
				{:else}
					<p class="text-muted-foreground m-0 mt-1 font-mono text-xs">{server.url}</p>
				{/if}
				{#if server.env_keys.length > 0 || server.header_keys.length > 0}
					<p class="text-muted-foreground m-0 mt-1 text-xs">
						Secrets (values never shown):
						{[...server.env_keys.map((k) => `env ${k}`), ...server.header_keys.map((k) => `header ${k}`)].join(', ')}
					</p>
				{/if}

				{#if state === 'failed' && server.last_probe_error}
					<p
						class="text-destructive m-0 mt-2 text-xs"
						role="alert"
						data-testid="mcp-server-{server.name}-error"
					>
						{server.last_probe_error}
					</p>
				{/if}

				{#if liveProbe?.ok}
					<div class="mt-2" data-testid="mcp-server-{server.name}-tools">
						<p class="text-muted-foreground m-0 text-xs">
							Probe OK ({liveProbe.duration_ms} ms{liveProbe.server_info
								? ` · ${liveProbe.server_info}`
								: ''}) — {liveProbe.tools.length} tools listed:
						</p>
						<ul class="m-0 mt-1 flex list-none flex-col gap-0.5 p-0">
							{#each liveProbe.tools as tool (tool.name)}
								<li class="flex items-baseline gap-2 text-xs">
									<span class="text-foreground font-mono">{tool.name}</span>
									{#if tool.included}
										<span class="text-primary font-mono text-[10px]">{tool.kind}</span>
									{:else}
										<span class="text-muted-foreground text-[10px] italic">filtered out</span>
									{/if}
									{#if tool.description}
										<span class="text-muted-foreground min-w-0 flex-1 truncate" title={tool.description}>
											{tool.description}
										</span>
									{/if}
								</li>
							{/each}
						</ul>
					</div>
				{:else if server.tools != null && state === 'ok'}
					<p class="text-muted-foreground m-0 mt-2 text-xs">
						Cached tools: {toolCount(server)} contributing
						{#if server.catalog_kinds.length > 0}
							(<span class="font-mono">{server.catalog_kinds.join(', ')}</span>)
						{/if}
					</p>
				{/if}
			</li>
		{/each}
	</ul>
</section>
