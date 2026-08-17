"""Synchronous and asynchronous clients for the Live Tennis API.

    from livetennisapi import LiveTennisAPI

    with LiveTennisAPI(api_key="twjp_…") as client:
        for match in client.list_matches(status="live"):
            print(match.tournament, match.p1.name, "vs", match.p2.name)

The async client mirrors the sync one method for method::

    async with AsyncLiveTennisAPI() as client:
        matches = await client.list_matches(status="live")
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

import httpx

from ._base import _BaseClient
from .errors import APIConnectionError, APITimeoutError
from .models import (
    ArchiveCareer,
    ArchiveMatch,
    ArchivePlayerBio,
    ChartingMatch,
    ChartingPlayer,
    Event,
    Fixture,
    HeadToHead,
    HistoryPackage,
    HistoryTape,
    LivePoint,
    Market,
    Match,
    MatchStatistics,
    Page,
    Player,
    PointsPage,
    RallyMatch,
    RankingRecord,
    Score,
    Tournament,
    Usage,
    WSToken,
)

__all__ = ["LiveTennisAPI", "AsyncLiveTennisAPI"]

_MAX_LIMIT = 200


def _page(data: Any, model: type | None) -> Page:
    """Wrap a ``{data, meta}`` body, tolerating a bare list."""
    if isinstance(data, Mapping):
        items = data.get("data") or []
        meta = data.get("meta")
    else:
        items = data or []
        meta = None

    if model is not None and isinstance(items, list):
        items = [m for m in (model.from_dict(i) for i in items) if m is not None]

    from .models import ListMeta

    page = Page(data=list(items), meta=ListMeta.from_dict(meta) if isinstance(meta, Mapping) else None)
    page.raw = dict(data) if isinstance(data, Mapping) else {"data": items}
    return page


class LiveTennisAPI(_BaseClient):
    """Synchronous client.

    The key is read from the ``api_key`` argument, falling back to the
    ``LIVETENNISAPI_KEY`` environment variable.
    """

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        transport = kwargs.pop("transport", None)
        super().__init__(api_key, **kwargs)
        self._client = httpx.Client(
            timeout=self.timeout,
            headers=self._headers(),
            follow_redirects=True,
            transport=transport,
        )

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LiveTennisAPI:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport ------------------------------------------------------------

    def _request(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = self._url(path)
        last: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(url, params=dict(params or {}))
            except httpx.TimeoutException as exc:
                last = APITimeoutError(f"request to {url} timed out after {self.timeout}s")
                if attempt >= self.max_retries:
                    raise last from exc
                time.sleep(self._backoff(attempt, None))
                continue
            except httpx.HTTPError as exc:
                last = APIConnectionError(f"could not reach {url}: {exc}")
                if attempt >= self.max_retries:
                    raise last from exc
                time.sleep(self._backoff(attempt, None))
                continue

            if self._retryable(response) and attempt < self.max_retries:
                from ._base import _retry_after_seconds

                time.sleep(self._backoff(attempt, _retry_after_seconds(response.headers)))
                continue

            self._raise_for_status(response, path, params)
            return self._decode(response)

        if last:
            raise last
        raise APIConnectionError(f"request to {url} failed")  # pragma: no cover

    # -- endpoints ------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Liveness probe. Needs no authentication."""
        return self._request("/health") or {}

    def list_matches(
        self,
        status: str = "live",
        *,
        tour: str | None = None,
        player: int | list[int] | None = None,
        country: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        """Matches by lifecycle status: ``live``, ``upcoming`` or ``completed``.

        All filters are optional and AND-composed:

        - ``player`` — matches where this player is EITHER participant. Pass a
          list to union several ids (repeated on the wire, max 50).
        - ``from_`` / ``to`` — play-date bounds, ``YYYY-MM-DD`` or ISO-8601
          UTC. A bare date covers the whole UTC day.
        - ``country`` — either participant's ``player.country`` equals this
          lowercase 3-letter code. The vocabulary is what the Player object
          returns — IOC-style codes (``ned``, ``sui``, ``gre``), NOT ISO-3166.
          Players with no recorded country never match.
        """
        return _page(
            self._request(
                "/matches",
                self._params(
                    {
                        "status": status,
                        "tour": tour,
                        "player": player,
                        "country": country,
                        "from": from_,
                        "to": to,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            Match,
        )

    def get_match(self, match_id: int) -> Match | None:
        """Full match detail. Embeds ``market`` at PRO and ``analysis`` at ULTRA."""
        return Match.from_dict(self._request(f"/matches/{match_id}"))

    def get_match_score(self, match_id: int) -> Score | None:
        """Current score only — the lowest-latency read available."""
        return Score.from_dict(self._request(f"/matches/{match_id}/score"))

    def list_match_events(self, match_id: int, *, limit: int = 50, offset: int = 0) -> Page:
        """Match events, newest first. **PRO.**"""
        return _page(
            self._request(f"/matches/{match_id}/events", self._params({"limit": limit, "offset": offset})),
            Event,
        )

    def get_match_analysis(self, match_id: int):
        """Model analysis for a match. **ULTRA.**"""
        from .models import Analysis

        return Analysis.from_dict(self._request(f"/matches/{match_id}/analysis"))

    def search_players(self, search: str | None = None, *, limit: int = 50, offset: int = 0) -> Page:
        """Search players by name. Ranked players come first."""
        return _page(
            self._request("/players", self._params({"search": search, "limit": limit, "offset": offset})),
            Player,
        )

    def get_player(self, player_id: int) -> Player | None:
        """One player's bio, ranking and cached stats."""
        return Player.from_dict(self._request(f"/players/{player_id}"))

    def list_markets(self, match_id: int) -> Page:
        """Match-winner market(s) for a match. **PRO.**"""
        return _page(self._request("/markets", {"match_id": match_id}), Market)

    def get_market_prices(self, match_id: int, *, limit: int = 50) -> Market | None:
        """Market with recent price ticks per side, newest first. **PRO.**"""
        return Market.from_dict(self._request(f"/markets/{match_id}/prices", self._params({"limit": limit})))

    def list_completed_matches(
        self,
        *,
        tour: str | None = None,
        player: int | list[int] | None = None,
        country: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        coverage: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        """Completed matches, newest first, with a derived ``winner``. **BASIC.**

        Takes the same ``tour`` / ``player`` / ``country`` / ``from_`` / ``to``
        filters as :meth:`list_matches`, plus ``coverage`` — keep only matches
        whose tape has that coverage (``from_start`` | ``partial`` |
        ``reconstructed`` | ``reconstructed_partial`` | ``none``). Note the
        coverage filter is applied AFTER the page is cut, so a filtered page is
        routinely shorter than ``limit`` (and may be empty) while later pages
        still hold matching matches — a short filtered page is not an
        end-of-data signal there.
        """
        return _page(
            self._request(
                "/history/matches",
                self._params(
                    {
                        "tour": tour,
                        "player": player,
                        "country": country,
                        "from": from_,
                        "to": to,
                        "coverage": coverage,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            Match,
        )

    def list_fixtures(self, *, limit: int = 50, offset: int = 0) -> Page:
        """Upcoming scheduled fixtures, earliest first."""
        return _page(self._request("/fixtures", self._params({"limit": limit, "offset": offset})), Fixture)

    def list_tournaments(
        self, search: str | None = None, *, tour: str | None = None, limit: int = 50, offset: int = 0
    ) -> Page:
        """Tournament catalogue, name order — the id space ``Match.tournament_id`` joins.

        ``search`` is a case-insensitive substring match on the tournament name.
        """
        return _page(
            self._request(
                "/tournaments",
                self._params({"search": search, "tour": tour, "limit": limit, "offset": offset}),
            ),
            Tournament,
        )

    def get_tournament(self, tournament_id: str) -> Tournament | None:
        """One tournament by its stable id — the ``tournament_id`` carried on match objects."""
        return Tournament.from_dict(self._request(f"/tournaments/{tournament_id}"))

    def list_archive_matches(
        self,
        *,
        tour: str | None = None,
        name: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        round: str | None = None,
        level: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        """The results archive (1968–2022), newest tournament first. **BASIC.**

        Completed-match RESULTS from a licensed historical corpus — ATP and
        WTA, main draws, qualifying and the ITF/futures tiers. Distinct from
        the point-by-point tape (2023→now) served by
        :meth:`list_completed_matches`: the archive ends exactly where the tape
        begins, so no match is ever served from two datasets.

        ``name`` matches EITHER player's name (case-insensitive substring, min
        3 chars); ``from_`` / ``to`` bound the tournament START date — the only
        date this era's records carry; ``round`` uses the controlled vocabulary
        (``F`` ``SF`` ``QF`` ``R16`` … ``Q1``-``Q4``); ``level`` is the source
        tier code (G, M, A, F, D, C, O, or a futures category code like "15").
        """
        return _page(
            self._request(
                "/history/archive/matches",
                self._params(
                    {
                        "tour": tour,
                        "name": name,
                        "from": from_,
                        "to": to,
                        "round": round,
                        "level": level,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            ArchiveMatch,
        )

    def get_archive_match(self, archive_id: int) -> ArchiveMatch | None:
        """One results-archive record, with serve statistics where the era recorded them. **BASIC.**

        ``stats`` is ``None`` for most rows before 1991 — that ``None`` is
        honest, never synthesised.
        """
        return ArchiveMatch.from_dict(self._request(f"/history/archive/matches/{archive_id}"))

    def list_archive_players(
        self, name: str | None = None, *, tour: str | None = None, limit: int = 50, offset: int = 0
    ) -> Page:
        """The people of the results archive (1968–2022), ordered by name. **BASIC.**

        Bios: hand, date of birth, country, height, and career-high rank with
        the earliest week it was reached. Their ``id`` is the corpus person id
        that archive match rows carry as ``winner.player_id`` /
        ``loser.player_id``, scoped per tour — never a roster id.
        """
        return _page(
            self._request(
                "/history/archive/players",
                self._params({"name": name, "tour": tour, "limit": limit, "offset": offset}),
            ),
            ArchivePlayerBio,
        )

    def get_archive_career(self, name: str) -> ArchiveCareer | None:
        """Career aggregates over the results archive (1968–2022) for one player. **BASIC.**

        ``name`` must resolve to exactly one person: an ambiguous fragment
        raises :class:`~livetennisapi.BadRequest` with
        ``error_code == "ambiguous_name"`` and the candidate list in
        ``exc.body["candidates"]``; an unknown one raises
        :class:`~livetennisapi.NotFound`.
        """
        return ArchiveCareer.from_dict(self._request("/history/archive/career", {"name": name}))

    def get_h2h(self, p1: str, p2: str) -> HeadToHead | None:
        """Head-to-head across both halves of the product. **BASIC.**

        The results archive (1968–2022) plus our own completed matches
        (2023→now), in one call. Names are the keys (min 3 chars each) —
        archive people have no roster ids. A fragment matching more than one
        player raises :class:`~livetennisapi.BadRequest` with
        ``error_code == "ambiguous_name"`` and the candidate list in
        ``exc.body["candidates"]``. ``meetings[i]["winner"]`` is 1|2 OF THIS
        REQUEST (your ``p1``/``p2``), not of the underlying match row.
        """
        return HeadToHead.from_dict(self._request("/h2h", {"p1": p1, "p2": p2}))

    def get_match_tape(self, match_id: int, *, sequence: str | None = None) -> HistoryTape | None:
        """The point-by-point tape for one match. **BASIC, or any History plan.**

        Works on a LIVE match too — the tape is assembled from whatever has
        been committed so far, so this is how you read the point-by-point
        history of a match in progress. ``sequence="raw"`` (the default) is
        every row we committed, deliberately non-monotonic (independent
        sources race, and a higher-trust one may correct a lower-trust one
        backwards); ``sequence="clean"`` collapses to one row per distinct
        score state — and only clean rows carry
        :attr:`~livetennisapi.TapeRow.point_winner`. Check ``meta.coverage``
        and ``meta.point_source`` before backtesting; per-set tiebreak final
        scores ride along in ``tiebreaks``.
        """
        return HistoryTape.from_dict(
            self._request(f"/history/matches/{match_id}", self._params({"sequence": sequence}))
        )

    def get_match_statistics(self, match_id: int) -> MatchStatistics | None:
        """In-play statistics for one match. **ULTRA.**

        Aces, double faults, the serve split, hold/break percentages, break
        points and service/return points — in two families that are
        deliberately not merged (derived vs measured; see
        :class:`~livetennisapi.MatchStatistics`). A match we hold nothing for
        answers 200 with null ``players``, not 404.
        """
        return MatchStatistics.from_dict(self._request(f"/matches/{match_id}/statistics"))

    def get_match_points(self, match_id: int, *, after_seq: int = 0) -> PointsPage | None:
        """One page of a match's committed points, in ``seq`` order. **ULTRA.**

        The REST read of the per-point stream: every committed point with
        ``seq`` strictly greater than ``after_seq``, at most 500 per page.
        Follow ``has_more`` / ``last_seq`` for the rest — or use
        :meth:`iter_match_points`, which runs that loop for you. Works on a
        live match (the pages grow as points commit) and on a completed one.

        Read ``covers_from_start`` before treating ``seq`` 1 as the match's
        true first point, and ``pbp_coverage`` / ``quality`` before
        backtesting. A negative or ahead-of-the-match ``after_seq`` answers
        400 ``bad_after_seq``; 400 ``points_disabled`` means the feature is
        switched off server-side, which no retry will change.
        """
        return PointsPage.from_dict(
            self._request(f"/matches/{match_id}/points", self._params({"after_seq": after_seq}))
        )

    def iter_match_points(self, match_id: int, *, after_seq: int = 0) -> Iterator[LivePoint]:
        """Every committed point with ``seq > after_seq``, walking the pages.

        A dedicated loop rather than :meth:`paginate`, because the cursor
        here is the point SEQUENCE, not an offset: each page's ``last_seq``
        becomes the next request's ``after_seq``, and the walk ends when
        ``has_more`` goes false — never on a short page (a live match's
        newest page is routinely short while more points are still coming).
        """
        cursor = int(after_seq)
        while True:
            page = self.get_match_points(match_id, after_seq=cursor)
            if page is None:
                return
            yield from page.points
            if not page.has_more:
                return
            next_cursor = page.last_seq if isinstance(page.last_seq, int) else None
            if next_cursor is None and page.points and isinstance(page.points[-1].seq, int):
                next_cursor = page.points[-1].seq
            if next_cursor is None or next_cursor <= cursor:
                return  # no forward progress — never loop on a broken page
            cursor = next_cursor

    def list_rankings(
        self,
        *,
        player: int | list[int] | None = None,
        system: str | list[str] | None = None,
        as_of: str | None = None,
        tour: str | None = None,
        surface: str | None = None,
        archive_player: int | list[int] | None = None,
        min_matches: int | None = None,
        activity_weeks: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        """Point-in-time rankings, in two modes with two gates.

        - **Listing (PRO)** — omit ``player`` and name exactly one ``system``:
          the full published table in rank order, the newest week at or before
          ``as_of``. Rows carry ``player_name`` as published and a null
          ``player_id`` for players outside our roster, so the table has no
          silent holes. ``utr`` has no listing (a rating, not a ranking).
        - **Per-player as-of (ULTRA)** — pass ``player`` (an id or a list,
          repeated on the wire, max 50): per system, the newest record
          effective ON OR BEFORE ``as_of`` — never one dated after it.

        ``system`` is ``atp`` | ``wta`` | ``itf_jt`` | ``itf_mt`` | ``itf_wt``
        | ``utr`` | ``elo``; ``as_of`` is ``YYYY-MM-DD`` (omit for the latest
        known). Elo is NEVER included implicitly — no mode returns Elo records
        unless ``system`` names it. Its companion parameters ride through to
        the server as given: ``tour`` (the Elo leaderboard — the listing mode
        — requires it), ``surface`` (a per-surface rating instead of the
        overall one), ``archive_player`` (address archive person ids — the
        corpus id space archive rows carry — where roster ids do not reach;
        an id or a list, repeated on the wire), and the leaderboard filters
        ``min_matches`` / ``activity_weeks``. Read ``meta.coverage`` before
        trusting an empty result — ITF and UTR history begins 2026-07-29 and
        cannot be reconstructed earlier.
        """
        return _page(
            self._request(
                "/rankings",
                self._params(
                    {
                        "player": player,
                        "system": system,
                        "as_of": as_of,
                        "tour": tour,
                        "surface": surface,
                        "archive_player": archive_player,
                        "min_matches": min_matches,
                        "activity_weeks": activity_weeks,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            RankingRecord,
        )

    def list_history_packages(self, *, kind: str | None = None, year: str | None = None) -> Page:
        """Pre-built monthly bulk packages, newest period first. **PRO.**

        ``kind`` picks the package family — ``"tape"`` (the default:
        point-by-point match tapes), ``"rankings"`` (as-of ranking records,
        **ULTRA**), ``"rally"`` (the charted rally corpus as yearly exports,
        **ULTRA**), or ``"archive"`` (the 1968-2022 results archive as yearly
        exports — same entitlement as the tape packages). ``year="YYYY"``
        lists every published month of that year (the year-archive listing —
        ULTRA, History Business, or a 1-year package). Coverage is not a
        contiguous run of months: treat this listing as the authoritative set
        of months that exist.
        """
        return _page(
            self._request("/history/packages", self._params({"kind": kind, "year": year})),
            HistoryPackage,
        )

    def get_history_package(self, period: str, *, kind: str | None = None) -> HistoryPackage | None:
        """One monthly package's manifest — files, sizes and checksums. **PRO.**

        ``period`` is ``YYYY-MM`` — except for the yearly kinds
        (``"rally"``, ``"archive"``), whose period is a bare ``YYYY``.
        ``"rankings"`` and ``"rally"`` need **ULTRA**; ``"archive"`` rides
        the tape entitlement.
        The manifest's ``files`` name the downloadable artifacts; the files
        themselves stream from the same URL with ``?format=jsonl|csv``.
        """
        return HistoryPackage.from_dict(self._request(f"/history/packages/{period}", self._params({"kind": kind})))

    def list_rally_matches(
        self,
        *,
        player: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        surface: str | None = None,
        gender: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        """Charted matches with shot-by-shot data, newest first. **ULTRA.**

        Rally construction is the layer below the tape: the tape says what the
        score became after each point, this says how the point was played. It
        has its OWN id space — the charted corpus reaches back decades and
        concentrates on the biggest events. Ask this endpoint for the
        authoritative coverage list rather than assuming a match is charted.
        ``player`` is a substring match on either player name; ``gender`` is
        ``M`` | ``W``.
        """
        return _page(
            self._request(
                "/rally/matches",
                self._params(
                    {
                        "player": player,
                        "from": from_,
                        "to": to,
                        "surface": surface,
                        "gender": gender,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            RallyMatch,
        )

    def get_rally_match(self, rally_match_id: int, *, limit: int = 50, offset: int = 0) -> RallyMatch | None:
        """One charted match with its points, in play order. **ULTRA.**

        Paged with ``limit``/``offset`` over the points; ``meta.total`` is the
        match's full point count.
        """
        return RallyMatch.from_dict(
            self._request(f"/rally/matches/{rally_match_id}", self._params({"limit": limit, "offset": offset}))
        )

    def get_match_rally(self, match_id: int, *, limit: int = 50, offset: int = 0) -> RallyMatch | None:
        """Rally construction addressed by OUR match id. **ULTRA.**

        Resolved through the optional link between the two id spaces. Answers
        404 with ``error_code == "not_charted"`` when we hold the match but
        nobody charted it — deliberately distinct from "no such match",
        because most of our matches are not charted.
        """
        return RallyMatch.from_dict(
            self._request(f"/history/matches/{match_id}/rally", self._params({"limit": limit, "offset": offset}))
        )

    def get_charting_player(self, name: str, *, gender: str | None = None) -> ChartingPlayer | None:
        """Career shot-level charting aggregate for one player. **ULTRA.**

        ``name`` (min 3 chars) is the key; a fragment matching more than one
        charted person raises :class:`~livetennisapi.BadRequest` with the
        candidate list, and ``gender="men"`` / ``"women"`` disambiguates.
        """
        return ChartingPlayer.from_dict(
            self._request("/charting/players", self._params({"name": name, "gender": gender}))
        )

    def get_charting_match(self, charting_match_id: int) -> ChartingMatch | None:
        """One charted match, every stat family for both players. **ULTRA.**"""
        return ChartingMatch.from_dict(self._request(f"/charting/matches/{charting_match_id}"))

    def get_ws_token(self) -> WSToken | None:
        """Mint a connection token for the high-fan-out push feed. **ULTRA.**

        Returns the push WebSocket URL plus the channel vocabulary —
        ``match:{id}`` per-match streams and ``slate:all`` for every live
        score frame (:meth:`~livetennisapi.WSToken.match_channel` and
        :attr:`~livetennisapi.WSToken.slate_channel` read them). Score frames
        on the push feed carry the same ULTRA model fields as REST
        (``win_probability_p1``, ``danger``). The token is short-lived — mint
        a fresh one on reconnect.
        """
        return WSToken.from_dict(self._request("/ws-token"))

    def get_usage(self) -> Usage | None:
        """Your own usage vs quota. Any tier, and the read itself is quota-exempt.

        Today's calls are current to the second, plus a 30-day history. The
        per-minute window lives on the ``X-RateLimit-*`` headers of every
        response; the daily reset instant is only ever stated by the daily-429
        body (``resets_at``), not here.
        """
        return Usage.from_dict(self._request("/usage"))

    # -- pagination -----------------------------------------------------------

    def paginate(self, method: str, /, *args: Any, page_size: int = _MAX_LIMIT, **kwargs: Any) -> Iterator[Any]:
        """Walk every page of a list endpoint, yielding items.

            for player in client.paginate("search_players", search="djokovic"):
                ...

        Stops when a page comes back short, which is the only reliable
        end-of-data signal: ``meta.count`` describes the page, not the total.
        """
        fn = getattr(self, method)
        offset = int(kwargs.pop("offset", 0))
        page_size = max(1, min(int(page_size), _MAX_LIMIT))

        while True:
            page = fn(*args, limit=page_size, offset=offset, **kwargs)
            items = list(page)
            yield from items
            if len(items) < page_size:
                return
            offset += page_size


class AsyncLiveTennisAPI(_BaseClient):
    """Asynchronous client. Mirrors :class:`LiveTennisAPI` method for method."""

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        transport = kwargs.pop("transport", None)
        super().__init__(api_key, **kwargs)
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._headers(),
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncLiveTennisAPI:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = self._url(path)
        last: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(url, params=dict(params or {}))
            except httpx.TimeoutException as exc:
                last = APITimeoutError(f"request to {url} timed out after {self.timeout}s")
                if attempt >= self.max_retries:
                    raise last from exc
                await asyncio.sleep(self._backoff(attempt, None))
                continue
            except httpx.HTTPError as exc:
                last = APIConnectionError(f"could not reach {url}: {exc}")
                if attempt >= self.max_retries:
                    raise last from exc
                await asyncio.sleep(self._backoff(attempt, None))
                continue

            if self._retryable(response) and attempt < self.max_retries:
                from ._base import _retry_after_seconds

                await asyncio.sleep(self._backoff(attempt, _retry_after_seconds(response.headers)))
                continue

            self._raise_for_status(response, path, params)
            return self._decode(response)

        if last:
            raise last
        raise APIConnectionError(f"request to {url} failed")  # pragma: no cover

    async def health(self) -> dict[str, Any]:
        return await self._request("/health") or {}

    async def list_matches(
        self,
        status: str = "live",
        *,
        tour: str | None = None,
        player: int | list[int] | None = None,
        country: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        return _page(
            await self._request(
                "/matches",
                self._params(
                    {
                        "status": status,
                        "tour": tour,
                        "player": player,
                        "country": country,
                        "from": from_,
                        "to": to,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            Match,
        )

    async def get_match(self, match_id: int) -> Match | None:
        return Match.from_dict(await self._request(f"/matches/{match_id}"))

    async def get_match_score(self, match_id: int) -> Score | None:
        return Score.from_dict(await self._request(f"/matches/{match_id}/score"))

    async def list_match_events(self, match_id: int, *, limit: int = 50, offset: int = 0) -> Page:
        return _page(
            await self._request(f"/matches/{match_id}/events", self._params({"limit": limit, "offset": offset})),
            Event,
        )

    async def get_match_analysis(self, match_id: int):
        from .models import Analysis

        return Analysis.from_dict(await self._request(f"/matches/{match_id}/analysis"))

    async def search_players(self, search: str | None = None, *, limit: int = 50, offset: int = 0) -> Page:
        return _page(
            await self._request("/players", self._params({"search": search, "limit": limit, "offset": offset})),
            Player,
        )

    async def get_player(self, player_id: int) -> Player | None:
        return Player.from_dict(await self._request(f"/players/{player_id}"))

    async def list_markets(self, match_id: int) -> Page:
        return _page(await self._request("/markets", {"match_id": match_id}), Market)

    async def get_market_prices(self, match_id: int, *, limit: int = 50) -> Market | None:
        return Market.from_dict(await self._request(f"/markets/{match_id}/prices", self._params({"limit": limit})))

    async def list_completed_matches(
        self,
        *,
        tour: str | None = None,
        player: int | list[int] | None = None,
        country: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        coverage: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        return _page(
            await self._request(
                "/history/matches",
                self._params(
                    {
                        "tour": tour,
                        "player": player,
                        "country": country,
                        "from": from_,
                        "to": to,
                        "coverage": coverage,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            Match,
        )

    async def list_fixtures(self, *, limit: int = 50, offset: int = 0) -> Page:
        return _page(await self._request("/fixtures", self._params({"limit": limit, "offset": offset})), Fixture)

    async def list_tournaments(
        self, search: str | None = None, *, tour: str | None = None, limit: int = 50, offset: int = 0
    ) -> Page:
        return _page(
            await self._request(
                "/tournaments",
                self._params({"search": search, "tour": tour, "limit": limit, "offset": offset}),
            ),
            Tournament,
        )

    async def get_tournament(self, tournament_id: str) -> Tournament | None:
        return Tournament.from_dict(await self._request(f"/tournaments/{tournament_id}"))

    async def list_archive_matches(
        self,
        *,
        tour: str | None = None,
        name: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        round: str | None = None,
        level: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        return _page(
            await self._request(
                "/history/archive/matches",
                self._params(
                    {
                        "tour": tour,
                        "name": name,
                        "from": from_,
                        "to": to,
                        "round": round,
                        "level": level,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            ArchiveMatch,
        )

    async def get_archive_match(self, archive_id: int) -> ArchiveMatch | None:
        return ArchiveMatch.from_dict(await self._request(f"/history/archive/matches/{archive_id}"))

    async def list_archive_players(
        self, name: str | None = None, *, tour: str | None = None, limit: int = 50, offset: int = 0
    ) -> Page:
        return _page(
            await self._request(
                "/history/archive/players",
                self._params({"name": name, "tour": tour, "limit": limit, "offset": offset}),
            ),
            ArchivePlayerBio,
        )

    async def get_archive_career(self, name: str) -> ArchiveCareer | None:
        return ArchiveCareer.from_dict(await self._request("/history/archive/career", {"name": name}))

    async def get_h2h(self, p1: str, p2: str) -> HeadToHead | None:
        return HeadToHead.from_dict(await self._request("/h2h", {"p1": p1, "p2": p2}))

    async def get_match_tape(self, match_id: int, *, sequence: str | None = None) -> HistoryTape | None:
        return HistoryTape.from_dict(
            await self._request(f"/history/matches/{match_id}", self._params({"sequence": sequence}))
        )

    async def get_match_statistics(self, match_id: int) -> MatchStatistics | None:
        return MatchStatistics.from_dict(await self._request(f"/matches/{match_id}/statistics"))

    async def get_match_points(self, match_id: int, *, after_seq: int = 0) -> PointsPage | None:
        return PointsPage.from_dict(
            await self._request(f"/matches/{match_id}/points", self._params({"after_seq": after_seq}))
        )

    async def iter_match_points(self, match_id: int, *, after_seq: int = 0) -> AsyncIterator[LivePoint]:
        cursor = int(after_seq)
        while True:
            page = await self.get_match_points(match_id, after_seq=cursor)
            if page is None:
                return
            for point in page.points:
                yield point
            if not page.has_more:
                return
            next_cursor = page.last_seq if isinstance(page.last_seq, int) else None
            if next_cursor is None and page.points and isinstance(page.points[-1].seq, int):
                next_cursor = page.points[-1].seq
            if next_cursor is None or next_cursor <= cursor:
                return  # no forward progress — never loop on a broken page
            cursor = next_cursor

    async def list_rankings(
        self,
        *,
        player: int | list[int] | None = None,
        system: str | list[str] | None = None,
        as_of: str | None = None,
        tour: str | None = None,
        surface: str | None = None,
        archive_player: int | list[int] | None = None,
        min_matches: int | None = None,
        activity_weeks: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        return _page(
            await self._request(
                "/rankings",
                self._params(
                    {
                        "player": player,
                        "system": system,
                        "as_of": as_of,
                        "tour": tour,
                        "surface": surface,
                        "archive_player": archive_player,
                        "min_matches": min_matches,
                        "activity_weeks": activity_weeks,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            RankingRecord,
        )

    async def list_history_packages(self, *, kind: str | None = None, year: str | None = None) -> Page:
        return _page(
            await self._request("/history/packages", self._params({"kind": kind, "year": year})),
            HistoryPackage,
        )

    async def get_history_package(self, period: str, *, kind: str | None = None) -> HistoryPackage | None:
        return HistoryPackage.from_dict(
            await self._request(f"/history/packages/{period}", self._params({"kind": kind}))
        )

    async def list_rally_matches(
        self,
        *,
        player: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        surface: str | None = None,
        gender: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        return _page(
            await self._request(
                "/rally/matches",
                self._params(
                    {
                        "player": player,
                        "from": from_,
                        "to": to,
                        "surface": surface,
                        "gender": gender,
                        "limit": limit,
                        "offset": offset,
                    }
                ),
            ),
            RallyMatch,
        )

    async def get_rally_match(self, rally_match_id: int, *, limit: int = 50, offset: int = 0) -> RallyMatch | None:
        return RallyMatch.from_dict(
            await self._request(f"/rally/matches/{rally_match_id}", self._params({"limit": limit, "offset": offset}))
        )

    async def get_match_rally(self, match_id: int, *, limit: int = 50, offset: int = 0) -> RallyMatch | None:
        return RallyMatch.from_dict(
            await self._request(f"/history/matches/{match_id}/rally", self._params({"limit": limit, "offset": offset}))
        )

    async def get_charting_player(self, name: str, *, gender: str | None = None) -> ChartingPlayer | None:
        return ChartingPlayer.from_dict(
            await self._request("/charting/players", self._params({"name": name, "gender": gender}))
        )

    async def get_charting_match(self, charting_match_id: int) -> ChartingMatch | None:
        return ChartingMatch.from_dict(await self._request(f"/charting/matches/{charting_match_id}"))

    async def get_ws_token(self) -> WSToken | None:
        return WSToken.from_dict(await self._request("/ws-token"))

    async def get_usage(self) -> Usage | None:
        return Usage.from_dict(await self._request("/usage"))

    async def paginate(
        self, method: str, /, *args: Any, page_size: int = _MAX_LIMIT, **kwargs: Any
    ) -> AsyncIterator[Any]:
        fn = getattr(self, method)
        offset = int(kwargs.pop("offset", 0))
        page_size = max(1, min(int(page_size), _MAX_LIMIT))

        while True:
            page = await fn(*args, limit=page_size, offset=offset, **kwargs)
            items = list(page)
            for item in items:
                yield item
            if len(items) < page_size:
                return
            offset += page_size
