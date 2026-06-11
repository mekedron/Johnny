"""Frontmatter parse matrix for SKILL.md (Johnny-trt.23).

Covers the acceptance matrix — valid documents (both metadata shapes),
missing required fields, malformed metadata — plus the openclaw
wire-compat details: multi-line flow-mapping metadata, ``anyBins``
spellings, the legacy ``clawdbot`` manifest key, and Johnny's additive
``metadata.johnny`` namespace.
"""

from __future__ import annotations

from johnny.skills.frontmatter import SkillRunSpec, parse_skill_markdown

OPENCLAW_STYLE = """---
name: himalaya
description: "Himalaya CLI for IMAP/SMTP mail: list, read, search."
homepage: https://github.com/pimalaya/himalaya
metadata:
  {
    "openclaw":
      {
        "emoji": "📧",
        "requires": { "bins": ["himalaya"] },
        "install":
          [{ "id": "brew", "kind": "brew", "formula": "himalaya" }],
      },
  }
---

Use the himalaya CLI.
"""

JOHNNY_STYLE = """---
name: google-calendar
description: "Look up upcoming events on the connected Google calendar."
metadata:
  {
    "openclaw": { "requires": { "bins": ["gog"] } },
    "johnny":
      {
        "run": { "argv": ["bash", "/skills/google-calendar/run.sh"], "timeout_s": 60 },
        "keywords": ["calendar", "schedule"],
      },
  }
---

Body instructions.
"""


def test_openclaw_multiline_flow_metadata_parses() -> None:
    doc = parse_skill_markdown(OPENCLAW_STYLE)
    assert doc.problems == ()
    assert doc.name == "himalaya"
    assert doc.description.startswith("Himalaya CLI")
    assert doc.homepage == "https://github.com/pimalaya/himalaya"
    assert doc.requires.bins == ("himalaya",)
    assert doc.run is None  # no johnny namespace — discoverable, not runnable
    assert doc.manifest["emoji"] == "📧"  # uninterpreted fields carried raw
    assert "himalaya CLI" in doc.body


def test_johnny_namespace_run_and_keywords() -> None:
    doc = parse_skill_markdown(JOHNNY_STYLE)
    assert doc.problems == ()
    assert doc.requires.bins == ("gog",)
    assert doc.run == SkillRunSpec(
        argv=("bash", "/skills/google-calendar/run.sh"), timeout_s=60.0
    )
    assert doc.keywords == ("calendar", "schedule")


def test_single_line_json_string_metadata() -> None:
    text = (
        "---\n"
        "name: x\n"
        "description: d\n"
        'metadata: \'{"openclaw": {"requires": {"bins": ["jq"]}}}\'\n'
        "---\nbody\n"
    )
    doc = parse_skill_markdown(text)
    assert doc.problems == ()
    assert doc.requires.bins == ("jq",)


def test_missing_name_is_a_problem_not_a_crash() -> None:
    doc = parse_skill_markdown("---\ndescription: d\n---\nbody\n")
    assert doc.name == ""
    assert any("'name'" in problem for problem in doc.problems)


def test_missing_description_is_a_problem() -> None:
    doc = parse_skill_markdown("---\nname: x\n---\nbody\n")
    assert any("'description'" in problem for problem in doc.problems)


def test_malformed_metadata_json_string() -> None:
    doc = parse_skill_markdown(
        "---\nname: x\ndescription: d\nmetadata: '{not json'\n---\nbody\n"
    )
    assert any("not valid JSON" in problem for problem in doc.problems)
    assert doc.requires.bins == ()


def test_metadata_wrong_type() -> None:
    doc = parse_skill_markdown("---\nname: x\ndescription: d\nmetadata: 7\n---\nbody\n")
    assert any("metadata must be" in problem for problem in doc.problems)


def test_no_frontmatter_block() -> None:
    doc = parse_skill_markdown("# just markdown\n")
    assert doc.name == ""
    assert any("no frontmatter" in problem for problem in doc.problems)
    assert doc.body == "# just markdown\n"


def test_unterminated_frontmatter_is_no_frontmatter() -> None:
    doc = parse_skill_markdown("---\nname: x\ndescription: d\nbody without closing\n")
    assert any("no frontmatter" in problem for problem in doc.problems)


def test_invalid_yaml_frontmatter() -> None:
    doc = parse_skill_markdown("---\nname: [unclosed\n---\nbody\n")
    assert any("not valid YAML" in problem for problem in doc.problems)


def test_non_mapping_frontmatter() -> None:
    doc = parse_skill_markdown("---\n- a list\n---\nbody\n")
    assert doc.problems == ("frontmatter must be a YAML mapping",)


def test_requires_accepts_comma_separated_loose_string() -> None:
    text = (
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"bins": "jq, gog"}}}\n'
        "---\nbody\n"
    )
    doc = parse_skill_markdown(text)
    assert doc.requires.bins == ("jq", "gog")


def test_any_bins_both_spellings() -> None:
    camel = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"anyBins": ["a", "b"]}}}\n'
        "---\n"
    )
    snake = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"any_bins": ["a", "b"]}}}\n'
        "---\n"
    )
    assert camel.requires.any_bins == ("a", "b")
    assert snake.requires.any_bins == ("a", "b")


def test_legacy_clawdbot_manifest_key_still_read() -> None:
    doc = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"clawdbot": {"requires": {"bins": ["old"]}, "os": ["Darwin"]}}\n'
        "---\n"
    )
    assert doc.requires.bins == ("old",)
    assert doc.os == ("darwin",)  # normalized lowercase


def test_run_spec_validation_matrix() -> None:
    base = "---\nname: x\ndescription: d\nmetadata: {{\"johnny\": {{\"run\": {run}}}}}\n---\n"
    empty_argv = parse_skill_markdown(base.format(run='{"argv": []}'))
    assert empty_argv.run is None
    assert any("argv" in problem for problem in empty_argv.problems)

    non_string = parse_skill_markdown(base.format(run='{"argv": ["a", 3]}'))
    assert non_string.run is None

    bad_timeout = parse_skill_markdown(
        base.format(run='{"argv": ["a"], "timeout_s": -5}')
    )
    assert bad_timeout.run == SkillRunSpec(argv=("a",), timeout_s=None)
    assert any("timeout_s" in problem for problem in bad_timeout.problems)

    not_mapping = parse_skill_markdown(base.format(run='"a string"'))
    assert not_mapping.run is None
    assert any("johnny.run must be a mapping" in problem for problem in not_mapping.problems)


def test_env_and_config_requirements_carried_for_trt55() -> None:
    doc = parse_skill_markdown(
        "---\nname: x\ndescription: d\n"
        'metadata: {"openclaw": {"requires": {"env": ["API_KEY"], "config": ["~/.x"]}}}\n'
        "---\n"
    )
    assert doc.requires.env == ("API_KEY",)
    assert doc.requires.config == ("~/.x",)
    assert doc.problems == ()
