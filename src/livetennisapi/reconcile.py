"""Parse live-feed frames and keep the per-match ``seq`` order yourself.

    from livetennisapi.reconcile import PointSequencer, ScoreBook, parse_frame

No socket and no HTTP in here. You call these with frames you already have:
from the native ``/ws`` feed on any WebSocket library (asyncio included),
from a webhook body, or from a ``PushStream`` frame's ``.raw``. Nothing here
needs ``websockets``.

:func:`parse_frame` turns one message into a typed frame object, or raises
:class:`FrameError` when the message is not a JSON object with a ``type``.

:class:`PointSequencer` handles ``point`` frames. They are events that the
server sends once, with no replay, so a dropped frame is a lost point until
you fetch it. Each match's ``seq`` starts at 1 and never skips a value, so the
sequencer can tell exactly what is missing. It drops repeats and holds frames
that arrive ahead of a gap. It tells you which range to fetch from
``GET /matches/{matchId}/points?after_seq=``, then releases the held frames
in ``seq`` order once the gap is filled.

:class:`ScoreBook` keeps the newest score per match and applies
``score_withdrawn`` frames: a withdrawn or withheld state is dropped, a
deferred one is kept and flagged.

Live Tennis API: https://livetennisapi.com
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Union, cast

from .models import LivePoint, Model, PointsPage
from .ws import BreakPoint, BreakPointResult, PointUpdate, ScoreUpdate

__all__ = [
    "ControlFrame",
    "FeedFrame",
    "FrameError",
    "Gap",
    "PointSequencer",
    "PointTagUpdate",
    "ScoreBook",
    "ScoreWithdrawn",
    "parse_frame",
]


class FrameError(ValueError):
    """The message is not a frame: bad JSON, not an object, or no ``type``.

    ``message`` holds what was received, so you can log it or count it.
    """

    def __init__(self, reason: str, message: Any) -> None:
        super().__init__(reason)
        self.message = message


@dataclass
class ScoreWithdrawn(Model):
    """One ``score_withdrawn`` frame: a score state already sent is taken back.

    ``superseded_sequence`` is the score ``sequence`` to act on. ``kind`` is
    ``withdrawn`` (discard it), ``deferred`` (it may be early, so keep it
    until the next accepted state) or ``withheld`` (nothing can be published
    for the match right now). The frame carries no replacement score: the
    next ``score`` frame, or ``GET /matches/{matchId}/score``, is where to
    resume.
    """

    _datetime_fields: ClassVar[tuple[str, ...]] = ("ts", "published_at")

    type: str | None = None
    match_id: int | None = None
    kind: str | None = None
    superseded_sequence: int | None = None
    reason: str | None = None
    safe_to_resume: bool | None = None
    ts: datetime | None = None
    published_at: datetime | None = None


@dataclass
class PointTagUpdate(Model):
    """One ``point_update`` frame: ``serve`` and ``outcome`` for a point already sent.

    Apply it to the point you hold with the same ``seq``. Nothing else about
    that point changes.
    """

    _datetime_fields: ClassVar[tuple[str, ...]] = ("published_at",)

    type: str | None = None
    match_id: int | None = None
    seq: int | None = None
    serve: int | None = None
    outcome: str | None = None
    published_at: datetime | None = None


@dataclass
class ControlFrame(Model):
    """Any other frame: ``subscribed``, ``ping``, ``error``, or a type this version does not know.

    Read ``type`` to tell them apart. Every field the server sent is on
    ``raw`` and readable as an attribute, for example ``frame.error``.
    """

    type: str | None = None


#: What :func:`parse_frame` returns.
FeedFrame = Union[
    ScoreUpdate,
    PointUpdate,
    PointTagUpdate,
    ScoreWithdrawn,
    BreakPoint,
    BreakPointResult,
    ControlFrame,
]

_PARSERS: dict[str, type[Model]] = {
    "score": ScoreUpdate,
    "point": PointUpdate,
    "point_update": PointTagUpdate,
    "score_withdrawn": ScoreWithdrawn,
    "break_point": BreakPoint,
    "break_point_result": BreakPointResult,
}


def parse_frame(message: str | bytes | bytearray | Mapping[str, Any]) -> FeedFrame:
    """Turn one feed message into a typed frame.

    Accepts the text or bytes of a WebSocket message, a webhook body, or a
    dict you already decoded. Raises :class:`FrameError` when the message is
    bad JSON, is not a JSON object, or has no ``type`` string.

    An unknown type comes back as :class:`ControlFrame`, because the feed
    adds frame types without a new API version.
    """
    data: Any = message
    if isinstance(message, (bytes, bytearray)):
        try:
            data = bytes(message).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameError("frame is not valid UTF-8", message) from exc
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise FrameError(f"frame is not valid JSON: {exc}", message) from exc
    if not isinstance(data, Mapping):
        raise FrameError("frame is not a JSON object", message)
    kind = data.get("type")
    if not isinstance(kind, str) or not kind:
        raise FrameError("frame has no type", message)
    parser = _PARSERS.get(kind, ControlFrame)
    frame = parser.from_dict(data)
    if frame is None:  # from_dict returns None only for None input
        raise FrameError("frame could not be parsed", message)
    return cast(FeedFrame, frame)


def _as_seq(value: Any) -> int | None:
    """A usable ``seq``: an int of at least 1. ``True`` is not a seq."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _as_match_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True)
