"""Unit matrix for the heuristic complexity pre-scorer (Johnny-trt.50).

Covers the bead's test acceptance: per-dimension scoring incl. RU/FI samples,
the tier-boundary + sigmoid-confidence math, the >=2-reasoning-marker
override, the ambiguous → safe-default-tier resolution, the dynamic
task-catalog dimension (the delegate prior), and the persisted 4-key shadow
payload. Pure stdlib — no livekit import, so this file runs without the
``agent`` extra.
"""

from __future__ import annotations

import json
import math

import pytest

from johnny.agent.complexity import (
    COMPLEX_TIER,
    DIMENSION_WEIGHTS,
    MAX_TOP_SIGNALS,
    MEDIUM_TIER,
    REASONING_TIER,
    SIMPLE_TIER,
    ComplexityConfig,
    _calibrate_confidence,
    _tier_for,
    score_complexity,
)
from johnny.agent.task_catalog import STUB_TASK_CATALOG, TaskCatalogEntry

CFG = ComplexityConfig()


def _dimension(verdict, name: str):
    return next(d for d in verdict.dimensions if d.name == name)


# --------------------------------------------------------------------------- #
# Calibration math                                                            #
# --------------------------------------------------------------------------- #


def test_dimension_weights_sum_to_one() -> None:
    assert math.isclose(sum(DIMENSION_WEIGHTS.values()), 1.0, abs_tol=1e-9)


def test_sigmoid_confidence_calibration() -> None:
    """rules.ts ``calibrateConfidence``: 1 / (1 + e^(-steepness * distance))."""
    assert _calibrate_confidence(0.0, 12.0) == pytest.approx(0.5)
    assert _calibrate_confidence(0.1, 12.0) == pytest.approx(0.7685, abs=1e-4)
    assert _calibrate_confidence(1.0, 12.0) > 0.99
    # Monotonic in distance.
    assert _calibrate_confidence(0.05, 12.0) < _calibrate_confidence(0.2, 12.0)


def test_tier_boundaries_and_distances() -> None:
    """SIMPLE < 0.0 <= MEDIUM < 0.3 <= COMPLEX < 0.5 <= REASONING (config.ts)."""
    assert _tier_for(-0.01, CFG) == (SIMPLE_TIER, pytest.approx(0.01))
    assert _tier_for(0.0, CFG)[0] == MEDIUM_TIER  # boundary belongs to the upper tier
    assert _tier_for(0.299, CFG)[0] == MEDIUM_TIER
    assert _tier_for(0.3, CFG)[0] == COMPLEX_TIER
    assert _tier_for(0.499, CFG)[0] == COMPLEX_TIER
    assert _tier_for(0.5, CFG)[0] == REASONING_TIER
    # Middle tiers measure distance to the NEARER of their two boundaries.
    assert _tier_for(0.35, CFG) == (COMPLEX_TIER, pytest.approx(0.05))
    assert _tier_for(0.1, CFG) == (MEDIUM_TIER, pytest.approx(0.1))
    # Edge tiers measure distance to their single boundary.
    assert _tier_for(0.8, CFG) == (REASONING_TIER, pytest.approx(0.3))


# --------------------------------------------------------------------------- #
# Simple indicators (negative) — EN / RU / FI                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "hello there, thanks!",
        "привет, как дела?",
        "moi, mitä kuuluu?",
    ],
)
def test_greetings_score_simple_with_confidence(text: str) -> None:
    verdict = score_complexity(text)
    assert verdict.tier == SIMPLE_TIER
    assert not verdict.ambiguous
    assert verdict.score < 0
    assert verdict.confidence >= 0.9
    assert _dimension(verdict, "simple_indicators").score == -1.0
    assert any(s.startswith("simple") for s in verdict.signals)


# --------------------------------------------------------------------------- #
# Reasoning override — EN / RU / FI                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "walk me through step by step why the deploy failed",
        "объясни почему упал сервис и разложи по полочкам",
        "perustele vaihe vaiheelta miksi näin kävi",
    ],
)
def test_two_reasoning_markers_override_to_reasoning(text: str) -> None:
    verdict = score_complexity(text)
    assert verdict.tier == REASONING_TIER
    assert verdict.reasoning_override
    assert not verdict.ambiguous
    assert verdict.confidence >= 0.85  # the override floor (rules.ts)


