"""Heuristic complexity pre-scorer for voice turns — shadow mode (Johnny-trt.50).

A pure-stdlib port of the *pattern* (not the code) of ClawRouter's rule-based
classifier — ``~/Projects/ClawRouter`` ``src/router/rules.ts`` +
``src/router/config.ts``, MIT licensed. Attribution: the weighted-dimension
mechanism, the keyword-match threshold ladders, the tier boundaries, the
sigmoid confidence calibration, and the reasoning-marker override all come
from ClawRouter v2 scoring; the dimensions and keyword sets are re-derived
for Johnny's spoken meeting turns (short utterances, EN + RU + FI — the
operator's languages; ClawRouter ships nine).

Mechanism (rules.ts ``classifyByRules``):

* each dimension scores the turn text in ``[-1, 1]`` with an optional
  human-readable signal;
* the weighted sum (weights sum to 1.0, :data:`DIMENSION_WEIGHTS`) maps to a
  tier via fixed boundaries — ``SIMPLE < 0.0 <= MEDIUM < 0.3 <= COMPLEX
  < 0.5 <= REASONING`` (config.ts ``tierBoundaries``, the 2026-03 calibration:
  ``mediumComplex`` raised 0.18→0.3, ``complexReasoning`` 0.4→0.5);
* confidence is a sigmoid of the distance to the nearest tier boundary,
  steepness 12 (config.ts ``confidenceSteepness``); below the 0.7 ambiguity
  threshold (config.ts ``confidenceThreshold``) the verdict is *ambiguous*
  and resolves to the safe default tier (config.ts
  ``overrides.ambiguousDefaultTier = MEDIUM``) — ClawRouter falls back to an
  LLM classifier there; Johnny's router LLM is already the authority, so the
  shadow verdict just records the default + the low confidence;
* two or more reasoning-marker hits override straight to ``REASONING`` with
  confidence ``max(sigmoid, 0.85)`` (rules.ts "direct reasoning override").

Johnny adaptations (each deliberate, all shadow-observable in signals):

* **Dimensions** — seven, voice-turn shaped: reasoning markers, multi-step
  patterns, agentic/imperative verbs (ClawRouter's separate ``agenticTask`` +
  ``imperativeVerbs`` merged, threshold ladder shortened 4/3/1 → 3/2/1
  because spoken turns are far shorter than coding prompts), the
  task-catalog keyword match (the **delegate prior** — sourced dynamically
  from :mod:`johnny.agent.task_catalog` entries so Phase-4 skills/tools
  extend the scorer without code changes), simple indicators (scored
  NEGATIVELY, ClawRouter's ``{low: -1.0, high: -1.0}``), a token estimate
  (thresholds 50/500 → 12/60: a 60-token utterance is already a long spoken
  turn), and output-format markers.
* **Matching** — ClawRouter uses plain ``includes`` substring matching; this
  port matches each keyword as a *left word-boundary prefix*
  (``\\b<keyword>``, free suffix). Russian and Finnish are heavily inflected,
  so keywords are stems ("провер" hits проверь/проверить/проверка,
  "tarkist" hits tarkista/tarkistaa) while the left boundary blocks
  mid-word noise ("etsi" must not fire inside "metsissä").

Stdlib-only and import-cheap (``re``/``math``/``dataclasses``), importable
without livekit or the provider stack — the same contract as
:mod:`johnny.agent.gate` and :mod:`johnny.agent.task_catalog`.

SHADOW-ONLY VERDICT, REAL KEYWORD HELPERS: the complexity *verdict*
(:func:`score_complexity` / :class:`ComplexityVerdict`) is observability only —
its sole production caller is
:meth:`johnny.agent.router_gate.RouterGate.run_turn`, which computes it
synchronously before awaiting the triage LLM and stashes
:meth:`ComplexityVerdict.shadow_payload` under :data:`SHADOW_KEY` inside the
decision's raw payload (→ ``agent_decisions.raw_output`` JSON — no
migration). Nothing reads the verdict back at runtime; its purpose is the
labeled per-turn dataset (heuristic verdict × router action) that gates any
later behavioral use (Johnny-trt.51 fast-paths, the Phase-6 name-addressing
gate Johnny-trt.52). The module's *keyword helpers*, by contrast, ARE used for
real post-router decisions: :func:`matched_catalog_kinds` (the Johnny-etu.6
delegate recovery) and :func:`detect_background_request` (the Johnny-d6w.13
off-turn promotion) — both pure, deterministic, and replay-safe, sharing the
same left-word-boundary matching as the scorer's catalog dimension so the two
never drift on what "a keyword hit" means.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from johnny.agent.task_catalog import TaskCatalogEntry

# --------------------------------------------------------------------------- #
# Tiers                                                                       #
# --------------------------------------------------------------------------- #

SIMPLE_TIER = "SIMPLE"
MEDIUM_TIER = "MEDIUM"
COMPLEX_TIER = "COMPLEX"
REASONING_TIER = "REASONING"

COMPLEXITY_TIERS: tuple[str, ...] = (SIMPLE_TIER, MEDIUM_TIER, COMPLEX_TIER, REASONING_TIER)
"""Ascending complexity tiers (ClawRouter types.ts ``Tier``)."""

SHADOW_KEY = "complexity_shadow"
"""The key the gate writes :meth:`ComplexityVerdict.shadow_payload` under in
``RouterDecision.raw`` — and therefore the key the verdict lives at inside the
persisted ``agent_decisions.raw_output`` JSON, next to the router's own
``action`` field the dataset pairs it with."""

MAX_TOP_SIGNALS = 5
"""Signals kept in the persisted payload (ordered by weighted contribution)."""


# --------------------------------------------------------------------------- #
# Result / config shapes                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DimensionScore:
    """One dimension's verdict: a score in ``[-1, 1]`` + an optional signal.

    ``signal`` is the human-readable why ("reasoning (step by step, prove)",
    "short (6 tokens)"); ``None`` when the dimension saw nothing — exactly
    rules.ts ``DimensionScore``.
    """

    name: str
    score: float
    signal: str | None = None


@dataclass(frozen=True, slots=True)
class ComplexityConfig:
    """The calibration knobs, with ClawRouter's shipped values as defaults.

    Tier boundaries / steepness / ambiguity threshold / default tier are the
    config.ts constants (see the module docstring for provenance); the token
    thresholds are the voice-turn adaptation (50/500 chat-prompt tokens →
    12/60 spoken tokens).
    """

    simple_medium: float = 0.0
    medium_complex: float = 0.3
    complex_reasoning: float = 0.5
    confidence_steepness: float = 12.0
    ambiguity_threshold: float = 0.7
    ambiguous_default_tier: str = MEDIUM_TIER
    token_simple_max: int = 12
    token_complex_min: int = 60
    tokens_per_word: float = 1.3


DEFAULT_COMPLEXITY_CONFIG = ComplexityConfig()


@dataclass(frozen=True, slots=True)
class ComplexityVerdict:
    """The scorer's full verdict for one turn.

    ``tier`` is always concrete: when ``ambiguous`` (confidence below the
    threshold) it is the config's safe default tier — ClawRouter returns
    ``tier=null`` there and lets a fallback classifier decide; Johnny's
    fallback *is* the triage LLM the gate awaits anyway, so the shadow
    verdict records the default it would have used. ``reasoning_override``
    marks the >=2-reasoning-marker direct override. ``signals`` are ordered
    by absolute weighted contribution (largest first); ``dimensions`` is the
    full per-dimension breakdown for tests / offline analysis.
    """

    score: float
    tier: str
    confidence: float
    ambiguous: bool
    reasoning_override: bool
    signals: tuple[str, ...] = ()
    dimensions: tuple[DimensionScore, ...] = field(default=())

    def shadow_payload(self) -> dict[str, Any]:
        """The JSON blob persisted under :data:`SHADOW_KEY` (Johnny-trt.50).

        Exactly the four keys the bead pins — ``score`` / ``tier`` /
        ``confidence`` / ``top_signals`` — JSON-safe and small. Ambiguity is
        recoverable offline (``confidence < 0.7`` ⇒ ``tier`` is the safe
        default), so it is deliberately not a fifth key.
        """
        return {
            "score": round(self.score, 4),
            "tier": self.tier,
            "confidence": round(self.confidence, 4),
            "top_signals": list(self.signals[:MAX_TOP_SIGNALS]),
        }


# --------------------------------------------------------------------------- #
# Dimension weights (sum to 1.0)                                              #
# --------------------------------------------------------------------------- #

DIMENSION_WEIGHTS: dict[str, float] = {
    # ClawRouter reasoningMarkers 0.18, raised: with code/technical/creative
    # dimensions dropped (meaningless for meeting speech) reasoning is the
    # strongest "needs a big model" signal left.
    "reasoning_markers": 0.22,
    # ClawRouter multiStepPatterns 0.12, raised slightly — multi-step speech
    # ("first... then...") is a strong delegation/complexity tell in meetings.
    "multi_step": 0.16,
    # ClawRouter agenticTask 0.04 + imperativeVerbs 0.03, merged and raised:
    # for a voice assistant the do-real-work prior is a primary axis, not a
    # tie-breaker for a coding proxy.
    "agentic_verbs": 0.14,
    # The delegate prior. Takes the weight class of ClawRouter's codePresence
    # (0.15) — the catalog match is to Johnny what code presence is to a
    # coding router: the single most actionable content signal.
    "catalog_match": 0.18,
    # ClawRouter simpleIndicators 0.02 — but that low value leans on their
    # token/code dimensions to push greetings down; here simple smalltalk is
    # most of the negative mass, so it takes the old pre-prune weight (0.12).
    "simple_indicators": 0.12,
    # ClawRouter tokenCount 0.08, comparable.
    "token_estimate": 0.10,
    # ClawRouter outputFormat 0.03, slightly raised in the smaller mix.
    "output_format": 0.08,
}
"""Per-dimension weights — must sum to exactly 1.0 (pinned by test)."""


# --------------------------------------------------------------------------- #
# Keyword sets (EN + RU + FI; stems for the inflected languages)              #
# --------------------------------------------------------------------------- #

REASONING_KEYWORDS: tuple[str, ...] = (
    # English
    "step by step",
    "walk me through",
    "talk me through",
    "think it through",
    "think through",
    "reason through",
    "reasoning",
    "explain why",
    "why exactly",
    "prove",
    "proof",
    "derive",
    "theorem",
    "logically",
    "first principles",
    "root cause",
    "pros and cons",
    "trade-off",
    "tradeoff",
    "compare and contrast",
    # Russian (stems where inflection varies)
    "шаг за шагом",
    "пошагов",
    "поэтапн",
    "докаж",
    "доказательств",
    "обоснуй",
    "обоснов",
    "рассужд",
    "логическ",
    "логичн",
    "объясни почему",
    "почему именно",
    "разложи по полочкам",
    "первопричин",
    "за и против",
    "плюсы и минусы",
    # Finnish
    "askel askeleelta",
    "vaihe vaiheelta",
    "perustel",
    "todista",
    "päättel",
    "loogi",
    "selitä miksi",
    "miksi juuri",
    "juurisyy",
    "hyödyt ja haitat",
    "puolesta ja vastaan",
    "syvällise",
)
"""Strong reasoning asks. Two or more distinct hits trigger the direct
``REASONING`` override (rules.ts), so every entry must be individually strong
— no bare "why"/"почему"/"miksi"."""


_MULTI_STEP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # English (rules.ts: /first.*then/i, /step \d/, /\d\.\s/)
    re.compile(r"\bfirst\b.{0,100}\bthen\b"),
    re.compile(r"\bstep\s*\d"),
    re.compile(r"\b\d\s*\.\s"),
    re.compile(r"\bafter that\b"),
    # Russian
    re.compile(r"\bсначала\b.{0,100}\b(?:потом|затем)\b"),
    re.compile(r"\bшаг\s*\d"),
    re.compile(r"\bпосле этого\b"),
    re.compile(r"\bво-первых\b"),
    re.compile(r"\bво-вторых\b"),
    # Finnish
    re.compile(r"\bensin\b.{0,100}\bsitten\b"),
    re.compile(r"\bvaihe\s*\d"),
    re.compile(r"\bsen jälkeen\b"),
    re.compile(r"\bensiksi\b"),
    re.compile(r"\btoiseksi\b"),
)
"""Sequencing patterns; any hit scores 0.5 (rules.ts ``scoreMultiStep``)."""


AGENTIC_KEYWORDS: tuple[str, ...] = (
    # English — imperatives that ask Johnny to *do* something
    "create",
    "schedule",
    "send",
    "book",
    "set up",
    "find",
    "look up",
    "search",
    "check",
    "fix",
    "update",
    "remind",
    "draft",
    "prepare",
    "organize",
    "organise",
    "cancel",
    "reschedule",
    "follow up",
    "write down",
    "make a note",
    "take a note",
    # Russian
    "созда",
    "сделай",
    "отправ",
    "запланир",
    "забронир",
    "найди",
    "поищ",
    "провер",
    "исправ",
    "обнов",
    "напомн",
    "добав",
    "подготов",
    "организуй",
    "отмен",
    "перенес",
    "напиш",
    "запиш",
    # Finnish
    "lähet",
    "varaa",
    "etsi",
    "hae",
    "tarkist",
    "korjaa",
    "päivit",
    "muistut",
    "lisää",
    "laadi",
    "valmistel",
    "järjestä",
    "peruut",
    "siirrä",
    "kirjoit",
    "selvit",
)
"""Do-real-work verbs (ClawRouter ``agenticTaskKeywords`` + ``imperativeVerbs``
merged). Deliberately no bare "add"/"make"/"do"/"tee"/"luo" — too short or too
common, they fire inside unrelated words ("address") or smalltalk."""


SIMPLE_KEYWORDS: tuple[str, ...] = (
    # English (config.ts simpleKeywords, voice-adapted)
    "hello",
    "hey there",
    "thanks",
    "thank you",
    "okay",
    "yes or no",
    "what is",
    "who is",
    "when was",
    "what time",
    "how old",
    "how are you",
    "good morning",
    "good afternoon",
    "good evening",
    "goodbye",
    "bye",
    "see you",
    "sounds good",
    "got it",
    "no problem",
    "never mind",
    # Russian
    "привет",
    "здравствуй",
    "спасибо",
    "пока",
    "ладно",
    "понятно",
    "ясно",
    "хорошо",
    "окей",
    "договорились",
    "что такое",
    "кто такой",
    "кто такая",
    "сколько лет",
    "который час",
    "сколько времени",
    "как дела",
    "как ты",
    "доброе утро",
    "добрый день",
    "добрый вечер",
    "до свидания",
    "да или нет",
    "неважно",
    # Finnish
    "moi",
    "moikka",
    "terve",
    "kiitos",
    "kiitti",
    "selvä",
    "okei",
    "mikä on",
    "kuka on",
    "paljonko kello",
    "mitä kuuluu",
    "miten menee",
    "huomenta",
    "hyvää päivää",
    "hyvää iltaa",
    "näkemiin",
    "heippa",
    "hei hei",
    "kyllä vai ei",
    "ihan sama",
)
"""Greetings / acks / trivial lookups. ANY hit scores -1.0 (rules.ts
simpleIndicators ``{low: -1.0, high: -1.0}``). No bare "hi"/"hei" — they fire
inside "high"/"heitä"."""


OUTPUT_FORMAT_KEYWORDS: tuple[str, ...] = (
    # English
    "as a list",
    "make a list",
    "bullet point",
    "bullets",
    "summar",
    "in a table",
    "one sentence",
    "word for word",
    "verbatim",
    "outline",
    "template",
    "in writing",
    "write it up",
    "spell it out",
    # Russian
    "списк",
    "по пунктам",
    "тезисно",
    "маркированн",
    "резюм",
    "кратко",
    "вкратце",
    "подробн",
    "таблиц",
    "одним предложением",
    "дословно",
    "шаблон",
    "конспект",
    # Finnish
    "listana",
    "listaksi",
    "luettel",
    "ranskalais",
    "tiivist",
    "yhteenvet",
    "taulukk",
    "lyhyesti",
    "yksityiskohtaise",
    "yhdellä lauseella",
    "sanasta sanaan",
    "luonnos",
)
"""Structured-output asks (ClawRouter ``outputFormatKeywords``). No bare
"list" — it fires inside "listen". "ranskalais" = "ranskalaiset viivat",
Finnish for bullet points."""


CATALOG_KEYWORD_TRANSLATIONS: dict[str, tuple[str, ...]] = {
    # calendar.upcoming_events
    "calendar": ("календар", "kalenter"),
    "schedule": ("расписани", "aikataulu"),
    "meeting": ("встреч", "совещани", "kokou", "palaver", "tapaami"),
    "event": ("событи", "мероприяти", "tapahtum"),
    "appointment": ("запись на", "varaus", "vastaanott"),
    "agenda": ("повестк", "esityslist"),
    "free slot": ("свободное окно", "свободный слот", "vapaa aika", "vapaita aikoja"),
    "availability": ("доступност", "saatavuu", "когда свободен"),
    # gmail.search
    "email": ("почт", "имейл", "письм", "sähköposti", "meili"),
    "inbox": ("входящи", "saapuneet", "postilaatik"),
    "message from": ("сообщение от", "письмо от", "viesti"),
    "unread": ("непрочитанн", "lukematt"),
    # meeting.leave (Johnny-trt.57 internal tool)
    "leave": ("покин", "уход", "poistu", "lähde"),
    "disconnect": ("отключ", "katkais"),
    "goodbye": ("до свидани", "näkemiin"),
    # session.end (Johnny-trt.57 internal tool)
    "end the session": ("заверши сесси", "закончи сесси", "lopeta istunto", "lopeta sessio"),
    "stop the session": ("останови сесси", "pysäytä istunto"),
    "shut down": ("выключ", "sammuta"),
}
"""RU/FI stems for the *English* keywords catalog entries carry (the
task_catalog contract keeps entry keywords English; the scorer owns the
multilingual sets). Keyed by the exact entry keyword; an entry keyword with
no mapping — e.g. a Phase-4 skill's novel vocabulary — simply matches
English-only until a stem is added here or the entry ships its own."""


BACKGROUND_REQUEST_KEYWORDS: tuple[str, ...] = (
    # English — explicit, directive "handle this off-turn" phrasing (the
    # Johnny-d6w.13 / US-201 trigger phrases: "do it in the background", "keep
    # working on that"). Ambient phrasing ("while we talk", "later", "when you
    # get a chance") is DELIBERATELY excluded: on a meeting surface this
    # detector is the only signal that lifts the trt.50 over-delegation
    # suppression for an explicit request, so the set biases to false-negatives
    # (a missed background ask stays inline, answered honestly) over
    # false-positives (an unasked skill run mid-meeting).
    "in the background",
    "in background",
    "keep working on",
    "report back when",
    # Russian
    "на фоне",
    "в фоновом режим",
    "продолжай работ",
    "доложи когда",
    # Finnish
    "taustal",
    "jatka työ",
    "raportoi kun",
)
"""Directive background-handling phrases (EN + RU/FI stems) for the Johnny-d6w.13
off-turn promotion trigger (:func:`detect_background_request`). Matched with the
same left-word-boundary primitive (:func:`_keyword_pattern`) as the catalog
keywords. Kept SEPARATE from :data:`CATALOG_KEYWORD_TRANSLATIONS` (which is keyed
by English catalog keywords) — these are a flat set of stand-alone phrases, not
per-entry translations, and none collide with a catalog keyword so the two
matchers never fight."""


REDELIVER_REQUEST_KEYWORDS: tuple[str, ...] = (
    # English — explicit "say the previous result again" phrasing (the
    # Johnny-d6w.31 re-deliver trigger: "can you share it again?", "what did it
    # say?"). Multi-word by design: the left-word-boundary primitive matches a
    # free suffix, so a bare "again" would also hit "against" — every phrase
    # here carries enough context to be unambiguous. Like
    # :data:`BACKGROUND_REQUEST_KEYWORDS` this biases to false-negatives (a
    # missed re-deliver ask just falls through to the normal status/answer
    # path) over false-positives (re-reading a result nobody asked for).
    "it again",
    "that again",
    "say again",
    "once again",
    "one more time",
    "repeat that",
    "repeat it",
    "repeat the",
    "tell me again",
    "read it back",
    "read that back",
    "what did it say",
    "what did you find",
    "what was the result",
    "what did it find",
)
"""Directive re-deliver phrases for the Johnny-d6w.31 "share it again" trigger
(:func:`detect_redeliver_request`). Matched with the same left-word-boundary
primitive (:func:`_keyword_pattern`) as the catalog keywords and
:data:`BACKGROUND_REQUEST_KEYWORDS`, so it is replay-deterministic and adds no
prompt bytes. Stand-alone phrases, kept SEPARATE from the per-entry catalog
translations; English-only in v1 (RU/FI stems can be added the same way the
background set carries them)."""


# --------------------------------------------------------------------------- #
# Matching                                                                    #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=4096)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Compile one keyword as a left-word-boundary prefix match.

    ``\\b<keyword>`` with a free suffix: stems survive RU/FI inflection
    ("провер" → проверь/проверка) while the left boundary blocks mid-word
    hits ("etsi" never fires inside "metsissä"). Cached process-wide — the
    static sets compile once, and dynamic catalog keywords are a small stable
    vocabulary per deployment.
    """
    return re.compile(r"\b" + re.escape(keyword.casefold()))


