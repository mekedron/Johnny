"""Skill discovery, eligibility gating, and the router-catalog source.

Skills are directories on the shared skills volume (``/skills`` in every
container that mounts it) holding a ``SKILL.md``
(:mod:`johnny.skills.frontmatter`). :func:`load_skill_registry` scans the
volume once per assembly:

1. parse every ``<dir>/SKILL.md`` — defects become listed reasons, never
   silent drops;
2. coalesce all skills' ``requires.bins`` / ``anyBins`` into **one** batched
   sandbox ``GET /bins`` probe (the openclaw ``system.which`` pattern —
   never per skill, never on the turn hot path). Bins in the guaranteed
   baseline toolset are implicitly satisfied and not probed at all, so a
   volume of baseline-only skills loads with zero sandbox round-trips;
3. evaluate eligibility per skill through exactly one function,
   :func:`evaluate_skill_eligibility` — the seam Johnny-trt.55 extends with
   credential / auth / env dimensions (and trt.38 with policy scope)
   without reshaping the loader;
4. compute the exec bin policy allow set from the *eligible* skills
   (:func:`johnny.skills.policy.compute_allowed_bins`).

The resulting :class:`SkillRegistry` feeds three consumers: the router's
task catalog (:meth:`SkillRegistry.catalog_entries` — kind + one-liner,
replacing Phase 3's ``STUB_TASK_CATALOG`` as the source), the executor
prompt (:meth:`SkillRegistry.instructions` — the SKILL.md body, for the
Johnny-trt.22/24 engine), and the deterministic v1 runner
(:mod:`johnny.skills.executor`).

Caching note (Johnny-trt.55 takes this further): one volume scan + at most
one /bins call per *session assembly* is deliberate v1 — session start is
not the per-turn hot path. The boot-time capability snapshot service with
change-event invalidation lands with trt.55; keep new probe logic out of
this module's callers so that swap stays local.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.skills.frontmatter import SkillDocument, parse_skill_markdown
from johnny.skills.policy import BASELINE_BINS, compute_allowed_bins

logger = logging.getLogger(__name__)

SKILL_FILE_NAME = "SKILL.md"

# Sandbox containers are linux; a skill pinned to other platforms can never
# run regardless of bins (e.g. a darwin-only ClawHub skill dropped in).
_SANDBOX_PLATFORM = "linux"

_ONE_LINER_CAP = 160
"""Router-prompt discipline: catalog one-liners stay short (the full body is
progressive-disclosure territory for the executor prompt, not the router)."""

CheckBins = Callable[[list[str]], Awaitable[Mapping[str, bool]]]
"""Resolve binaries inside the sandbox — ``SandboxClient.check_bins`` in
production, a fake in unit tests."""


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """One discovered skill directory plus its eligibility verdict.

    ``name`` is the task ``kind`` the router targets (``task.kind`` →
    ``agent_tasks.kind``). Ineligible skills stay listed with
    human-readable :attr:`reasons` (and machine-readable
    :attr:`missing_bins`) — the Johnny-trt.23 acceptance contract and the
    feed for the Phase-6 management UI.
    """

    name: str
    directory: str
    document: SkillDocument
    eligible: bool
    reasons: tuple[str, ...] = ()
    missing_bins: tuple[str, ...] = ()

    @property
    def description(self) -> str:
        return self.document.description


def evaluate_skill_eligibility(
    document: SkillDocument,
    present_bins: Mapping[str, bool],
    *,
    baseline: tuple[str, ...] = BASELINE_BINS,
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """THE eligibility predicate — returns ``(eligible, reasons, missing_bins)``.

    v1 dimensions: parse problems, platform pinning, ``requires.bins`` (all
    required) and ``requires.anyBins`` (one required) resolved against
    ``present_bins`` — with every baseline bin implicitly satisfied.
    ``requires.env`` / ``requires.config`` are carried but NOT evaluated
    here; they join in Johnny-trt.55's availability predicate alongside
    credential/auth state (which also fills the catalog's
    ``available``/``unavailable_reason`` fields there). Keep new dimensions
    inside this one function so callers never reshape.
    """
    reasons: list[str] = []
    missing: list[str] = []

    if document.problems:
        reasons.extend(document.problems)

    if document.os and _SANDBOX_PLATFORM not in document.os:
        reasons.append(
            f"supports {', '.join(document.os)} only — the skills sandbox runs {_SANDBOX_PLATFORM}"
        )

    baseline_set = set(baseline)
    for name in document.requires.bins:
        if name in baseline_set:
            continue  # guaranteed baseline toolset: implicitly satisfied
        if not present_bins.get(name, False):
            missing.append(name)
    if missing:
        reasons.append(
            "missing required tool(s) in the sandbox: " + ", ".join(missing)
        )

    any_bins = document.requires.any_bins
    if any_bins:
        any_present = any(
            name in baseline_set or present_bins.get(name, False) for name in any_bins
        )
        if not any_present:
            reasons.append(
                "none of the alternative tools are in the sandbox: " + ", ".join(any_bins)
            )
            missing.extend(any_bins)

    return (not reasons, tuple(reasons), tuple(missing))


@dataclass(frozen=True, slots=True)
class SkillRegistry:
    """Every skill found on the volume, with verdicts and the policy allow set."""

    skills: tuple[LoadedSkill, ...] = ()
    allowed_bins: frozenset[str] = field(default_factory=frozenset)
    skills_dir: str = ""

    def eligible(self) -> tuple[LoadedSkill, ...]:
        return tuple(skill for skill in self.skills if skill.eligible)

    def ineligible(self) -> tuple[LoadedSkill, ...]:
        return tuple(skill for skill in self.skills if not skill.eligible)

    def get(self, kind: str) -> LoadedSkill | None:
        for skill in self.skills:
            if skill.name == kind:
                return skill
        return None

    def catalog_entries(self) -> tuple[TaskCatalogEntry, ...]:
        """Eligible skills as router task-catalog entries (Phase-3 contract).

        ``kind`` = skill name, ``one_liner`` = the description's first line
        (length-capped: the router gets kind + one-liner ONLY; the SKILL.md
        body is executor-prompt territory). ``keywords`` feed the trt.50
        scorer's delegate-prior dimension and never render into the prompt.
        """
        entries = []
        for skill in self.eligible():
            one_liner = skill.description.splitlines()[0].strip()
            if len(one_liner) > _ONE_LINER_CAP:
                one_liner = one_liner[: _ONE_LINER_CAP - 1].rstrip() + "…"
            entries.append(
                TaskCatalogEntry(
                    kind=skill.name,
                    one_liner=one_liner,
                    keywords=skill.document.keywords,
                )
            )
        return tuple(entries)

    def instructions(self, kind: str) -> str | None:
        """The SKILL.md body for the executor prompt (progressive disclosure:
        full instructions reach only the engine actually running the kind)."""
        skill = self.get(kind)
        if skill is None:
            return None
        return skill.document.body

    def summary(self) -> str:
        """One log line: what loaded, what didn't, and why."""
        parts = []
        for skill in self.skills:
            if skill.eligible:
                parts.append(f"{skill.name}: eligible")
            else:
                parts.append(f"{skill.name}: INELIGIBLE ({'; '.join(skill.reasons)})")
        return f"{len(self.eligible())}/{len(self.skills)} skills eligible — " + (
            "; ".join(parts) if parts else "volume empty"
        )


