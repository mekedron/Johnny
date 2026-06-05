"""One function per wizard phase.

Each step takes the shared :class:`WizardContext` and the chosen
:class:`Prompter`, runs interactive prompts + side-effects, and returns
a :class:`StepResult`. The CLI orchestrator (:mod:`cli`) chains them in
order and prints the final report.

The steps are written so re-running the wizard is safe: each step
detects existing state (``.env`` keys already filled, providers already
registered, models already downloaded) and offers to skip or overwrite.
"""

from __future__ import annotations

import logging
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from johnny.wizard import compose, env_file, models, prereqs, providers
from johnny.wizard.api_client import WizardApiClient, WizardApiError, find_existing_provider
from johnny.wizard.prompts import NonInteractivePrompter, Prompter

logger = logging.getLogger(__name__)


# --- Shared context --------------------------------------------------------


@dataclass
class WizardContext:
    """State that flows between steps."""

    project_root: Path
    env_path: Path
    env_template_path: Path
    api_url: str
    console: Console
    open_browser: bool = True

    # State filled in by steps as the run progresses. Used so later
    # steps know what earlier steps did.
    env: dict[str, str] = field(default_factory=dict)
    registered_providers: list[dict[str, Any]] = field(default_factory=list)
    test_results: list[dict[str, Any]] = field(default_factory=list)
    downloaded_models: list[models.DownloadResult] = field(default_factory=list)


@dataclass
class StepResult:
    """Outcome of one wizard phase, for the final report."""

    name: str
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)


# --- Console helpers -------------------------------------------------------


def render_panel(console: Console, title: str, body: str) -> None:
    console.print(Panel.fit(body, title=title, border_style="cyan"))


def render_prereqs_table(console: Console, results: Sequence[prereqs.PrereqResult]) -> None:
    table = Table(title="Prerequisites", expand=False)
    table.add_column("Tool")
    table.add_column("Status")
    table.add_column("Detail")
    table.add_column("Install")
    for r in results:
        status = "[green]OK[/green]" if r.ok else "[red]missing[/red]"
        url = r.install_url or ""
        table.add_row(r.name, status, r.detail, url)
    console.print(table)


def _set_key(prompter: Prompter, key: str) -> None:
    """If ``prompter`` is a :class:`NonInteractivePrompter`, scope the next answer."""
    if isinstance(prompter, NonInteractivePrompter):
        prompter.set_key(key)


# --- Step: prerequisites ---------------------------------------------------


def step_prereqs(ctx: WizardContext) -> StepResult:
    """Run prerequisite checks and surface anything missing.

    Required (Docker, Compose) failures cause this step to report
    ``ok=False`` so the CLI can prompt the user to abort. Soft checks
    (uv, pnpm, Ollama, GPU, disk) report but do not block.
    """
    results = prereqs.check_all()
    render_prereqs_table(ctx.console, results)
    missing = prereqs.missing_required(results)
    if missing:
        items = ", ".join(f"{r.name} ({r.install_url})" for r in missing)
        return StepResult(
            name="Prerequisites",
            ok=False,
            summary=f"Missing required tools: {items}",
            details=[r.detail for r in missing],
        )
    soft_missing = [r for r in results if not r.ok and r not in missing]
    summary = "All required tools present"
    if soft_missing:
        summary += f" (warnings: {', '.join(r.name for r in soft_missing)})"
    return StepResult(name="Prerequisites", ok=True, summary=summary)


# --- Step: .env + Fernet key -----------------------------------------------


def step_env_and_fernet(ctx: WizardContext, prompter: Prompter) -> StepResult:
    """Ensure ``.env`` exists and ``FERNET_KEY`` is populated."""
    created = env_file.ensure_env_file(ctx.env_path, ctx.env_template_path)
    ctx.env = env_file.read_env_file(ctx.env_path)
    if created:
        ctx.console.print(
            f"[green]Created[/green] {ctx.env_path} from {ctx.env_template_path}"
        )

    existing_key = ctx.env.get("FERNET_KEY", "").strip()
    if existing_key:
        ctx.console.print(
            f"[yellow]FERNET_KEY already present in {ctx.env_path}[/yellow] — leaving it."
        )
        return StepResult(
            name=".env / FERNET_KEY",
            ok=True,
            summary="FERNET_KEY already set",
        )

    new_key = Fernet.generate_key().decode("ascii")
    env_file.write_env_values(ctx.env_path, {"FERNET_KEY": new_key})
    ctx.env["FERNET_KEY"] = new_key
    ctx.console.print(
        "[green]Generated[/green] FERNET_KEY and wrote it to .env. "
        "Back this file up — losing this key makes encrypted rows unrecoverable."
    )
    return StepResult(name=".env / FERNET_KEY", ok=True, summary="generated and saved")