def _match_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    """Distinct keywords that hit ``text`` (already casefolded), in set order."""
    return [kw for kw in keywords if kw and _keyword_pattern(kw).search(text)]


def _keyword_dimension(
    matches: list[str],
    *,
    name: str,
    label: str,
    low: int,
    high: int,
    score_low: float,
    score_high: float,
) -> DimensionScore:
    """The generic threshold ladder (rules.ts ``scoreKeywordMatch``).

    ``>= high`` distinct matches → ``score_high``; ``>= low`` → ``score_low``;
    else 0 with no signal. The signal lists up to three matched keywords.
    """
    if len(matches) >= high:
        score = score_high
    elif len(matches) >= low:
        score = score_low
    else:
        return DimensionScore(name=name, score=0.0)
    return DimensionScore(name=name, score=score, signal=f"{label} ({', '.join(matches[:3])})")


def _score_multi_step(text: str) -> DimensionScore:
    """Any sequencing pattern hit → flat 0.5 (rules.ts ``scoreMultiStep``)."""
    if any(p.search(text) for p in _MULTI_STEP_PATTERNS):
        return DimensionScore(name="multi_step", score=0.5, signal="multi-step")
    return DimensionScore(name="multi_step", score=0.0)


def _score_agentic(matches: list[str]) -> DimensionScore:
    """The agentic ladder — 3+/2/1 → 1.0/0.6/0.3.

    Adapted from rules.ts ``scoreAgenticTask`` (4+/3/1+ → 1.0/0.6/0.2): a
    spoken turn rarely packs four imperatives, so the rungs shift down one
    and the single-verb rung rises 0.2 → 0.3.
    """
    if len(matches) >= 3:
        score, label = 1.0, "agentic"
    elif len(matches) >= 2:
        score, label = 0.6, "agentic"
    elif len(matches) >= 1:
        score, label = 0.3, "agentic-light"
    else:
        return DimensionScore(name="agentic_verbs", score=0.0)
    return DimensionScore(
        name="agentic_verbs", score=score, signal=f"{label} ({', '.join(matches[:3])})"
    )


