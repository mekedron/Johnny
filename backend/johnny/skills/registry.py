"""Skill discovery, eligibility + availability gating, and the router-catalog source.

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
   :func:`evaluate_skill_eligibility` (parse / platform / bins — can the
   skill run here at all);
4. evaluate **availability** per eligible skill through exactly one function,
   :func:`evaluate_skill_availability` (Johnny-trt.55 — can THIS session use
   it now): ``requires.env`` resolved inside the sandbox (one batched probe)
   and the skill-declared credential check (``metadata.johnny.availability``,
   e.g. "is gog authed") run in-sandbox. Johnny-trt.38's per-agent policy
   scope and Johnny-trt.36's MCP server health join inside that same
   function;
5. compute the exec bin policy allow set from the *eligible* skills
   (:func:`johnny.skills.policy.compute_allowed_bins`).

The resulting :class:`SkillRegistry` feeds three consumers: the router's
task catalog (:meth:`SkillRegistry.catalog_entries` — kind + one-liner,
with unavailable kinds carried as honest-decline entries, replacing Phase
3's ``STUB_TASK_CATALOG`` as the source), the executor prompt
(:meth:`SkillRegistry.instructions` — the SKILL.md body, for the
Johnny-trt.22/24 engine), and the deterministic v1 runner
(:mod:`johnny.skills.executor`, which re-runs the availability check at
claim time — links can break mid-session).

Snapshot lifecycle (Johnny-trt.55, documented stance): availability is
evaluated **once per session assembly** and the session's catalog stays
frozen — there is no mid-session refresh on integration-change events (no
cheap event source exists for sandbox credential state). The staleness
window is bounded by the executor's claim-time revalidation (a break after
assembly fails honestly with the same actionable reason) and by the next
session picking up the new state at its own assembly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.skills.frontmatter import SkillDocument, SkillRunSpec, parse_skill_markdown
from johnny.skills.policy import BASELINE_BINS, compute_allowed_bins

if TYPE_CHECKING:  # pragma: no cover - import-cheap module, sandbox is httpx-heavy
    from johnny.skills.sandbox import SandboxClient

logger = logging.getLogger(__name__)

SKILL_FILE_NAME = "SKILL.md"

# Sandbox containers are linux; a skill pinned to other platforms can never
# run regardless of bins (e.g. a darwin-only ClawHub skill dropped in).
_SANDBOX_PLATFORM = "linux"

_ONE_LINER_CAP = 160
"""Router-prompt discipline: catalog one-liners stay short (the full body is
progressive-disclosure territory for the executor prompt, not the router)."""

_UNAVAILABLE_REASON_CAP = 240
"""Cap on the spoken-form unavailable reason a check's stdout may author —
roomier than a one-liner (it carries the fix) but never a monologue."""

DEFAULT_AVAILABILITY_CHECK_TIMEOUT_S = 10.0
"""Ceiling for a skill's declared availability check when the spec names no
``timeout_s`` — assembly-time probes must stay snappy (the sandbox default of
30 s is a *run* budget, not a probe budget)."""

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""Valid POSIX env var names — anything else is never probed (and therefore
never present)."""

CheckBins = Callable[[list[str]], Awaitable[Mapping[str, bool]]]
"""Resolve binaries inside the sandbox — ``SandboxClient.check_bins`` in
production, a fake in unit tests."""

CheckEnv = Callable[[list[str]], Awaitable[Mapping[str, bool]]]
"""Resolve which env vars are set inside the sandbox —
``SandboxClient.check_env`` in production, a fake in unit tests. Batched
exactly like :data:`CheckBins`: one call per assembly covering every
``requires.env`` name any eligible skill declares."""


@dataclass(frozen=True, slots=True)
class AvailabilityProbeOutcome:
    """What one skill's declared availability check produced.

    ``ran=True`` means the command executed to an exit code (``exit_code`` /
    ``stdout`` are meaningful); ``ran=False`` means no verdict was obtained
    (sandbox unreachable, timeout, rejection) with ``failure`` as the
    diagnostic. Unknown is not unavailable-with-the-skill's-reason: a probe
    failure degrades to an honest "could not verify" entry instead of
    asserting a credential gap that may not exist.
    """

    ran: bool
    exit_code: int = -1
    stdout: str = ""
    failure: str = ""


RunAvailabilityCheck = Callable[[SkillRunSpec], Awaitable[AvailabilityProbeOutcome]]
"""Run one skill's declared availability check inside the sandbox. Production
wiring comes from :func:`build_sandbox_availability_runner`; unit tests pass a
fake. Must not raise — failures come back as ``ran=False`` outcomes."""