def test_single_reasoning_marker_does_not_override() -> None:
    verdict = score_complexity("prove the budget adds up")
    assert not verdict.reasoning_override
    assert verdict.tier != REASONING_TIER
    assert _dimension(verdict, "reasoning_markers").score == pytest.approx(0.7)


# --------------------------------------------------------------------------- #
# Catalog dimension — the dynamic delegate prior                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "check my calendar, what meetings do I have tomorrow",
        "проверь календарь, какие встречи завтра",
        "tarkista kalenteri, mitä kokouksia on huomenna",
    ],
)
def test_stub_catalog_delegate_ask_lands_confident_medium(text: str) -> None:
    """Two catalog hits + an imperative ≈ the canonical delegate shape → MEDIUM."""
    verdict = score_complexity(text, catalog=STUB_TASK_CATALOG)
    assert verdict.tier == MEDIUM_TIER
    assert not verdict.ambiguous
    catalog_dim = _dimension(verdict, "catalog_match")
    assert catalog_dim.score == 1.0
    assert catalog_dim.signal is not None
    assert "calendar.upcoming_events" in catalog_dim.signal


def test_single_catalog_hit_is_ambiguous_medium() -> None:
    """One catalog keyword + one verb sits near the SIMPLE/MEDIUM boundary —
    recorded ambiguous with the safe default tier (calibration pin)."""
    verdict = score_complexity("check my calendar for tomorrow", catalog=STUB_TASK_CATALOG)
    assert verdict.ambiguous
    assert verdict.tier == MEDIUM_TIER  # the ambiguous default, not the raw tier
    assert verdict.confidence < CFG.ambiguity_threshold
    assert _dimension(verdict, "catalog_match").score == pytest.approx(0.6)


def test_catalog_dimension_is_sourced_dynamically() -> None:
    """The same text scores the dimension only when the catalog carries it."""
    custom = (
        TaskCatalogEntry(kind="jira.search", one_liner="Search Jira.", keywords=("jira", "sprint")),
    )
    text = "look up the jira board for the sprint"
    with_catalog = score_complexity(text, catalog=custom)
    without_catalog = score_complexity(text)
    dim = _dimension(with_catalog, "catalog_match")
    assert dim.score == 1.0
    assert dim.signal is not None and "jira.search" in dim.signal
    assert _dimension(without_catalog, "catalog_match").score == 0.0
    assert without_catalog.score < with_catalog.score


def test_gmail_entry_matches_through_russian_translation_bridge() -> None:
    verdict = score_complexity("найди письмо от клиента во входящих", catalog=STUB_TASK_CATALOG)
    dim = _dimension(verdict, "catalog_match")
    assert dim.score == 1.0
    assert dim.signal is not None and "gmail.search" in dim.signal


# --------------------------------------------------------------------------- #
# Multi-step / agentic / token / format dimensions                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "first draft the agenda and then send it to the team",
        "сначала подготовь план, потом отправь его команде",
        "ensin laadi suunnitelma ja sitten lähetä se tiimille",
    ],
)
def test_multi_step_patterns_fire(text: str) -> None:
    verdict = score_complexity(text)
    assert _dimension(verdict, "multi_step").score == pytest.approx(0.5)
    assert "multi-step" in verdict.signals


def test_agentic_ladder_one_two_three_verbs() -> None:
    one = score_complexity("update the doc")
    two = score_complexity("update the doc and send it")
    three = score_complexity("update the doc, send it, and schedule a review")
    assert _dimension(one, "agentic_verbs").score == pytest.approx(0.3)
    assert _dimension(two, "agentic_verbs").score == pytest.approx(0.6)
    assert _dimension(three, "agentic_verbs").score == pytest.approx(1.0)


def test_token_estimate_thresholds() -> None:
    short = score_complexity("just a quick note")
    mid = score_complexity(" ".join(["word"] * 20))  # ~26 estimated tokens
    long = score_complexity(" ".join(["word"] * 50))  # ~65 estimated tokens
    assert _dimension(short, "token_estimate").score == -1.0
    assert _dimension(mid, "token_estimate").score == 0.0
    long_dim = _dimension(long, "token_estimate")
    assert long_dim.score == 1.0
    assert long_dim.signal is not None and long_dim.signal.startswith("long")


