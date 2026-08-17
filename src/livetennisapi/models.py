"""Response models.

Two rules govern everything here, both taken from the API's own contract:

1. **Never reject an unknown field.** The spec states that additive changes ship
   within ``v1``, so a client that validates strictly will break the first time
   a field is added. Unknown keys are kept in :attr:`Model.raw` and are also
   reachable as attributes, so a new server-side field is usable from an old
   client without an upgrade.

2. **Never lose the payload.** Every model keeps the exact dict it was built
   from. If a model is wrong, ``obj.raw`` is still the truth.

Consequently ``from_dict`` never raises on shape. A field that is absent
becomes ``None``; a field of an unexpected type is passed through untouched
rather than coerced.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from typing import Any, ClassVar, TypeVar

__all__ = [
    "Model",
    "ListMeta",
    "Page",
    "Score",
    "Player",
    "Match",
    "Analysis",
    "Market",
    "Price",
    "Event",
    "Fixture",
    "Tournament",
    "ArchiveMatch",
    "ArchiveParticipant",
    "ArchivePlayerBio",
    "ArchiveCareer",
    "HeadToHead",
    "TapeRow",
    "TapeMeta",
    "HistoryTape",
    "MatchStatistics",
    "RankingRecord",
    "LivePoint",
    "PointsPage",
    "RallyPoint",
    "RallyMatch",
    "ChartingPlayer",
    "ChartingMatch",
    "HistoryPackage",
    "WSToken",
    "Usage",
]

T = TypeVar("T", bound="Model")


def _parse_datetime(value: Any) -> Any:
    """ISO 8601 -> datetime, leaving anything unparseable exactly as it came.

    The API documents UTC with a ``Z`` suffix, which ``fromisoformat`` only
    accepts natively from Python 3.11, so ``Z`` is normalised first.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value