def build_sandbox_availability_runner(client: SandboxClient) -> RunAvailabilityCheck:
    """Adapt a :class:`~johnny.skills.sandbox.SandboxClient` to the check seam.

    Maps the exec API's reply / failure modes onto
    :class:`AvailabilityProbeOutcome` (timeout and transport errors are
    ``ran=False``). Lives here rather than in the sandbox module so the
    probe semantics stay next to the predicate that consumes them.
    """
    from johnny.skills.sandbox import SandboxError

    async def _run(spec: SkillRunSpec) -> AvailabilityProbeOutcome:
        timeout_s = spec.timeout_s or DEFAULT_AVAILABILITY_CHECK_TIMEOUT_S
        try:
            result = await client.exec(argv=list(spec.argv), timeout_s=timeout_s)
        except SandboxError as exc:
            return AvailabilityProbeOutcome(ran=False, failure=str(exc))
        except Exception as exc:  # noqa: BLE001 — the seam must never raise
            return AvailabilityProbeOutcome(ran=False, failure=f"{type(exc).__name__}: {exc}")
        if result.timed_out:
            return AvailabilityProbeOutcome(
                ran=False, failure=f"availability check timed out after {timeout_s:.0f}s"
            )
        return AvailabilityProbeOutcome(
            ran=True, exit_code=result.exit_code, stdout=result.stdout
        )

    return _run


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """One discovered skill directory plus its eligibility + availability verdicts.

    ``name`` is the task ``kind`` the router targets (``task.kind`` →
    ``agent_tasks.kind``). Ineligible skills stay listed with
    human-readable :attr:`reasons` (and machine-readable
    :attr:`missing_bins`) — the Johnny-trt.23 acceptance contract and the
    feed for the Phase-6 management UI.

    ``available`` / ``unavailable_reason`` (Johnny-trt.55) record whether
    THIS session can use the skill *now* (credentials linked, configuration
    set): an eligible-but-unavailable skill still enters the router catalog —
    as an honest-decline entry carrying the spoken-form reason — while an
    ineligible skill stays out of the catalog entirely (its reasons are
    operator diagnostics, not user-actionable speech). Ineligible implies
    unavailable.
    """

    name: str
    directory: str
    document: SkillDocument
    eligible: bool
    reasons: tuple[str, ...] = ()
    missing_bins: tuple[str, ...] = ()
    available: bool = True
    unavailable_reason: str = ""

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
    ``requires.env`` and credential/auth state are NOT eligibility — they are
    the *availability* dimensions (:func:`evaluate_skill_availability`,
    Johnny-trt.55), which also fill the catalog's
    ``available``/``unavailable_reason`` fields. Keep new can-it-run-here
    dimensions inside this one function so callers never reshape.
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


def _spoken_reason(text: str) -> str:
    """Normalize skill-authored spoken copy: one line, capped, stripped."""
    collapsed = " ".join(text.split())
    if len(collapsed) > _UNAVAILABLE_REASON_CAP:
        collapsed = collapsed[: _UNAVAILABLE_REASON_CAP - 1].rstrip() + "…"
    return collapsed


