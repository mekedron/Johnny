"""Skill discovery + eligibility gating (Johnny-trt.23).

The loader contract: every skill on the volume is listed (ineligible ones
with their reasons, never silently dropped), baseline bins are implicitly
satisfied, all non-baseline requirements coalesce into ONE batched probe,
and the eligibility verdict comes from exactly one function (the
Johnny-trt.55 extension seam).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from johnny.agent.task_catalog import TaskCatalogEntry, render_task_catalog
from johnny.skills.frontmatter import (
    SkillRequirements,
    SkillRunSpec,
    parse_skill_markdown,
)
from johnny.skills.registry import (
    EMPTY_SKILL_REGISTRY,
    AvailabilityProbeOutcome,
    discover_skill_dirs,
    evaluate_skill_availability,
    evaluate_skill_eligibility,
    load_skill_registry,
)


def _write_skill(
    root: Path,
    dirname: str,
    *,
    name: str | None = None,
    description: str = "Do the thing.",
    metadata: str = "",
    body: str = "Instructions.",
) -> Path:
    directory = root / dirname
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name if name is not None else dirname}",
        f'description: "{description}"',
    ]
    if metadata:
        lines.append(f"metadata: {metadata}")
    lines += ["---", "", body]
    (directory / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return directory


class _RecordingChecker:
    """A typed fake for the batched /bins probe."""

    def __init__(self, present: dict[str, bool] | None = None, *, fail: bool = False) -> None:
        self.present = present or {}
        self.fail = fail
        self.calls: list[list[str]] = []

    async def __call__(self, names: list[str]) -> dict[str, bool]:
        self.calls.append(list(names))
        if self.fail:
            raise ConnectionError("sandbox down")
        return {name: self.present.get(name, False) for name in names}


# --- discovery ----------------------------------------------------------------


def test_discovery_finds_only_dirs_with_skill_md(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha")
    (tmp_path / "not-a-skill").mkdir()
    (tmp_path / "loose-file.md").write_text("x", encoding="utf-8")
    dirs = discover_skill_dirs(tmp_path)
    assert [d.name for d in dirs] == ["alpha"]


def test_discovery_of_missing_root_is_empty() -> None:
    assert discover_skill_dirs("/no/such/dir") == ()


# --- the eligibility predicate (the trt.55 seam) -------------------------------


def test_baseline_bins_implicitly_satisfied() -> None:
    doc = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"bins": ["grep", "jq", "python3"]}}}\n---\n'
    )
    eligible, reasons, missing = evaluate_skill_eligibility(doc, {})
    assert eligible is True
    assert reasons == ()
    assert missing == ()


def test_missing_non_baseline_bin_named_in_reason() -> None:
    doc = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"bins": ["himalaya"]}}}\n---\n'
    )
    eligible, reasons, missing = evaluate_skill_eligibility(doc, {"himalaya": False})
    assert eligible is False
    assert missing == ("himalaya",)
    assert any("himalaya" in reason for reason in reasons)


def test_any_bins_one_present_suffices() -> None:
    doc = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"anyBins": ["magick", "convert"]}}}\n---\n'
    )
    eligible, _, _ = evaluate_skill_eligibility(doc, {"magick": False, "convert": True})
    assert eligible is True
    eligible2, reasons2, _ = evaluate_skill_eligibility(doc, {"magick": False, "convert": False})
    assert eligible2 is False
    assert any("alternative" in reason for reason in reasons2)


def test_platform_pinned_skill_ineligible_on_linux_sandbox() -> None:
    doc = parse_skill_markdown(
        "---\nname: peekaboo\ndescription: d\n"
        'metadata: {"openclaw": {"os": ["darwin"], "requires": {"bins": ["peekaboo"]}}}\n---\n'
    )
    eligible, reasons, _ = evaluate_skill_eligibility(doc, {"peekaboo": True})
    assert eligible is False
    assert any("darwin" in reason and "linux" in reason for reason in reasons)


def test_parse_problems_make_skill_ineligible() -> None:
    doc = parse_skill_markdown("---\ndescription: d\n---\n")
    eligible, reasons, _ = evaluate_skill_eligibility(doc, {})
    assert eligible is False
    assert any("'name'" in reason for reason in reasons)


# --- the availability predicate (Johnny-trt.55) --------------------------------


def _doc_with_availability(*, reason: str = ""):
    """A parsed doc declaring an availability check (+ optional fallback copy)."""
    parts = ['"check": {"argv": ["bash", "/skills/x/check.sh"]}']
    if reason:
        parts.append(f'"unavailable_reason": "{reason}"')
    return parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        f'metadata: {{"johnny": {{"availability": {{{", ".join(parts)}}}}}}}\n---\n'
    )


def test_availability_no_dimensions_declared_is_available() -> None:
    doc = parse_skill_markdown("---\nname: x\ndescription: d\n---\n")
    available, reason = evaluate_skill_availability(
        doc, env_present={}, check_outcome=None
    )
    assert available is True
    assert reason == ""


def test_availability_env_missing_names_the_vars() -> None:
    doc = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"env": ["API_KEY", "REGION"]}}}\n---\n'
    )
    available, reason = evaluate_skill_availability(
        doc, env_present={"API_KEY": True, "REGION": False}, check_outcome=None
    )
    assert available is False
    assert "REGION" in reason and "API_KEY" not in reason
    assert "configuration" in reason

    ok, _ = evaluate_skill_availability(
        doc, env_present={"API_KEY": True, "REGION": True}, check_outcome=None
    )
    assert ok is True


def test_availability_env_unverifiable_holds_back_honestly() -> None:
    doc = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"env": ["API_KEY"]}}}\n---\n'
    )
    available, reason = evaluate_skill_availability(
        doc, env_present=None, check_outcome=None
    )
    assert available is False
    assert "couldn't verify" in reason
    # No false missing-vars claim.
    assert "API_KEY" not in reason


def test_availability_check_failure_speaks_stdout_copy() -> None:
    doc = _doc_with_availability(reason="fallback copy.")
    outcome = AvailabilityProbeOutcome(
        ran=True,
        exit_code=2,
        stdout="no Google account is connected — link one in settings.\n",
    )
    available, reason = evaluate_skill_availability(
        doc, env_present={}, check_outcome=outcome
    )
    assert available is False
    assert reason == "no Google account is connected — link one in settings."


def test_availability_check_failure_without_stdout_uses_declared_reason() -> None:
    doc = _doc_with_availability(reason="the account isn't linked yet.")
    outcome = AvailabilityProbeOutcome(ran=True, exit_code=1, stdout="  ")
    available, reason = evaluate_skill_availability(
        doc, env_present={}, check_outcome=outcome
    )
    assert available is False
    assert reason == "the account isn't linked yet."


def test_availability_check_pass_is_available() -> None:
    doc = _doc_with_availability()
    outcome = AvailabilityProbeOutcome(ran=True, exit_code=0, stdout="")
    assert evaluate_skill_availability(doc, env_present={}, check_outcome=outcome) == (
        True,
        "",
    )


def test_availability_check_that_never_ran_holds_back_honestly() -> None:
    doc = _doc_with_availability(reason="the account isn't linked yet.")
    for outcome in (None, AvailabilityProbeOutcome(ran=False, failure="sandbox down")):
        available, reason = evaluate_skill_availability(
            doc, env_present={}, check_outcome=outcome
        )
        assert available is False
        # Unknown is not "unlinked": the declared reason must NOT be asserted.
        assert "couldn't verify" in reason


def test_availability_check_stdout_collapsed_and_capped() -> None:
    doc = _doc_with_availability()
    outcome = AvailabilityProbeOutcome(
        ran=True, exit_code=1, stdout="line one\nline two  " + "x" * 400
    )
    available, reason = evaluate_skill_availability(
        doc, env_present={}, check_outcome=outcome
    )
    assert available is False
    assert "\n" not in reason
    assert len(reason) <= 240
    assert reason.endswith("…")


# --- the loader ---------------------------------------------------------------


async def test_baseline_only_volume_probes_nothing(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "text-tools",
        metadata='{"openclaw": {"requires": {"bins": ["grep", "wc"]}}}',
    )
    checker = _RecordingChecker()
    registry = await load_skill_registry(tmp_path, check_bins=checker)
    assert checker.calls == []  # zero sandbox round-trips
    assert [skill.name for skill in registry.eligible()] == ["text-tools"]


async def test_one_batched_probe_covers_all_skills(tmp_path: Path) -> None:
    _write_skill(
        tmp_path, "mail", metadata='{"openclaw": {"requires": {"bins": ["himalaya"]}}}'
    )
    _write_skill(
        tmp_path,
        "images",
        metadata='{"openclaw": {"requires": {"anyBins": ["magick", "convert"]}}}',
    )
    checker = _RecordingChecker({"himalaya": True, "convert": True})
    registry = await load_skill_registry(tmp_path, check_bins=checker)
    assert checker.calls == [["convert", "himalaya", "magick"]]  # one sorted batch
    assert {skill.name for skill in registry.eligible()} == {"images", "mail"}


async def test_probe_failure_holds_back_with_honest_reason(tmp_path: Path) -> None:
    _write_skill(
        tmp_path, "mail", metadata='{"openclaw": {"requires": {"bins": ["himalaya"]}}}'
    )
    _write_skill(tmp_path, "plain")  # no requirements — must stay eligible
    checker = _RecordingChecker(fail=True)
    registry = await load_skill_registry(tmp_path, check_bins=checker)

    mail = registry.get("mail")
    assert mail is not None and mail.eligible is False
    assert any("could not verify" in reason for reason in mail.reasons)
    # Unknown is not "missing": no false missing-tools claim.
    assert mail.missing_bins == ()
    assert not any("missing required" in reason for reason in mail.reasons)

    plain = registry.get("plain")
    assert plain is not None and plain.eligible is True


async def test_no_checker_means_non_baseline_unverifiable(tmp_path: Path) -> None:
    _write_skill(
        tmp_path, "mail", metadata='{"openclaw": {"requires": {"bins": ["himalaya"]}}}'
    )
    registry = await load_skill_registry(tmp_path, check_bins=None)
    mail = registry.get("mail")
    assert mail is not None and mail.eligible is False
    assert any("no sandbox configured" in reason for reason in mail.reasons)


async def test_broken_skill_listed_with_reason_not_dropped(tmp_path: Path) -> None:
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\ndescription: only\n---\n", encoding="utf-8")
    registry = await load_skill_registry(tmp_path, check_bins=_RecordingChecker())
    broken = registry.get("broken")  # name falls back to the directory
    assert broken is not None
    assert broken.eligible is False
    assert any("'name'" in reason for reason in broken.reasons)
    assert registry.catalog_entries() == ()


async def test_kinds_carries_every_skill_any_eligibility(tmp_path: Path) -> None:
    """The skills half of the executor-known set (Johnny-trt.62): ineligible
    skills still *resolve* in the executor to honest skill-specific settles,
    so kinds() carries them — only kinds outside the set are hallucinations
    the gate degrades pre-ack."""
    _write_skill(tmp_path, "plain")
    _write_skill(
        tmp_path, "mail", metadata='{"openclaw": {"requires": {"bins": ["himalaya"]}}}'
    )
    # No checker ⇒ mail is unverifiable ⇒ ineligible — but still a known kind.
    registry = await load_skill_registry(tmp_path, check_bins=None)
    mail = registry.get("mail")
    assert mail is not None and mail.eligible is False
    assert registry.kinds() == frozenset({"plain", "mail"})
    assert EMPTY_SKILL_REGISTRY.kinds() == frozenset()


async def test_duplicate_skill_names_second_listed_ineligible(tmp_path: Path) -> None:
    _write_skill(tmp_path, "a-dir", name="same")
    _write_skill(tmp_path, "b-dir", name="same")
    registry = await load_skill_registry(tmp_path, check_bins=_RecordingChecker())
    verdicts = {skill.directory: skill.eligible for skill in registry.skills}
    assert verdicts[str(tmp_path / "a-dir")] is True
    assert verdicts[str(tmp_path / "b-dir")] is False
    assert len(registry.catalog_entries()) == 1


async def test_openclaw_format_skill_drops_in_unchanged(tmp_path: Path) -> None:
    """The wire-compat acceptance: a ClawHub-style skill needs no Johnny edits."""
    directory = tmp_path / "himalaya"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        """---