def _parse_date(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        return date.fromisoformat(value)
    except ValueError:
        return value


@dataclass
class Model:
    """Base for every response object.

    Subclasses declare the fields documented in the spec. Anything else the
    server sends is preserved: available via :attr:`raw`, and readable as an
    attribute through :meth:`__getattr__`.
    """

    #: Fields parsed as ISO 8601 datetimes.
    _datetime_fields: ClassVar[tuple[str, ...]] = ()
    #: Fields parsed as ISO 8601 dates.
    _date_fields: ClassVar[tuple[str, ...]] = ()

    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls: type[T], data: Mapping[str, Any] | None) -> T | None:
        """Build a model from a response object. ``None`` in, ``None`` out."""
        if data is None:
            return None
        if not isinstance(data, Mapping):
            # The server sent something unexpected. Keep it rather than crash.
            return cls(raw={"_unexpected": data})  # type: ignore[arg-type]

        known = {f.name for f in fields(cls)} - {"raw"}
        kwargs: dict[str, Any] = {}
        for name in known:
            if name not in data:
                continue
            value = data[name]
            if name in cls._datetime_fields:
                value = _parse_datetime(value)
            elif name in cls._date_fields:
                value = _parse_date(value)
            kwargs[name] = value

        obj = cls(**kwargs)  # type: ignore[arg-type]
        obj.raw = dict(data)
        return obj

    def __getattr__(self, name: str) -> Any:
        """Expose fields the server sent that this version doesn't declare.

        Only consulted when normal attribute lookup fails, so declared fields
        always win and this costs nothing on the common path.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return object.__getattribute__(self, "raw")[name]
        except (KeyError, AttributeError):
            raise AttributeError(
                f"{type(self).__name__!r} has no field {name!r} (and the server did not send one)"
            ) from None

    def to_dict(self) -> dict[str, Any]:
        """The original payload, exactly as received."""
        return dict(self.raw)


@dataclass
class ListMeta(Model):
    """Pagination envelope returned alongside list responses.

    ``total`` is the size of the whole filtered set (``None`` when it cannot be
    counted cheaply); ``has_more`` says more results exist beyond this page —
    read that rather than comparing ``count`` to ``limit``.
    """

    limit: int | None = None
    offset: int | None = None
    count: int | None = None
    total: int | None = None
    has_more: bool | None = None


@dataclass
class Score(Model):
    """A match score at a point in time.

    ``sets`` is ``[sets_p1, sets_p2]``.

    ``games`` is ``[games_p1, games_p2]`` where **each side is a per-set list**
    — so ``[[6, 3, 2], [4, 6, 1]]`` reads 6-4, 3-6, 2-1. Note this is
    player-major, not set-major; indexing it the other way is the single most
    common mistake against this API.

    ``win_probability_p1`` and ``danger`` are present only on the ULTRA tier —
    on REST and on the WebSocket score frames alike. A null value means the
    model had no output for that state, not that the channel withholds it.
    """

    _datetime_fields: ClassVar[tuple[str, ...]] = ("timestamp",)

    sets: list[int] | None = None
    games: list[list[int]] | None = None
    points: list[str] | None = None
    server: int | None = None
    is_tiebreak: bool | None = None
    win_probability_p1: float | None = None
    danger: float | None = None
    timestamp: datetime | None = None

    def games_for_set(self, set_index: int) -> tuple[int | None, int | None]:
        """Games for one set as ``(p1, p2)``, guarding the player-major layout."""
        if not self.games or len(self.games) < 2:
            return (None, None)
        p1, p2 = self.games[0] or [], self.games[1] or []
        return (
            p1[set_index] if set_index < len(p1) else None,
            p2[set_index] if set_index < len(p2) else None,
        )


@dataclass
class Player(Model):
    _date_fields: ClassVar[tuple[str, ...]] = ("birthday",)

    id: int | None = None
    name: str | None = None
    tour: str | None = None
    country: str | None = None
    ranking: int | None = None
    ranking_points: int | None = None
    ranking_movement: str | None = None
    hand: str | None = None
    backhand: int | None = None
    birthday: date | None = None
    is_doubles_team: bool | None = None
    #: Only populated by the single-player endpoint.
    stats: dict[str, Any] | None = None


@dataclass
class Price(Model):
    """One price tick. ``side`` is 1 for p1's outcome, 2 for p2's."""

    _datetime_fields: ClassVar[tuple[str, ...]] = ("timestamp",)

    side: int | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread: float | None = None
    timestamp: datetime | None = None


@dataclass
class Market(Model):
    """A match-winner market. PRO tier and above."""

    _datetime_fields: ClassVar[tuple[str, ...]] = ("end_date",)

    id: int | None = None
    question: str | None = None
    status: str | None = None
    volume: float | None = None
    liquidity: float | None = None
    end_date: datetime | None = None
    prices: list[Price] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Market | None:
        obj = super().from_dict(data)
        if obj is not None and isinstance(obj.prices, list):
            obj.prices = [p for p in (Price.from_dict(x) for x in obj.prices) if p]
        return obj


@dataclass
class Analysis(Model):
    """Model analysis for a match. ULTRA tier only; either half may be null."""

    thesis: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None


@dataclass
class Event(Model):
    """A match event. PRO tier and above."""

    _datetime_fields: ClassVar[tuple[str, ...]] = ("timestamp",)

    type: str | None = None
    player: int | None = None
    timestamp: datetime | None = None


@dataclass
class Fixture(Model):
    """A scheduled fixture.

    Names are always present; ``player1_id`` / ``player2_id`` are our roster
    ids where the participant resolves by exact key — ``None`` otherwise, which
    is a real state, not an omission. ``start_time`` is ``None`` until the
    order of play assigns a time — a date-only fixture is a real state too.
    """

    _date_fields: ClassVar[tuple[str, ...]] = ("event_date",)
    _datetime_fields: ClassVar[tuple[str, ...]] = ("start_time",)

    id: int | None = None
    event_date: date | None = None
    start_time: datetime | None = None
    player1_id: int | None = None
    player2_id: int | None = None
    tour: str | None = None
    tournament: str | None = None
    round: str | None = None
    #: Normalized round — same vocabulary as :attr:`Match.round_code`.
    round_code: str | None = None
    surface: str | None = None
    player1_name: str | None = None
    player2_name: str | None = None
    status: str | None = None


@dataclass
class Match(Model):
    """A match.

    ``market`` is present from PRO, ``analysis`` from ULTRA — both are absent
    (not null) below those tiers, so treat ``None`` as "not entitled or not
    available" rather than "no market exists".
    """

    _datetime_fields: ClassVar[tuple[str, ...]] = ("scheduled_time",)

    id: int | None = None
    tournament: str | None = None
    #: ``atp`` | ``wta`` | ``challenger`` | ``itf`` | ``juniors`` — the same
    #: vocabulary the ``tour=`` filter accepts, so a match selected by
    #: ``tour="itf"`` always carries ``"itf"`` here. ``None`` when the feed
    #: never stated a tour (exhibitions, team and mixed events). Group and
    #: filter on this; never parse the tournament name for it.
    tour: str | None = None
    #: Stable tournament identity — one id per tournament × event type, stable
    #: across seasons. Joins :meth:`~livetennisapi.LiveTennisAPI.get_tournament`.
    #: ``None`` on matches ingested before the catalogue covered their tournament.
    tournament_id: str | None = None
    surface: str | None = None
    indoor: bool | None = None
    format: str | None = None
    round: str | None = None
    #: The round in the archive's controlled vocabulary (``F`` ``SF`` ``QF``
    #: ``R16`` … ``Q1``-``Q4``), normalized from the free-text ``round`` label.
    #: This is the field to branch on — it matches the results archive's
    #: ``round=`` filter exactly. ``None`` when unrecognised, never guessed.
    round_code: str | None = None
    status: str | None = None
    #: How the match ended (or paused) when it did not run its course:
    #: ``Retired`` | ``Cancelled`` | ``Walk Over`` | ``Postponed`` |
    #: ``Interrupted``. ``None`` means completed normally OR never resolved —
    #: the feed does not distinguish those.
    event_status: str | None = None
    is_doubles: bool | None = None
    scheduled_time: datetime | None = None
    players: dict[str, Any] | None = None
    score: Score | None = None
    winner: int | None = None
    #: *(completed only)* which player retired or conceded the walkover —
    #: present only when ``event_status`` is ``Retired``/``Walk Over`` and the
    #: winner is derivable. Absent means "not a withdrawal, or no evidence".
    withdrew: int | None = None
    market: Market | None = None
    analysis: Analysis | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Match | None:
        obj = super().from_dict(data)
        if obj is None:
            return None
        if isinstance(obj.score, Mapping):
            obj.score = Score.from_dict(obj.score)
        if isinstance(obj.market, Mapping):
            obj.market = Market.from_dict(obj.market)
        if isinstance(obj.analysis, Mapping):
            obj.analysis = Analysis.from_dict(obj.analysis)
        if isinstance(obj.players, Mapping):
            obj.players = {
                key: (Player.from_dict(val) if isinstance(val, Mapping) else val) for key, val in obj.players.items()
            }
        return obj

    @property
    def p1(self) -> Player | None:
        """Player 1, or ``None`` if the payload had no players object."""
        return (self.players or {}).get("p1")

    @property
    def p2(self) -> Player | None:
        return (self.players or {}).get("p2")


@dataclass
class Tournament(Model):
    """A tournament-catalogue row — the stable id space ``Match.tournament_id`` joins.

    One id per tournament × event type, stable across seasons. ``city`` and
    ``country`` (host location, ISO-3166 alpha-2) come from a curated table and
    are ``None`` where not curated; ``category`` (``grand_slam`` |
    ``masters_1000`` | ``tour_finals`` | ``atp_500`` | … | ``juniors``) is set
    only where our catalogues agree unambiguously on an exact-name join —
    never derived from the name.
    """

    id: str | None = None
    name: str | None = None
    tour: str | None = None
    surface: str | None = None
    indoor: bool | None = None
    city: str | None = None
    country: str | None = None
    category: str | None = None


@dataclass
class ArchiveParticipant(Model):
    """One side of a results-archive record — winner or loser.

    ``player_id`` is the corpus person id (joins the archive player bios
    within the same tour), NOT a roster player id: the archive is its own id
    space. ``rank`` is the player's rank AT THE TIME of the match, as
    published; ``age`` is at the time of the match; ``entry`` is the draw
    entry where recorded (``WC``/``Q``/``LL``/…), ``None`` for direct
    acceptances.
    """

    name: str | None = None
    hand: str | None = None
    country: str | None = None
    rank: int | None = None
    seed: int | None = None
    player_id: int | None = None
    height_cm: int | None = None
    age: float | None = None
    entry: str | None = None


@dataclass
class ArchiveMatch(Model):
    """One result from the results archive (1968–2022).

    ATP and WTA — main draws, qualifying and the ITF/futures tiers, 1968
    through 2022. Winner/loser-shaped: results data is recorded that way at
    the source, so the winner is a field, never an inference. The archive ends
    2022-12-31 by design — from 2023 the history product serves our own
    matches with the point-by-point tape, so no match is ever served from two
    datasets.

    ``event_date`` is the TOURNAMENT START date — per-match dates do not exist
    in this era's records, and none are invented. ``stats`` (detail endpoint
    only) holds per-match serve statistics where the era recorded them, and is
    ``None`` for most rows before 1991 — that ``None`` is honest, never filled
    in.
    """

    _date_fields: ClassVar[tuple[str, ...]] = ("event_date",)

    id: int | None = None
    #: The stable corpus key.
    source_id: str | None = None
    tour: str | None = None
    #: Source tier code: G, M, A, F, D, C, O, or a futures category code (e.g. "15").
    level: str | None = None
    tournament: str | None = None
    surface: str | None = None
    draw_size: int | None = None
    event_date: date | None = None
    round: str | None = None
    best_of: int | None = None
    minutes: int | None = None
    winner: ArchiveParticipant | None = None
    loser: ArchiveParticipant | None = None
    #: The final score as published, e.g. ``"6-4 7-6(5)"``, ``"6-3 RET"``, ``"W/O"``.
    score: str | None = None
    #: ``completed`` | ``retired`` | ``walkover`` | ``default`` | ``abandoned``,
    #: parsed from the score's own vocabulary; ``None`` when unparseable.
    outcome: str | None = None
    stats: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ArchiveMatch | None:
        obj = super().from_dict(data)
        if obj is None:
            return None
        if isinstance(obj.winner, Mapping):
            obj.winner = ArchiveParticipant.from_dict(obj.winner)
        if isinstance(obj.loser, Mapping):
            obj.loser = ArchiveParticipant.from_dict(obj.loser)
        return obj


@dataclass
class ArchivePlayerBio(Model):
    """One person of the results archive (1968–2022).

    ``id`` is the corpus person id that archive match rows carry as
    ``winner.player_id`` / ``loser.player_id``, scoped per tour — never a
    roster id. ``career_high_rank`` and ``career_high_date`` (the earliest
    week that rank was reached) are computed offline from the corpus's own
    weekly ranking tables (ATP from 1973, WTA from 1984), never modelled.
    ``None`` fields are the era's silence.
    """

    _date_fields: ClassVar[tuple[str, ...]] = ("dob", "career_high_date")

    id: int | None = None
    tour: str | None = None
    name: str | None = None
    hand: str | None = None
    dob: date | None = None
    country: str | None = None
    height_cm: int | None = None
    career_high_rank: int | None = None
    career_high_date: date | None = None


@dataclass
class ArchiveCareer(Model):
    """Career aggregates over the results archive (1968–2022).

    Everything is a sum or a ratio of sums over rows the archive list can
    fetch individually — nothing is modelled. ``serve["matches_with_stats"]``
    states the coverage honestly: the corpus records per-match serve
    statistics from 1991 only, so a 1970s career has a full W-L record and an
    empty serve block.
    """

    player: dict[str, Any] | None = None
    span: dict[str, Any] | None = None
    record: dict[str, Any] | None = None
    by_year: list[dict[str, Any]] | None = None
    serve: dict[str, Any] | None = None


@dataclass
class HeadToHead(Model):
    """The record between two players, across both halves of the product.

    The results archive (1968–2022) plus our own completed matches (2023→now)
    in one call. ``totals`` counts only meetings with a KNOWN winner;
    ``undecided`` counts the rest. Each meeting carries ``era`` (``archive`` |
    ``current``), ``outcome`` (walkovers and retirements are part of the
    record — exclude them yourself if you want to) and ``winner`` — which is
    **1|2 of the request** (your ``p1``/``p2``), not of the underlying match
    row. ``players`` is ``None`` when no player matches the name fragments.
    """

    players: dict[str, Any] | None = None
    totals: dict[str, Any] | None = None
    by_surface: dict[str, Any] | None = None
    meetings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TapeRow(Score):
    """One row of a match tape — a :class:`Score` plus point attribution.

    Rows we watched live carry a real ``timestamp``; rows expanded after the
    fact from a finished-match record carry a null ``timestamp`` AND null
    model fields — nothing is synthesised, so a null ``timestamp`` is the
    reliable row-level marker of a reconstructed row.

    ``point_winner`` (1|2) is who won the point this row records. It is
    present ONLY on ``sequence="clean"`` rows, and only where the transition
    from the previous row is a single attributable point — null on gaps, torn
    rows and the first row, and never on the raw sequence (raw rows are
    corrections, not points). Derived at read time, never guessed.
    """

    point_winner: int | None = None


@dataclass
class TapeMeta(Model):
    """Coverage metadata for one tape.

    ``coverage`` says how the tape came to exist (``from_start`` | ``partial``
    | ``reconstructed`` | ``reconstructed_partial`` | ``none``) and
    ``point_source`` where the served rows came from (``observed`` |
    ``reconstructed`` | ``mixed``, null on an empty tape) — read both before
    backtesting. ``rows`` is the served length AFTER any ``sequence="clean"``
    collapse; ``raw_rows`` the length before it.
    """

    _datetime_fields: ClassVar[tuple[str, ...]] = ("generated_at",)

    match_id: int | None = None
    rows: int | None = None
    coverage: str | None = None
    point_source: str | None = None
    raw_rows: int | None = None
    unique_states: int | None = None
    sequence: str | None = None
    from_archive: bool | None = None
    generated_at: datetime | None = None


@dataclass
class HistoryTape(Model):
    """The point-by-point tape for one match. **BASIC, or any History plan.**

    ``tape`` is the chronological score sequence; it works on a LIVE match
    too, assembled from whatever has been committed so far. ``tiebreaks`` is
    per-set tiebreak final scores from observed states only, aligned to the
    sets of the final scoreline — ``{"p1": …, "p2": …}`` for a 7-6 set whose
    observed maximum tiebreak state is a valid terminal shape, null per set
    otherwise, and null overall when the match has no 7-6 set. ``profiles``
    holds model profiles, oldest first.
    """

    match: Match | None = None
    tape: list[TapeRow] = field(default_factory=list)
    tiebreaks: list[dict[str, Any] | None] | None = None
    profiles: list[dict[str, Any]] | None = None
    meta: TapeMeta | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> HistoryTape | None:
        obj = super().from_dict(data)
        if obj is None:
            return None
        if isinstance(obj.match, Mapping):
            obj.match = Match.from_dict(obj.match)
        if isinstance(obj.tape, list):
            obj.tape = [r for r in (TapeRow.from_dict(x) for x in obj.tape) if r is not None]
        if isinstance(obj.meta, Mapping):
            obj.meta = TapeMeta.from_dict(obj.meta)
        return obj


@dataclass
class MatchStatistics(Model):
    """In-play statistics for one match. **ULTRA.**

    Two families that are deliberately not merged: the top level of
    ``players["pN"]`` is DERIVED from the point-by-point record;
    ``players["pN"]["measured"]`` is counted upstream — which is why only the
    measured family can hold aces, double faults, the serve split, winners and
    unforced errors. Every measured field is optional and an absent field is
    OMITTED, never zero-filled.

    Branch on ``freshness["derived"]`` / ``freshness["measured"]`` rather than
    the top-level ``coverage``, which only summarises the response. The two
    ``age_seconds`` use DIFFERENT clocks (derived: behind the newest score
    row; measured: wall clock) and must not be compared. ``coverage`` of
    ``"none"`` on both families returns 200 with null ``players`` — the match
    exists and holding nothing for it is the honest answer.
    """

    match_id: int | None = None
    coverage: str | None = None
    as_of: str | None = None
    age_seconds: int | None = None
    games_counted: int | None = None
    tiebreak_games_excluded: int | None = None
    inconsistent_games_excluded: int | None = None
    sets_covered: list[int] | None = None
    freshness: dict[str, Any] | None = None
    detail: str | None = None
    players: dict[str, Any] | None = None

    @property
    def p1(self) -> dict[str, Any] | None:
        """Player 1's statistics block, or ``None`` when nothing is held."""
        return (self.players or {}).get("p1")

    @property
    def p2(self) -> dict[str, Any] | None:
        return (self.players or {}).get("p2")


