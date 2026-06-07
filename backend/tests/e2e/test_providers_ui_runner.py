"""Unit tests for the runner's tier-paywall translation (Johnny-uga).

These are pure-function tests that don't need the live stack — they
verify the substring matcher used to translate provider tier paywalls
(e.g. ElevenLabs free-tier library voices returning HTTP 402) from a
FAIL into a SKIP with an actionable hint.
"""

from __future__ import annotations

from app.providers.base import ProviderKind
from tests.e2e.providers_ui.plans import ProviderPlan
from tests.e2e.providers_ui.runner import _detect_tier_paywall_failure


def _elevenlabs_plan() -> ProviderPlan:
    """Minimal ElevenLabs plan stand-in for the matcher tests."""
    return ProviderPlan(
        plan_id="tts-elevenlabs",
        kind=ProviderKind.TTS,
        provider_name="elevenlabs",
        display_name="e2e-tts-elevenlabs",
        static_options={"voice_id": "21m00Tcm4TlvDq8ikWAM"},
    )


def test_elevenlabs_free_tier_library_voice_becomes_skip() -> None:
    """The exact 402 detail that free-tier returns must surface as SKIP."""
    detail = (
        "smoke call failed — elevenlabs TTS HTTP 402: Free users cannot use "
        "library voices via the API. Please upgrade your subscription to use "
        "this voice."
    )
    result = _detect_tier_paywall_failure(_elevenlabs_plan(), detail)
    assert result is not None
    # The hint must name the voice id and pin the cause + remediation.
    assert "21m00Tcm4TlvDq8ikWAM" in result
    assert "paid plan" in result.lower()
    assert "upgrade" in result.lower() or "account-owned" in result.lower()


def test_elevenlabs_upgrade_phrasing_alone_also_matches() -> None:
    """Either of the configured substrings should latch the SKIP path."""
    detail = "TTS HTTP 402: please upgrade your subscription to use this voice."
    result = _detect_tier_paywall_failure(_elevenlabs_plan(), detail)
    assert result is not None


def test_unrelated_elevenlabs_error_stays_fail() -> None:
    """Genuine FAILs (bad request, server error) must NOT be masked as SKIP."""
    detail = "smoke call failed — elevenlabs TTS HTTP 500: internal error"
    assert _detect_tier_paywall_failure(_elevenlabs_plan(), detail) is None


def test_non_elevenlabs_plan_returns_none() -> None:
    """No matcher registered → never translate, even with a matching string."""
    plan = ProviderPlan(
        plan_id="tts-openai",
        kind=ProviderKind.TTS,
        provider_name="openai",
        display_name="e2e-tts-openai",
        static_options={"voice_id": "alloy"},
    )
    detail = "free users cannot use library voices via the API"
    assert _detect_tier_paywall_failure(plan, detail) is None


def test_empty_detail_returns_none() -> None:
    """An empty smoke detail must be a no-op, not a panic."""
    assert _detect_tier_paywall_failure(_elevenlabs_plan(), "") is None