EMPTY_SKILL_REGISTRY = SkillRegistry()
"""The no-skills registry — what delegation-incapable assemblies carry."""


def discover_skill_dirs(skills_dir: str | Path) -> tuple[Path, ...]:
    """Immediate subdirectories of the volume holding a ``SKILL.md``, sorted."""
    root = Path(skills_dir)
    try:
        if not root.is_dir():
            return ()
        return tuple(
            sorted(
                (entry for entry in root.iterdir() if (entry / SKILL_FILE_NAME).is_file()),
                key=lambda entry: entry.name,
            )
        )
    except OSError as exc:
        logger.warning("skills: cannot scan %s: %s", skills_dir, exc)
        return ()


async def load_skill_registry(
    skills_dir: str | Path,
    *,
    check_bins: CheckBins | None,
    baseline: tuple[str, ...] = BASELINE_BINS,
) -> SkillRegistry:
    """Scan the volume, probe the sandbox once, and gate every skill.

    Never raises: a broken SKILL.md, an unreadable directory, or an
    unreachable sandbox all degrade to listed-ineligible skills (with the
    failure as the reason) so session assembly keeps working. With
    ``check_bins=None`` (no sandbox wired) only baseline-satisfied skills
    can be eligible — declared non-baseline bins are reported unverifiable.
    """
    parsed: list[tuple[str, str, SkillDocument]] = []  # (name, dir, document)
    for directory in discover_skill_dirs(skills_dir):
        path = directory / SKILL_FILE_NAME
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("skills: cannot read %s: %s", path, exc)
            parsed.append(
                (
                    directory.name,
                    str(directory),
                    SkillDocument(problems=(f"cannot read SKILL.md: {exc}",)),
                )
            )
            continue
        document = parse_skill_markdown(text)
        name = document.name or directory.name
        if document.name and document.name != directory.name:
            # openclaw keys skills by frontmatter name; flag the mismatch but
            # keep the frontmatter name as the kind.
            logger.info(
                "skills: %s frontmatter name %r differs from directory name",
                path,
                document.name,
            )
        parsed.append((name, str(directory), document))

    # One batched probe for every non-baseline bin any skill mentions.
    baseline_set = set(baseline)
    to_probe = sorted(
        {
            name
            for _, _, document in parsed
            for name in (*document.requires.bins, *document.requires.any_bins)
            if name not in baseline_set
        }
    )
    present_bins: Mapping[str, bool] = {}
    probe_failure: str | None = None
    if to_probe:
        if check_bins is None:
            probe_failure = "no sandbox configured to verify tools"
        else:
            try:
                present_bins = await check_bins(to_probe)
            except Exception as exc:  # noqa: BLE001 — any probe failure degrades, never raises
                probe_failure = f"could not verify tools in the sandbox: {exc}"
                logger.warning("skills: bins probe failed: %s", exc)

    skills: list[LoadedSkill] = []
    seen: set[str] = set()
    for name, skill_dir, document in parsed:
        if name in seen:
            duplicate = f"duplicate skill name {name!r} — an earlier directory already provides it"
            skills.append(
                LoadedSkill(
                    name=name,
                    directory=skill_dir,
                    document=document,
                    eligible=False,
                    reasons=(duplicate,),
                )
            )
            continue
        seen.add(name)
        needs_probe = any(
            bin_name not in baseline_set
            for bin_name in (*document.requires.bins, *document.requires.any_bins)
        )
        if probe_failure is not None and needs_probe:
            # Presence unknown ≠ missing AND unknown ≠ present: evaluate the
            # other dimensions with the declared bins assumed present (so no
            # false "missing" reason), then hold the skill back with the
            # probe failure as the honest reason.
            assumed = {
                bin_name: True
                for bin_name in (*document.requires.bins, *document.requires.any_bins)
            }
            _, reasons, _ = evaluate_skill_eligibility(document, assumed, baseline=baseline)
            eligible = False
            reasons = (*reasons, probe_failure)
            missing: tuple[str, ...] = ()
        else:
            eligible, reasons, missing = evaluate_skill_eligibility(
                document, present_bins, baseline=baseline
            )
        skills.append(
            LoadedSkill(
                name=name,
                directory=skill_dir,
                document=document,
                eligible=eligible,
                reasons=reasons,
                missing_bins=missing,
            )
        )

    registry = SkillRegistry(
        skills=tuple(skills),
        allowed_bins=compute_allowed_bins(
            tuple(skill.document.requires for skill in skills if skill.eligible),
            baseline=baseline,
        ),
        skills_dir=str(skills_dir),
    )
    logger.info("skills: %s", registry.summary())
    return registry


__all__ = [
    "EMPTY_SKILL_REGISTRY",
    "CheckBins",
    "LoadedSkill",
    "SKILL_FILE_NAME",
    "SkillRegistry",
    "discover_skill_dirs",
    "evaluate_skill_eligibility",
    "load_skill_registry",
]