@pytest.mark.parametrize(
    "text",
    [
        "summarize the discussion as a list",
        "резюмируй обсуждение по пунктам",
        "tee yhteenveto ranskalaisilla viivoilla",
    ],
)
def test_output_format_markers_fire(text: str) -> None:
    verdict = score_complexity(text)
    assert _dimension(verdict, "output_format").score > 0


def test_complex_tier_reachable_without_reasoning_markers() -> None:
    """Multi-step + 3 verbs + 2 catalog hits at mid length → confident COMPLEX."""
    text = (
        "first check the calendar and find the meetings for next week, "
        "then search the inbox and draft a reply to the client about the schedule"
    )
    verdict = score_complexity(text, catalog=STUB_TASK_CATALOG)
    assert verdict.tier == COMPLEX_TIER
    assert not verdict.ambiguous
    assert not verdict.reasoning_override
    assert verdict.score == pytest.approx(0.40, abs=1e-9)


# --------------------------------------------------------------------------- #
# Ambiguity → safe default tier                                               #
# --------------------------------------------------------------------------- #


def test_near_boundary_score_is_ambiguous_and_defaults_to_medium() -> None:
    # One light verb (+0.042) against a short turn (-0.10) lands at -0.058 —
    # raw SIMPLE side, but within the ambiguity band of the 0.0 boundary.
    verdict = score_complexity("update the doc")
    assert verdict.ambiguous
    assert verdict.tier == MEDIUM_TIER
    assert verdict.confidence < CFG.ambiguity_threshold
    assert verdict.score < 0  # the default tier overrode the raw SIMPLE mapping


# --------------------------------------------------------------------------- #
# Matching semantics: stems, boundaries, casefold                             #
# --------------------------------------------------------------------------- #


def test_left_boundary_blocks_mid_word_hits() -> None:
    # "etsi" must not fire inside "metsissä", "list" inside "listen" is not a
    # keyword at all, and agentic has no bare "add" to fire inside "address".
    assert _dimension(score_complexity("metsissä on hiljaista"), "agentic_verbs").score == 0.0
    assert _dimension(score_complexity("listen carefully please"), "output_format").score == 0.0
    assert _dimension(score_complexity("address the team today"), "agentic_verbs").score == 0.0


def test_stems_survive_inflection() -> None:
    # "провер" → "проверьте", "tarkist" → "tarkistaisitko" (conditional).
    assert _dimension(score_complexity("проверьте статус"), "agentic_verbs").score > 0
    assert _dimension(score_complexity("tarkistaisitko tilanteen"), "agentic_verbs").score > 0


def test_scoring_is_case_insensitive() -> None:
    upper = score_complexity("CHECK MY CALENDAR, WHAT MEETINGS TOMORROW", catalog=STUB_TASK_CATALOG)
    lower = score_complexity("check my calendar, what meetings tomorrow", catalog=STUB_TASK_CATALOG)
    assert upper.score == pytest.approx(lower.score)
    assert upper.tier == lower.tier


def test_empty_text_scores_simple_without_crashing() -> None:
    verdict = score_complexity("")
    assert verdict.tier == SIMPLE_TIER
    assert not verdict.ambiguous


# --------------------------------------------------------------------------- #
# Shadow payload                                                              #
# --------------------------------------------------------------------------- #


def test_shadow_payload_shape_and_json_safety() -> None:
    verdict = score_complexity(
        "first check the calendar and find the meetings, then summarize as a list "
        "and send the draft to the team",
        catalog=STUB_TASK_CATALOG,
    )
    payload = verdict.shadow_payload()
    assert set(payload) == {"score", "tier", "confidence", "top_signals"}
    assert payload["tier"] == verdict.tier
    assert payload["score"] == round(verdict.score, 4)
    assert payload["confidence"] == round(verdict.confidence, 4)
    assert isinstance(payload["top_signals"], list)
    assert len(payload["top_signals"]) <= MAX_TOP_SIGNALS
    json.dumps(payload)  # must be JSON-serializable as-is


def test_signals_ordered_by_weighted_contribution() -> None:
    # Catalog high (0.18) must outrank the short-token signal (0.10).
    verdict = score_complexity(
        "check my calendar, what meetings tomorrow", catalog=STUB_TASK_CATALOG
    )
    assert verdict.signals[0].startswith("catalog")
