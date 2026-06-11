"""Prioritized speech queue — the pure core of Phase-5 conversational re-entry.

Build **Johnny-trt.27** (epic Johnny-trt, plan §5.1). A Phase-3/4 ``delegate``
verdict speaks an ack and runs the work off the turn loop; when the executor
finishes, the speech-ready ``result_text`` lands in ``agent_tasks`` and a
``TaskCompleted`` event reaches the agent process — and then nothing speaks it
(validated live in trt.26: "result NOT spoken (Phase-5 boundary held)"). This
module is the data structure + gating state machine that closes that gap: out-of-
band speech (acks, status answers, unsolicited task results, notices) waits here
until the conversation offers a boundary, then leaves in strict priority order.

What this module deliberately is **not**: it never speaks, never schedules, never
reads a clock, and never touches livekit / sqlalchemy / asyncio. Like
:mod:`johnny.agent.gate` and :mod:`johnny.agent.approval` it is stdlib-only with
every effect injected — but it goes one step further and is *fully synchronous*:
the caller owns the event loop, the timers, and the clock, and passes ``now``
(a ``time.monotonic()``-domain float) into every time-dependent call. That makes
every behavior — ordering, expiry, grace — a deterministic function of the call
sequence, which is exactly what the unit tests pin (they run without the
``agent`` extra). The real wiring is **Johnny-trt.28** (``task_wiring.py``,
ApprovalCoordinator pattern): a per-job delivery loop that feeds
:meth:`SpeechQueue.note_speech_onset` / :meth:`~SpeechQueue.note_silence_onset`
from ``user_state_changed`` + the bot's own ``current_speech``, polls
:meth:`~SpeechQueue.pop_ready`, speaks via ``session.say()`` (pre-composed text,
no LLM hop), and reports back through :meth:`~SpeechQueue.mark_spoken` /
:meth:`~SpeechQueue.mark_interrupted`.

Design points (each carries an acceptance test):

* **Priority classes** — :class:`SpeechPriority`: ``ACK`` >
  ``STATUS_REQUESTED`` > ``RESULT_UNSOLICITED`` > ``NOTICE``; FIFO within a
  class (a queue-assigned monotonic ``seq``). An ack is the promise the bot
  *just made* — it outranks everything. A requested status answer is the user
  waiting *right now*. Unsolicited results and ambient notices yield to both.
  Direct answers (the router's plain SPEAK path) **bypass the queue entirely** —
  they are the turn's reply, owned by the gate/reply machinery, never enqueued.
* **Expiry** — a stale ack is *worse than silence* ("let me check on that"
  twelve seconds after the room moved on), so ``ACK`` expires at ~5 s. Results
  keep ~120 s; past that the moment is gone and the UI copy is the delivery
  surface. Expiry is evaluated lazily (``sweep_expired`` runs inside
  ``pop_ready``); a drop fires ``on_dropped`` **exactly once** with a reason
  from the small vocabulary below. For ``RESULT_UNSOLICITED`` items the trt.28
  wiring turns that callback into the ``TaskResultExpired`` event
  (``voice_pipeline.events`` reserves it for exactly this — reason strings here
  match its documented examples: ``"undelivered for 120s"``, ``"interrupted
  twice"``). Expiry applies to *queued* items only — once popped, the item is
  committed to the mouth and settles via mark_spoken/mark_interrupted.
* **Silence-grace gating** — a pure two-state machine (speaking / silent-since).
  :meth:`~SpeechQueue.pop_ready` releases an item only when silence has held for
  ``grace_s`` (default ~1.2 s, the trt.28 conversational-pause budget). Any
  speech onset resets the anchor; the next silence onset restarts the clock;
  duplicate silence notifications do **not** extend the wait (first anchor
  wins). Silence that already held longer than the grace delivers immediately —
  the grace is "don't jump in right after someone stops", not a per-item delay.
* **One mouth** — at most one item is in flight: ``pop_ready`` returns ``None``
  until the wiring settles the previous item. Combined with the grace machine
  (the bot's own delivery is a speech onset too, if the wiring reports it),
  consecutive deliveries space out at conversational rhythm.
* **Requeue-once** — an interrupted item re-enters its class at its *original*
  ``seq`` (an interruption must not push it behind newer arrivals) and keeps its
  *original* deadline. The interruption budget is ``max_requeues`` (default 1):
  the second interruption drops it — the user has talked over it twice; it is
  not wanted aloud.
* **Exactly-once terminals** — every item settles exactly once, ``SPOKEN`` xor
  ``DROPPED``, through a single chokepoint (:meth:`SpeechQueue._settle`,
  mirroring ``TurnLedger``'s first-wins discipline). This matters because the
  **ack item's callbacks carry the delegating turn's ledger terminal** from the
  gate-branching task (trt.17): once acks route through this queue, ``on_spoken``
  resolves the parked turn ``replied`` and ``on_dropped`` resolves its
  ``no_reply`` — INV-1 ("exactly one terminal per turn") therefore leans on
  exactly-once here. Callbacks are **synchronous** callables; the async wiring
  bridges with ``loop.call_soon`` / ``create_task``. A raising callback is
  logged and swallowed — it can mar one item's bookkeeping, never the queue.
* **Out-of-band consumption seam** (trt.28 notes / trt.29) — when a user turn
  about the delegated topic arrives while its RESULT sits queued, the answer
  path must consume the queued copy rather than deliver it twice (or worse,
  answer blind and let the model hallucinate the result — observed live,
  session 4). :meth:`~SpeechQueue.mark_spoken` therefore also accepts a still-
  QUEUED item ("delivered through another path"), and :meth:`~SpeechQueue.items`
  exposes the read path.

Teardown: :meth:`SpeechQueue.close` (from ``AgentRuntime.aclose`` in trt.28)
drops everything — queued and in-flight — firing each ``on_dropped`` once;
``enqueue`` after close settles the new item dropped immediately, so a teardown
race can never strand an ack's ledger terminal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, IntEnum

logger = logging.getLogger(__name__)

# How long silence must hold before the queue releases an item. Mirrors the
# trt.28 delivery-gating spec ("~1200ms silence grace with speech-onset reset");
# the wiring may pass its own value, this default keeps the pure core honest.
DEFAULT_SILENCE_GRACE_S = 1.2

# Interruption budget: how many times an in-flight item may be interrupted and
# re-queued before the next interruption drops it ("re-queues once then drops",
# trt.28 acceptance).
DEFAULT_MAX_REQUEUES = 1


class SpeechPriority(IntEnum):
    """Delivery classes, most urgent first (lower value delivers first).

    Direct answers never enter the queue (module docstring); these four cover
    every *out-of-band* utterance Phase 5 produces.
    """

    ACK = 0
    STATUS_REQUESTED = 1
    RESULT_UNSOLICITED = 2
    NOTICE = 3


# Per-class default time-to-live. ACK ~5 s and RESULT ~120 s are pinned by the
# plan (§5.1) — a stale ack is worse than silence; a two-minute-old result has
# missed its moment (the UI keeps it readable). STATUS_REQUESTED ~20 s and
# NOTICE ~60 s are this module's judgment calls (the user who asked for status
# is waiting *now*; an ambient notice keeps a little longer) — per-item
# ``ttl_s`` overrides both.
ACK_DEFAULT_TTL_S = 5.0
STATUS_DEFAULT_TTL_S = 20.0
RESULT_DEFAULT_TTL_S = 120.0
NOTICE_DEFAULT_TTL_S = 60.0

DEFAULT_TTLS: dict[SpeechPriority, float] = {
    SpeechPriority.ACK: ACK_DEFAULT_TTL_S,
    SpeechPriority.STATUS_REQUESTED: STATUS_DEFAULT_TTL_S,
    SpeechPriority.RESULT_UNSOLICITED: RESULT_DEFAULT_TTL_S,
    SpeechPriority.NOTICE: NOTICE_DEFAULT_TTL_S,
}

# Drop-reason vocabulary. The queue owns it (events.TaskResultExpired keeps its
# ``reason`` untyped on purpose); expiry/interruption strings match the examples
# documented on that event so the operator-facing copy never skews.
DROP_QUEUE_CLOSED = "queue closed"
DROP_CANCELLED = "cancelled"


def expiry_drop_reason(ttl_s: float) -> str:
    """``"undelivered for 120s"`` — the documented ``TaskResultExpired`` shape."""
    return f"undelivered for {ttl_s:g}s"


def interruption_drop_reason(count: int) -> str:
    """``"interrupted twice"`` for the default budget; honest for any other."""
    if count == 1:
        return "interrupted once"
    if count == 2:
        return "interrupted twice"
    return f"interrupted {count} times"


class ItemState(Enum):
    """Per-item lifecycle. ``SPOKEN``/``DROPPED`` are terminal (exactly one)."""

    QUEUED = "queued"
    IN_FLIGHT = "in_flight"
    SPOKEN = "spoken"
    DROPPED = "dropped"


_TERMINAL_STATES = frozenset({ItemState.SPOKEN, ItemState.DROPPED})

# Injected per-item effects (synchronous on purpose — module docstring). The
# async trt.28 wiring bridges via ``loop.call_soon`` / ``asyncio.create_task``.
OnSpoken = Callable[["SpeechItem"], None]
"""Fired exactly once when the item was delivered aloud (or consumed into a
direct answer). For ack items this carries the delegating turn's ledger
``replied`` terminal."""

OnDropped = Callable[["SpeechItem", str], None]
"""Fired exactly once when the item will never be spoken; the ``str`` is the
drop reason (see the vocabulary above). For RESULT items the wiring emits
``TaskResultExpired`` from here; for ack items it resolves the parked turn's
``no_reply`` terminal."""


@dataclass(slots=True)
class SpeechItem:
    """One queued utterance. Constructed by :meth:`SpeechQueue.enqueue` only.

    ``text`` is pre-composed and final — the delivery path speaks it verbatim
    (``session.say``), no LLM hop. ``task_id`` / ``kind`` / ``turn_id`` are
    optional correlation for the wiring's events and ledger terminals (they
    mirror ``TaskResultExpired``'s fields; ``turn_id`` is the LiveKit string
    turn id the :class:`~johnny.agent.gate.TurnLedger` keys on). ``state``,
    ``interruptions`` and ``drop_reason`` are queue-owned bookkeeping — callers
    read them, never write them.
    """

    text: str
    priority: SpeechPriority
    seq: int
    enqueued_at: float
    expires_at: float
    task_id: int | None = None
    kind: str = ""
    turn_id: str | None = None
    on_spoken: OnSpoken | None = field(default=None, repr=False)
    on_dropped: OnDropped | None = field(default=None, repr=False)
    state: ItemState = field(default=ItemState.QUEUED, init=False)
    interruptions: int = field(default=0, init=False)
    drop_reason: str = field(default="", init=False)

    @property
    def terminal(self) -> bool:
        """True once the item has settled (``SPOKEN`` or ``DROPPED``)."""
        return self.state in _TERMINAL_STATES


class SpeechQueue:
    """The prioritized, expiring, silence-gated out-of-band speech buffer.

    Construct one per agent session. ``now`` stamps the initial silence anchor
    (a fresh session is silent until someone speaks); all subsequent calls pass
    the caller's monotonic ``now`` — the queue never reads a clock. Not
    thread-safe: confine to the session's event loop like every other per-
    session structure.
    """

    def __init__(
        self,
        now: float,
        *,
        grace_s: float = DEFAULT_SILENCE_GRACE_S,
        max_requeues: int = DEFAULT_MAX_REQUEUES,
        ttl_defaults: dict[SpeechPriority, float] | None = None,
    ) -> None:
        if grace_s < 0:
            raise ValueError(f"grace_s must be >= 0, got {grace_s}")
        if max_requeues < 0:
            raise ValueError(f"max_requeues must be >= 0, got {max_requeues}")
        self._grace_s = grace_s
        self._max_requeues = max_requeues
        self._ttls = dict(DEFAULT_TTLS)
        if ttl_defaults:
            self._ttls.update(ttl_defaults)
        self._queues: dict[SpeechPriority, list[SpeechItem]] = {p: [] for p in SpeechPriority}
        self._seq = 0
        self._in_flight: SpeechItem | None = None
        self._speaking = False
        self._silence_since: float | None = now
        self._closed = False

    # ------------------------------------------------------------------ #
    # Gating state machine (speech onsets injected by the wiring)        #
    # ------------------------------------------------------------------ #

    def note_speech_onset(self) -> None:
        """Someone started speaking (user *or* bot) — delivery blocks, anchor resets.

        Idempotent. The wiring decides which onsets it reports (e.g. it may skip
        the queue's own deliveries to chain back-to-back results without a grace
        gap — by default it should report them, spacing deliveries naturally).
        """
        self._speaking = True
        self._silence_since = None

    def note_silence_onset(self, now: float) -> None:
        """Speech ended at ``now`` — the grace clock starts (or keeps) running.

        Duplicate silence notifications while already silent keep the *original*
        anchor: a re-delivered event must not extend the wait.
        """
        if self._speaking or self._silence_since is None:
            self._speaking = False
            self._silence_since = now

    def silence_held(self, now: float) -> bool:
        """True when silence has held for at least ``grace_s`` — the release gate."""
        if self._speaking or self._silence_since is None:
            return False
        return (now - self._silence_since) >= self._grace_s

    # ------------------------------------------------------------------ #
    # Producer side                                                      #
    # ------------------------------------------------------------------ #

    def enqueue(
        self,
        text: str,
        priority: SpeechPriority,
        *,
        now: float,
        ttl_s: float | None = None,
        on_spoken: OnSpoken | None = None,
        on_dropped: OnDropped | None = None,
        task_id: int | None = None,
        kind: str = "",
        turn_id: str | None = None,
    ) -> SpeechItem:
        """Queue one utterance; returns its (queue-owned) :class:`SpeechItem`.

        ``ttl_s`` overrides the class default. On a closed queue the item is
        created and settled **dropped immediately** (``on_dropped`` fires with
        ``"queue closed"``) so a teardown race never strands an ack's ledger
        terminal — check ``item.terminal`` if you need to know.

        Raises :class:`ValueError` on blank ``text`` or non-positive ``ttl_s`` —
        both are wiring bugs better caught loud than spoken as silence.
        """
        if not text.strip():
            raise ValueError("speech item text must be non-empty")
        effective_ttl = self._ttls[priority] if ttl_s is None else ttl_s
        if effective_ttl <= 0:
            raise ValueError(f"ttl_s must be > 0, got {effective_ttl}")
        self._seq += 1
        item = SpeechItem(
            text=text,
            priority=priority,
            seq=self._seq,
            enqueued_at=now,
            expires_at=now + effective_ttl,
            task_id=task_id,
            kind=kind,
            turn_id=turn_id,
            on_spoken=on_spoken,
            on_dropped=on_dropped,
        )
        if self._closed:
            logger.warning(
                "speech_queue.enqueue on closed queue: dropping %s item seq=%d immediately",
                priority.name,
                item.seq,
            )
            self._settle(item, spoken=False, reason=DROP_QUEUE_CLOSED)
            return item
        self._insert(item)
        logger.debug(
            "speech_queue.enqueue: %s seq=%d ttl=%.1fs queued=%d",
            priority.name,
            item.seq,
            effective_ttl,
            len(self),
        )
        return item

    # ------------------------------------------------------------------ #
    # Consumer side (the trt.28 delivery loop)                           #
    # ------------------------------------------------------------------ #

    def pop_ready(self, now: float) -> SpeechItem | None:
        """The next deliverable item, or ``None``.

        Runs the expiry sweep first (so expired acks settle their terminals
        promptly even while gated), then releases the head of the highest non-
        empty priority class iff nothing is in flight and silence has held for
        the grace. The returned item is committed: it no longer expires, and the
        caller **must** settle it via :meth:`mark_spoken` /
        :meth:`mark_interrupted` (or :meth:`drop`).
        """
        if self._closed:
            return None
        self.sweep_expired(now)
        if self._in_flight is not None or not self.silence_held(now):
            return None
        for priority in SpeechPriority:
            bucket = self._queues[priority]
            if bucket:
                item = bucket.pop(0)
                item.state = ItemState.IN_FLIGHT
                self._in_flight = item
                logger.debug(
                    "speech_queue.pop_ready: releasing %s seq=%d after %.2fs queued",
                    priority.name,
                    item.seq,
                    now - item.enqueued_at,
                )
                return item
        return None

    def mark_spoken(self, item: SpeechItem, now: float) -> bool:
        """The item was delivered — fire ``on_spoken`` exactly once.

        Normal path: the in-flight item finished playing. Also accepted for a
        still-QUEUED item: the out-of-band consumption seam (module docstring) —
        a direct answer already carried this content, so it leaves the queue as
        *delivered*, never to be spoken twice. Returns ``False`` (logged) if the
        item already settled.
        """
        settled = self._settle(item, spoken=True)
        if settled:
            logger.debug(
                "speech_queue.mark_spoken: %s seq=%d settled %.2fs after enqueue",
                item.priority.name,
                item.seq,
                now - item.enqueued_at,
            )
        return settled

    def mark_interrupted(self, item: SpeechItem, now: float) -> bool:
        """The in-flight item was talked over — requeue once, then drop.

        Within the ``max_requeues`` budget the item re-enters its class at its
        original ``seq`` (ahead of later arrivals) keeping its original
        deadline; if that deadline already passed, it drops now with the expiry
        reason (an ack's ledger terminal must not wait for the next sweep).
        Past the budget it drops with ``"interrupted twice"``. Returns ``False``
        (logged) unless ``item`` is the current in-flight item; inspect
        ``item.state`` for the outcome (``QUEUED`` vs ``DROPPED``).
        """
        if item is not self._in_flight or item.state is not ItemState.IN_FLIGHT:
            logger.warning(
                "speech_queue.mark_interrupted: %s seq=%d is not the in-flight item "
                "(state=%s) — ignoring",
                item.priority.name,
                item.seq,
                item.state.value,
            )
            return False
        self._in_flight = None
        item.interruptions += 1
        if item.interruptions > self._max_requeues:
            self._settle(item, spoken=False, reason=interruption_drop_reason(item.interruptions))
            return True
        if now >= item.expires_at:
            self._settle(
                item,
                spoken=False,
                reason=expiry_drop_reason(item.expires_at - item.enqueued_at),
            )
            return True
        item.state = ItemState.QUEUED
        self._insert(item)
        logger.debug(
            "speech_queue.mark_interrupted: %s seq=%d re-queued (interruption %d/%d)",
            item.priority.name,
            item.seq,
            item.interruptions,
            self._max_requeues,
        )
        return True

    # ------------------------------------------------------------------ #
    # Maintenance / reads                                                #
    # ------------------------------------------------------------------ #

    def sweep_expired(self, now: float) -> list[SpeechItem]:
        """Drop every queued item whose deadline passed; returns them.

        Each drop fires ``on_dropped`` exactly once with the expiry reason. The
        in-flight item is never swept (committed to the mouth). ``pop_ready``
        calls this; the wiring may also call it on its own tick so terminals
        fire promptly while delivery stays gated.
        """
        dropped: list[SpeechItem] = []
        for bucket in self._queues.values():
            for item in [i for i in bucket if now >= i.expires_at]:
                self._settle(
                    item,
                    spoken=False,
                    reason=expiry_drop_reason(item.expires_at - item.enqueued_at),
                )
                dropped.append(item)
        return dropped

    def drop(
        self, item: SpeechItem, reason: str = DROP_CANCELLED, *, fire_callback: bool = True
    ) -> bool:
        """Explicitly settle ``item`` as dropped (queued or in-flight).

        ``fire_callback=False`` suppresses ``on_dropped`` but still settles the
        item terminally (its callbacks can never fire later) — for callers that
        take over the item's bookkeeping themselves. Returns ``False`` if it
        already settled.
        """
        return self._settle(item, spoken=False, reason=reason, fire=fire_callback)

    def close(self, reason: str = DROP_QUEUE_CLOSED) -> list[SpeechItem]:
        """Teardown: drop everything, then refuse further delivery. Idempotent.

        Fires each remaining item's ``on_dropped`` exactly once (queued items in
        delivery order, then the in-flight one) and returns them.
        ``AgentRuntime.aclose`` calls this in trt.28.
        """
        if self._closed:
            return []
        self._closed = True
        dropped = list(self.items())
        in_flight = self._in_flight
        if in_flight is not None:
            dropped.append(in_flight)
        for item in dropped:
            self._settle(item, spoken=False, reason=reason)
        logger.debug("speech_queue.close: dropped %d undelivered item(s)", len(dropped))
        return dropped

    def items(self) -> tuple[SpeechItem, ...]:
        """Snapshot of queued items in delivery order (in-flight excluded).

        The trt.28/29 read path: answering a turn about a delegated topic checks
        here for a queued RESULT to consume (then settles it via
        :meth:`mark_spoken`). Expiry is not evaluated — call
        :meth:`sweep_expired` first if staleness matters.
        """
        return tuple(item for p in SpeechPriority for item in self._queues[p])

    def __len__(self) -> int:
        """Queued item count (the in-flight item is not counted)."""
        return sum(len(bucket) for bucket in self._queues.values())

    @property
    def in_flight(self) -> SpeechItem | None:
        """The item currently committed to delivery, if any."""
        return self._in_flight

    @property
    def speaking(self) -> bool:
        """Last reported gating state: is someone audibly speaking?"""
        return self._speaking

    @property
    def silence_since(self) -> float | None:
        """The current silence anchor (``None`` while speech is on)."""
        return self._silence_since

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _insert(self, item: SpeechItem) -> None:
        """Insert into the item's class bucket ordered by ``seq``.

        Fresh enqueues carry the highest seq and append; a requeued item lands
        back ahead of anything enqueued after it. Buckets are a handful of
        items, so the linear walk is fine.
        """
        bucket = self._queues[item.priority]
        idx = len(bucket)
        while idx > 0 and bucket[idx - 1].seq > item.seq:
            idx -= 1
        bucket.insert(idx, item)

    def _settle(
        self, item: SpeechItem, *, spoken: bool, reason: str = "", fire: bool = True
    ) -> bool:
        """The single exactly-once chokepoint: detach + terminal + callback.

        First settle wins (``TurnLedger`` discipline); a second attempt logs and
        returns ``False`` without firing anything. Callback exceptions are
        logged and swallowed — a broken hook never corrupts queue state.
        """
        if item.terminal:
            logger.warning(
                "speech_queue: %s seq=%d already settled %s(%s) — ignoring %s",
                item.priority.name,
                item.seq,
                item.state.value,
                item.drop_reason,
                "spoken" if spoken else f"drop({reason})",
            )
            return False
        if self._in_flight is item:
            self._in_flight = None
        else:
            bucket = self._queues[item.priority]
            if item in bucket:
                bucket.remove(item)
        if spoken:
            item.state = ItemState.SPOKEN
            if fire and item.on_spoken is not None:
                try:
                    item.on_spoken(item)
                except Exception:
                    logger.exception(
                        "speech_queue: on_spoken hook failed for %s seq=%d",
                        item.priority.name,
                        item.seq,
                    )
        else:
            item.state = ItemState.DROPPED
            item.drop_reason = reason
            logger.info(
                "speech_queue: dropped %s seq=%d (%s)", item.priority.name, item.seq, reason
            )
            if fire and item.on_dropped is not None:
                try:
                    item.on_dropped(item, reason)
                except Exception:
                    logger.exception(
                        "speech_queue: on_dropped hook failed for %s seq=%d",
                        item.priority.name,
                        item.seq,
                    )
        return True


__all__ = [
    "ACK_DEFAULT_TTL_S",
    "DEFAULT_MAX_REQUEUES",
    "DEFAULT_SILENCE_GRACE_S",
    "DEFAULT_TTLS",
    "DROP_CANCELLED",
    "DROP_QUEUE_CLOSED",
    "ItemState",
    "NOTICE_DEFAULT_TTL_S",
    "OnDropped",
    "OnSpoken",
    "RESULT_DEFAULT_TTL_S",
    "STATUS_DEFAULT_TTL_S",
    "SpeechItem",
    "SpeechPriority",
    "SpeechQueue",
    "expiry_drop_reason",
    "interruption_drop_reason",
]
