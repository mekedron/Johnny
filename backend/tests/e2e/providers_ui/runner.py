"""API-level runner that exercises every provider plan end-to-end.

This is the path pytest takes when ``-m e2e_ui`` is selected. It mirrors
exactly what the UI does — POST /providers → POST /providers/{id}/test →
POST /providers/{id}/activate → DELETE /providers/{id} — but bypasses the
browser so a CI runner without chrome-devtools-mcp can still gate on
the provider contract.

The chrome-devtools agent flow consumes the same plans and the same
report shape so an agent-driven run produces a directly comparable
artifact (just with screenshots populated).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from app.providers.base import ProviderKind
from tests.e2e.providers_ui.api import (
    JohnnyAPI,
    delete_all_providers,
    fetch_active_for_kind,
    fetch_provider_list,
)
from tests.e2e.providers_ui.plans import (
    PROVIDER_PLANS,
    PlanOutcome,
    ProviderPlan,
    plans_by_kind,
)
from tests.e2e.providers_ui.preflight import preflight_plan
from tests.e2e.providers_ui.report import ProviderReport, Report, now_iso

logger = logging.getLogger(__name__)


def _provider_create_payload(plan: ProviderPlan) -> dict[str, Any]:
    """Build the ``POST /providers`` JSON body for a plan."""
    return {
        "kind": plan.kind.value,
        "provider_name": plan.provider_name,
        "display_name": plan.display_name,
        "credentials": plan.resolved_credentials(),
        "options": plan.resolved_options(),
    }


def _row_matching_plan(
    grouped: dict[str, list[dict[str, Any]]],
    plan: ProviderPlan,
) -> dict[str, Any] | None:
    """Find a row in ``grouped`` matching the plan by display_name."""
    for row in grouped.get(plan.kind.value, []):
        if row.get("display_name") == plan.display_name:
            return row
    return None


def _step_create(api: JohnnyAPI, plan: ProviderPlan, report: ProviderReport) -> int | None:
    """Create the provider row and assert the API echoes it back."""
    payload = _provider_create_payload(plan)
    created = api.create_provider(payload)
    report.add_step(
        "POST /providers",
        ok=created.get("kind") == plan.kind.value
        and created.get("provider_name") == plan.provider_name
        and created.get("display_name") == plan.display_name,
        detail=f"id={created.get('id')}",
    )
    provider_id = int(created["id"])
    report.provider_id = provider_id

    grouped = fetch_provider_list(api)
    found = _row_matching_plan(grouped, plan)
    report.add_step(
        "GET /providers (row visible)",
        ok=found is not None and int(found.get("id", -1)) == provider_id,
        detail=(
            f"display_name={plan.display_name} found"
            if found
            else "row not visible after create"
        ),
    )
    return provider_id


def _step_test(api: JohnnyAPI, provider_id: int, report: ProviderReport) -> bool:
    """Run the provider smoke test and surface ok/detail in the report."""
    result = api.test_provider(provider_id)
    ok = bool(result.get("ok"))
    detail = result.get("message") or ""
    if result.get("detail"):
        detail = f"{detail} — {result['detail']}"
    report.add_step("POST /providers/{id}/test", ok=ok, detail=detail)
    return ok


def _step_activate(
    api: JohnnyAPI, plan: ProviderPlan, provider_id: int, report: ProviderReport
) -> bool:
    """Activate the provider and assert at most one row is active per kind."""
    api.activate_provider(provider_id)
    active = fetch_active_for_kind(plan.kind.value, api)
    ok_active = active is not None and int(active.get("id", -1)) == provider_id
    report.add_step(
        "POST /providers/{id}/activate",
        ok=ok_active,
        detail=f"active.id={active.get('id') if active else None}",
    )

    grouped = fetch_provider_list(api)
    active_count = sum(1 for r in grouped[plan.kind.value] if r.get("is_active"))
    report.add_step(
        "exactly one active per kind",
        ok=active_count == 1,
        detail=f"active_count={active_count}",
    )
    return ok_active and active_count == 1


def _step_delete(
    api: JohnnyAPI, plan: ProviderPlan, provider_id: int, report: ProviderReport
) -> bool:
    """Delete the row and confirm it disappeared from the listing."""
    api.delete_provider(provider_id)
    grouped = fetch_provider_list(api)
    found = _row_matching_plan(grouped, plan)
    ok = found is None
    if ok:
        detail = "row removed"
    else:
        assert found is not None  # narrowed by ``ok``
        detail = f"row still visible id={found.get('id')}"
    report.add_step("DELETE /providers/{id}", ok=ok, detail=detail)
    return ok


def _run_single_plan(api: JohnnyAPI, plan: ProviderPlan) -> ProviderReport:
    """Run preflight → create → test → activate → delete for one plan."""
    report = ProviderReport(
        plan_id=plan.plan_id,
        kind=plan.kind.value,
        provider_name=plan.provider_name,
        display_name=plan.display_name,
        outcome=PlanOutcome.PASS,
    )

    pre = preflight_plan(plan)
    if not pre.runnable:
        report.outcome = PlanOutcome.SKIP
        report.reason = pre.skip_reason
        return report

    try:
        provider_id = _step_create(api, plan, report)
        if provider_id is None:
            return report
        _step_test(api, provider_id, report)
        _step_activate(api, plan, provider_id, report)
        _step_delete(api, plan, provider_id, report)
    except Exception as exc:  # noqa: BLE001 — surface any harness error to report
        report.add_step(
            "harness error",
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return report


def _run_kind_switch_check(
    api: JohnnyAPI, plans: Iterable[ProviderPlan], report: Report
) -> None:
    """Verify activating a sibling deactivates the previous active.

    Only meaningful when two or more runnable cloud plans exist for the
    same kind. Skipped silently otherwise so the absence of an LLM key
    doesn't fabricate a FAIL.
    """
    runnable = [p for p in plans if preflight_plan(p).runnable]
    if len(runnable) < 2:
        return
    plan_a, plan_b = runnable[0], runnable[1]
    switch_report = ProviderReport(
        plan_id=f"{plan_a.kind.value}-switch",
        kind=plan_a.kind.value,
        provider_name=f"{plan_a.provider_name}<->{plan_b.provider_name}",
        display_name=f"e2e-{plan_a.kind.value}-switch",
        outcome=PlanOutcome.PASS,
    )
    try:
        a = api.create_provider(_provider_create_payload(plan_a))
        b = api.create_provider(_provider_create_payload(plan_b))
        api.activate_provider(int(a["id"]))
        active_after_a = fetch_active_for_kind(plan_a.kind.value, api)
        switch_report.add_step(
            "activate A",
            ok=active_after_a is not None
            and int(active_after_a.get("id", -1)) == int(a["id"]),
            detail=f"active={active_after_a.get('id') if active_after_a else None}",
        )
        api.activate_provider(int(b["id"]))
        active_after_b = fetch_active_for_kind(plan_a.kind.value, api)
        switch_report.add_step(
            "activate B (switch)",
            ok=active_after_b is not None
            and int(active_after_b.get("id", -1)) == int(b["id"]),
            detail=f"active={active_after_b.get('id') if active_after_b else None}",
        )
        api.delete_provider(int(a["id"]))
        api.delete_provider(int(b["id"]))
    except Exception as exc:  # noqa: BLE001
        switch_report.add_step(
            "harness error", ok=False, detail=f"{type(exc).__name__}: {exc}"
        )
    report.add_provider(switch_report)


def _run_negative_checks(api: JohnnyAPI, report: Report) -> None:
    """Smoke-test the edge cases: invalid key + duplicate display_name."""
    edge_report = ProviderReport(
        plan_id="edge-invalid-key",
        kind=ProviderKind.LLM.value,
        provider_name="anthropic",
        display_name="e2e-edge-invalid-key",
        outcome=PlanOutcome.PASS,
    )
    bad_payload = {
        "kind": ProviderKind.LLM.value,
        "provider_name": "anthropic",
        "display_name": edge_report.display_name,
        "credentials": {"api_key": "sk-ant-invalid-deadbeef"},
        "options": {"model": "claude-haiku-4-5"},
    }
    try:
        created = api.create_provider(bad_payload)
        provider_id = int(created["id"])
        edge_report.provider_id = provider_id
        edge_report.add_step("POST /providers (bad key)", ok=True, detail=f"id={provider_id}")
        result = api.test_provider(provider_id)
        # The smoke test should mark this as not-ok. If it returns ok,
        # that's the actual regression we want to catch.
        edge_report.add_step(
            "test rejects invalid key",
            ok=not result.get("ok", True),
            detail=str(result.get("detail") or result.get("message") or ""),
        )
        api.delete_provider(provider_id)
        edge_report.add_step("cleanup invalid-key row", ok=True)

        dup_payload = {
            "kind": ProviderKind.LLM.value,
            "provider_name": "anthropic",
            "display_name": "e2e-edge-duplicate",
            "credentials": {"api_key": "sk-ant-placeholder"},
            "options": {"model": "claude-haiku-4-5"},
        }
        first = api.create_provider(dup_payload)
        first_id = int(first["id"])
        dup_rejected = False
        dup_detail = ""
        try:
            api.create_provider(dup_payload)
        except Exception as exc:  # noqa: BLE001 — expected 409
            dup_rejected = True
            dup_detail = f"{type(exc).__name__}"
        edge_report.add_step(
            "duplicate display_name rejected",
            ok=dup_rejected,
            detail=dup_detail or "second create did not raise",
        )
        api.delete_provider(first_id)
    except Exception as exc:  # noqa: BLE001
        edge_report.add_step(
            "harness error", ok=False, detail=f"{type(exc).__name__}: {exc}"
        )
    report.add_provider(edge_report)


def run_harness(
    api: JohnnyAPI | None = None,
    *,
    force_reset: bool = False,
    plans: Iterable[ProviderPlan] = PROVIDER_PLANS,
) -> Report:
    """Run every plan against the live stack and return the report.

    ``force_reset`` nukes every existing provider row first. By default
    the harness only deletes rows it created (``e2e-`` prefix) so a
    misconfigured run cannot wipe operator-curated providers.
    """
    client = api or JohnnyAPI()
    plan_list = list(plans)

    if force_reset:
        deleted = delete_all_providers(client, only_prefix=None)
        logger.info("force-reset removed %d existing providers", deleted)
    else:
        delete_all_providers(client, only_prefix="e2e-")

    report = Report(started_at=now_iso())

    for plan in plan_list:
        result = _run_single_plan(client, plan)
        report.add_provider(result)

    # Cross-plan: switch + edge cases.
    for kind in (ProviderKind.STT, ProviderKind.LLM, ProviderKind.TTS):
        _run_kind_switch_check(client, plans_by_kind(kind), report)
    _run_negative_checks(client, report)

    # Final cleanup pass — anything left over (e.g. from a failed plan
    # that didn't reach DELETE) gets removed so the next run starts clean.
    delete_all_providers(client, only_prefix="e2e-")

    report.finished_at = now_iso()
    return report


__all__ = ["run_harness"]
