"""Procedural recipe for driving the provider UI via chrome-devtools-mcp.

The Python runner exercises the public ``/providers`` HTTP API; it can
neither click buttons nor take screenshots. This module is the
specification that an agent (or any tool that wraps chrome-devtools-mcp)
follows to drive the SvelteKit page and produce the screenshot half of
the artifact directory.

The functions here intentionally return **action descriptors** instead
of executing tool calls — the consumer (an agent, a wrapper script) is
responsible for actually invoking ``mcp__chrome-devtools__*``. Treating
the steps as data keeps the driver testable from regular pytest while
remaining the source of truth for the agent procedure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.e2e.providers_ui.plans import ProviderPlan

PROVIDERS_PAGE_URL_DEFAULT = "http://localhost:5173/providers"


@dataclass
class UIAction:
    """One step the agent must execute against chrome-devtools-mcp.

    ``tool`` is the suffix after ``mcp__chrome-devtools__`` (e.g.
    ``"click"``, ``"fill_form"``). ``args`` is the JSON-serialisable
    argument map fed to the tool. ``assert_text`` is a substring the
    agent must wait for on the resulting page (rendered via
    ``mcp__chrome-devtools__wait_for``). ``screenshot`` is the relative
    filename inside ``screenshots/`` to save before moving on.
    """

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    assert_text: tuple[str, ...] = ()
    screenshot: str = ""


def _kv_lines(mapping: dict[str, Any]) -> str:
    """Render a ``key=value`` block matching the form's parser contract."""
    return "\n".join(f"{k}={v}" for k, v in mapping.items())


def open_page_actions(
    url: str = PROVIDERS_PAGE_URL_DEFAULT,
) -> list[UIAction]:
    """Open the providers page and capture the empty-state screenshot."""
    return [
        UIAction(
            tool="new_page",
            args={"url": url},
            assert_text=("Providers", "Add provider"),
            screenshot="00-empty-state.png",
        ),
    ]


def add_provider_actions(plan: ProviderPlan) -> list[UIAction]:
    """Drive the modal: open → fill → submit → assert row visible.

    The form uses the generic ``Credentials`` / ``Options`` textareas
    today; once ``/providers/schemas`` lands (Johnny-mma) the driver
    should fetch the schema and emit per-field ``fill`` actions instead
    of one ``key=value`` textarea fill.
    """
    creds_text = _kv_lines(plan.resolved_credentials())
    opts_text = _kv_lines(plan.resolved_options())
    kind_label = {
        "stt": "STT (Speech-to-Text)",
        "llm": "LLM (Language Model)",
        "tts": "TTS (Text-to-Speech)",
    }[plan.kind.value]
    return [
        UIAction(
            tool="click",
            args={"selector_role": "button", "selector_name": "Add provider"},
            assert_text=("Add provider", "Kind"),
            screenshot=f"{plan.plan_id}-01-form-empty.png",
        ),
        UIAction(
            tool="fill_form",
            args={
                "fields": [
                    {"role": "combobox", "name": "Kind", "value": kind_label},
                    {"role": "textbox", "name": "Provider name", "value": plan.provider_name},
                    {"role": "textbox", "name": "Display name", "value": plan.display_name},
                    {
                        "role": "textbox",
                        "name": "Credentials (key=value per line)",
                        "value": creds_text,
                    },
                    {
                        "role": "textbox",
                        "name": "Options (key=value per line)",
                        "value": opts_text,
                    },
                ],
            },
            screenshot=f"{plan.plan_id}-02-form-filled.png",
        ),
        UIAction(
            tool="click",
            args={"selector_role": "button", "selector_name": "Create"},
            assert_text=(plan.display_name,),
            screenshot=f"{plan.plan_id}-03-row-created.png",
        ),
    ]


def test_provider_actions(plan: ProviderPlan) -> list[UIAction]:
    """Click ``Test`` for the row and wait for the success/failure banner."""
    return [
        UIAction(
            tool="click",
            args={
                "selector_within": plan.display_name,
                "selector_role": "button",
                "selector_name": "Test",
            },
            assert_text=("Test OK", "Test failed"),
            screenshot=f"{plan.plan_id}-04-test-result.png",
        ),
    ]


def activate_provider_actions(plan: ProviderPlan) -> list[UIAction]:
    """Activate the row and assert the ACTIVE badge appears."""
    return [
        UIAction(
            tool="click",
            args={
                "selector_within": plan.display_name,
                "selector_role": "button",
                "selector_name": "Activate",
            },
            assert_text=(f"Active: {plan.display_name}", "ACTIVE"),
            screenshot=f"{plan.plan_id}-05-activated.png",
        ),
    ]


def delete_provider_actions(plan: ProviderPlan) -> list[UIAction]:
    """Patch window.confirm to accept, then click Delete and assert removal."""
    return [
        UIAction(
            tool="evaluate_script",
            args={"function": "() => { window.confirm = () => true; }"},
        ),
        UIAction(
            tool="click",
            args={
                "selector_within": plan.display_name,
                "selector_role": "button",
                "selector_name": "Delete",
            },
            # After deletion the per-kind empty-state copy returns. We
            # do not wait for ``plan.display_name`` to disappear because
            # the snapshot polling may catch a stale frame.
            assert_text=("No providers configured for this kind.",),
            screenshot=f"{plan.plan_id}-06-deleted.png",
        ),
    ]


def full_plan_actions(plan: ProviderPlan) -> list[UIAction]:
    """Ordered list of actions covering one plan end-to-end."""
    return [
        *add_provider_actions(plan),
        *test_provider_actions(plan),
        *activate_provider_actions(plan),
        *delete_provider_actions(plan),
    ]


__all__ = [
    "PROVIDERS_PAGE_URL_DEFAULT",
    "UIAction",
    "activate_provider_actions",
    "add_provider_actions",
    "delete_provider_actions",
    "full_plan_actions",
    "open_page_actions",
    "test_provider_actions",
]