@dataclass
class RankingRecord(Model):
    """One ranking record in force at the requested instant.

    ``system`` is always explicit and the systems are never collapsed into a
    single "rank" — they are not comparable. ATP/WTA and the ITF circuits
    carry ``rank`` + ``points``; UTR carries ``rating`` with null rank and
    points, because a rating has neither. ``previous_rank`` is the rank at the
    immediately preceding snapshot week (ATP/WTA only; null when no prior week
    is held, and always null for ITF/UTR); ``rank_movement`` is the circuit's
    own signed weekly movement (ITF systems only). ``player_name`` is present
    on listing rows — where ``player_id`` may be null for players outside the
    roster, so the published table has no silent holes.
    """

    _date_fields: ClassVar[tuple[str, ...]] = ("effective_date",)
    _datetime_fields: ClassVar[tuple[str, ...]] = ("observed_at",)

    player_id: int | None = None
    player_name: str | None = None
    system: str | None = None
    tour: str | None = None
    rank: int | None = None
    points: int | None = None
    previous_rank: int | None = None
    rank_movement: int | None = None
    rating: float | None = None
    effective_date: date | None = None
    observed_at: datetime | None = None


@dataclass
class LivePoint(Model):
    """One committed point of a match's point-by-point stream. **ULTRA.**

    ``seq`` is the per-match point sequence — monotonic and gapless, starting
    at 1 — and is THE dedup/resume key: the same point carries the same
    ``seq`` whether it arrived over REST (:meth:`~livetennisapi.LiveTennisAPI.get_match_points`)
    or as a push/WebSocket ``point`` frame, so the two reads deduplicate
    against each other by ``seq`` alone.

    ``score`` is the point score AFTER this point, as display strings
    (``{"p1": "40", "p2": "30"}``); ``sets`` is ``[sets_p1, sets_p2]`` and
    ``games`` follows the usual player-major layout (each side a per-set
    list). ``server`` / ``winner`` are 1|2, null when the feed did not state
    them. ``ts`` is the capture time, not an on-court clock.
    """

    _datetime_fields: ClassVar[tuple[str, ...]] = ("ts",)

    seq: int | None = None
    set: int | None = None
    game: int | None = None
    number: int | None = None
    tiebreak: bool | None = None
    server: int | None = None
    winner: int | None = None
    score: dict[str, Any] | None = None
    sets: list[int] | None = None
    games: list[list[int]] | None = None
    ts: datetime | None = None