name: himalaya
description: "Himalaya CLI for IMAP/SMTP mail: list, read, search."
homepage: https://github.com/pimalaya/himalaya
metadata:
  {
    "openclaw":
      {
        "emoji": "📧",
        "requires": { "bins": ["himalaya"] },
        "install": [{ "id": "brew", "kind": "brew", "formula": "himalaya" }],
      },
  }
---

Use the himalaya CLI.
""",
        encoding="utf-8",
    )
    checker = _RecordingChecker({"himalaya": True})
    registry = await load_skill_registry(tmp_path, check_bins=checker)
    skill = registry.get("himalaya")
    assert skill is not None and skill.eligible is True
    assert registry.catalog_entries() == (
        TaskCatalogEntry(
            kind="himalaya",
            one_liner="Himalaya CLI for IMAP/SMTP mail: list, read, search.",
        ),
    )
    # Declared bins of eligible skills enter the exec allow set.
    assert "himalaya" in registry.allowed_bins


# --- catalog + instructions ----------------------------------------------------


class _RecordingEnvChecker:
    """A typed fake for the batched env probe."""

    def __init__(self, present: dict[str, bool] | None = None, *, fail: bool = False) -> None:
        self.present = present or {}
        self.fail = fail
        self.calls: list[list[str]] = []

    async def __call__(self, names: list[str]) -> dict[str, bool]:
        self.calls.append(list(names))
        if self.fail:
            raise ConnectionError("sandbox down")
        return {name: self.present.get(name, False) for name in names}


class _RecordingCheckRunner:
    """A typed fake for the per-skill availability-check runner."""

    def __init__(self, outcome: AvailabilityProbeOutcome | None = None) -> None:
        self.outcome = outcome or AvailabilityProbeOutcome(ran=True, exit_code=0)
        self.calls: list[SkillRunSpec] = []

    async def __call__(self, spec: SkillRunSpec) -> AvailabilityProbeOutcome:
        self.calls.append(spec)
        return self.outcome


_CHECK_METADATA = (
    '{"openclaw": {"requires": {"bins": ["gog"]}}, '
    '"johnny": {"availability": {"check": {"argv": ["bash", "/skills/cal/check.sh"], '
    '"timeout_s": 10}, "unavailable_reason": "no account linked."}, '
    '"keywords": ["calendar", "agenda"]}}'
)


async def test_loader_runs_declared_check_and_marks_unavailable(tmp_path: Path) -> None:
    """The credential dimension end-to-end: a failing check makes the skill
    eligible-but-unavailable, with the check's stdout as the catalog reason
    and the keywords EXCLUDED from the scorer feed (the trt.55 contract)."""
    _write_skill(tmp_path, "cal", metadata=_CHECK_METADATA)
    runner = _RecordingCheckRunner(
        AvailabilityProbeOutcome(ran=True, exit_code=2, stdout="no account — link one.\n")
    )
    registry = await load_skill_registry(
        tmp_path,
        check_bins=_RecordingChecker({"gog": True}),
        check_env=_RecordingEnvChecker(),
        run_check=runner,
    )

    assert [spec.argv for spec in runner.calls] == [("bash", "/skills/cal/check.sh")]
    skill = registry.get("cal")
    assert skill is not None
    assert skill.eligible is True  # bins fine — it could run
    assert skill.available is False  # but the account is not linked
    assert skill.unavailable_reason == "no account — link one."

    (entry,) = registry.catalog_entries()
    assert entry == TaskCatalogEntry(
        kind="cal",
        one_liner="Do the thing.",
        keywords=(),  # scorer-feed exclusion happens at catalog assembly
        available=False,
        unavailable_reason="no account — link one.",
    )
    # The rendered prompt teaches the decline.
    rendered = render_task_catalog(registry.catalog_entries())
    assert "no account — link one." in rendered


async def test_loader_passing_check_keeps_skill_available(tmp_path: Path) -> None:
    _write_skill(tmp_path, "cal", metadata=_CHECK_METADATA)
    registry = await load_skill_registry(
        tmp_path,
        check_bins=_RecordingChecker({"gog": True}),
        check_env=_RecordingEnvChecker(),
        run_check=_RecordingCheckRunner(),  # exit 0
    )
    skill = registry.get("cal")
    assert skill is not None and skill.eligible and skill.available
    (entry,) = registry.catalog_entries()
    assert entry.available is True
    assert entry.keywords == ("calendar", "agenda")  # feed intact when available


async def test_loader_no_check_runner_holds_declaring_skill_back(tmp_path: Path) -> None:
    _write_skill(tmp_path, "cal", metadata=_CHECK_METADATA)
    registry = await load_skill_registry(
        tmp_path, check_bins=_RecordingChecker({"gog": True})
    )
    skill = registry.get("cal")
    assert skill is not None and skill.eligible is True
    assert skill.available is False
    assert "couldn't verify" in skill.unavailable_reason


async def test_loader_env_probe_is_one_sorted_batch(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "a",
        metadata='{"openclaw": {"requires": {"env": ["ZULU_KEY", "ALPHA_KEY"]}}}',
    )
    _write_skill(
        tmp_path, "b", metadata='{"openclaw": {"requires": {"env": ["ALPHA_KEY"]}}}'
    )
    _write_skill(tmp_path, "plain")
    env = _RecordingEnvChecker({"ALPHA_KEY": True, "ZULU_KEY": True})
    registry = await load_skill_registry(
        tmp_path, check_bins=_RecordingChecker(), check_env=env
    )
    assert env.calls == [["ALPHA_KEY", "ZULU_KEY"]]  # one sorted batch
    assert all(skill.available for skill in registry.skills)


async def test_loader_missing_env_marks_unavailable_not_ineligible(tmp_path: Path) -> None:
    _write_skill(
        tmp_path, "a", metadata='{"openclaw": {"requires": {"env": ["API_KEY"]}}}'
    )
    registry = await load_skill_registry(
        tmp_path, check_bins=_RecordingChecker(), check_env=_RecordingEnvChecker()
    )
    skill = registry.get("a")
    assert skill is not None
    assert skill.eligible is True
    assert skill.available is False
    assert "API_KEY" in skill.unavailable_reason
    # Still cataloged — as the honest-decline entry.
    (entry,) = registry.catalog_entries()
    assert entry.available is False


async def test_loader_env_probe_failure_degrades_to_could_not_verify(tmp_path: Path) -> None:
    _write_skill(
        tmp_path, "a", metadata='{"openclaw": {"requires": {"env": ["API_KEY"]}}}'
    )
    registry = await load_skill_registry(
        tmp_path,
        check_bins=_RecordingChecker(),
        check_env=_RecordingEnvChecker(fail=True),
    )
    skill = registry.get("a")
    assert skill is not None and skill.available is False
    assert "couldn't verify" in skill.unavailable_reason
    assert "API_KEY" not in skill.unavailable_reason  # no false missing claim


async def test_loader_ineligible_skill_stays_out_of_catalog(tmp_path: Path) -> None:
    """The trt.23 stance is unchanged: ineligible (missing bins) ⇒ omitted from
    the catalog entirely — operator diagnostics, not spoken declines."""
    _write_skill(
        tmp_path, "mail", metadata='{"openclaw": {"requires": {"bins": ["himalaya"]}}}'
    )
    registry = await load_skill_registry(
        tmp_path, check_bins=_RecordingChecker({"himalaya": False})
    )
    skill = registry.get("mail")
    assert skill is not None and skill.eligible is False and skill.available is False
    assert registry.catalog_entries() == ()


async def test_loader_checks_not_run_for_ineligible_skills(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "cal",
        metadata=(
            '{"openclaw": {"requires": {"bins": ["missing-bin"]}}, '
            '"johnny": {"availability": {"check": {"argv": ["x"]}}}}'
        ),
    )
    runner = _RecordingCheckRunner()
    await load_skill_registry(
        tmp_path,
        check_bins=_RecordingChecker({"missing-bin": False}),
        check_env=_RecordingEnvChecker(),
        run_check=runner,
    )
    assert runner.calls == []  # no probe spent on a skill that can't run anyway


async def test_catalog_entries_shape_and_keywords(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "google-calendar",
        description="Look up upcoming events on the connected Google calendar.",
        metadata=(
            '{"openclaw": {"requires": {"bins": ["gog"]}}, '
            '"johnny": {"keywords": ["calendar", "agenda"]}}'
        ),
    )
    registry = await load_skill_registry(tmp_path, check_bins=_RecordingChecker())
    entries = registry.catalog_entries()
    assert entries == (
        TaskCatalogEntry(
            kind="google-calendar",
            one_liner="Look up upcoming events on the connected Google calendar.",
            keywords=("calendar", "agenda"),
        ),
    )
    # The Phase-3 renderer consumes loader-built entries unchanged.
    rendered = render_task_catalog(entries)
    assert "- google-calendar: Look up upcoming events" in rendered


async def test_one_liner_capped_for_prompt_discipline(tmp_path: Path) -> None:
    _write_skill(tmp_path, "wordy", description="w" * 400)
    registry = await load_skill_registry(tmp_path, check_bins=_RecordingChecker())
    (entry,) = registry.catalog_entries()
    assert len(entry.one_liner) <= 160
    assert entry.one_liner.endswith("…")


async def test_instructions_return_the_body(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", body="Step 1. Step 2.")
    registry = await load_skill_registry(tmp_path, check_bins=_RecordingChecker())
    instructions = registry.instructions("alpha")
    assert instructions is not None and "Step 1. Step 2." in instructions
    assert registry.instructions("nope") is None


def test_empty_registry_constant() -> None:
    assert EMPTY_SKILL_REGISTRY.skills == ()
    assert EMPTY_SKILL_REGISTRY.catalog_entries() == ()
    assert EMPTY_SKILL_REGISTRY.eligible() == ()


@pytest.mark.parametrize("requires", [SkillRequirements(), SkillRequirements(bins=("grep",))])
def test_requirements_dataclass_is_hashable_for_policy(requires: SkillRequirements) -> None:
    # compute_allowed_bins consumes these from frozen LoadedSkill documents.
    assert isinstance(hash(requires), int)
