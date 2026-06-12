"""MCP server rows ↔ runtime configs + the probe orchestration (Johnny-trt.36).

The DB row (:class:`app.db.models.McpServer`) is the operator's source of
truth; this module is the only place rows become
:class:`johnny.mcp.config.McpServerConfig` value objects:

* :func:`load_server_configs` — the worker's per-claim read (the
  trt.38 no-restart pattern: enable/disable/filter edits bite the very next
  claimed task) and the probe path. Decrypts the env/headers blob.
* :func:`load_server_snapshots` — catalog assembly's read: configs WITHOUT
  secrets (the catalog only needs names/filters/cache, so session assembly
  never touches the Fernet key) plus the cached tool list + probe verdict.
* :func:`probe_server_row` — connect + initialize + ``tools/list`` against
  the row's live config, then persist the verdict: ``tools_cache`` updates
  only on success (a failed probe keeps the stale list so the catalog can
  render unavailable-with-reason instead of forgetting the tools,
  Johnny-trt.55), ``last_probe_*`` update always.

Rows that fail validation or decryption are SKIPPED with a log by the bulk
loaders — one corrupt row must never take down catalog assembly or the
worker's claim pass; the executor's per-kind degrade speaks honestly for it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import McpServer
from app.security.crypto import CredentialCrypto, CryptoError
from johnny.mcp.catalog import McpServerSnapshot, McpToolInfo
from johnny.mcp.config import McpConfigError, McpServerConfig

logger = logging.getLogger(__name__)


def _str_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


def _str_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def encrypt_secrets(
    crypto: CredentialCrypto, *, env: dict[str, str], headers: dict[str, str]
) -> str | None:
    """The ``secrets_encrypted`` blob for a row; ``None`` when there are none."""
    if not env and not headers:
        return None
    payload = {"env": dict(env), "headers": dict(headers)}
    return crypto.encrypt(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def decrypt_secrets(
    crypto: CredentialCrypto, secrets_encrypted: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    """``(env, headers)`` out of the blob. Raises :class:`CryptoError` on a bad key."""
    if not secrets_encrypted:
        return {}, {}
    payload = json.loads(crypto.decrypt(secrets_encrypted))
    if not isinstance(payload, dict):
        raise CryptoError("mcp secrets blob is not a JSON object")
    return _str_dict(payload.get("env")), _str_dict(payload.get("headers"))


def secret_key_names(
    crypto: CredentialCrypto | None, secrets_encrypted: str | None
) -> tuple[list[str], list[str]]:
    """``(env_keys, header_keys)`` for masked API responses; never raises."""
    if crypto is None or not secrets_encrypted:
        return [], []
    try:
        env, headers = decrypt_secrets(crypto, secrets_encrypted)
    except (CryptoError, ValueError, json.JSONDecodeError):
        return [], []
    return sorted(env), sorted(headers)


def row_to_config(
    row: McpServer, *, crypto: CredentialCrypto | None = None
) -> McpServerConfig:
    """One row as the validated runtime value object.

    ``crypto=None`` builds the SECRETLESS view (catalog assembly); passing a
    crypto decrypts env/headers for the connecting paths (worker, probe).
    Raises :class:`McpConfigError` / :class:`CryptoError` — bulk loaders
    catch and skip, API paths surface them.
    """
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    if crypto is not None:
        env, headers = decrypt_secrets(crypto, row.secrets_encrypted)
    include_raw = row.tool_include
    return McpServerConfig(
        name=row.name,
        transport=row.transport,
        enabled=bool(row.enabled),
        command=row.command or "",
        args=_str_list(row.args),
        env=env,
        url=row.url or "",
        headers=headers,
        tool_include=None if include_raw is None else _str_list(include_raw),
        tool_exclude=_str_list(row.tool_exclude),
        connect_timeout_s=float(row.connect_timeout_s),
        call_timeout_s=float(row.call_timeout_s),
        idle_ttl_s=float(row.idle_ttl_s),
    )


def cached_tools(row: McpServer) -> tuple[McpToolInfo, ...]:
    """The row's ``tools_cache`` as value objects (empty when never probed)."""
    if not isinstance(row.tools_cache, list):
        return ()
    infos: list[McpToolInfo] = []
    for item in row.tools_cache:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        infos.append(McpToolInfo(name=name, description=str(item.get("description") or "")))
    return tuple(infos)


def list_server_rows(db: Session) -> list[McpServer]:
    return list(db.scalars(select(McpServer).order_by(McpServer.name)).all())


def get_server_row(db: Session, server_id: int) -> McpServer | None:
    return db.get(McpServer, server_id)


def load_server_configs(
    db: Session, crypto: CredentialCrypto
) -> tuple[McpServerConfig, ...]:
    """Every server's full config — the worker's fresh per-claim read.

    Disabled rows are INCLUDED (``enabled=False`` on the value object): the
    executor distinguishes "connector isn't enabled" from "isn't configured"
    in its spoken degrade. Catalog assembly filters enabled rows itself.
    """
    configs: list[McpServerConfig] = []
    for row in db.scalars(select(McpServer).order_by(McpServer.name)):
        try:
            configs.append(row_to_config(row, crypto=crypto))
        except (McpConfigError, CryptoError, ValueError, json.JSONDecodeError):
            logger.exception(
                "mcp: skipping unusable server row id=%s name=%r", row.id, row.name
            )
    return tuple(configs)


def load_server_snapshots(db: Session) -> tuple[McpServerSnapshot, ...]:
    """Catalog assembly's view: secretless configs + cached tools + probe state."""
    snapshots: list[McpServerSnapshot] = []
    for row in db.scalars(
        select(McpServer).where(McpServer.enabled.is_(True)).order_by(McpServer.name)
    ):
        try:
            config = row_to_config(row, crypto=None)
        except McpConfigError:
            logger.exception(
                "mcp: skipping invalid server row id=%s name=%r in catalog",
                row.id,
                row.name,
            )
            continue
        snapshots.append(
            McpServerSnapshot(
                config=config,
                tools=cached_tools(row),
                probe_ok=row.last_probe_ok,
                probe_error=row.last_probe_error or "",
            )
        )
    return tuple(snapshots)


async def probe_server_row(
    db: Session, row: McpServer, crypto: CredentialCrypto, *, sandbox_url: str
) -> Any:
    """Probe one row's live config and persist the verdict on the row.

    Returns the :class:`johnny.mcp.client.McpProbeResult`. Works for
    disabled rows on purpose — the management flow is add → probe → enable.
    The ``johnny.mcp.client`` import stays lazy: the api process only pays
    the SDK import when an operator actually probes.
    """
    from johnny.mcp.client import probe_mcp_server

    config = row_to_config(row, crypto=crypto)
    result = await probe_mcp_server(config, sandbox_url=sandbox_url)
    row.last_probe_at = datetime.now(UTC)
    row.last_probe_ok = bool(result.ok)
    row.last_probe_error = "" if result.ok else (result.error or "probe failed")
    if result.ok:
        row.tools_cache = [
            {"name": tool.name, "description": tool.description}
            for tool in result.tools
        ]
    db.flush()
    return result


__all__ = [
    "cached_tools",
    "decrypt_secrets",
    "encrypt_secrets",
    "get_server_row",
    "list_server_rows",
    "load_server_configs",
    "load_server_snapshots",
    "probe_server_row",
    "row_to_config",
    "secret_key_names",
]