@dataclass
class PointsPage(Model):
    """One page of a match's point-by-point stream. **ULTRA.**

    ``points`` is ordered by ``seq``; ``last_seq`` is the highest sequence on
    this page — the cursor for the next read (``after_seq=last_seq``) — and
    ``has_more`` says whether committed points exist beyond it (read that,
    never the page length). ``pbp_coverage`` (``point`` | ``game``) and
    ``quality`` (``clean`` | ``revised``) describe the whole match's stream,
    not this page. ``covers_from_start`` says whether ``seq`` 1 really is the
    match's first point; ``None`` means the server did not state it (older
    servers omit the field entirely) — not measured, never "no".
    """

    match_id: int | None = None
    pbp_coverage: str | None = None
    quality: str | None = None
    covers_from_start: bool | None = None
    points: list[LivePoint] = field(default_factory=list)
    last_seq: int | None = None
    has_more: bool | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> PointsPage | None:
        obj = super().from_dict(data)
        if obj is None:
            return None
        if isinstance(obj.points, list):
            obj.points = [p for p in (LivePoint.from_dict(x) for x in obj.points) if p is not None]
        else:
            # A null or garbage ``points`` reads as an empty page — the same
            # normalization the list endpoints apply to ``data`` — so the
            # page iterators and the push resume never crash on it.
            obj.points = []
        return obj

    def __iter__(self):
        return iter(self.points)

    def __len__(self) -> int:
        return len(self.points)