def _score_catalog(text: str, catalog: tuple[TaskCatalogEntry, ...]) -> DimensionScore:
    """The delegate prior: match the dynamic task-catalog keywords.

    Each entry keyword counts at most once, matching either as-is (English,
    per the task_catalog contract) or through any of its
    :data:`CATALOG_KEYWORD_TRANSLATIONS` stems. Scored on ClawRouter's
    ``{low: 1 → 0.6, high: 2 → 1.0}`` ladder; the signal names the matched
    *kinds* (the labels the dataset analysis pairs with ``action='delegate'``)
    plus up to three matched keywords.
    """
    matched_kinds: list[str] = []
    matched_keywords: list[str] = []
    total = 0
    for entry in catalog:
        entry_hits = 0
        for keyword in entry.keywords:
            if not keyword:
                continue
            folded = keyword.casefold()
            for variant in (folded, *CATALOG_KEYWORD_TRANSLATIONS.get(folded, ())):
                if _keyword_pattern(variant).search(text):
                    entry_hits += 1
                    if len(matched_keywords) < 3:
                        matched_keywords.append(variant)
                    break
        if entry_hits:
            matched_kinds.append(entry.kind)
            total += entry_hits
    if total >= 2:
        score = 1.0
    elif total >= 1:
        score = 0.6
    else:
        return DimensionScore(name="catalog_match", score=0.0)
    signal = f"catalog ({', '.join(matched_kinds)}: {', '.join(matched_keywords)})"
    return DimensionScore(name="catalog_match", score=score, signal=signal)