# --- Step: Google OAuth setup ---------------------------------------------


GOOGLE_CONSOLE_URLS = {
    "project": "https://console.cloud.google.com/projectcreate",
    "calendar_api": "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com",
    "consent": "https://console.cloud.google.com/apis/credentials/consent",
    "credentials": "https://console.cloud.google.com/apis/credentials",
}


def step_google_oauth(ctx: WizardContext, prompter: Prompter) -> StepResult:
    """Walk the user through registering a Google OAuth desktop client."""
    existing_client_id = ctx.env.get("GOOGLE_CLIENT_ID", "").strip()
    existing_client_secret = ctx.env.get("GOOGLE_CLIENT_SECRET", "").strip()

    if existing_client_id and existing_client_secret:
        ctx.console.print(
            f"[yellow]GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are already set in "
            f"{ctx.env_path}[/yellow]"
        )
        _set_key(prompter, "google_reconfigure")
        if not prompter.ask_confirm(
            "Reconfigure Google OAuth credentials?", default=False
        ):
            return StepResult(
                name="Google OAuth",
                ok=True,
                summary="kept existing credentials",
            )

    render_panel(
        ctx.console,
        "Google OAuth desktop client",
        (
            "Johnny joins meetings as a Google identity. You need to register a\n"
            "[bold]Desktop[/bold] OAuth client and grant Calendar read access.\n\n"
            f"1. Create a project:  {GOOGLE_CONSOLE_URLS['project']}\n"
            f"2. Enable Calendar API: {GOOGLE_CONSOLE_URLS['calendar_api']}\n"
            f"3. Configure consent: {GOOGLE_CONSOLE_URLS['consent']}\n"
            "   - User type: External\n"
            "   - Scopes: openid, userinfo.email, userinfo.profile,\n"
            "             calendar.readonly, calendar.events.readonly\n"
            "   - Test users: add your personal email AND the johnny-bot account\n"
            f"4. Create OAuth client: {GOOGLE_CONSOLE_URLS['credentials']}\n"
            "   - Application type: [bold]Desktop app[/bold]\n"
            "5. Copy the Client ID and Client Secret into the prompts below."
        ),
    )

    _set_key(prompter, "google_open_browser")
    if ctx.open_browser and prompter.ask_confirm(
        "Open the Google Cloud Console in your browser now?", default=True
    ):
        try:
            webbrowser.open(GOOGLE_CONSOLE_URLS["credentials"])
        except webbrowser.Error as exc:  # pragma: no cover — depends on host
            ctx.console.print(f"[yellow]Could not open browser:[/yellow] {exc}")

    _set_key(prompter, "google_client_id")
    client_id = prompter.ask_text("Google Client ID", default=existing_client_id or None)
    _set_key(prompter, "google_client_secret")
    client_secret = prompter.ask_secret("Google Client Secret")

    updates = {
        "GOOGLE_CLIENT_ID": client_id.strip(),
        "GOOGLE_CLIENT_SECRET": client_secret.strip(),
    }
    if "GOOGLE_OAUTH_REDIRECT_URI" not in ctx.env or not ctx.env.get("GOOGLE_OAUTH_REDIRECT_URI"):
        updates["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8000/auth/google/callback"

    env_file.write_env_values(ctx.env_path, updates)
    ctx.env.update(updates)
    ctx.console.print(
        f"[green]Wrote[/green] GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to {ctx.env_path}."
    )
    return StepResult(name="Google OAuth", ok=True, summary="credentials saved to .env")


# --- Step: stack up --------------------------------------------------------


def step_compose_up(ctx: WizardContext, prompter: Prompter) -> StepResult:
    """Bring up the Compose stack (if not already running) and wait for ``/health``."""
    if compose.is_stack_running(ctx.project_root):
        ctx.console.print("[yellow]Compose stack already running[/yellow] — skipping `up`.")
        already = True
    else:
        _set_key(prompter, "compose_up")
        if not prompter.ask_confirm(
            "Bring up the Docker Compose stack now? (api, worker, frontend, postgres, redis)",
            default=True,
        ):
            return StepResult(
                name="Compose up",
                ok=False,
                summary="user declined to start the stack",
            )
        result = compose.compose_up(ctx.project_root, detach=True)
        if not result.ok:
            return StepResult(
                name="Compose up",
                ok=False,
                summary=result.detail,
            )
        already = False

    with WizardApiClient(ctx.api_url) as client:
        if not client.wait_for_health(timeout_s=90.0):
            return StepResult(
                name="Compose up",
                ok=False,
                summary=f"API never became healthy at {ctx.api_url}",
            )
    return StepResult(
        name="Compose up",
        ok=True,
        summary=(
            "stack already running, API reachable" if already else "stack started, API reachable"
        ),
    )


# --- Step: meet-worker image ----------------------------------------------


def step_meet_worker_image(ctx: WizardContext, prompter: Prompter) -> StepResult:
    """Build the meet-worker image if missing (required for local STT download)."""
    if models.image_exists(models.MEET_WORKER_IMAGE):
        return StepResult(
            name="meet-worker image",
            ok=True,
            summary=f"{models.MEET_WORKER_IMAGE} already built",
        )
    _set_key(prompter, "build_meet_worker")
    if not prompter.ask_confirm(
        "Build the meet-worker image now? (~10 min on first build)",
        default=True,
    ):
        return StepResult(
            name="meet-worker image",
            ok=False,
            summary="user declined to build meet-worker image",
        )
    result = models.build_meet_worker_image(ctx.project_root)
    return StepResult(
        name="meet-worker image",
        ok=result.ok,
        summary=result.detail,
    )


# --- Step: provider configuration -----------------------------------------


def _select_choice(
    ctx: WizardContext,
    prompter: Prompter,
    kind: providers.Kind,
    *,
    key_hosting: str,
    key_provider: str,
) -> providers.ProviderChoice | None:
    """Ask the user (or YAML) to pick one catalog entry for ``kind``."""
    _set_key(prompter, key_hosting)
    hosting_index = prompter.ask_choice(
        f"{kind.value.upper()} hosting",
        ["Local (on-device, no third party)", "Cloud (managed API)"],
        default_index=0,
    )
    hosting = providers.Hosting.LOCAL if hosting_index == 0 else providers.Hosting.CLOUD

    options = providers.choices_for(kind, hosting)
    if not options:
        ctx.console.print(f"[red]No {hosting.value} options for {kind.value}[/red]")
        return None
    labels = [c.label + (" (RECOMMENDED)" if i == 0 else "") for i, c in enumerate(options)]
    _set_key(prompter, key_provider)
    index = prompter.ask_choice(
        f"{kind.value.upper()} provider", labels, default_index=0
    )
    return options[index]


def _ensure_local_artifacts(
    ctx: WizardContext, prompter: Prompter, choice: providers.ProviderChoice
) -> tuple[bool, str, dict[str, Any]]:
    """For a local provider, download model files and return updated options.

    Returns ``(ok, message, extra_options)``. Extra options override
    ``choice.default_options`` to encode the user's model/voice pick.
    """
    install = choice.install
    if install is None:
        return True, "no local artifacts to download", {}

    extra_options: dict[str, Any] = {}

    if install["kind"] == "whisper":
        labels = [m["label"] for m in install["models"]]
        _set_key(prompter, f"whisper_model_{choice.kind.value}")
        default_id = install["default_model"]
        default_index = next(
            (i for i, m in enumerate(install["models"]) if m["id"] == default_id), 0
        )
        index = prompter.ask_choice("Whisper model", labels, default_index=default_index)
        model = install["models"][index]
        model_size = str(model["id"])
        extra_options["model_size"] = model_size

        if models.whisper_model_present(model_size):
            ctx.console.print(
                f"[yellow]Whisper model {model_size!r} already present in volume[/yellow]"
                " — skipping download."
            )
            return True, f"whisper {model_size} already cached", extra_options

        ctx.console.print(
            f"Downloading faster-whisper {model_size} into volume "
            f"{models.WHISPER_VOLUME}…"
        )
        result = models.download_whisper_model(model_size)
        ctx.downloaded_models.append(result)
        return result.ok, result.detail, extra_options

    if install["kind"] == "piper":
        labels = [v["label"] for v in install["voices"]]
        _set_key(prompter, f"piper_voice_{choice.kind.value}")
        default_voice = install["default_voice"]
        default_index = next(
            (i for i, v in enumerate(install["voices"]) if v["id"] == default_voice), 0
        )
        index = prompter.ask_choice("Piper voice", labels, default_index=default_index)
        voice = install["voices"][index]
        voice_id = str(voice["id"])
        extra_options["voice_id"] = voice_id

        if models.piper_voice_present(voice_id):
            ctx.console.print(
                f"[yellow]Piper voice {voice_id!r} already present in volume[/yellow]"
                " — skipping download."
            )
            return True, f"piper voice {voice_id} already cached", extra_options

        ctx.console.print(
            f"Downloading piper voice {voice_id} into volume {models.PIPER_VOLUME}…"
        )
        result = models.download_piper_voice(
            voice_id,
            onnx_url=str(voice["onnx_url"]),
            json_url=str(voice["json_url"]),
        )
        ctx.downloaded_models.append(result)
        return result.ok, result.detail, extra_options

    if install["kind"] == "ollama":
        if not models.ollama_available():
            ctx.console.print(
                "[red]Ollama CLI is not installed.[/red] Install it from "
                "https://ollama.com/download then re-run the wizard."
            )
            return False, "ollama not installed", extra_options

        labels = [m["label"] for m in install["models"]]
        _set_key(prompter, f"ollama_model_{choice.kind.value}")
        default_id = install["default_model"]
        default_index = next(
            (i for i, m in enumerate(install["models"]) if m["id"] == default_id), 0
        )
        index = prompter.ask_choice("Ollama model", labels, default_index=default_index)
        model = install["models"][index]
        model_tag = str(model["id"])
        extra_options["model"] = model_tag

        result = models.pull_ollama_model(model_tag)
        ctx.downloaded_models.append(result)
        return result.ok, result.detail, extra_options

    return False, f"unknown install kind: {install['kind']}", extra_options


def _gather_credentials(
    ctx: WizardContext,
    prompter: Prompter,
    choice: providers.ProviderChoice,
) -> dict[str, str]:
    """Prompt for the credential keys the chosen provider requires."""
    creds: dict[str, str] = {}
    for key in choice.credential_keys:
        # If the user already wrote the matching .env var, surface it as the default.
        env_default = ""
        if choice.env_key and key == "api_key":
            env_default = ctx.env.get(choice.env_key, "").strip()

        # Local providers may need a non-secret api_key placeholder (Ollama).
        if (
            choice.hosting is providers.Hosting.LOCAL
            and choice.provider_name == "openai-compatible"
            and key == "api_key"
            and not env_default
        ):
            env_default = "ollama"

        prompt_key = f"{choice.kind.value}_{choice.provider_name}_{key}"
        _set_key(prompter, prompt_key)
        if env_default:
            value = prompter.ask_text(
                f"{key} for {choice.display_name}", default=env_default
            )
        else:
            value = prompter.ask_secret(f"{key} for {choice.display_name}")
        creds[key] = value.strip()
    return creds


def _register_one(
    ctx: WizardContext,
    prompter: Prompter,
    kind: providers.Kind,
    api: WizardApiClient,
    listing: dict[str, list[dict[str, Any]]],
) -> tuple[bool, str]:
    """Walk the user through choosing + registering one provider for ``kind``."""
    choice = _select_choice(
        ctx,
        prompter,
        kind,
        key_hosting=f"{kind.value}_hosting",
        key_provider=f"{kind.value}_provider",
    )
    if choice is None:
        return False, "no provider selected"

    existing = find_existing_provider(
        listing, kind=kind.value, provider_name=choice.provider_name
    )
    if existing is not None:
        ctx.console.print(
            f"[yellow]A {kind.value} provider {choice.provider_name!r} (id={existing['id']}) "
            f"is already registered.[/yellow]"
        )
        _set_key(prompter, f"reuse_{kind.value}_{choice.provider_name}")
        if prompter.ask_confirm(
            "Reuse the existing record and skip re-registration?", default=True
        ):
            try:
                api.activate_provider(existing["id"])
            except WizardApiError as exc:
                return False, f"activate failed: {exc}"
            ctx.registered_providers.append(existing)
            return True, f"reused id={existing['id']} ({choice.display_name})"

    ok, message, extra_options = _ensure_local_artifacts(ctx, prompter, choice)
    if not ok:
        return False, message

    options = dict(choice.default_options)
    options.update(extra_options)
    creds = _gather_credentials(ctx, prompter, choice)

    # If the user provided an API key for a cloud provider, also sync the
    # convenience .env var so subsequent runs detect it.
    if choice.env_key and creds.get("api_key"):
        env_file.write_env_values(ctx.env_path, {choice.env_key: creds["api_key"]})
        ctx.env[choice.env_key] = creds["api_key"]

    try:
        created = api.create_provider(
            kind=kind.value,
            provider_name=choice.provider_name,
            display_name=choice.display_name,
            credentials=creds,
            options=options,
        )
        api.activate_provider(int(created["id"]))
    except WizardApiError as exc:
        return False, f"registration failed: {exc}"

    ctx.registered_providers.append(created)
    return True, f"registered + activated id={created['id']} ({choice.display_name})"


def step_providers(ctx: WizardContext, prompter: Prompter) -> StepResult:
    """Configure STT, LLM, and TTS providers via the running API."""
    api = WizardApiClient(ctx.api_url, timeout=120.0)
    try:
        try:
            listing = api.list_providers()
        except WizardApiError as exc:
            return StepResult(
                name="Providers",
                ok=False,
                summary=f"cannot reach providers API: {exc}",
            )

        kinds = [providers.Kind.STT, providers.Kind.LLM, providers.Kind.TTS]
        details: list[str] = []
        all_ok = True
        for kind in kinds:
            ctx.console.rule(f"[bold cyan]{kind.value.upper()}[/bold cyan] provider")
            ok, message = _register_one(ctx, prompter, kind, api, listing)
            details.append(f"{kind.value}: {message}")
            if not ok:
                all_ok = False
            try:
                listing = api.list_providers()
            except WizardApiError:
                pass  # next iteration will surface the error if it persists
        return StepResult(
            name="Providers",
            ok=all_ok,
            summary=("all three providers configured" if all_ok else "some providers failed"),
            details=details,
        )
    finally:
        api.close()


# --- Step: smoke tests -----------------------------------------------------


def step_smoke_tests(ctx: WizardContext, prompter: Prompter) -> StepResult:
    """Run :class:`POST /providers/{id}/test` against every registered provider."""
    if not ctx.registered_providers:
        return StepResult(
            name="Smoke tests",
            ok=False,
            summary="no providers were registered",
        )
    api = WizardApiClient(ctx.api_url, timeout=180.0)
    details: list[str] = []
    all_ok = True
    try:
        for entry in ctx.registered_providers:
            provider_id = int(entry["id"])
            kind = entry.get("kind", "?")
            display_name = entry.get("display_name", "?")
            try:
                result = api.test_provider(provider_id, timeout=180.0)
            except WizardApiError as exc:
                details.append(f"[{kind}] {display_name}: client error — {exc}")
                all_ok = False
                continue
            ctx.test_results.append(result)
            if result.get("ok"):
                details.append(
                    f"[{kind}] {display_name}: [green]OK[/green] — {result.get('message', '')}"
                )
            else:
                details.append(
                    f"[{kind}] {display_name}: [red]FAIL[/red] — {result.get('message', '')}"
                )
                all_ok = False
    finally:
        api.close()
    for line in details:
        ctx.console.print(line)
    return StepResult(
        name="Smoke tests",
        ok=all_ok,
        summary=("all smoke tests passed" if all_ok else "one or more smoke tests failed"),
        details=details,
    )


# --- Step: open UI ---------------------------------------------------------


def step_open_ui(ctx: WizardContext, prompter: Prompter) -> StepResult:
    """Offer to open the SvelteKit UI."""
    url = "http://localhost:5173"
    _set_key(prompter, "open_ui")
    if not prompter.ask_confirm(f"Open the Johnny UI ({url}) in your browser now?", default=True):
        return StepResult(name="Open UI", ok=True, summary="skipped — open it later")
    if ctx.open_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error as exc:  # pragma: no cover — depends on host
            return StepResult(
                name="Open UI",
                ok=False,
                summary=f"could not open browser: {exc}",
            )
    return StepResult(name="Open UI", ok=True, summary=f"opened {url}")


__all__ = [
    "GOOGLE_CONSOLE_URLS",
    "StepResult",
    "WizardContext",
    "render_panel",
    "render_prereqs_table",
    "step_compose_up",
    "step_env_and_fernet",
    "step_google_oauth",
    "step_meet_worker_image",
    "step_open_ui",
    "step_prereqs",
    "step_providers",
    "step_smoke_tests",
]
