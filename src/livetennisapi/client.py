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
from .models import Event, Fixture, Market, Match, Page, Player, Score

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

    def list_matches(self, status: str = "live", *, limit: int = 50, offset: int = 0) -> Page:
        """Matches by lifecycle status: ``live``, ``upcoming`` or ``completed``."""
        return _page(
            self._request("/matches", self._params({"status": status, "limit": limit, "offset": offset})),
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

    def list_completed_matches(self, *, limit: int = 50, offset: int = 0) -> Page:
        """Completed matches, newest first, with a derived ``winner``."""
        return _page(
            self._request("/history/matches", self._params({"limit": limit, "offset": offset})),
            Match,
        )

    def list_fixtures(self, *, limit: int = 50, offset: int = 0) -> Page:
        """Upcoming scheduled fixtures, earliest first."""
        return _page(self._request("/fixtures", self._params({"limit": limit, "offset": offset})), Fixture)

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

    async def list_matches(self, status: str = "live", *, limit: int = 50, offset: int = 0) -> Page:
        return _page(
            await self._request("/matches", self._params({"status": status, "limit": limit, "offset": offset})),
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

    async def list_completed_matches(self, *, limit: int = 50, offset: int = 0) -> Page:
        return _page(
            await self._request("/history/matches", self._params({"limit": limit, "offset": offset})),
            Match,
        )

    async def list_fixtures(self, *, limit: int = 50, offset: int = 0) -> Page:
        return _page(await self._request("/fixtures", self._params({"limit": limit, "offset": offset})), Fixture)

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
