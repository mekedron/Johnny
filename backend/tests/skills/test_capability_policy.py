"""Unit pins for the layered capability-policy engine (Johnny-trt.38).

THE resolution-order matrix lives here: GLOBAL → PER-AGENT →
PER-SESSION-MODE → PER-SESSION OVERRIDE with deny winning at every merge —
the acceptance examples verbatim (global-deny beats mode-allow, mode-deny
beats session-allow), allow-list redefinition + alsoAllow extension, glob
matching (``mcp__<server>__*``), layer attribution, the editable safe-bins
baseline (removal blocks, reset restores trt.35), the snapshot payload
round-trip, the catalog transform (policy-hidden kinds render NOWHERE), and
the exec-bin integration (:func:`compute_allowed_bins` + policy-attributed
:class:`ExecDenial`).
"""

from __future__ import annotations

from johnny.agent.task_catalog import (
    TaskCatalogEntry,
    render_capability_notes,
    render_task_catalog,
)
from johnny.skills.capability_policy import (
    ALLOW_LIST_RULE,
    DEFAULT_ALLOW_LAYER,
    POLICY_DENIED_SPOKEN_REASON,
    POLICY_SCOPE_AGENT,
    POLICY_SCOPE_GLOBAL,
    POLICY_SCOPE_SESSION,
    POLICY_SCOPE_SESSION_MODE,
    SAFE_BINS_REMOVED_RULE,
    UNRESTRICTED_POLICY,
    CapabilityPolicyLayer,
    ResolvedCapabilityPolicy,
    apply_policy_to_catalog,
    resolve_policy,
)
from johnny.skills.frontmatter import SkillRequirements
from johnny.skills.policy import (
    BASELINE_BINS,
    SHELL_UTILITY_BINS,
    ExecBinPolicy,
    compute_allowed_bins,
)


def _layer(scope: str, document: dict, detail: str = "") -> CapabilityPolicyLayer:
    return CapabilityPolicyLayer.from_document(scope, document, scope_detail=detail)


# --- the deny-wins merge matrix (the acceptance pins) -----------------------


def test_unrestricted_default_allows_everything() -> None:
    policy = resolve_policy(())
    decision = policy.check_tool("anything.at_all")
    assert decision.allowed
    assert decision.layer == DEFAULT_ALLOW_LAYER
    assert policy.is_unrestricted
    assert policy.tools_unrestricted


def test_global_deny_beats_mode_allow() -> None:
    """The acceptance example: a global deny survives a mode-layer allow."""
    policy = resolve_policy(
        [
            _layer(POLICY_SCOPE_GLOBAL, {"tools_deny": ["gmail.search"]}),
            _layer(POLICY_SCOPE_SESSION_MODE, {"tools_allow": ["gmail.search"]}, "browser"),
        ]
    )
    decision = policy.check_tool("gmail.search")
    assert not decision.allowed
    assert decision.layer == POLICY_SCOPE_GLOBAL
    assert decision.rule == "gmail.search"


def test_mode_deny_beats_session_allow() -> None:
    """The acceptance example: a mode deny survives a session-override allow."""
    policy = resolve_policy(
        [
            _layer(POLICY_SCOPE_SESSION_MODE, {"tools_deny": ["financial-reports"]}, "meet"),
            _layer(POLICY_SCOPE_SESSION, {"tools_also_allow": ["financial-reports"]}),
            _layer(POLICY_SCOPE_SESSION, {"tools_allow": ["*"]}),
        ]
    )
    decision = policy.check_tool("financial-reports")
    assert not decision.allowed
    assert decision.layer == POLICY_SCOPE_SESSION_MODE
    assert decision.detail == "meet"


def test_agent_deny_beats_session_layers() -> None:
    policy = resolve_policy(
        [
            _layer(POLICY_SCOPE_AGENT, {"tools_deny": ["mcp__shady__*"]}, "Progress Bot"),
            _layer(POLICY_SCOPE_SESSION, {"tools_allow": ["mcp__shady__exfiltrate"]}),
        ]
    )
    decision = policy.check_tool("mcp__shady__exfiltrate")
    assert not decision.allowed
    assert decision.layer == POLICY_SCOPE_AGENT
    assert decision.rule == "mcp__shady__*"
    assert decision.detail == "Progress Bot"


def test_earliest_layer_wins_deny_attribution() -> None:
    policy = resolve_policy(
        [
            _layer(POLICY_SCOPE_GLOBAL, {"tools_deny": ["gmail.*"]}),
            _layer(POLICY_SCOPE_AGENT, {"tools_deny": ["gmail.search"]}, "A"),
        ]
    )
    decision = policy.check_tool("gmail.search")
    assert not decision.allowed
    assert decision.layer == POLICY_SCOPE_GLOBAL


