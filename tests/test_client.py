"""Client behaviour against a mocked transport.

These assert the parts a user feels but rarely reads: which exception a status
maps to, what gets retried, and when pagination stops.
"""

import httpx
import pytest

from livetennisapi import (
    AsyncLiveTennisAPI,
    BadRequest,
    LiveTennisAPI,
    NotFound,
    RateLimited,
    ServerError,
    ServiceUnavailable,
    Unauthorized,
    UpgradeRequired,
)
from livetennisapi.errors import APIConnectionError, APITimeoutError

BASE = "https://api.livetennisapi.com/api/public/v1"


def client_returning(*responses, **kwargs):
    """A client whose transport replays the given responses in order."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = LiveTennisAPI("twjp_test", transport=httpx.MockTransport(handler), **kwargs)
    client.requests = seen  # type: ignore[attr-defined]
    return client


class TestAuth:
    def test_bearer_header_by_default(self):
        client = client_returning(httpx.Response(200, json={"status": "ok"}))
        client.health()
        assert client.requests[0].headers["authorization"] == "Bearer twjp_test"

    def test_x_api_key_when_requested(self):
        client = client_returning(httpx.Response(200, json={}), auth_header="x-api-key")
        client.health()
        assert client.requests[0].headers["x-api-key"] == "twjp_test"
        assert "authorization" not in client.requests[0].headers

    def test_key_read_from_environment(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "twjp_from_env")
        assert LiveTennisAPI().api_key == "twjp_from_env"

    def test_explicit_key_beats_environment(self, monkeypatch):
        monkeypatch.setenv("LIVETENNISAPI_KEY", "twjp_from_env")
        assert LiveTennisAPI("twjp_explicit").api_key == "twjp_explicit"

    def test_user_agent_identifies_the_sdk(self):
        client = client_returning(httpx.Response(200, json={}))
        client.health()
        assert "livetennisapi-python/" in client.requests[0].headers["user-agent"]

    def test_invalid_auth_header_rejected(self):
        with pytest.raises(ValueError):
            LiveTennisAPI("k", auth_header="cookie")


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (400, BadRequest),
            (401, Unauthorized),
            (403, UpgradeRequired),
            (404, NotFound),
            (429, RateLimited),
            (500, ServerError),
            (503, ServiceUnavailable),
        ],
    )
    def test_status_maps_to_exception(self, status, expected):
        client = client_returning(httpx.Response(status, json={"error": "x"}), max_retries=0)
        with pytest.raises(expected):
            client.get_match(1)

    def test_upgrade_required_names_the_tier_for_analysis(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.get_match_analysis(1)
        assert exc.value.required_tier == "ULTRA"
        assert "ULTRA" in str(exc.value)

    def test_upgrade_required_names_pro_for_events(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_match_events(1)
        assert exc.value.required_tier == "PRO"

    def test_upgrade_required_names_pro_for_markets(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_markets(1)
        assert exc.value.required_tier == "PRO"

    def test_upgrade_required_names_basic_for_history(self):
        """FREE stops short of /history/matches, so a free key hitting it must be
        told BASIC rather than left with the API's bare ``upgrade_required``."""
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_completed_matches()
        assert exc.value.required_tier == "BASIC"

    def test_rate_limited_exposes_retry_after(self):
        client = client_returning(
            httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "12"}),
            max_retries=0,
        )
        with pytest.raises(RateLimited) as exc:
            client.get_match(1)
        assert exc.value.retry_after == 12.0
        assert "12" in str(exc.value)

    def test_rate_limited_without_the_header(self):
        client = client_returning(httpx.Response(429, json={}), max_retries=0)
        with pytest.raises(RateLimited) as exc:
            client.get_match(1)
        assert exc.value.retry_after is None

    def test_error_code_is_exposed(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.get_match(1)
        assert exc.value.error_code == "upgrade_required"

    def test_non_json_error_body_does_not_crash(self):
        client = client_returning(httpx.Response(500, text="<html>nginx</html>"), max_retries=0)
        with pytest.raises(ServerError) as exc:
            client.get_match(1)
        assert exc.value.body is None


class TestRetries:
    def test_429_is_retried_then_succeeds(self):
        client = client_returning(
            httpx.Response(429, json={}, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": 1}),
            max_retries=2,
        )
        assert client.get_match(1).id == 1
        assert len(client.requests) == 2

    def test_500_is_retried(self):
        client = client_returning(
            httpx.Response(500, json={}),
            httpx.Response(200, json={"id": 1}),
            max_retries=2,
        )
        assert client.get_match(1).id == 1
        assert len(client.requests) == 2

    def test_400_is_never_retried(self):
        """A client-side mistake cannot start working — retrying just burns quota."""
        client = client_returning(httpx.Response(400, json={"error": "bad"}), max_retries=3)
        with pytest.raises(BadRequest):
            client.get_match(1)
        assert len(client.requests) == 1

    def test_401_is_never_retried(self):
        client = client_returning(httpx.Response(401, json={"error": "unauthorized"}), max_retries=3)
        with pytest.raises(Unauthorized):
            client.get_match(1)
        assert len(client.requests) == 1

    def test_403_is_never_retried(self):
        client = client_returning(httpx.Response(403, json={"error": "x"}), max_retries=3)
        with pytest.raises(UpgradeRequired):
            client.get_match(1)
        assert len(client.requests) == 1

    def test_retries_are_bounded(self):
        client = client_returning(httpx.Response(500, json={}), max_retries=2)
        with pytest.raises(ServerError):
            client.get_match(1)
        assert len(client.requests) == 3  # initial + 2 retries

    def test_connection_error_is_wrapped(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        client = LiveTennisAPI("k", transport=httpx.MockTransport(boom), max_retries=0)
        with pytest.raises(APIConnectionError):
            client.get_match(1)

    def test_timeout_is_wrapped(self):
        def boom(request):
            raise httpx.ReadTimeout("slow")

        client = LiveTennisAPI("k", transport=httpx.MockTransport(boom), max_retries=0)
        with pytest.raises(APITimeoutError):
            client.get_match(1)


class TestRequests:
    def test_none_params_are_omitted(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.search_players(None, limit=10)
        assert "search" not in client.requests[0].url.params

    def test_status_is_passed_through(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_matches(status="upcoming")
        assert client.requests[0].url.params["status"] == "upcoming"

    def test_paths_are_built_correctly(self):
        client = client_returning(httpx.Response(200, json={}))
        client.get_match_score(18953)
        assert str(client.requests[0].url).startswith(f"{BASE}/matches/18953/score")


class TestResponses:
    def test_list_response_is_parsed(self):
        client = client_returning(
            httpx.Response(
                200,
                json={"data": [{"id": 1, "tournament": "X"}], "meta": {"limit": 50, "offset": 0, "count": 1}},
            )
        )
        page = client.list_matches()
        assert len(page) == 1
        assert page[0].tournament == "X"
        assert page.meta.limit == 50

    def test_bare_list_is_tolerated(self):
        """Defensive: the documented shape is {data, meta}, but never crash."""
        client = client_returning(httpx.Response(200, json=[{"id": 1}]))
        assert len(client.list_matches()) == 1

    def test_empty_data(self):
        client = client_returning(httpx.Response(200, json={"data": [], "meta": {"count": 0}}))
        assert len(client.list_matches()) == 0


class TestPagination:
    def test_stops_on_a_short_page(self):
        pages = [
            httpx.Response(200, json={"data": [{"id": i} for i in range(200)]}),
            httpx.Response(200, json={"data": [{"id": 999}]}),
        ]
        idx = {"n": 0}

        def handler(request):
            response = pages[min(idx["n"], len(pages) - 1)]
            idx["n"] += 1
            return response

        client = LiveTennisAPI("k", transport=httpx.MockTransport(handler))
        assert len(list(client.paginate("list_matches"))) == 201

    def test_single_short_page_makes_one_request(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": [{"id": 1}]})

        client = LiveTennisAPI("k", transport=httpx.MockTransport(handler))
        assert len(list(client.paginate("list_matches"))) == 1
        assert len(seen) == 1

    def test_page_size_is_capped_at_the_api_maximum(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": []})

        client = LiveTennisAPI("k", transport=httpx.MockTransport(handler))
        list(client.paginate("list_matches", page_size=5000))
        assert seen[0].url.params["limit"] == "200"


class TestAsyncClient:
    async def test_async_get(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"id": 1, "tournament": "X"}))
        async with AsyncLiveTennisAPI("k", transport=transport) as client:
            match = await client.get_match(1)
        assert match.tournament == "X"

    async def test_async_errors_map_identically(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(403, json={"error": "upgrade_required"}))
        async with AsyncLiveTennisAPI("k", transport=transport, max_retries=0) as client:
            with pytest.raises(UpgradeRequired) as exc:
                await client.get_match_analysis(1)
        assert exc.value.required_tier == "ULTRA"

    async def test_async_pagination(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": 1}]}))
        async with AsyncLiveTennisAPI("k", transport=transport) as client:
            items = [m async for m in client.paginate("list_matches")]
        assert len(items) == 1


class TestParity:
    def test_both_clients_expose_the_same_endpoints(self):
        """A method added to one client must be added to the other."""
        endpoints = {
            "health", "list_matches", "get_match", "get_match_score",
            "list_match_events", "get_match_analysis", "search_players",
            "get_player", "list_markets", "get_market_prices",
            "list_completed_matches", "list_fixtures", "paginate",
        }
        for name in endpoints:
            assert hasattr(LiveTennisAPI, name), f"sync client missing {name}"
            assert hasattr(AsyncLiveTennisAPI, name), f"async client missing {name}"