GENERIC_DELEGATE_STOPWORDS: frozenset[str] = frozenset(
    {
        # High-frequency English words that are common as BOTH an everyday verb
        # and a noun, where the verb sense is generic and unrelated to any
        # particular skill domain. A skill keyword that is ONLY one of these
        # (e.g. the financial noun "share"/"shares") collides with the same word
        # used as a plain verb ("can you share it again?") and must not, on its
        # own, force a keyword delegate-recovery (Johnny-d6w.31). General and
        # dynamic — never per-skill: it names no skill or skill keyword, only
        # ambiguous English words. Curated conservatively, biased to
        # false-negatives (under-suppress) so a discriminating domain keyword on
        # the same entry still recovers; extend it with the same principle.
        "share",
        "shares",
        "run",
        "runs",
        "book",
        "books",
        "order",
        "orders",
        "post",
        "posts",
        "play",
        "plays",
        "store",
        "stores",
        "set",
        "sets",
        "trade",
        "trades",
    }
)
"""Generic verb/noun-collision words that, alone, are too weak to force a
keyword delegate-recovery (Johnny-d6w.31). Consulted only when
:func:`matched_catalog_kinds` is called with ``require_discriminating=True`` (the
gate's recovery path); the shadow scorer and the default raw matcher are
unaffected."""