def test_layers_resolve_in_normative_order_regardless_of_input_order() -> None:
    """Rows may arrive in any order; resolution sorts by POLICY_SCOPE_ORDER."""
    shuffled = [
        _layer(POLICY_SCOPE_SESSION, {"tools_also_allow": ["extra.kind"]}),
        _layer(POLICY_SCOPE_GLOBAL, {"tools_deny": ["denied.kind"]}),
        _layer(POLICY_SCOPE_AGENT, {"tools_allow": ["calendar*"]}, "A"),
    ]
    policy = resolve_policy(shuffled)
    assert policy.check_tool("calendar.upcoming_events").allowed
    assert policy.check_tool("extra.kind").allowed  # alsoAllow extends the agent list
    assert not policy.check_tool("denied.kind").allowed
    assert not policy.check_tool("unrelated.kind").allowed  # outside the allow-list


# --- the canonical least-privilege scenario (operator anchor) ---------------


def test_canonical_scenario_progress_agent_allows_only_calendar_and_tasks() -> None:
    """The 2026-06-12 operator anchor: the progress agent's allow-list denies
    the finance skill AT THE AGENT LAYER; the management agent (no agent
    layer) keeps it."""
    progress = resolve_policy(
        [_layer(POLICY_SCOPE_AGENT, {"tools_allow": ["google-calendar", "tasks.*"]}, "Progress")]
    )
    management = resolve_policy(())

    assert progress.check_tool("google-calendar").allowed
    assert progress.check_tool("tasks.list").allowed
    finance = progress.check_tool("financial-reports")
    assert not finance.allowed
    assert finance.layer == POLICY_SCOPE_AGENT
    assert finance.rule == ALLOW_LIST_RULE
    assert finance.detail == "Progress"

    assert management.check_tool("financial-reports").allowed


# --- allow-list semantics ----------------------------------------------------


def test_later_allow_redefines_the_list_openclaw_scope_override() -> None:
    policy = resolve_policy(
        [
            _layer(POLICY_SCOPE_GLOBAL, {"tools_allow": ["calendar*"]}),
            _layer(POLICY_SCOPE_AGENT, {"tools_allow": ["gmail.*"]}, "A"),
        ]
    )
    # The agent layer REDEFINED the allow-list — calendar is no longer on it.
    assert not policy.check_tool("calendar.upcoming_events").allowed
    allowed = policy.check_tool("gmail.search")
    assert allowed.allowed and allowed.layer == POLICY_SCOPE_AGENT


def test_also_allow_extends_without_replacing() -> None:
    policy = resolve_policy(
        [
            _layer(POLICY_SCOPE_AGENT, {"tools_allow": ["calendar*"]}, "A"),
            _layer(POLICY_SCOPE_SESSION, {"tools_also_allow": ["session.end"]}),
        ]
    )
    assert policy.check_tool("calendar.upcoming_events").allowed
    extended = policy.check_tool("session.end")
    assert extended.allowed
    assert extended.layer == POLICY_SCOPE_SESSION  # the extending layer is named
    assert not policy.check_tool("gmail.search").allowed


def test_also_allow_without_an_allow_list_is_a_noop() -> None:
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_SESSION, {"tools_also_allow": ["whatever"]})]
    )
    assert policy.tools_unrestricted
    assert policy.check_tool("anything").allowed


def test_allow_list_denial_names_the_defining_layer() -> None:
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_SESSION_MODE, {"tools_allow": ["calendar*"]}, "meet")]
    )
    decision = policy.check_tool("gmail.search")
    assert not decision.allowed
    assert decision.layer == POLICY_SCOPE_SESSION_MODE
    assert decision.rule == ALLOW_LIST_RULE
    assert decision.detail == "meet"


def test_glob_matching_is_fnmatch_style() -> None:
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_GLOBAL, {"tools_deny": ["mcp__server__*", "task?"]})]
    )
    assert not policy.check_tool("mcp__server__send").allowed
    assert policy.check_tool("mcp__other__send").allowed
    assert not policy.check_tool("task1").allowed
    assert policy.check_tool("task12").allowed


# --- safe-bins (the editable trt.35 baseline) -------------------------------


def test_default_safe_bins_is_the_trt35_baseline() -> None:
    assert UNRESTRICTED_POLICY.safe_bins == BASELINE_BINS
    assert UNRESTRICTED_POLICY.removed_baseline_bins == ()


def test_removing_a_baseline_bin_denies_it_even_against_skill_grants() -> None:
    edited = tuple(b for b in BASELINE_BINS if b != "curl")
    policy = resolve_policy([_layer(POLICY_SCOPE_GLOBAL, {"safe_bins": list(edited)})])
    decision = policy.check_bin("curl")
    assert not decision.allowed
    assert decision.layer == POLICY_SCOPE_GLOBAL
    assert decision.rule == SAFE_BINS_REMOVED_RULE

    # A skill declaring requires.bins: [curl] cannot resurrect it — the
    # trt.38 acceptance: removal actually blocks it in sandbox.exec.
    grants = (SkillRequirements(bins=("curl",)),)
    allowed = compute_allowed_bins(grants, policy=policy)
    assert "curl" not in allowed
    assert "git" in allowed  # untouched baseline members survive