def evaluate_skill_availability(
    document: SkillDocument,
    *,
    env_present: Mapping[str, bool] | None,
    check_outcome: AvailabilityProbeOutcome | None,
) -> tuple[bool, str]:
    """THE availability predicate (Johnny-trt.55) — ``(available, unavailable_reason)``.

    Decides whether THIS session can use an *eligible* skill right now
    (eligibility — parse / platform / bins — is
    :func:`evaluate_skill_eligibility` upstream; the loader composes the
    two, and ineligible is trivially unavailable). Dimensions, in order:

    * ``requires.env`` — every declared env var must be set inside the
      sandbox. ``env_present`` is the batched probe result; ``None`` when
      the skill declares env vars means the probe could not run, which holds
      the skill back with an honest could-not-verify reason (unknown is
      neither present nor missing — the trt.23 probe-failure stance).
    * the skill-declared credential check
      (``metadata.johnny.availability.check``) — ``check_outcome`` is the
      in-sandbox run. Exit 0 passes; any other exit is unavailable with the
      check's stdout (skill-authored spoken copy, the run.sh contract) or
      the declared ``unavailable_reason`` as the reason; a probe that never
      ran (``ran=False``) degrades to could-not-verify.
    * ``requires.config`` is carried but not evaluated — no config registry
      exists yet to resolve it against.

    Later CAPABILITY dimensions (e.g. Johnny-trt.36's MCP server health)
    join INSIDE this function, exactly like trt.55 joined eligibility, so
    the loader and every caller stay unreshaped. Johnny-trt.38's capability
    POLICY deliberately did NOT join here: a policy-denied kind must be
    *hidden* from every rendered prompt block (the canonical least-privilege
    scenario), which is stronger than unavailable-with-reason — it is
    applied as a downstream catalog transform
    (:func:`johnny.skills.capability_policy.apply_policy_to_catalog`) and at
    the worker's claim gate, keeping this predicate policy-agnostic (the
    registry is shared across agents whose policies differ).

    The returned reason is spoken-form and actionable by contract — it is
    rendered into the router prompt for the honest decline and spoken
    verbatim by the gate's delegate backstop.
    """
    declared_env = tuple(document.requires.env)
    if declared_env:
        if env_present is None:
            return (
                False,
                "I couldn't verify my tools' configuration just now — try again shortly.",
            )
        missing_env = [name for name in declared_env if not env_present.get(name, False)]
        if missing_env:
            return (
                False,
                "it needs configuration that isn't set on my side: "
                + ", ".join(missing_env),
            )

    spec = document.availability
    if spec is not None and spec.check is not None:
        if check_outcome is None or not check_outcome.ran:
            return (
                False,
                "I couldn't verify access for this capability just now — try again shortly.",
            )
        if check_outcome.exit_code != 0:
            reason = (
                _spoken_reason(check_outcome.stdout)
                or _spoken_reason(spec.unavailable_reason)
                or "it isn't connected right now."
            )
            return (False, reason)

    return (True, "")


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

    def available(self) -> tuple[LoadedSkill, ...]:
        """Eligible AND usable by this session now (Johnny-trt.55)."""
        return tuple(skill for skill in self.skills if skill.eligible and skill.available)

    def get(self, kind: str) -> LoadedSkill | None:
        for skill in self.skills:
            if skill.name == kind:
                return skill
        return None

    def kinds(self) -> frozenset[str]:
        """Every kind on this volume, ANY eligibility (Johnny-trt.62).

        The skills half of the executor-known set the gate's pre-ack
        membership check validates delegate verdicts against: ineligible and
        unavailable skills still *resolve* in
        :func:`johnny.skills.executor.build_skill_task_executor` to honest,
        skill-specific settles — only kinds outside this set hit the stub's
        unsupported-kind leg, so only those count as hallucinated.
        """
        return frozenset(skill.name for skill in self.skills)

    def catalog_entries(self) -> tuple[TaskCatalogEntry, ...]:
        """Eligible skills as router task-catalog entries (Phase-3 contract).

        ``kind`` = skill name, ``one_liner`` = the description's first line
        (length-capped: the router gets kind + one-liner ONLY; the SKILL.md
        body is executor-prompt territory). ``keywords`` feed the trt.50
        scorer's delegate-prior dimension and never render into the prompt.

        Eligible-but-unavailable skills (Johnny-trt.55) enter as
        ``available=False`` entries carrying the spoken-form reason — the
        router declines honestly instead of never learning the kind exists —
        and with ``keywords=()`` so the trt.50 delegate prior never fires
        for work this session cannot do (the scorer-feed exclusion happens
        HERE, at catalog assembly, by contract). Ineligible skills stay out
        entirely, as in trt.23.
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
                    keywords=skill.document.keywords if skill.available else (),
                    available=skill.available,
                    unavailable_reason=skill.unavailable_reason,
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
            if skill.eligible and skill.available:
                parts.append(f"{skill.name}: available")
            elif skill.eligible:
                parts.append(f"{skill.name}: UNAVAILABLE ({skill.unavailable_reason})")
            else:
                parts.append(f"{skill.name}: INELIGIBLE ({'; '.join(skill.reasons)})")
        return (
            f"{len(self.available())}/{len(self.skills)} skills available "
            f"({len(self.eligible())} eligible) — "
            + ("; ".join(parts) if parts else "volume empty")
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


async def _probe_env(
    documents: list[SkillDocument],
    *,
    check_env: CheckEnv | None,
) -> Mapping[str, bool] | None:
    """One batched env probe for every valid ``requires.env`` name declared.

    Returns the presence map, ``{}`` when nothing needed probing (a real
    verdict: invalid names are simply never present), or ``None`` when the
    probe could not run — the predicate then holds env-declaring skills back
    could-not-verify instead of inventing a missing-vars claim.
    """
    names = sorted(
        {
            name
            for document in documents
            for name in document.requires.env
            if _ENV_NAME_RE.match(name)
        }
    )
    if not names:
        return {}
    if check_env is None:
        logger.warning(
            "skills: requires.env declared but no sandbox env probe configured"
        )
        return None
    try:
        return dict(await check_env(names))
    except Exception as exc:  # noqa: BLE001 — any probe failure degrades, never raises
        logger.warning("skills: env probe failed: %s", exc)
        return None


async def _run_availability_checks(
    candidates: list[tuple[str, SkillDocument]],
    *,
    run_check: RunAvailabilityCheck | None,
) -> dict[str, AvailabilityProbeOutcome]:
    """Run every eligible skill's declared availability check, concurrently.

    One in-sandbox exec per declaring skill (skills without a check cost
    nothing). A missing ``run_check`` seam yields ``ran=False`` outcomes so
    the predicate degrades those skills to could-not-verify.
    """
    declaring = [
        (name, document.availability.check)
        for name, document in candidates
        if document.availability is not None and document.availability.check is not None
    ]
    if not declaring:
        return {}
    if run_check is None:
        logger.warning(
            "skills: availability checks declared but no sandbox runner configured"
        )
        return {
            name: AvailabilityProbeOutcome(
                ran=False, failure="no sandbox configured to run availability checks"
            )
            for name, _ in declaring
        }

    async def _one(spec: SkillRunSpec) -> AvailabilityProbeOutcome:
        try:
            return await run_check(spec)
        except Exception as exc:  # noqa: BLE001 — the seam contract says no raise; belt and braces
            return AvailabilityProbeOutcome(ran=False, failure=f"{type(exc).__name__}: {exc}")

    outcomes = await asyncio.gather(*(_one(spec) for _, spec in declaring))
    results: dict[str, AvailabilityProbeOutcome] = {}
    for (name, _), outcome in zip(declaring, outcomes, strict=True):
        if not outcome.ran:
            logger.warning(
                "skills: availability check for %s did not run: %s", name, outcome.failure
            )
        results[name] = outcome
    return results


async def load_skill_registry(
    skills_dir: str | Path,
    *,
    check_bins: CheckBins | None,
    check_env: CheckEnv | None = None,
    run_check: RunAvailabilityCheck | None = None,
    baseline: tuple[str, ...] = BASELINE_BINS,
) -> SkillRegistry:
    """Scan the volume, probe the sandbox, and gate every skill.

    Never raises: a broken SKILL.md, an unreadable directory, or an
    unreachable sandbox all degrade to listed-ineligible skills (with the
    failure as the reason) so session assembly keeps working. With
    ``check_bins=None`` (no sandbox wired) only baseline-satisfied skills
    can be eligible — declared non-baseline bins are reported unverifiable.

    Availability (Johnny-trt.55) is the session-start snapshot: after
    eligibility, the loader runs at most one batched ``check_env`` probe
    (every ``requires.env`` name any eligible skill declares) plus the
    eligible skills' declared availability checks (concurrently via
    ``run_check``), and composes :func:`evaluate_skill_availability` per
    skill. With the seams unwired (``None``), skills declaring those
    dimensions are held back could-not-verify — never assumed available
    (delegate-into-failure is the failure mode this bead removes).
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

    verdicts: list[tuple[str, str, SkillDocument, bool, tuple[str, ...], tuple[str, ...]]] = []
    seen: set[str] = set()
    for name, skill_dir, document in parsed:
        if name in seen:
            duplicate = f"duplicate skill name {name!r} — an earlier directory already provides it"
            verdicts.append((name, skill_dir, document, False, (duplicate,), ()))
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
        verdicts.append((name, skill_dir, document, eligible, reasons, missing))

    # Availability probes (Johnny-trt.55), eligible skills only: one batched
    # env probe + the declared credential checks run concurrently. Probe
    # failures degrade to could-not-verify entries inside the predicate.
    env_present = await _probe_env(
        [document for _, _, document, eligible, _, _ in verdicts if eligible],
        check_env=check_env,
    )
    check_outcomes = await _run_availability_checks(
        [
            (name, document)
            for name, _, document, eligible, _, _ in verdicts
            if eligible
        ],
        run_check=run_check,
    )

    skills: list[LoadedSkill] = []
    for name, skill_dir, document, eligible, reasons, missing in verdicts:
        if eligible:
            available, unavailable_reason = evaluate_skill_availability(
                document,
                env_present=env_present,
                check_outcome=check_outcomes.get(name),
            )
            if not available:
                logger.info(
                    "skills: %s eligible but unavailable — %s", name, unavailable_reason
                )
        else:
            available, unavailable_reason = False, (reasons[0] if reasons else "")
        skills.append(
            LoadedSkill(
                name=name,
                directory=skill_dir,
                document=document,
                eligible=eligible,
                reasons=reasons,
                missing_bins=missing,
                available=available,
                unavailable_reason=unavailable_reason,
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
    "DEFAULT_AVAILABILITY_CHECK_TIMEOUT_S",
    "EMPTY_SKILL_REGISTRY",
    "AvailabilityProbeOutcome",
    "CheckBins",
    "CheckEnv",
    "LoadedSkill",
    "RunAvailabilityCheck",
    "SKILL_FILE_NAME",
    "SkillRegistry",
    "build_sandbox_availability_runner",
    "discover_skill_dirs",
    "evaluate_skill_availability",
    "evaluate_skill_eligibility",
    "load_skill_registry",
]
