"""Phase 2 tests for the openclaw tool-use prompt block (Johnny-3ow.3).

Asserts the new ``tool_use_notes`` block renders in the right place, enumerates
the real native tools + the skill-discovery recipe, and — crucially — leaves a
tool-less prompt byte-identical so the etu.17 self-awareness guards stay green.
"""

from __future__ import annotations

from johnny.agent.session import (
    TOOL_USE_NOTES,
    AgentInstructionsConfig,
    build_agent_instructions,
)


def test_tool_use_notes_empty_keeps_prompt_byte_identical() -> None:
    base = build_agent_instructions(AgentInstructionsConfig())
    # The default field is "", so the openclaw block never renders on a
    # tool-less session — the prompt the etu.17 tests pin is untouched.
    assert "list_dir('/skills')" not in base
    assert TOOL_USE_NOTES not in base
    assert build_agent_instructions(AgentInstructionsConfig(tool_use_notes="")) == base


def test_tool_use_notes_render_between_capability_notes_and_context() -> None:
    cfg = AgentInstructionsConfig(
        character_prompt="[personality: Pirate]",
        capability_notes=(
            "Some requests are handled for you by background tools:\n- x: do x."
        ),
        tool_use_notes=TOOL_USE_NOTES,
        context="Quarterly planning.",
    )
    text = build_agent_instructions(cfg)
    assert (
        text.index("background tools")
        < text.index("- exec:")
        < text.index("Context: Quarterly planning.")
    )


def test_tool_use_notes_enumerate_real_tools_and_discovery_recipe() -> None:
    text = build_agent_instructions(AgentInstructionsConfig(tool_use_notes=TOOL_USE_NOTES))
    for tool in ("exec", "read", "write", "list_dir"):
        assert f"- {tool}:" in text
    # The openclaw "scan → read SKILL.md → run" recipe and the pass-real-args
    # rule (the always-London fix), by example.
    assert "list_dir('/skills')" in text
    assert "SKILL.md" in text
    assert "Helsinki" in text
    # The etu.17 self-awareness guard still coexists in the same prompt.
    assert "Never invent or role-play abilities you do not have" in text


def test_tool_use_notes_avoids_the_cannot_token() -> None:
    # The capability gap-block owns "CANNOT"; the tool block must not introduce
    # it (keeps the trt.55 / etu.7 byte-stability guards stable).
    assert "CANNOT" not in TOOL_USE_NOTES