def matched_catalog_kinds(
    text: str,
    catalog: tuple[TaskCatalogEntry, ...],
    *,
    require_discriminating: bool = False,
) -> list[str]:
    """The catalog kinds whose keywords hit ``text`` — the delegate prior (Johnny-etu.6).

    Same matching as the shadow-only :func:`_score_catalog` (English keywords
    per the task_catalog contract, plus :data:`CATALOG_KEYWORD_TRANSLATIONS`
    RU/FI stems, left-word-boundary prefix) but returned as the matched kinds
    so the gate can recover the kind the user clearly asked for when the small
    local router declined to ``delegate`` and fell back to ``speak`` / ``status``
    (:meth:`johnny.agent.router_gate.RouterGate._recover_keyword_delegate`). The
    score ladder is irrelevant here — the caller wants *which* kinds matched, not
    how complex the turn is. Order follows the catalog; each kind appears at most
    once; an entry with no keywords (every unavailable entry, by the
    catalog-assembly contract) never matches, so passing the full catalog or only
    its available slice is equivalent. Kept beside :func:`_score_catalog` so the
    two never drift on what "a catalog keyword hit" means.

    ``require_discriminating`` (Johnny-d6w.31) tightens the match for the gate's
    recovery path: a kind qualifies only if it hit on at least one keyword that
    is NOT in :data:`GENERIC_DELEGATE_STOPWORDS`. So an entry matched solely by a
    generic verb/noun-collision word (the financial-noun keyword "share" while
    the user meant the verb in "share it again") is dropped, while a
    discriminating keyword on the same entry ("price", "calendar", "end the
    session") still recovers. Default ``False`` keeps the raw matcher (and its
    direct callers/tests) byte-for-byte unchanged.
    """
    folded = text.casefold()
    matched: list[str] = []
    for entry in catalog:
        any_hit = False
        discriminating = False
        for keyword in entry.keywords:
            if not keyword:
                continue
            kw = keyword.casefold()
            variants = (kw, *CATALOG_KEYWORD_TRANSLATIONS.get(kw, ()))
            if any(_keyword_pattern(variant).search(folded) for variant in variants):
                any_hit = True
                if kw not in GENERIC_DELEGATE_STOPWORDS:
                    discriminating = True
                    break  # one discriminating hit settles the entry
                # a stopword-only hit is too weak on its own — keep scanning
                # this entry for a discriminating keyword.
        if any_hit and (discriminating or not require_discriminating):
            matched.append(entry.kind)
    return matched