@dataclass
class RallyPoint(Model):
    """One charted point — how the point was played, not just what it scored.

    The parsed fields are our reading of the charter's own notation string
    (available as :attr:`notation`, always present verbatim); ``parsed`` is
    False when the notation contained something we could not read cleanly —
    the recognised part is still returned, so filter on ``parsed`` if you want
    only unambiguous rows. ``rally_length`` counts strokes including the serve
    (an ace is 1, a double fault 0). ``outcome`` of ``"error"`` means the
    charter recorded a miss without saying whether it was forced — never
    guessed.
    """

    point: int | None = None
    set: list[int | None] | None = None
    games: list[int | None] | None = None
    score: str | None = None
    game: int | None = None
    is_tiebreak: bool | None = None
    server: int | None = None
    point_winner: int | None = None
    parsed: bool | None = None
    serve_number: int | None = None
    serve_direction: str | None = None
    rally_length: int | None = None
    outcome: str | None = None
    error_location: str | None = None
    ending_stroke: str | None = None
    ending_wing: str | None = None
    is_ace: bool | None = None
    is_double_fault: bool | None = None
    is_serve_and_volley: bool | None = None
    shots: list[dict[str, Any]] | None = None

    @property
    def notation(self) -> str | None:
        """The charter's own shot string, verbatim (the wire field ``raw``).

        Named ``notation`` here because ``.raw`` is already every model's
        whole payload; both serves are joined by ``;`` when the first was a
        fault.
        """
        value = self.raw.get("raw")
        return value if isinstance(value, str) else None


