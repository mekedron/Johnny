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
:func:`compute_allowed_bins`, so the Phase-6 configurable policy engine
(Johnny-trt.38: layered allow/deny, editable safe-bins, per-agent scope) can
replace or wrap that single seam without reshaping callers — and so the
Phase-6 management UI (Johnny-trt.37) can render the same set the executor
enforces (:attr:`ExecBinPolicy.allowed` is plain data).

Honest scope note: the *security* boundary is the sandbox container itself
(non-root, resource-limited, no host mounts — Johnny-trt.35). This policy is
the visibility / least-surprise layer on top: it keeps the executor and the
future LLM engine from reaching for tools nobody declared, and it makes the
reachable surface inspectable. Shell strings are screened with a conservative
scanner (substitution is rejected outright); a skill's own scripts run under
baseline ``bash`` by design — declaring the script's *interesting* bins in
``requires.bins`` is what surfaces them here. openclaw's per-flag safe-bin
profiles (grep-stdin-only style) are a documented later extension, not v1.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from posixpath import basename

from johnny.skills.frontmatter import SkillRequirements

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
) -> frozenset[str]:
    """THE allow-set computation (exec bin policy v1).

    Baseline toolset + everyday shell utilities + every bin declared by an
    *eligible* skill (``requires.bins`` and ``requires.anyBins`` members — an
    any-bins alternative that is absent simply fails with exit 127 at run
    time). Keep all policy composition here: Johnny-trt.38 layers its
    configurable engine on this one seam, and the trt.37 UI renders its
    output.
    """
    allowed = set(baseline) | set(extras)
    for requires in eligible_requirements:
        allowed.update(requires.bins)
        allowed.update(requires.any_bins)
    return frozenset(allowed)


@dataclass(frozen=True, slots=True)
class ExecBinPolicy:
    """Membership check for exec requests against a computed allow set.

    ``check_*`` methods return ``None`` when the request is allowed, or a
    human-readable denial naming the offending binary (the Johnny-trt.23
    acceptance contract). :attr:`allowed` is plain inspectable data for the
    Phase-6 management UI.
    """

    allowed: frozenset[str]

    def check_argv(self, argv: Iterable[str]) -> str | None:
        """Vet an argv-form exec: the executable is ``argv[0]``'s basename."""
        head = next(iter(argv), "")
        name = basename(head.strip()) if head.strip() else ""
        if not name:
            return "exec request has an empty command"
        if name in self.allowed:
            return None
        return self._denial(name)

    def check_cmd(self, cmd: str) -> str | None:
        """Conservatively vet a shell-string exec.

        Command substitution / process substitution is refused outright (the
        scanner will not chase what they expand to — use argv form or a skill
        script). Otherwise the string is tokenized, split into pipeline /
        list segments, and the first word of each segment — skipping
        ``VAR=value`` assignments, redirections, shell keywords and builtins —
        must be in the allow set.
        """
        for marker in _SUBSTITUTION_MARKERS:
            if marker in cmd:
                return (
                    f"shell substitution ({marker!r}) is not allowed in sandbox.exec "
                    "v1 — use argv form or ship the logic as a skill script"
                )
        if "\n" in cmd:
            # The lexer treats newlines as whitespace, so a second line's
            # command would escape command-position checking. Multi-line
            # shell belongs in a skill script.
            return (
                "multi-line shell strings are not allowed in sandbox.exec v1 — "
                "use ';' separators, argv form, or ship the logic as a skill script"
            )
        # shlex with punctuation_chars groups operator runs (|, &&, ;, …) into
        # their own tokens even when not whitespace-separated ("a|b" → a, |, b)
        # while quoting still protects literals ("grep 'a|b'" stays one word).
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError as exc:
            return f"could not parse shell command for the bin policy: {exc}"

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

    def check(self, *, argv: Iterable[str] | None = None, cmd: str | None = None) -> str | None:
        """Vet whichever form the exec request uses (exactly one expected)."""
        if argv is not None:
            return self.check_argv(argv)
        if cmd is not None:
            return self.check_cmd(cmd)
        return "exec request carries neither argv nor cmd"

    def _denial(self, name: str) -> str:
        return (
            f"binary {name!r} is not allowed by the sandbox exec policy "
            "(baseline toolset + bins declared by eligible skills); declare it "
            "in the skill's requires.bins or install it in the sandbox image"
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
    "build_policy",
    "compute_allowed_bins",
]