def detect_background_request(text: str) -> bool:
    """Does the turn explicitly ask for off-turn / background handling? (US-201).

    The deterministic, persisted-signal trigger for Johnny-d6w.13's opt-in
    promotion: the user said "do it in the background" / "keep working on that"
    (EN) or an RU/FI equivalent. A pure keyword match over the *transcript* with
    the same left-word-boundary primitive (:func:`_keyword_pattern`) the catalog
    matcher uses, so it is replay-deterministic and adds no prompt bytes — the
    gate's :meth:`johnny.agent.router_gate.RouterGate._promote_background_request`
    reads it to promote a dropped-delegate verdict to ``delegate`` and, on a
    meeting surface, to lift the trt.50 ambient-chatter suppression for an
    *explicit* request.

    Conservative BY DESIGN: this is the SOLE signal that lets an explicit
    background ask override the over-delegation guard on a meeting surface, so
    :data:`BACKGROUND_REQUEST_KEYWORDS` carries only directive phrasing and the
    function biases to false-negatives (miss → stay inline, answer honestly)
    over false-positives (an unasked skill run mid-meeting). The downstream
    promotion still requires exactly one *available* catalog kind to match and
    that kind to be free — this detector only opens the door.
    """
    folded = text.casefold()
    return any(_keyword_pattern(kw).search(folded) for kw in BACKGROUND_REQUEST_KEYWORDS)