class Gap:
    """Points missing from one match: everything after ``after_seq`` and before ``before_seq``.

    Fetch them with ``client.get_match_points(match_id, after_seq=gap.after_seq)``
    and hand each page to :meth:`PointSequencer.fill`. ``before_seq`` is the
    first point already held past the gap. The fetch can stop there. Reading
    on does no harm, because repeats are dropped.
    """

    match_id: int
    after_seq: int
    before_seq: int

    @property
    def missing(self) -> range:
        """The ``seq`` values not yet received."""
        return range(self.after_seq + 1, self.before_seq)


class PointSequencer:
    """Put ``point`` frames in ``seq`` order per match, drop repeats, report gaps.

    Feed it every point frame with :meth:`add` and every REST catch-up page
    with :meth:`fill`. Both return the points that are now ready, in ``seq``
    order, each exactly once.

    ``from_start`` decides what happens the first time a match shows up.
    False (the default) starts that match at the first point you see. That
    suits a live view on the native feed. A lower ``seq`` that turns up later
    is dropped as handled. True treats ``seq`` 1 as expected, so joining a
    match in the middle reports everything before as a gap to fetch. Use
    True, or :meth:`resume`, when frames can arrive out of order from the
    start, as webhooks can.

    ``seq`` is the order points were stored, not always the order they were
    played. To replay a match in playing order, sort by ``(set, game, number)``.
    """

    def __init__(self, *, from_start: bool = False) -> None:
        self.from_start = bool(from_start)
        self._cursors: dict[int, int] = {}
        self._held: dict[int, dict[int, PointUpdate]] = {}

    # -- state ----------------------------------------------------------------

    def cursor(self, match_id: int) -> int | None:
        """The highest ``seq`` released for a match, or None if it was never seen."""
        return self._cursors.get(match_id)

    @property
    def cursors(self) -> dict[int, int]:
        """A copy of every match's cursor. On reconnect, fetch ``after_seq=cursor`` for each."""
        return dict(self._cursors)

    def resume(self, match_id: int, last_seq: int) -> list[PointUpdate]:
        """Set a match's cursor, for example from a ``seq`` you stored before a restart.

        ``last_seq`` 0 means nothing is held yet, so ``seq`` 1 is the next one
        expected. Held points at or below the new cursor are dropped, and any
        held points that now follow on from it are returned, in ``seq`` order.
        The cursor never moves back, because points already returned would
        come out a second time. Call :meth:`forget` first to start over.
        """
        if isinstance(last_seq, bool) or not isinstance(last_seq, int) or last_seq < 0:
            raise ValueError(f"last_seq must be an int >= 0, got {last_seq!r}")
        current = self._cursors.get(match_id)
        if current is not None and last_seq < current:
            raise ValueError(f"match {match_id}: last_seq {last_seq} is behind the cursor {current}")
        self._cursors[match_id] = last_seq
        held = self._held.get(match_id)
        if held:
            for seq in [s for s in held if s <= last_seq]:
                del held[seq]
        return self._drain(match_id)

    def forget(self, match_id: int) -> None:
        """Drop all state for a match, for example once it has finished."""
        self._cursors.pop(match_id, None)
        self._held.pop(match_id, None)

    def held(self, match_id: int) -> int:
        """How many points of a match are waiting for a gap to be filled."""
        return len(self._held.get(match_id, ()))

    def gap(self, match_id: int) -> Gap | None:
        """The match's open gap, or None when nothing is missing."""
        held = self._held.get(match_id)
        cursor = self._cursors.get(match_id)
        if not held or cursor is None:
            return None
        return Gap(match_id=match_id, after_seq=cursor, before_seq=min(held))

    def gaps(self) -> list[Gap]:
        """Every open gap, one per match."""
        out = [self.gap(m) for m in self._held]
        return [g for g in out if g is not None]

    # -- input ----------------------------------------------------------------

    def add(self, frame: PointUpdate | Mapping[str, Any]) -> list[PointUpdate]:
        """Take one live ``point`` frame. Returns the points now ready, in ``seq`` order.

        An empty list means the frame was a repeat, or it is held behind a gap
        (check :meth:`gap`). A frame with no usable ``match_id`` or ``seq`` is
        returned as it is, because it cannot be ordered and dropping it would
        lose data.
        """
        update = frame if isinstance(frame, PointUpdate) else PointUpdate.from_dict(frame)
        if update is None:
            return []
        match_id = _as_match_id(update.match_id)
        seq = _as_seq(update.point.seq if isinstance(update.point, LivePoint) else None)
        if match_id is None or seq is None:
            return [update]
        return self._offer(match_id, seq, update)

    def fill(self, match_id: int, page: PointsPage | Mapping[str, Any] | Iterable[LivePoint]) -> list[PointUpdate]:
        """Take one REST page from ``get_match_points``. Returns the points now ready.

        Pass a :class:`~livetennisapi.PointsPage`, the decoded JSON of
        ``GET /matches/{matchId}/points``, or a list of
        :class:`~livetennisapi.LivePoint` (or point dicts). Points you
        already have are dropped. A page for another match raises
        ``ValueError``. Raises ``ValueError`` for a page on the ``reconstruction``
        basis: that is a different ``seq`` numbering for a finished match, so
        it cannot be merged with live frames. Read it from ``after_seq=0``
        with a fresh sequencer instead.
        """
        coverage: str | None = None
        quality: str | None = None
        if isinstance(page, Mapping):
            page = PointsPage.from_dict(page) or PointsPage()
        if isinstance(page, PointsPage):
            if page.raw.get("basis") == "reconstruction":
                raise ValueError(
                    f"match {match_id}: this page is on the reconstruction basis, whose seq numbering "
                    "differs from the live stream. Read it from after_seq=0 with a fresh PointSequencer"
                )
            page_match = _as_match_id(page.match_id)
            if page_match is not None and page_match != match_id:
                raise ValueError(f"this page is for match {page_match}, not match {match_id}")
            coverage, quality = page.pbp_coverage, page.quality
            points: Iterable[LivePoint] = page.points
        else:
            points = page

        parsed = [LivePoint.from_dict(p) if isinstance(p, Mapping) else p for p in points]
        usable = [(seq, p) for p in parsed if isinstance(p, LivePoint) and (seq := _as_seq(p.seq)) is not None]
        ready: list[PointUpdate] = []
        for seq, point in sorted(usable, key=lambda pair: pair[0]):
            update = PointUpdate(match_id=match_id, point=point, pbp_coverage=coverage, quality=quality)
            update.raw = {
                "type": "point",
                "match_id": match_id,
                "point": point.raw,
                "pbp_coverage": coverage,
                "quality": quality,
            }
            ready.extend(self._offer(match_id, seq, update))
        return ready

    # -- internals ------------------------------------------------------------

    def _offer(self, match_id: int, seq: int, update: PointUpdate) -> list[PointUpdate]:
        cursor = self._cursors.get(match_id)
        if cursor is None:
            cursor = 0 if self.from_start else seq - 1
            self._cursors[match_id] = cursor
        if seq <= cursor:
            return []  # already released
        held = self._held.setdefault(match_id, {})
        if seq in held:
            return []  # already waiting
        held[seq] = update
        return self._drain(match_id)

    def _drain(self, match_id: int) -> list[PointUpdate]:
        """Release held points that follow on from the cursor."""
        held = self._held.get(match_id)
        if held is None:
            return []
        cursor = self._cursors[match_id]
        ready: list[PointUpdate] = []
        while cursor + 1 in held:
            cursor += 1
            ready.append(held.pop(cursor))
        self._cursors[match_id] = cursor
        if not held:
            del self._held[match_id]
        return ready


