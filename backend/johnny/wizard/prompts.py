"""Interactive prompt helpers built on Rich.

A thin abstraction over :mod:`rich.prompt` so the steps module can render
the same flow whether or not the user attached a TTY. In
``--non-interactive`` mode the wizard substitutes a
:class:`NonInteractivePrompter` that reads answers from a YAML file —
both implementations share the :class:`Prompter` protocol below.

Keeping prompting behind a protocol also makes the step functions
trivially testable: tests pass a stub that returns canned answers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rich.console import Console
from rich.prompt import Confirm, Prompt


class Prompter(Protocol):
    """Minimal interface for asking the user questions."""

    def ask_text(self, question: str, default: str | None = None) -> str: ...

    def ask_secret(self, question: str) -> str: ...

    def ask_confirm(self, question: str, default: bool = True) -> bool: ...

    def ask_choice(
        self,
        question: str,
        options: Sequence[str],
        *,
        default_index: int = 0,
    ) -> int: ...


class RichPrompter:
    """Interactive prompts rendered via Rich on the given console."""

    def __init__(self, console: Console) -> None:
        self._console = console

    def ask_text(self, question: str, default: str | None = None) -> str:
        value = Prompt.ask(question, console=self._console, default=default or "")
        return value

    def ask_secret(self, question: str) -> str:
        return Prompt.ask(question, console=self._console, password=True)

    def ask_confirm(self, question: str, default: bool = True) -> bool:
        return Confirm.ask(question, console=self._console, default=default)

    def ask_choice(
        self,
        question: str,
        options: Sequence[str],
        *,
        default_index: int = 0,
    ) -> int:
        # Render a numbered menu, then ask for an index. We do not use
        # rich's `choices=` because the option labels themselves contain
        # spaces and parentheses that make typing them error-prone.
        self._console.print(f"[bold]{question}[/bold]")
        for index, label in enumerate(options, start=1):
            marker = "*" if (index - 1) == default_index else " "
            self._console.print(f"  [cyan]{index}[/cyan] {marker} {label}")
        while True:
            raw = Prompt.ask(
                "Choice",
                console=self._console,
                default=str(default_index + 1),
            )
            try:
                value = int(raw)
            except ValueError:
                self._console.print("[red]Enter a number from the list above.[/red]")
                continue
            if 1 <= value <= len(options):
                return value - 1
            self._console.print(f"[red]Choice must be between 1 and {len(options)}.[/red]")


class NonInteractivePrompter:
    """Reads answers from a flat dict (typically loaded from YAML).

    Each prompt has a key that the caller supplies (see :mod:`steps`).
    When a key is missing, falls back to the supplied default — or
    raises :class:`KeyError` if there is none. This lets us write a
    fully scripted setup file for CI / unattended runs while still
    enforcing that every answer is explicit when no default exists.
    """

    def __init__(self, answers: dict[str, str | bool | int]) -> None:
        self._answers = answers
        self._asked: list[str] = []
        # Bound to a specific question key set later by the steps.
        self._current_key: str | None = None

    # The protocol does not include the key, but the wizard's steps
    # call ``set_key`` before each prompt to scope answers.

    def set_key(self, key: str) -> None:
        self._current_key = key

    def _resolve(self, default: str | bool | int | None) -> str | bool | int:
        if self._current_key is None:
            raise RuntimeError("NonInteractivePrompter.set_key must be called before each prompt")
        self._asked.append(self._current_key)
        key = self._current_key
        self._current_key = None
        if key in self._answers:
            return self._answers[key]
        if default is None:
            raise KeyError(
                f"non-interactive mode requires {key!r} in the answers file (no default)"
            )
        return default

    def ask_text(self, question: str, default: str | None = None) -> str:
        value = self._resolve(default)
        return str(value)

    def ask_secret(self, question: str) -> str:
        value = self._resolve(None)
        return str(value)

    def ask_confirm(self, question: str, default: bool = True) -> bool:
        value = self._resolve(default)
        if isinstance(value, str):
            return value.strip().lower() in {"y", "yes", "true", "1", "on"}
        return bool(value)

    def ask_choice(
        self,
        question: str,
        options: Sequence[str],
        *,
        default_index: int = 0,
    ) -> int:
        value = self._resolve(default_index)
        if isinstance(value, int):
            index = value
        else:
            text = str(value).strip()
            try:
                index = int(text)
            except ValueError:
                # Fallback: substring match against the option labels.
                for i, label in enumerate(options):
                    if text and text in label:
                        index = i
                        break
                else:
                    raise KeyError(
                        f"non-interactive answer for choice does not match any option: {text!r}"
                    )
        if not (0 <= index < len(options)):
            raise KeyError(
                f"non-interactive choice index {index} out of range for {len(options)} options"
            )
        return index

    @property
    def asked_keys(self) -> list[str]:
        return list(self._asked)


__all__ = ["NonInteractivePrompter", "Prompter", "RichPrompter"]
