"""Tests for the wizard's prompt helpers."""

from __future__ import annotations

import pytest

from johnny.wizard.prompts import NonInteractivePrompter


def test_non_interactive_text_returns_canned_answer() -> None:
    p = NonInteractivePrompter({"name": "Alice"})
    p.set_key("name")
    assert p.ask_text("Your name?") == "Alice"


def test_non_interactive_text_falls_back_to_default() -> None:
    p = NonInteractivePrompter({})
    p.set_key("name")
    assert p.ask_text("Your name?", default="anon") == "anon"


def test_non_interactive_text_no_default_raises() -> None:
    p = NonInteractivePrompter({})
    p.set_key("name")
    with pytest.raises(KeyError):
        p.ask_text("Your name?")


def test_non_interactive_secret_requires_explicit_value() -> None:
    p = NonInteractivePrompter({"api_key": "sk-test"})
    p.set_key("api_key")
    assert p.ask_secret("API key?") == "sk-test"


def test_non_interactive_confirm_parses_bool_strings() -> None:
    cases = {"yes": True, "no": False, "true": True, "0": False, "on": True}
    for value, expected in cases.items():
        p = NonInteractivePrompter({"k": value})
        p.set_key("k")
        assert p.ask_confirm("?", default=True) is expected, value


def test_non_interactive_confirm_accepts_native_bool() -> None:
    p = NonInteractivePrompter({"k": True})
    p.set_key("k")
    assert p.ask_confirm("?", default=False) is True


def test_non_interactive_choice_by_index() -> None:
    p = NonInteractivePrompter({"k": 2})
    p.set_key("k")
    assert p.ask_choice("Pick", ["a", "b", "c"], default_index=0) == 2


def test_non_interactive_choice_by_substring_match() -> None:
    p = NonInteractivePrompter({"k": "banana"})
    p.set_key("k")
    options = ["apple", "banana split", "cherry"]
    assert p.ask_choice("Pick", options) == 1


def test_non_interactive_choice_out_of_range_raises() -> None:
    p = NonInteractivePrompter({"k": 5})
    p.set_key("k")
    with pytest.raises(KeyError):
        p.ask_choice("Pick", ["a", "b"])


def test_non_interactive_choice_unknown_substring_raises() -> None:
    p = NonInteractivePrompter({"k": "zzz"})
    p.set_key("k")
    with pytest.raises(KeyError):
        p.ask_choice("Pick", ["a", "b"])


def test_non_interactive_records_asked_keys() -> None:
    p = NonInteractivePrompter({"foo": "x", "bar": "y"})
    p.set_key("foo")
    p.ask_text("?")
    p.set_key("bar")
    p.ask_text("?")
    assert p.asked_keys == ["foo", "bar"]


def test_non_interactive_set_key_required_before_each_prompt() -> None:
    p = NonInteractivePrompter({"foo": "x"})
    with pytest.raises(RuntimeError):
        p.ask_text("?")


def test_non_interactive_set_key_consumed_after_prompt() -> None:
    p = NonInteractivePrompter({"foo": "x"})
    p.set_key("foo")
    p.ask_text("?")
    # Second prompt without set_key must fail.
    with pytest.raises(RuntimeError):
        p.ask_text("?")
