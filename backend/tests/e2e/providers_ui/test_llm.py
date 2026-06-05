"""LLM provider matrix — exercises the public API path the UI calls."""

from __future__ import annotations

import pytest

from app.providers.base import ProviderKind
from tests.e2e.providers_ui.api import JohnnyAPI, delete_all_providers
from tests.e2e.providers_ui.plans import ProviderPlan, plans_by_kind
from tests.e2e.providers_ui.preflight import preflight_plan
from tests.e2e.providers_ui.runner import _run_single_plan

pytestmark = pytest.mark.e2e_ui


@pytest.mark.parametrize(
    "plan",
    plans_by_kind(ProviderKind.LLM),
    ids=lambda p: p.plan_id,
)
def test_llm_provider_lifecycle(plan: ProviderPlan, johnny_api: JohnnyAPI) -> None:
    """For each LLM backend with a key, walk the full lifecycle."""
    pre = preflight_plan(plan)
    if not pre.runnable:
        pytest.skip(pre.skip_reason)
    delete_all_providers(johnny_api, only_prefix="e2e-")
    report = _run_single_plan(johnny_api, plan)
    delete_all_providers(johnny_api, only_prefix="e2e-")
    assert report.outcome.value == "PASS", (
        f"{plan.plan_id} did not PASS: {report.reason} | "
        f"steps={[s.name for s in report.steps if not s.ok]}"
    )
