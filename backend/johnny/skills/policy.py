"""Exec bin policy v1 for the ``sandbox.exec`` tool (Johnny-trt.23).

Which binaries a sandbox exec request may invoke. The openclaw
``DEFAULT_SAFE_BINS`` precedent (``src/infra/exec-safe-bin-policy-profiles.ts``)
applied to Johnny's topology:

* the **guaranteed baseline toolset** the skills-sandbox image ships
  (Johnny-trt.35; pinned by ``tests/integration/test_skills_sandbox.py``)
  is allowed by default — skills rely on these without declaring them;
* bins **declared by an eligible skill** (``requires.bins`` /
  ``requires.anyBins``) are allowed;
* anything else is **denied with an error naming the binary**.

The allow set is computed by exactly one function,
:func:`compute_allowed_bins` — and the Phase-6 configurable policy engine
(Johnny-trt.38, :mod:`johnny.skills.capability_policy`) layers on exactly
that seam: pass the session's :class:`ResolvedCapabilityPolicy` and the
operator-edited safe-bins baseline replaces :data:`BASELINE_BINS`, with
policy-denied bins (layered ``bins_deny`` globs + baseline removals)
filtered out of the final set — removals beat skill ``requires.bins``
grants. :class:`ExecBinPolicy` optionally carries the same policy's
``check_bin`` so denials are ATTRIBUTED (:class:`ExecDenial` names the
denying layer for the ``policy_denied`` event). The trt.37 management UI
renders the same set the executor enforces (:attr:`ExecBinPolicy.allowed`
is plain data).

Honest scope note: the *security* boundary is the sandbox container itself
(non-root, resource-limited, no host mounts — Johnny-trt.35). This policy is
the visibility / least-surprise layer on top: it keeps the executor and the
future LLM engine from reaching for tools nobody declared, and it makes the
reachable surface inspectable. Shell strings are screened with a conservative
scanner (substitution is rejected outright); a skill's own scripts run under
baseline ``bash`` by design — declaring the script's *interesting* bins in
``requires.bins`` is what surfaces them here.

**Per-bin profile extension point (documented, deliberately unbuilt)**:
openclaw's per-flag safe-bin profiles (grep-stdin-only style) stay OUT of
scope — the sandbox container is the security boundary, so bin-level + glob
control suffices (the Johnny-trt.38 decision). If a future bead revisits
that, the hook is :meth:`ExecBinPolicy._denial` / the ``policy_check``
callable: a per-bin profile engine would return a structured verdict for an
*allowed-but-constrained* bin there (argv inspection), without reshaping
:meth:`check_argv` / :meth:`check_cmd` callers or the exec wire format.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from posixpath import basename
from typing import TYPE_CHECKING, Any

from johnny.skills.frontmatter import SkillRequirements

if TYPE_CHECKING:
    from johnny.skills.capability_policy import ResolvedCapabilityPolicy

BASELINE_BINS: tuple[str, ...] = (
    "bash",
    # coreutils
    "cat",
    "cut",
    "head",
    "tail",
    "tr",
    "sort",
    "uniq",
    "wc",
    # text / search
    "grep",
    "sed",
    "awk",
    "gawk",
    "rg",
    # findutils + archives
    "find",
    "xargs",
    "tar",
    "gzip",
    # network / data
    "curl",
    "jq",
    # dev
    "git",
    "python3",
    # reference Google CLI (shipped by the sandbox image, Johnny-trt.35)
    "gog",
)
"""The guaranteed sandbox baseline toolset (Johnny-trt.35 operator
requirement). Mirrors ``sandbox/README.md`` and the integration test's
``BASELINE_BINS`` contract; skills may rely on every bin here without
declaring it in ``requires.bins``, and the loader treats them as implicitly
satisfied."""

SHELL_UTILITY_BINS: tuple[str, ...] = (
    "sh",
    "echo",
    "printf",
    "true",
    "false",
    "test",
    "ls",
    "pwd",
    "id",
    "env",
    "date",
    "sleep",
    "mkdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "ln",
    "stat",
    "dirname",
    "basename",
    "which",
    "tee",
    "diff",
    "gunzip",
    "sha256sum",
    "md5sum",
)
"""Everyday utilities allowed alongside the baseline so the exec tool stays
usable (``echo`` round-trips, ``ls`` listings, temp-file plumbing). Present in
any Debian base image but deliberately *not* part of the documented baseline
guarantee — the guarantee stays the curated trt.35 list."""

_SHELL_KEYWORDS: frozenset[str] = frozenset(
    {
        "if", "then", "else", "elif", "fi",
        "for", "while", "until", "do", "done",
        "case", "esac", "in", "function", "select", "time",
        "!", "{", "}", "(", ")", "[[", "]]", "[", "]",
    }
)
"""bash reserved words that may open a command position in a shell string."""

_SHELL_BUILTINS: frozenset[str] = frozenset(
    {
        "cd", "export", "set", "unset", "read", "shift", "local", "return",
        "break", "continue", "exit", "wait", "trap", "source", ".", ":",
        "command", "type", "ulimit", "umask", "alias", "unalias",
    }
)
"""bash builtins with no binary behind them — never policy-relevant."""

_SUBSTITUTION_MARKERS: tuple[str, ...] = ("$(", "`", "<(", ">(")
"""Constructs the conservative shell scanner refuses to reason about."""

_SEGMENT_SEPARATORS: frozenset[str] = frozenset({"|", "||", "&&", ";", ";;", "&", "\n"})

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
"""A leading ``VAR=value`` (or ``VAR+=value``) environment assignment."""

_REDIRECTION_RE = re.compile(r"^[0-9]*[<>]|^&>")
"""A redirection token (``>out``, ``2>err``, ``<in``, ``&>both`` …)."""


def compute_allowed_bins(
    eligible_requirements: Iterable[SkillRequirements] = (),
    *,
    baseline: tuple[str, ...] = BASELINE_BINS,
    extras: tuple[str, ...] = SHELL_UTILITY_BINS,
    policy: ResolvedCapabilityPolicy | None = None,
) -> frozenset[str]:
    """THE allow-set computation (exec bin policy v1 + the trt.38 layer).

    Baseline toolset + everyday shell utilities + every bin declared by an
    *eligible* skill (``requires.bins`` and ``requires.anyBins`` members — an
    any-bins alternative that is absent simply fails with exit 127 at run
    time). Keep all policy composition here: Johnny-trt.38 layers its
    configurable engine on this one seam, and the trt.37 UI renders its
    output.

    With ``policy`` (Johnny-trt.38): the operator-edited safe-bins list
    replaces ``baseline``, and the final set is filtered through
    ``policy.check_bin`` — layered ``bins_deny`` globs and removed-baseline
    bins drop out, REGARDLESS of who granted them (a removal beats a skill's
    ``requires.bins`` grant; the trt.38 acceptance contract). Callers owning
    per-kind enforcement should also pre-filter ``eligible_requirements`` to
    policy-allowed skills so a denied skill's grants never enter the union.
    """
    if policy is not None:
        baseline = tuple(policy.safe_bins)
    allowed = set(baseline) | set(extras)
    for requires in eligible_requirements:
        allowed.update(requires.bins)
        allowed.update(requires.any_bins)
    if policy is not None:
        allowed = {name for name in allowed if policy.check_bin(name).allowed}
    return frozenset(allowed)


@dataclass(frozen=True, slots=True)
class ExecDenial:
    """One structured exec denial: the message plus trt.38 policy attribution.

    ``policy_layer`` is non-empty only when the configurable capability
    policy made the call (then ``policy_rule`` / ``policy_detail`` carry the
    matching pattern and the deciding layer's target) — the
    ``policy_denied`` observability event reads exactly these fields.
    ``bin`` is the offending binary's basename.
    """

    message: str
    bin: str = ""
    policy_layer: str = ""
    policy_rule: str = ""
    policy_detail: str = ""


@dataclass(frozen=True, slots=True)
class ExecBinPolicy:
    """Membership check for exec requests against a computed allow set.

    ``check_*`` methods return ``None`` when the request is allowed, or a
    human-readable denial naming the offending binary (the Johnny-trt.23
    acceptance contract); the ``*_detailed`` variants return the same
    verdict as a structured :class:`ExecDenial` carrying trt.38 policy
    attribution. :attr:`allowed` is plain inspectable data for the Phase-6
    management UI.

    ``policy_check`` (Johnny-trt.38) is the resolved capability policy's
    ``check_bin`` — consulted only to ATTRIBUTE a denial (name the denying
    layer); membership in :attr:`allowed` stays the one allow decision
    (compute the set with :func:`compute_allowed_bins` and the same policy
    so the two can never disagree). This callable is also the documented
    per-bin profile extension point (module docstring).
    """

    allowed: frozenset[str]
    policy_check: Callable[[str], Any] | None = None
    allow_all: bool = False

    @classmethod
    def permit_all(cls) -> ExecBinPolicy:
        """Allow ANY binary and ANY shell construct — full sandbox-container
        access (Johnny-3ow, ``JOHNNY_SANDBOX_FULL_ACCESS``). The container
        itself is the security boundary (non-root, resource-limited, no host
        mounts, per-workspace), so this visibility layer collapses to
        "everything is reachable"; command substitution is permitted too.
        :attr:`allowed` is left empty because membership is short-circuited —
        the daemon's own timeout ceiling + output caps still bound every call,
        and the trace sink still records every command for the audit trail."""
        return cls(allowed=frozenset(), allow_all=True)

    def check_argv_detailed(self, argv: Iterable[str]) -> ExecDenial | None:
        """Vet an argv-form exec: the executable is ``argv[0]``'s basename."""
        if self.allow_all:
            return None
        head = next(iter(argv), "")
        name = basename(head.strip()) if head.strip() else ""
        if not name:
            return ExecDenial(message="exec request has an empty command")
        if name in self.allowed:
            return None
        return self._denial(name)

    def check_argv(self, argv: Iterable[str]) -> str | None:
        """String-form :meth:`check_argv_detailed` (the trt.23 contract)."""
        denial = self.check_argv_detailed(argv)
        return denial.message if denial is not None else None

    def check_cmd_detailed(self, cmd: str) -> ExecDenial | None:
        """Conservatively vet a shell-string exec.

        Command substitution / process substitution is refused outright (the
        scanner will not chase what they expand to — use argv form or a skill
        script). Otherwise the string is tokenized, split into pipeline /
        list segments, and the first word of each segment — skipping
        ``VAR=value`` assignments, redirections, shell keywords and builtins —
        must be in the allow set.
        """
        if self.allow_all:
            return None
        for marker in _SUBSTITUTION_MARKERS:
            if marker in cmd:
                return ExecDenial(
                    message=(
                        f"shell substitution ({marker!r}) is not allowed in sandbox.exec "
                        "v1 — use argv form or ship the logic as a skill script"
                    )
                )
        if "\n" in cmd:
            # The lexer treats newlines as whitespace, so a second line's
            # command would escape command-position checking. Multi-line
            # shell belongs in a skill script.
            return ExecDenial(
                message=(
                    "multi-line shell strings are not allowed in sandbox.exec v1 — "
                    "use ';' separators, argv form, or ship the logic as a skill script"
                )
            )
        # shlex with punctuation_chars groups operator runs (|, &&, ;, …) into
        # their own tokens even when not whitespace-separated ("a|b" → a, |, b)
        # while quoting still protects literals ("grep 'a|b'" stays one word).
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError as exc:
            return ExecDenial(
                message=f"could not parse shell command for the bin policy: {exc}"
            )

        expect_command = True
        for token in tokens:
            if token in _SEGMENT_SEPARATORS:
                expect_command = True
                continue
            if not expect_command:
                continue
            if token in _SHELL_KEYWORDS:
                continue  # keyword may still be followed by the real command
            if _ASSIGNMENT_RE.match(token):
                continue  # leading VAR=value assignment
            if _REDIRECTION_RE.match(token):
                continue  # redirection glued to the command position
            if token.startswith("-") or token == "{}":
                # An option/placeholder cannot be a binary; seen in command
                # position only via constructs like `find -exec … \; -print`.
                continue
            expect_command = False
            if token in _SHELL_BUILTINS:
                continue
            name = basename(token)
            if name not in self.allowed:
                return self._denial(name)
        return None

    def check_cmd(self, cmd: str) -> str | None:
        """String-form :meth:`check_cmd_detailed` (the trt.23 contract)."""
        denial = self.check_cmd_detailed(cmd)
        return denial.message if denial is not None else None

    def check_detailed(
        self, *, argv: Iterable[str] | None = None, cmd: str | None = None
    ) -> ExecDenial | None:
        """Vet whichever form the exec request uses (exactly one expected)."""
        if argv is not None:
            return self.check_argv_detailed(argv)
        if cmd is not None:
            return self.check_cmd_detailed(cmd)
        return ExecDenial(message="exec request carries neither argv nor cmd")

    def check(self, *, argv: Iterable[str] | None = None, cmd: str | None = None) -> str | None:
        """String-form :meth:`check_detailed` (the trt.23 contract)."""
        denial = self.check_detailed(argv=argv, cmd=cmd)
        return denial.message if denial is not None else None

    def _denial(self, name: str) -> ExecDenial:
        """Build the denial for one not-allowed binary, policy-attributed when possible.

        The ``policy_check`` consult is attribution-only: when the trt.38
        policy objects to the bin (a ``bins_deny`` glob or a removed
        baseline bin), the denial names the denying layer — the message the
        executor speaks from and the fields the ``policy_denied`` event
        carries. A bin the policy does not object to fell out of
        :attr:`allowed` for the v1 reason (nobody granted it), so the
        original trt.23 copy stands.
        """
        if self.policy_check is not None:
            try:
                decision = self.policy_check(name)
            except Exception:  # noqa: BLE001 — attribution must never break a denial
                decision = None
            if decision is not None and not getattr(decision, "allowed", True):
                layer = str(getattr(decision, "layer", "") or "")
                rule = str(getattr(decision, "rule", "") or "")
                detail = str(getattr(decision, "detail", "") or "")
                return ExecDenial(
                    message=(
                        f"binary {name!r} is blocked by the capability policy "
                        f"(denied at the {layer or 'configured'} layer"
                        + (f", rule {rule!r}" if rule else "")
                        + ")"
                    ),
                    bin=name,
                    policy_layer=layer,
                    policy_rule=rule,
                    policy_detail=detail,
                )
        return ExecDenial(
            message=(
                f"binary {name!r} is not allowed by the sandbox exec policy "
                "(baseline toolset + bins declared by eligible skills); declare it "
                "in the skill's requires.bins or install it in the sandbox image"
            ),
            bin=name,
        )


def build_policy(
    eligible_requirements: Iterable[SkillRequirements] = (),
) -> ExecBinPolicy:
    """Convenience: compute the v1 allow set and wrap it in a policy."""
    return ExecBinPolicy(allowed=compute_allowed_bins(eligible_requirements))


__all__ = [
    "BASELINE_BINS",
    "SHELL_UTILITY_BINS",
    "ExecBinPolicy",
    "ExecDenial",
    "build_policy",
    "compute_allowed_bins",
]