class ScoreBook:
    """The newest score per match, with ``score_withdrawn`` applied.

    Pass every ``score`` and ``score_withdrawn`` frame to :meth:`apply`. Other
    frames are ignored. Scores are ordered by the score's ``sequence``, so an
    older state arriving late never replaces a newer one.

    On ``withdrawn`` or ``withheld`` the matching state is removed, and
    :meth:`get` returns None until another score arrives. That can be an
    older state re-served by ``GET /matches/{matchId}/score``. Only the
    sequence taken back is refused.

    On ``deferred``, or a kind this version does not know, the state stays.
    :meth:`verdict` returns the frame so you can show the score as
    provisional. A newer score clears the verdict.
    """

    def __init__(self) -> None:
        self._scores: dict[int, ScoreUpdate] = {}
        self._verdicts: dict[int, ScoreWithdrawn] = {}
        #: Per match, the sequences taken back. A score with one of them is never shown again.
        self._taken_back: dict[int, set[int]] = {}

    @staticmethod
    def sequence(update: ScoreUpdate) -> int | None:
        """The score's ``sequence``, read from the nested score object."""
        score = update.score
        value = score.raw.get("sequence") if isinstance(score, Model) else None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def get(self, match_id: int) -> ScoreUpdate | None:
        """The score to show for a match, or None if there is none you can rely on."""
        return self._scores.get(match_id)

    def verdict(self, match_id: int) -> ScoreWithdrawn | None:
        """The last ``score_withdrawn`` that applied to this match's current state."""
        return self._verdicts.get(match_id)

    def forget(self, match_id: int) -> None:
        """Drop all state for a match, for example once it has finished."""
        self._scores.pop(match_id, None)
        self._verdicts.pop(match_id, None)
        self._taken_back.pop(match_id, None)

    def apply(self, frame: FeedFrame) -> bool:
        """Apply one frame. Returns True when the score for that match changed."""
        if isinstance(frame, ScoreUpdate):
            return self._apply_score(frame)
        if isinstance(frame, ScoreWithdrawn):
            return self._apply_withdrawal(frame)
        return False

    def _apply_score(self, update: ScoreUpdate) -> bool:
        match_id = _as_match_id(update.match_id)
        if match_id is None:
            return False
        held = self._scores.get(match_id)
        new_seq = self.sequence(update)
        old_seq = self.sequence(held) if held is not None else None
        if new_seq is not None and old_seq is not None and new_seq <= old_seq:
            return False  # stale or repeated
        if new_seq is not None and new_seq in self._taken_back.get(match_id, ()):
            return False  # the state that was taken back
        self._scores[match_id] = update
        self._verdicts.pop(match_id, None)
        return True

    def _apply_withdrawal(self, frame: ScoreWithdrawn) -> bool:
        match_id = _as_match_id(frame.match_id)
        if match_id is None:
            return False
        superseded = frame.superseded_sequence
        if isinstance(superseded, bool) or not isinstance(superseded, int):
            return False  # names no state, so it cannot apply to one
        discard = frame.kind in ("withdrawn", "withheld")
        if discard:
            # Remember it even when it is not the state we hold, so the
            # score frame cannot slip in later if the two arrive swapped.
            self._taken_back.setdefault(match_id, set()).add(superseded)
        held = self._scores.get(match_id)
        if held is None or self.sequence(held) != superseded:
            return False  # not the state we hold
        self._verdicts[match_id] = frame
        if not discard:
            return False  # deferred, or a kind this version does not know: keep it, flagged
        del self._scores[match_id]
        return True