def detect_redeliver_request(text: str) -> bool:
    """Does the turn explicitly ask to re-hear the previous result? (Johnny-d6w.31).

    The deterministic trigger for re-delivering a durably-held task result: the
    user said "can you share it again?", "what did it say?", "one more time"
    (EN). A pure keyword match over the *transcript* with the same
    left-word-boundary primitive (:func:`_keyword_pattern`) the catalog matcher
    and :func:`detect_background_request` use, so it is replay-deterministic and
    adds no prompt bytes — the gate
    (:meth:`johnny.agent.router_gate.RouterGate._handle_status`) reads it to
    re-speak the last finished result's ``result_text`` instead of the
    "already shared" dead-end.

    Conservative BY DESIGN, mirroring :data:`BACKGROUND_REQUEST_KEYWORDS`: the
    function biases to false-negatives (a missed re-deliver ask just falls
    through to the normal status/answer path) over false-positives (re-reading a
    result nobody asked for). It only changes spoken text when a finished result
    with non-blank ``result_text`` is actually held — see ``status_summary``.
    """
    folded = text.casefold()
    return any(_keyword_pattern(kw).search(folded) for kw in REDELIVER_REQUEST_KEYWORDS)


def _score_token_estimate(text: str, config: ComplexityConfig) -> DimensionScore:
    """Length prior (rules.ts ``scoreTokenCount``), word-based for multiscript.

    ``words × 1.3`` approximates tokens stably across Latin and Cyrillic
    (chars/4, ClawRouter's chat heuristic, undercounts Cyrillic badly).
    Voice thresholds: under 12 tokens is a short remark (-1.0), over 60 is a
    genuinely long spoken turn (+1.0).
    """
    estimated = round(len(text.split()) * config.tokens_per_word)
    if estimated < config.token_simple_max:
        return DimensionScore(
            name="token_estimate", score=-1.0, signal=f"short ({estimated} tokens)"
        )
    if estimated > config.token_complex_min:
        return DimensionScore(name="token_estimate", score=1.0, signal=f"long ({estimated} tokens)")
    return DimensionScore(name="token_estimate", score=0.0)


# --------------------------------------------------------------------------- #
# Calibration                                                                 #
# --------------------------------------------------------------------------- #


def _calibrate_confidence(distance: float, steepness: float) -> float:
    """Sigmoid of boundary distance → ``[0.5, 1.0)`` (rules.ts ``calibrateConfidence``)."""
    return 1.0 / (1.0 + math.exp(-steepness * distance))


def _tier_for(score: float, config: ComplexityConfig) -> tuple[str, float]:
    """Map the weighted score to ``(tier, distance-from-nearest-boundary)``.

    Exactly rules.ts lines 287–307: edge tiers measure distance to their one
    boundary; middle tiers to the nearer of their two.
    """
    if score < config.simple_medium:
        return SIMPLE_TIER, config.simple_medium - score
    if score < config.medium_complex:
        return MEDIUM_TIER, min(score - config.simple_medium, config.medium_complex - score)
    if score < config.complex_reasoning:
        return COMPLEX_TIER, min(score - config.medium_complex, config.complex_reasoning - score)
    return REASONING_TIER, score - config.complex_reasoning


