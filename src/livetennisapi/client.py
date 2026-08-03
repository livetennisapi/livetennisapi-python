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
    Event,
    Fixture,
    HeadToHead,
    Market,
    Match,
    Page,
    Player,
    Score,
    Tournament,
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

            if self._should_retry(response.status_code) and attempt < self.max_retries:
                from ._base import _retry_after_seconds

                time.sleep(self._backoff(attempt, _retry_after_seconds(response.headers)))
                continue

            self._raise_for_status(response, path)
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

            if self._should_retry(response.status_code) and attempt < self.max_retries:
                from ._base import _retry_after_seconds

                await asyncio.sleep(self._backoff(attempt, _retry_after_seconds(response.headers)))
                continue

            self._raise_for_status(response, path)
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