@dataclass
class RallyMatch(Model):
    """One charted match. **ULTRA.**

    Rally construction has its OWN id space (``rally_match_id``): the charted
    corpus reaches back decades and concentrates on the biggest events, so
    most charted matches predate our own collection — ``match_id`` is our id
    only when the charted match is also one we hold, null otherwise.
    ``points_parsed`` over ``points`` is the per-match parse-quality number.
    The detail endpoints add ``rally`` (the points, in play order, paged) and
    ``meta`` (``meta.total`` is the match's full point count).
    """

    _date_fields: ClassVar[tuple[str, ...]] = ("date",)

    rally_match_id: int | None = None
    source_id: str | None = None
    match_id: int | None = None
    date: date | None = None
    tournament: str | None = None
    round: str | None = None
    surface: str | None = None
    gender: str | None = None
    best_of: int | None = None
    players: list[dict[str, Any]] | None = None
    points: int | None = None
    points_parsed: int | None = None
    #: Detail endpoints only — the charted points, in play order.
    rally: list[RallyPoint] = field(default_factory=list)
    meta: ListMeta | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> RallyMatch | None:
        obj = super().from_dict(data)
        if obj is None:
            return None
        if isinstance(obj.rally, list):
            obj.rally = [p for p in (RallyPoint.from_dict(x) for x in obj.rally) if p is not None]
        if isinstance(obj.meta, Mapping):
            obj.meta = ListMeta.from_dict(obj.meta)
        return obj