def test_adding_a_bin_to_safe_bins_grants_it() -> None:
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_GLOBAL, {"safe_bins": [*BASELINE_BINS, "node"]})]
    )
    assert policy.check_bin("node").allowed
    assert "node" in compute_allowed_bins(policy=policy)


def test_reset_to_default_restores_the_baseline() -> None:
    """Deleting the edit (safe_bins back to None) restores trt.35 exactly."""
    edited = resolve_policy(
        [_layer(POLICY_SCOPE_GLOBAL, {"safe_bins": ["bash", "cat"]})]
    )
    assert set(edited.removed_baseline_bins) == set(BASELINE_BINS) - {"bash", "cat"}
    reset = resolve_policy([_layer(POLICY_SCOPE_GLOBAL, {})])
    assert reset.safe_bins == BASELINE_BINS
    assert reset.removed_baseline_bins == ()
    assert compute_allowed_bins(policy=reset) == compute_allowed_bins()


def test_bins_deny_accumulates_with_layer_attribution() -> None:
    policy = resolve_policy(
        [
            _layer(POLICY_SCOPE_GLOBAL, {"bins_deny": ["nmap*"]}),
            _layer(POLICY_SCOPE_AGENT, {"bins_deny": ["git"]}, "Strict Bot"),
        ]
    )
    global_denied = policy.check_bin("nmap")
    assert not global_denied.allowed and global_denied.layer == POLICY_SCOPE_GLOBAL
    agent_denied = policy.check_bin("git")
    assert not agent_denied.allowed and agent_denied.layer == POLICY_SCOPE_AGENT
    assert agent_denied.detail == "Strict Bot"
    # bins_deny filters even shell utilities and skill grants out of the set.
    allowed = compute_allowed_bins((SkillRequirements(bins=("nmap",)),), policy=policy)
    assert "nmap" not in allowed and "git" not in allowed


def test_safe_bins_on_a_non_global_layer_is_ignored() -> None:
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_AGENT, {"safe_bins": ["bash"]}, "A")]
    )
    assert policy.safe_bins == BASELINE_BINS  # the agent-layer edit is dropped


def test_denied_skills_grants_are_excluded_by_the_caller_filter() -> None:
    """The worker's pre-filter: a policy-denied skill's requires.bins never
    enter the union (the executor_for composition)."""
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_GLOBAL, {"tools_deny": ["shady-skill"]})]
    )
    skills = {
        "shady-skill": SkillRequirements(bins=("exfiltool",)),
        "good-skill": SkillRequirements(bins=("goodtool",)),
    }
    granted = tuple(
        requires
        for name, requires in skills.items()
        if policy.check_tool(name).allowed
    )
    allowed = compute_allowed_bins(granted, policy=policy)
    assert "goodtool" in allowed
    assert "exfiltool" not in allowed


# --- snapshot payload round-trip ---------------------------------------------


def test_payload_round_trip_preserves_decisions_and_attribution() -> None:
    policy = resolve_policy(
        [
            _layer(POLICY_SCOPE_GLOBAL, {"tools_deny": ["mcp__shady__*"], "bins_deny": ["nc"]}),
            _layer(POLICY_SCOPE_AGENT, {"tools_allow": ["calendar*"]}, "Progress"),
            _layer(POLICY_SCOPE_SESSION_MODE, {"tools_also_allow": ["session.end"]}, "meet"),
        ]
    )
    rebuilt = ResolvedCapabilityPolicy.from_payload(policy.to_payload())
    for name in ("calendar.upcoming_events", "mcp__shady__x", "gmail.search", "session.end"):
        assert rebuilt.check_tool(name) == policy.check_tool(name)
    for name in ("nc", "curl"):
        assert rebuilt.check_bin(name) == policy.check_bin(name)
    assert rebuilt.safe_bins == policy.safe_bins


def test_missing_or_malformed_payload_degrades_to_unrestricted() -> None:
    for payload in (None, "junk", 42, [], {"layers": "nope"}):
        rebuilt = ResolvedCapabilityPolicy.from_payload(payload)
        assert rebuilt.tools_unrestricted
        assert rebuilt.check_tool("anything").allowed