# --------------------------------------------------------------------------- #
# Main scorer                                                                 #
# --------------------------------------------------------------------------- #


def score_complexity(
    text: str,
    *,
    catalog: tuple[TaskCatalogEntry, ...] = (),
    config: ComplexityConfig = DEFAULT_COMPLEXITY_CONFIG,
) -> ComplexityVerdict:
    """Score one user turn's complexity — pure, synchronous, ~microseconds.

    ``text`` is the turn's transcript (the router's "Latest transcript" input,
    NOT the system prompt — scoring prompt boilerplate is ClawRouter's
    documented issue #50 mistake). ``catalog`` is the session's delegatable
    task catalog (``RouterGateConfig.task_catalog``): empty on non-delegation
    runtimes, which zeroes the delegate-prior dimension — the same
    capability-gating stance as the router prompt.
    """
    folded = text.casefold()

    reasoning_matches = _match_keywords(folded, REASONING_KEYWORDS)
    dimensions = (
        _keyword_dimension(
            reasoning_matches,
            name="reasoning_markers",
            label="reasoning",
            low=1,
            high=2,
            # rules.ts reasoningMarkers: {none: 0, low: 0.7, high: 1.0}
            score_low=0.7,
            score_high=1.0,
        ),
        _score_multi_step(folded),
        _score_agentic(_match_keywords(folded, AGENTIC_KEYWORDS)),
        _score_catalog(folded, catalog),
        _keyword_dimension(
            _match_keywords(folded, SIMPLE_KEYWORDS),
            name="simple_indicators",
            label="simple",
            low=1,
            high=2,
            # rules.ts simpleIndicators: any hit → -1.0
            score_low=-1.0,
            score_high=-1.0,
        ),
        _score_token_estimate(text, config),
        _keyword_dimension(
            _match_keywords(folded, OUTPUT_FORMAT_KEYWORDS),
            name="output_format",
            label="format",
            low=1,
            high=2,
            # rules.ts outputFormat: {none: 0, low: 0.4, high: 0.7}
            score_low=0.4,
            score_high=0.7,
        ),
    )

    weighted = sum(DIMENSION_WEIGHTS[d.name] * d.score for d in dimensions)

    # Signals ordered by absolute weighted contribution, ties in dimension order.
    contributing = sorted(
        (d for d in dimensions if d.signal is not None),
        key=lambda d: -abs(DIMENSION_WEIGHTS[d.name] * d.score),
    )
    signals = tuple(d.signal for d in contributing if d.signal is not None)

    # Direct reasoning override (rules.ts): 2+ markers ⇒ REASONING, floored
    # confidence — the boundaries never see these turns.
    if len(reasoning_matches) >= 2:
        confidence = max(
            _calibrate_confidence(max(weighted, 0.3), config.confidence_steepness), 0.85
        )
        return ComplexityVerdict(
            score=weighted,
            tier=REASONING_TIER,
            confidence=confidence,
            ambiguous=False,
            reasoning_override=True,
            signals=signals,
            dimensions=dimensions,
        )

    tier, distance = _tier_for(weighted, config)
    confidence = _calibrate_confidence(distance, config.confidence_steepness)
    if confidence < config.ambiguity_threshold:
        return ComplexityVerdict(
            score=weighted,
            tier=config.ambiguous_default_tier,
            confidence=confidence,
            ambiguous=True,
            reasoning_override=False,
            signals=signals,
            dimensions=dimensions,
        )
    return ComplexityVerdict(
        score=weighted,
        tier=tier,
        confidence=confidence,
        ambiguous=False,
        reasoning_override=False,
        signals=signals,
        dimensions=dimensions,
    )


__all__ = [
    "AGENTIC_KEYWORDS",
    "BACKGROUND_REQUEST_KEYWORDS",
    "CATALOG_KEYWORD_TRANSLATIONS",
    "COMPLEXITY_TIERS",
    "COMPLEX_TIER",
    "ComplexityConfig",
    "ComplexityVerdict",
    "DEFAULT_COMPLEXITY_CONFIG",
    "DIMENSION_WEIGHTS",
    "DimensionScore",
    "GENERIC_DELEGATE_STOPWORDS",
    "MAX_TOP_SIGNALS",
    "MEDIUM_TIER",
    "OUTPUT_FORMAT_KEYWORDS",
    "REASONING_KEYWORDS",
    "REASONING_TIER",
    "REDELIVER_REQUEST_KEYWORDS",
    "SHADOW_KEY",
    "SIMPLE_KEYWORDS",
    "SIMPLE_TIER",
    "detect_background_request",
    "detect_redeliver_request",
    "matched_catalog_kinds",
    "score_complexity",
]