@dataclass
class ChartingPlayer(Model):
    """Career shot-level charting aggregate for one player. **ULTRA.**

    From the Match Charting Project: serve placement, return depth and
    outcomes, net and serve-and-volley conversion, clutch serving and
    returning, winners and errors by wing, rally-length and shot-direction
    tendencies. Every number in ``families`` is a raw SUM over the player's
    charted matches and ``matches_charted`` states the sample. Coverage is
    curated — concentrated on the majors, NOT full-slate.
    """

    player: dict[str, Any] | None = None
    matches_charted: int | None = None
    coverage: str | None = None
    families: dict[str, Any] | None = None


@dataclass
class ChartingMatch(Model):
    """One charted match, every stat family for both players. **ULTRA.**

    ``families`` carries the per-set split (rows ``1``, ``2``, ``Total``)
    exactly as charted. ``charting_match_id`` is this product's own id space.
    """

    charting_match_id: int | None = None
    mcp_id: str | None = None
    gender: str | None = None
    players: dict[str, Any] | None = None
    families: dict[str, Any] | None = None


@dataclass
class HistoryPackage(Model):
    """A published monthly bulk package.

    Coverage is not a contiguous run of months and is still being extended
    backwards — treat the packages listing as the authoritative set of months
    that exist. The JSONL file holds one line PER MATCH (a whole tape object,
    coverage meta included); the CSV is one row per point and carries no
    coverage columns. ``kind`` is present only on non-tape packages; on a
    rankings package ``match_count`` counts players and ``row_count`` ranking
    records.
    """

    _datetime_fields: ClassVar[tuple[str, ...]] = ("built_at",)

    period: str | None = None
    status: str | None = None
    kind: str | None = None
    match_count: int | None = None
    row_count: int | None = None
    files: list[dict[str, Any]] | None = None
    built_at: datetime | None = None


