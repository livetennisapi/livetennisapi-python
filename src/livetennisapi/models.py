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

    ``win_probability_p1`` and ``danger`` are present only on the ULTRA tier.
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
