"""Seed a Playwright ``storage_state.json`` for the bot account.

This is the one-time helper an operator runs on the host before the
meet-worker can sign into Google as the bot. Workflow::

    cd backend
    uv sync --extra auth-seed                # installs Playwright on the host
    uv run playwright install chromium       # downloads the browser binary
    uv run python -m johnny.tools.seed_auth_state \\
        --account-id 3 \\
        --email nikita.rabykin@aikamatkat.fi

The script opens a visible Chromium window. The operator signs in to
Google as the bot account (the email is pre-typed). When the URL
settles on ``myaccount.google.com`` (or any signed-in destination), the
script saves Playwright's storage_state (cookies + localStorage) to a
temp JSON file and copies it into the shared docker volume
``google_auth_state`` under ``account-<id>/storage_state.json``.

Once seeded, the meet-worker bootstrap (US-020 → Johnny-ckz.1) finds
the file at ``/var/lib/johnny/google-auth/account-<id>/storage_state.json``
and Chromium loads the bot's session straight away — no sign-in prompt,
no perpetual "joining".

Re-run any time the cookies expire (or after a manual sign-out). The
existing file in the volume is overwritten atomically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("johnny.tools.seed_auth_state")

# Default container the API process runs in. The script copies the
# storage_state.json into the bind-mounted google_auth_state volume
# via this container so we don't have to know the host path of the
# Docker volume (which differs across Docker Desktop / Engine).
DEFAULT_API_CONTAINER = "johnny-api-1"
DEFAULT_VOLUME_TARGET_DIR = "/var/lib/johnny/google-auth"

# Hostnames whose pages mean "the user is signed in" (the destination
# after a successful Google login). We check the URL hostname, not a
# substring of the URL, so query params like ``?continue=https://myaccount...``
# on the sign-in page don't false-positive.
SIGNED_IN_HOSTS: frozenset[str] = frozenset(
    {
        "myaccount.google.com",
        "mail.google.com",
        "calendar.google.com",
        "meet.google.com",
    }
)

# Default Google entry URL — the AccountChooser pre-types the email if
# we pass ``Email=`` and routes returning users directly to password.
SIGN_IN_URL_TEMPLATE = (
    "https://accounts.google.com/AccountChooser?Email={email}&continue=https%3A%2F%2Fmyaccount.google.com"
)


class SeederError(RuntimeError):
    """Raised when a precondition fails (Playwright missing, copy fails, ...)."""


def _is_signed_in(url: str) -> bool:
    """Whether ``url`` belongs to a signed-in destination host.

    Checks the parsed hostname, not a string ``in url`` match, so the
    sign-in funnel's ``?continue=https://myaccount...`` query parameter
    doesn't trigger a false positive (the script used to fire as soon
    as Chromium hit the AccountChooser page).
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host in SIGNED_IN_HOSTS


async def wait_for_signin(page: object, *, poll_interval_s: float = 1.0) -> None:
    """Block until the live page lands on a signed-in URL.

    Polls ``page.url`` rather than relying on navigation events because
    Google's sign-in funnel does multiple redirects and SPA transitions
    that don't all fire ``framenavigated`` events.
    """
    while True:
        url = str(getattr(page, "url", ""))
        if _is_signed_in(url):
            return
        await asyncio.sleep(poll_interval_s)


async def export_storage_state(
    *,
    email: str,
    output_path: Path,
    headless: bool = False,
) -> None:
    """Open Chromium, wait for sign-in, write storage_state to ``output_path``."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SeederError(
            "Playwright is not installed on the host. Run "
            "`uv sync --extra auth-seed && uv run playwright install chromium`."
        ) from exc

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(SIGN_IN_URL_TEMPLATE.format(email=email))
            logger.info(
                "Chromium open. Sign in to Google as %s in the visible "
                "window — the script will continue automatically when "
                "the URL lands on a signed-in destination.",
                email,
            )
            await wait_for_signin(page)
            logger.info("Sign-in detected at %s — saving storage_state", page.url)
            await context.storage_state(path=str(output_path))
        finally:
            await browser.close()


def copy_to_volume(
    *,
    local_path: Path,
    account_id: int,
    container: str,
    target_root: str = DEFAULT_VOLUME_TARGET_DIR,
) -> None:
    """Copy the storage_state.json into the API container's mounted volume.

    Uses ``docker cp`` so the host doesn't need to know where the named
    volume is actually mounted on disk (Docker Desktop hides the path
    inside the VM). The target directory is created first via
    ``docker exec mkdir -p``.
    """
    target_dir = f"{target_root}/account-{account_id}"
    mkdir_cmd = ["docker", "exec", container, "mkdir", "-p", target_dir]
    cp_cmd = [
        "docker",
        "cp",
        str(local_path),
        f"{container}:{target_dir}/storage_state.json",
    ]
    _run(mkdir_cmd)
    _run(cp_cmd)
    logger.info(
        "copied storage_state.json into %s:%s/storage_state.json",
        container,
        target_dir,
    )


def _run(cmd: Iterable[str]) -> None:
    cmd_list = list(cmd)
    proc = subprocess.run(cmd_list, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SeederError(
            f"command {cmd_list!r} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="johnny.tools.seed_auth_state",
        description=(
            "Seed a Playwright storage_state.json for the bot account so "
            "the meet-worker can sign into Google without a prompt. "
            "Opens a headed Chromium for one manual sign-in, then persists "
            "the resulting cookies into the shared docker volume."
        ),
    )
    parser.add_argument(
        "--account-id",
        type=int,
        required=True,
        help="GoogleAccount row id (role=bot). e.g. 3.",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email pre-typed on the Google sign-in form.",
    )
    parser.add_argument(
        "--container",
        default=DEFAULT_API_CONTAINER,
        help=(
            f"API container the volume is mounted on. Defaults to "
            f"{DEFAULT_API_CONTAINER!r}."
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Run Chromium headless (debugging only — the operator can't "
            "complete the sign-in without seeing it)."
        ),
    )
    parser.add_argument(
        "--keep-local",
        type=Path,
        default=None,
        help=(
            "Also keep a copy of storage_state.json at this path on the "
            "host (handy for backup or sharing across stacks)."
        ),
    )
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="johnny-auth-seed-") as tmp:
        local_path = Path(tmp) / "storage_state.json"
        try:
            await export_storage_state(
                email=args.email,
                output_path=local_path,
                headless=args.headless,
            )
        except SeederError as exc:
            logger.error("seed failed: %s", exc)
            return 2

        # Sanity-check the JSON before copying so we don't ship a corrupt
        # file into the shared volume.
        try:
            json.loads(local_path.read_text())
        except json.JSONDecodeError as exc:
            logger.error("storage_state.json is malformed: %s", exc)
            return 3

        try:
            copy_to_volume(
                local_path=local_path,
                account_id=args.account_id,
                container=args.container,
            )
        except SeederError as exc:
            logger.error("copy to docker volume failed: %s", exc)
            return 4

        if args.keep_local is not None:
            args.keep_local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(local_path, args.keep_local)
            logger.info("kept a copy at %s", args.keep_local)

    logger.info(
        "DONE. The next bot session will load this Google session "
        "automatically — try Join Now in the UI."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


__all__ = [
    "DEFAULT_API_CONTAINER",
    "DEFAULT_VOLUME_TARGET_DIR",
    "SIGN_IN_URL_TEMPLATE",
    "SIGNED_IN_HOSTS",
    "SeederError",
    "copy_to_volume",
    "export_storage_state",
    "main",
    "parse_args",
    "wait_for_signin",
]