@dataclass
class WSToken(Model):
    """A connection token for the high-fan-out push WebSocket feed. **ULTRA.**

    ``ws_url`` is the push endpoint and ``channels`` the channel vocabulary:
    ``match:{id}`` per-match streams and ``slate:all`` for every live score
    frame, plus — where the mint advertises them — the point channels
    (``point:match:{id}`` / ``point:slate``). Frames are the same allowlist
    score objects the polling endpoints return. The token is short-lived —
    mint a fresh one on reconnect.

    Subscribe ONLY names read from this vocabulary. A channel family absent
    from ``channels`` means this key will not receive those frames — the
    server's feature gate is off, or the plan lacks them. That is an honest
    refusal, not a retry case: the helpers return ``None`` for it rather
    than guessing a name the server would refuse.
    """

    token: str | None = None
    expires_in: int | None = None
    ws_url: str | None = None
    channels: dict[str, Any] | None = None

    @property
    def slate_channel(self) -> str | None:
        """The all-matches channel name (``slate:all``)."""
        value = (self.channels or {}).get("slate")
        return value if isinstance(value, str) else None

    def match_channel(self, match_id: int) -> str | None:
        """The channel name for one match, from the server's own template."""
        template = (self.channels or {}).get("match")
        if not isinstance(template, str):
            return None
        return re.sub(r"\{[^}]*\}", str(match_id), template)

    @property
    def point_slate_channel(self) -> str | None:
        """The all-matches point channel (``point:slate``), from the vocabulary.

        ``None`` when the mint did not advertise it — this key will not
        receive point frames (server gate off, or the plan lacks point
        streams), so there is no name worth subscribing.
        """
        value = (self.channels or {}).get("point_slate")
        return value if isinstance(value, str) else None

    def point_match_channel(self, match_id: int) -> str | None:
        """The point channel for one match, from the server's own template.

        ``None`` when the mint did not advertise the family — same honest
        refusal as :attr:`point_slate_channel`.
        """
        template = (self.channels or {}).get("point_match")
        if not isinstance(template, str):
            return None
        return re.sub(r"\{[^}]*\}", str(match_id), template)


@dataclass
class Usage(Model):
    """Your own usage vs quota. Any tier; the read itself is quota-exempt.

    ``today`` is current to the second and ``history`` covers the last 30
    days, oldest first. The per-minute window lives on the ``X-RateLimit-*``
    headers of every response, not here — and the daily reset instant is only
    ever stated by the daily 429 body (``resets_at``), not by this endpoint.
    """

    _datetime_fields: ClassVar[tuple[str, ...]] = ("tier_expires_at", "as_of")

    principal: str | None = None
    tier: str | None = None
    base_tier: str | None = None
    tier_expires_at: datetime | None = None
    channel: str | None = None
    limits: dict[str, Any] | None = None
    today: dict[str, Any] | None = None
    history: list[dict[str, Any]] | None = None
    as_of: datetime | None = None


@dataclass
class Page(Model):
    """A single page of a list endpoint: ``{data, meta}``."""

    data: list[Any] = field(default_factory=list)
    meta: ListMeta | None = None

    @property
    def count(self) -> int:
        return len(self.data)

    def __iter__(self):
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, index: int) -> Any:
        return self.data[index]