def test_unknown_scope_layers_are_dropped_but_valid_layers_survive() -> None:
    payload = {
        "version": 1,
        "layers": [
            {"scope": "galaxy", "document": {"tools_deny": ["*"]}},
            {"scope": "global", "document": {"tools_deny": ["gmail.*"]}},
        ],
    }
    rebuilt = ResolvedCapabilityPolicy.from_payload(payload)
    assert rebuilt.check_tool("anything").allowed  # the galaxy deny-all is dropped
    assert not rebuilt.check_tool("gmail.search").allowed  # the global deny holds


# --- catalog transform (enforcement point #1) --------------------------------


_ENTRIES = (
    TaskCatalogEntry(kind="financial-reports", one_liner="Run the finance pack."),
    TaskCatalogEntry(
        kind="google-calendar",
        one_liner="Look up calendar events.",
        keywords=("calendar",),
    ),
    TaskCatalogEntry(
        kind="broken-skill",
        one_liner="A capability gap.",
        available=False,
        unavailable_reason="it isn't connected right now.",
    ),
)


def test_policy_denied_entries_become_hidden_with_attribution() -> None:
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_AGENT, {"tools_deny": ["financial-reports"]}, "Progress")]
    )
    transformed = apply_policy_to_catalog(_ENTRIES, policy)
    by_kind = {entry.kind: entry for entry in transformed}
    finance = by_kind["financial-reports"]
    assert finance.hidden and not finance.available
    assert finance.keywords == ()  # the trt.50 scorer-feed exclusion
    assert finance.unavailable_reason == POLICY_DENIED_SPOKEN_REASON
    assert finance.policy_layer == POLICY_SCOPE_AGENT
    assert finance.policy_rule == "financial-reports"
    # Allowed entries pass through identical; trt.55 gaps stay visible gaps.
    assert by_kind["google-calendar"] == _ENTRIES[1]
    assert by_kind["broken-skill"] == _ENTRIES[2]


def test_unrestricted_policy_returns_catalog_unchanged() -> None:
    assert apply_policy_to_catalog(_ENTRIES, UNRESTRICTED_POLICY) == _ENTRIES


def test_hidden_entries_render_nowhere_canonical_scenario() -> None:
    """The operator anchor: the rendered catalog must not even MENTION a
    policy-denied kind — in either prompt block, router or answer side."""
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_AGENT, {"tools_allow": ["google-calendar"]}, "Progress")]
    )
    transformed = apply_policy_to_catalog(_ENTRIES, policy)
    router_block = render_task_catalog(transformed)
    answer_block = render_capability_notes(transformed)
    assert "financial-reports" not in router_block
    assert "financial-reports" not in answer_block
    assert "broken-skill" not in router_block  # hidden too (outside the allow-list)
    assert "google-calendar" in router_block
    # The hidden entries are still IN the tuple for the gate's backstop.
    assert any(entry.hidden and entry.kind == "financial-reports" for entry in transformed)


def test_all_hidden_catalog_renders_empty() -> None:
    policy = resolve_policy([_layer(POLICY_SCOPE_GLOBAL, {"tools_deny": ["*"]})])
    transformed = apply_policy_to_catalog(_ENTRIES, policy)
    assert render_task_catalog(transformed) == ""
    assert render_capability_notes(transformed) == ""


# --- exec-bin integration (enforcement point #3) ------------------------------


def test_exec_bin_policy_attributes_policy_denials() -> None:
    edited = tuple(b for b in BASELINE_BINS if b != "curl")
    policy = resolve_policy(
        [_layer(POLICY_SCOPE_GLOBAL, {"safe_bins": list(edited), "bins_deny": ["nc*"]})]
    )
    bin_policy = ExecBinPolicy(
        allowed=compute_allowed_bins(policy=policy), policy_check=policy.check_bin
    )

    removed = bin_policy.check_argv_detailed(["curl", "https://x"])
    assert removed is not None
    assert removed.policy_layer == POLICY_SCOPE_GLOBAL
    assert removed.policy_rule == SAFE_BINS_REMOVED_RULE
    assert "capability policy" in removed.message

    denied = bin_policy.check_detailed(argv=["nc", "-l"])
    assert denied is not None and denied.policy_rule == "nc*"

    # A bin the policy does not object to keeps the v1 grant-model copy.
    plain = bin_policy.check_argv_detailed(["doesnotexist"])
    assert plain is not None
    assert plain.policy_layer == ""
    assert "requires.bins" in plain.message

    # Allowed bins still pass; the string-form contract is unchanged.
    assert bin_policy.check(argv=["git", "status"]) is None
    assert bin_policy.check_argv(["curl"]) == removed.message


def test_compute_allowed_bins_without_policy_is_byte_identical_to_v1() -> None:
    grants = (SkillRequirements(bins=("gh",), any_bins=("fd", "find")),)
    assert compute_allowed_bins(grants) == frozenset(
        {*BASELINE_BINS, *SHELL_UTILITY_BINS, "gh", "fd", "find"}
    )
