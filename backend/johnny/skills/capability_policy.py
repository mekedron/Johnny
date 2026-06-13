"""Layered capability policy engine (Johnny-trt.38).

Configurable allow/deny over the capability surface, following openclaw's
layered resolution model (``src/agents/agent-tools.policy.ts`` — scope
overrides, deny wins at every merge) but Johnny-shaped: DB-backed rows
(``capability_policies``, the provider-settings pattern) resolve into ONE
immutable :class:`ResolvedCapabilityPolicy` that rides the trt.41 agent
snapshot to turn-time enforcement — the agent process never re-reads policy
tables.

THE RESOLUTION ORDER (normative, unit-pinned by
``tests/skills/test_capability_policy.py``)::

    WORKSPACE  →  PER-AGENT  →  PER-SESSION-MODE  →  PER-SESSION OVERRIDE

The base layer is the WORKSPACE (Johnny-wks.9): policy is a property of the
workspace an agent runs in, not a single global install-wide row. There is
NO global policy — the old global layer maps onto the default workspace's
base layer (the 0035 migration), so default-workspace agents are unchanged.

Merge rules, applied walking the layers in that order:

* ``tools_deny`` **accumulates** — a deny matching at ANY layer denies the
  tool; **deny wins at every merge** (a workspace deny beats an agent/mode/
  session allow; a mode deny beats a session allow). When several layers
  deny, the EARLIEST layer in the order wins attribution.
* ``tools_allow`` non-empty **redefines** the restrictive allow-list (the
  openclaw scope-override rule): from that layer on, only matching tools are
  allowed. The canonical least-privilege scenario lives here — the
  progress-meeting agent's layer says ``tools_allow=["google-calendar",
  "tasks.*"]`` and everything else (the finance skill included) is denied
  *for that agent* with the agent layer named as the decider. A later
  layer's ``tools_allow`` redefines again; use ``tools_deny`` for guarantees
  that must survive lower layers.
* ``tools_also_allow`` **extends** the allow-list currently in force without
  replacing it (openclaw's ``alsoAllow``). When no allow-list is in force
  (everything still allowed) it is a documented no-op.
* ``bins_deny`` accumulates exactly like ``tools_deny``, over sandbox exec
  binaries (``argv[0]`` basenames).
* ``safe_bins`` — the editable trt.35 baseline toolset — is honored on the
  **workspace layer only** (one curated list per workspace, the wks.9
  governance boundary). ``None`` means the built-in
  :data:`johnny.skills.policy.BASELINE_BINS`. A baseline bin REMOVED from
  the edited list is hard-denied (it beats skill ``requires.bins`` grants);
  a bin added is granted alongside the baseline. Resetting to default =
  storing ``None``.

Tool names are the capability catalog's kinds — internal tools
(``meeting.leave``), skill kinds (``google-calendar``), and future MCP tools
(``mcp__<server>__<tool>``); per-skill enable/disable is expressed through
these same lists (deny the skill's kind). Matching is :func:`fnmatch.fnmatchcase`
globbing (``*``, ``?``, ``[seq]``) so ``mcp__shady__*`` denies a whole server.

Enforcement consumes the resolved policy at three points (the trt.38
contract):

1. **catalog filtering** — :func:`apply_policy_to_catalog` at session
   assembly: a denied kind becomes a ``hidden`` unavailable entry, ABSENT
   from the rendered router catalog and the answer model's capability notes
   (the canonical scenario: the progress agent's prompt never even mentions
   finance kinds) while staying in the entry list so the gate's trt.55
   delegate backstop can degrade a forced attempt to the spoken decline;
2. **executor tool dispatch** — the worker re-resolves the policy fresh from
   the DB per claimed task (policy edits bite running sessions' next
   delegation without a restart) and refuses denied kinds before the runner;
3. **sandbox.exec argv[0]** — :func:`johnny.skills.policy.compute_allowed_bins`
   takes the resolved policy (edited baseline in, denied bins out) and
   :class:`~johnny.skills.policy.ExecBinPolicy` attributes policy denials.

Every denied ATTEMPT (never the silent filtering) emits a ``policy_denied``
conversation event naming the denying layer.

Out of scope by design (the trt.38 bead): per-flag bin profiles (openclaw's
grep-stdin-only style) — the sandbox container is Johnny's security
boundary, so bin-level + glob control suffices. The extension hook is
documented on :class:`johnny.skills.policy.ExecBinPolicy`.

Identity is never policy (the Phase-7 invariant): WHO an agent acts as lives
in sandbox state (gog keyring …), so the same policy stays valid whether the
agent runs in the default or another workspace's sandbox.

Stdlib-only and import-cheap, like :mod:`johnny.agent.task_catalog` (which
it imports for the transform) — both ends of the wire (api dispatch surfaces
and the agent/worker processes) load it without the provider stack.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from posixpath import basename
from typing import Any

from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.skills.policy import BASELINE_BINS

logger = logging.getLogger(__name__)

POLICY_SCOPE_WORKSPACE = "workspace"
POLICY_SCOPE_AGENT = "agent"
POLICY_SCOPE_SESSION_MODE = "session_mode"
POLICY_SCOPE_SESSION = "session"

POLICY_SCOPE_ORDER: tuple[str, ...] = (
    POLICY_SCOPE_WORKSPACE,
    POLICY_SCOPE_AGENT,
    POLICY_SCOPE_SESSION_MODE,
    POLICY_SCOPE_SESSION,
)
"""THE resolution order — workspace → per-agent → per-session-mode →
per-session override (Johnny-wks.9). :func:`resolve_policy` sorts incoming
layers by this order, so callers may pass rows in any order."""

DEFAULT_ALLOW_LAYER = "default"
"""Attribution label when no layer decided (the unrestricted default-allow)."""

ALLOW_LIST_RULE = "allow-list"
"""Attribution rule label for "not on the effective allow-list" denials."""

SAFE_BINS_REMOVED_RULE = "removed from safe-bins"
"""Attribution rule label for baseline bins the operator removed."""

POLICY_DENIED_SPOKEN_REASON = (
    "my operator's policy has this switched off for this session — ask them "
    "to allow it if it's needed."
)
"""Spoken-form reason for policy-denied kinds (the trt.55 actionable-decline
contract). The denying LAYER is observability vocabulary (the
``policy_denied`` event), never speech — participants get the fix, not the
config tree."""

SNAPSHOT_PAYLOAD_VERSION = 1


def _clean_patterns(raw: Any) -> tuple[str, ...]:
    """Coerce a document list into deduped, stripped, non-empty patterns."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class CapabilityPolicyLayer:
    """One scope layer's policy document (one ``capability_policies`` row).

    ``scope`` is a :data:`POLICY_SCOPE_ORDER` member; ``scope_detail`` is the
    human-readable target for attribution copy (the workspace name, the
    agent's name, the mode value, the session id) — it rides decisions and
    events, never prompts. ``safe_bins`` is meaningful on the workspace layer
    only (enforced by the API; :func:`resolve_policy` ignores it elsewhere
    with a warning).
    """

    scope: str
    scope_detail: str = ""
    tools_allow: tuple[str, ...] = ()
    tools_also_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    bins_deny: tuple[str, ...] = ()
    safe_bins: tuple[str, ...] | None = None

    @classmethod
    def from_document(
        cls, scope: str, document: Mapping[str, Any] | None, *, scope_detail: str = ""
    ) -> CapabilityPolicyLayer:
        """Lenient parse of a stored/wire policy document (unknown keys ignored)."""
        doc: Mapping[str, Any] = document if isinstance(document, Mapping) else {}
        safe_bins_raw = doc.get("safe_bins")
        return cls(
            scope=scope,
            scope_detail=scope_detail,
            tools_allow=_clean_patterns(doc.get("tools_allow")),
            tools_also_allow=_clean_patterns(doc.get("tools_also_allow")),
            tools_deny=_clean_patterns(doc.get("tools_deny")),
            bins_deny=_clean_patterns(doc.get("bins_deny")),
            safe_bins=(
                _clean_patterns(safe_bins_raw) if safe_bins_raw is not None else None
            ),
        )

    def to_document(self) -> dict[str, Any]:
        """The canonical JSON document shape (what the DB row stores)."""
        doc: dict[str, Any] = {
            "tools_allow": list(self.tools_allow),
            "tools_also_allow": list(self.tools_also_allow),
            "tools_deny": list(self.tools_deny),
            "bins_deny": list(self.bins_deny),
        }
        if self.safe_bins is not None:
            doc["safe_bins"] = list(self.safe_bins)
        return doc

    def is_empty(self) -> bool:
        return (
            not self.tools_allow
            and not self.tools_also_allow
            and not self.tools_deny
            and not self.bins_deny
            and self.safe_bins is None
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One allow/deny verdict with the deciding layer named.

    ``layer`` is the deciding scope (:data:`DEFAULT_ALLOW_LAYER` for the
    unrestricted default), ``rule`` the matching pattern (or
    :data:`ALLOW_LIST_RULE` / :data:`SAFE_BINS_REMOVED_RULE`), ``detail`` the
    deciding layer's ``scope_detail``. This is the shape the resolution API
    (Johnny-trt.37's inspector) and the ``policy_denied`` event carry.
    """

    allowed: bool
    layer: str
    rule: str = ""
    detail: str = ""


# One attributed pattern: (pattern, layer scope, layer scope_detail).
_AttributedRule = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class ResolvedCapabilityPolicy:
    """The four layers merged into one immutable, snapshot-ridable verdict engine.

    Built by :func:`resolve_policy`; serialized with :meth:`to_payload` into
    the trt.41 agent snapshot and rebuilt with :meth:`from_payload` at the
    consuming end (the layers are the wire shape — resolution is
    deterministic, so attribution survives the round-trip byte-for-byte).
    """

    layers: tuple[CapabilityPolicyLayer, ...] = ()
    deny_rules: tuple[_AttributedRule, ...] = ()
    allow_rules: tuple[_AttributedRule, ...] | None = None
    allow_layer: str = ""
    allow_detail: str = ""
    bins_deny_rules: tuple[_AttributedRule, ...] = ()
    safe_bins: tuple[str, ...] = BASELINE_BINS
    removed_baseline_bins: tuple[str, ...] = ()
    baseline: tuple[str, ...] = BASELINE_BINS

    @property
    def tools_unrestricted(self) -> bool:
        """True when no layer constrains TOOLS (catalog filtering is a no-op)."""
        return not self.deny_rules and self.allow_rules is None

    @property
    def is_unrestricted(self) -> bool:
        """True when the whole policy is the do-nothing default."""
        return (
            self.tools_unrestricted
            and not self.bins_deny_rules
            and not self.removed_baseline_bins
            and set(self.safe_bins) == set(self.baseline)
        )

    def check_tool(self, name: str) -> PolicyDecision:
        """Resolve one tool/kind name. Deny first (deny wins), then allow-list."""
        candidate = str(name).strip()
        for pattern, layer, detail in self.deny_rules:
            if fnmatchcase(candidate, pattern):
                return PolicyDecision(allowed=False, layer=layer, rule=pattern, detail=detail)
        if self.allow_rules is None:
            return PolicyDecision(allowed=True, layer=DEFAULT_ALLOW_LAYER, rule="")
        for pattern, layer, detail in self.allow_rules:
            if fnmatchcase(candidate, pattern):
                return PolicyDecision(allowed=True, layer=layer, rule=pattern, detail=detail)
        return PolicyDecision(
            allowed=False,
            layer=self.allow_layer,
            rule=ALLOW_LIST_RULE,
            detail=self.allow_detail,
        )

    def check_bin(self, name: str) -> PolicyDecision:
        """Does the POLICY object to this exec binary?

        ``allowed=True`` means "no policy objection" — membership in the
        final exec allow set (baseline + skill grants) stays
        :class:`johnny.skills.policy.ExecBinPolicy`'s job; this check is the
        policy filter :func:`~johnny.skills.policy.compute_allowed_bins`
        applies and the attribution source for denial copy/events.
        """
        candidate = basename(str(name).strip())
        for pattern, layer, detail in self.bins_deny_rules:
            if fnmatchcase(candidate, pattern):
                return PolicyDecision(allowed=False, layer=layer, rule=pattern, detail=detail)
        if candidate in self.removed_baseline_bins:
            return PolicyDecision(
                allowed=False,
                layer=POLICY_SCOPE_WORKSPACE,
                rule=SAFE_BINS_REMOVED_RULE,
                detail="",
            )
        return PolicyDecision(allowed=True, layer=DEFAULT_ALLOW_LAYER, rule="")

    # -- snapshot serialization (the trt.41 agent_snapshot channel) --------

    def to_payload(self) -> dict[str, Any]:
        """Plain-JSON shape for ``agent_snapshot["capability_policy"]``."""
        return {
            "version": SNAPSHOT_PAYLOAD_VERSION,
            "baseline": list(self.baseline),
            "layers": [
                {
                    "scope": layer.scope,
                    "scope_detail": layer.scope_detail,
                    "document": layer.to_document(),
                }
                for layer in self.layers
            ],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ResolvedCapabilityPolicy:
        """Rebuild from a snapshot payload — lenient by contract.

        A missing/malformed payload degrades to the UNRESTRICTED policy (the
        pre-trt.38 behavior: snapshots stamped before the policy engine keep
        working); a payload with some malformed layers keeps the valid ones
        (a deny in a healthy layer must survive a corrupt sibling).
        """
        if not isinstance(payload, Mapping):
            return resolve_policy(())
        raw_layers = payload.get("layers")
        baseline_raw = _clean_patterns(payload.get("baseline"))
        baseline = baseline_raw if baseline_raw else BASELINE_BINS
        layers: list[CapabilityPolicyLayer] = []
        if isinstance(raw_layers, Sequence):
            for raw in raw_layers:
                if not isinstance(raw, Mapping):
                    continue
                scope = str(raw.get("scope") or "").strip()
                if scope not in POLICY_SCOPE_ORDER:
                    logger.warning(
                        "capability policy: ignoring snapshot layer with unknown scope %r",
                        scope,
                    )
                    continue
                layers.append(
                    CapabilityPolicyLayer.from_document(
                        scope,
                        raw.get("document"),
                        scope_detail=str(raw.get("scope_detail") or ""),
                    )
                )
        return resolve_policy(layers, baseline=baseline)


def resolve_policy(
    layers: Iterable[CapabilityPolicyLayer],
    *,
    baseline: tuple[str, ...] = BASELINE_BINS,
) -> ResolvedCapabilityPolicy:
    """Merge scope layers into one :class:`ResolvedCapabilityPolicy`.

    Implements the documented order (module docstring): layers are sorted by
    :data:`POLICY_SCOPE_ORDER` (stable — unknown scopes are dropped with a
    warning), denies accumulate (earliest layer wins attribution),
    ``tools_allow`` redefines the allow-list, ``tools_also_allow`` extends
    the one in force, and the workspace layer's ``safe_bins`` (when set)
    replaces ``baseline`` with removals hard-denied.
    """
    known = [layer for layer in layers if layer.scope in POLICY_SCOPE_ORDER]
    dropped = [layer.scope for layer in layers if layer.scope not in POLICY_SCOPE_ORDER]
    if dropped:
        logger.warning("capability policy: dropping layers with unknown scopes %s", dropped)
    ordered = sorted(known, key=lambda layer: POLICY_SCOPE_ORDER.index(layer.scope))

    deny_rules: list[_AttributedRule] = []
    bins_deny_rules: list[_AttributedRule] = []
    allow_rules: list[_AttributedRule] | None = None
    allow_layer = ""
    allow_detail = ""
    safe_bins: tuple[str, ...] | None = None

    for layer in ordered:
        deny_rules.extend((pat, layer.scope, layer.scope_detail) for pat in layer.tools_deny)
        bins_deny_rules.extend(
            (pat, layer.scope, layer.scope_detail) for pat in layer.bins_deny
        )
        if layer.tools_allow:
            allow_rules = [
                (pat, layer.scope, layer.scope_detail) for pat in layer.tools_allow
            ]
            allow_layer = layer.scope
            allow_detail = layer.scope_detail
        if layer.tools_also_allow and allow_rules is not None:
            allow_rules.extend(
                (pat, layer.scope, layer.scope_detail) for pat in layer.tools_also_allow
            )
        if layer.safe_bins is not None:
            if layer.scope == POLICY_SCOPE_WORKSPACE:
                safe_bins = layer.safe_bins
            else:
                logger.warning(
                    "capability policy: safe_bins on the %r layer is ignored — "
                    "the edited baseline is workspace-only by design",
                    layer.scope,
                )

    effective_safe_bins = safe_bins if safe_bins is not None else tuple(baseline)
    effective_set = set(effective_safe_bins)
    removed = tuple(b for b in baseline if b not in effective_set)

    return ResolvedCapabilityPolicy(
        layers=tuple(ordered),
        deny_rules=tuple(deny_rules),
        allow_rules=tuple(allow_rules) if allow_rules is not None else None,
        allow_layer=allow_layer,
        allow_detail=allow_detail,
        bins_deny_rules=tuple(bins_deny_rules),
        safe_bins=effective_safe_bins,
        removed_baseline_bins=removed,
        baseline=tuple(baseline),
    )


UNRESTRICTED_POLICY = resolve_policy(())
"""The no-rows default: everything allowed, the built-in trt.35 baseline."""


def apply_policy_to_catalog(
    entries: tuple[TaskCatalogEntry, ...], policy: ResolvedCapabilityPolicy
) -> tuple[TaskCatalogEntry, ...]:
    """Project the resolved policy onto a session's task catalog (enforcement #1).

    A policy-denied kind becomes ``hidden=True, available=False`` with the
    spoken-form policy reason and ``keywords=()`` (the trt.50 scorer-feed
    exclusion): the renderers skip hidden entries entirely — the canonical
    scenario's "must not even mention" guarantee — while the entry stays in
    the tuple so the gate's trt.55 unavailable backstop degrades a forced
    delegate to the spoken decline and the ``policy_denied`` event can name
    the denying layer (``policy_layer`` / ``policy_rule`` ride the entry).
    An unrestricted policy returns the input unchanged (replay parity: the
    no-policy prompt stays byte-identical).
    """
    if policy.tools_unrestricted:
        return tuple(entries)
    out: list[TaskCatalogEntry] = []
    for entry in entries:
        decision = policy.check_tool(entry.kind)
        if decision.allowed:
            out.append(entry)
            continue
        logger.info(
            "capability policy: kind=%r hidden from the catalog (denied at the "
            "%s layer, rule %r)",
            entry.kind,
            decision.layer,
            decision.rule or ALLOW_LIST_RULE,
        )
        out.append(
            replace(
                entry,
                available=False,
                hidden=True,
                keywords=(),
                unavailable_reason=POLICY_DENIED_SPOKEN_REASON,
                policy_layer=decision.layer,
                policy_rule=decision.rule,
            )
        )
    return tuple(out)


__all__ = [
    "ALLOW_LIST_RULE",
    "DEFAULT_ALLOW_LAYER",
    "POLICY_DENIED_SPOKEN_REASON",
    "POLICY_SCOPE_AGENT",
    "POLICY_SCOPE_ORDER",
    "POLICY_SCOPE_SESSION",
    "POLICY_SCOPE_SESSION_MODE",
    "POLICY_SCOPE_WORKSPACE",
    "SAFE_BINS_REMOVED_RULE",
    "UNRESTRICTED_POLICY",
    "CapabilityPolicyLayer",
    "PolicyDecision",
    "ResolvedCapabilityPolicy",
    "apply_policy_to_catalog",
    "resolve_policy",
]
