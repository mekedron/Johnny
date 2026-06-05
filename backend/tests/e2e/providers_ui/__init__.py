"""E2E test harness for the provider settings UI (Johnny-upg).

Drives the SvelteKit ``/providers`` page via chrome-devtools-mcp for an
agent-led run and exercises the same flows through the public HTTP API
for ``pytest -m e2e_ui``. Both paths share the declarative provider
plans in :mod:`tests.e2e.providers_ui.plans` and emit the same JSON
report shape under ``tests/e2e/artifacts/<timestamp>/``.

The harness is idempotent: every run starts by resetting the
``provider_credentials`` table via the API and finishes by deleting the
rows it created, so re-runs against the same stack are safe.
"""

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
from tests.e2e.providers_ui.report import (
    ProviderReport,
    Report,
    write_report,
)

__all__ = [
    "JohnnyAPI",
    "PROVIDER_PLANS",
    "PlanOutcome",
    "ProviderPlan",
    "ProviderReport",
    "Report",
    "delete_all_providers",
    "fetch_active_for_kind",
    "fetch_provider_list",
    "plans_by_kind",
    "write_report",
]
