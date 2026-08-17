"""Client behaviour against a mocked transport.

These assert the parts a user feels but rarely reads: which exception a status
maps to, what gets retried, and when pagination stops.
"""

from datetime import datetime, timezone

import httpx
import pytest

from livetennisapi import (
    AbuseThrottled,
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

    def test_upgrade_required_names_basic_for_the_results_archive(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_archive_matches(name="borg")
        assert exc.value.required_tier == "BASIC"

    def test_upgrade_required_names_basic_for_h2h(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.get_h2h("federer", "nadal")
        assert exc.value.required_tier == "BASIC"

    def test_ambiguous_name_candidates_stay_reachable(self):
        """/h2h and /history/archive/career refuse a fragment matching more than
        one player — the candidate list must survive onto the exception so the
        caller can disambiguate instead of guessing."""
        client = client_returning(
            httpx.Response(
                400,
                json={"error": "ambiguous_name", "candidates": ["Serena Williams", "Venus Williams"]},
            ),
            max_retries=0,
        )
        with pytest.raises(BadRequest) as exc:
            client.get_h2h("williams", "sharapova")
        assert exc.value.error_code == "ambiguous_name"
        assert "Venus Williams" in exc.value.body["candidates"]

    def test_upgrade_required_names_ultra_for_statistics(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.get_match_statistics(1)
        assert exc.value.required_tier == "ULTRA"

    def test_upgrade_required_names_ultra_for_rally_and_charting(self):
        for call in (
            lambda c: c.list_rally_matches(),
            lambda c: c.get_rally_match(1),
            lambda c: c.get_match_rally(1),  # /history/…/rally must NOT read as BASIC
            lambda c: c.get_charting_player("federer"),
            lambda c: c.get_charting_match(1),
            lambda c: c.get_ws_token(),
            lambda c: c.get_match_points(1),
        ):
            client = client_returning(
                httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
            )
            with pytest.raises(UpgradeRequired) as exc:
                call(client)
            assert exc.value.required_tier == "ULTRA"

    def test_rankings_listing_names_pro_but_per_player_names_ultra(self):
        """/rankings gates on its parameters: listing (no player) is PRO, the
        per-player as-of mode is ULTRA — the 403 must name the right one."""
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_rankings(system="atp")
        assert exc.value.required_tier == "PRO"

        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_rankings(player=[925, 1137])
        assert exc.value.required_tier == "ULTRA"

    def test_packages_name_pro_but_non_tape_kinds_and_year_name_ultra(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_history_packages()
        assert exc.value.required_tier == "PRO"

        for kwargs in ({"kind": "rankings"}, {"kind": "rally"}, {"year": "2025"}):
            client = client_returning(
                httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
            )
            with pytest.raises(UpgradeRequired) as exc:
                client.list_history_packages(**kwargs)
            assert exc.value.required_tier == "ULTRA", kwargs

    def test_completed_matches_listing_names_basic(self):
        """`/matches?status=completed` is gated even though /matches is FREE."""
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_matches(status="completed")
        assert exc.value.required_tier == "BASIC"

    def test_live_matches_403_names_no_tier(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.list_matches(status="live")
        assert exc.value.required_tier is None

    def test_tape_names_basic(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.get_match_tape(18953)
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

    def test_minute_429_has_no_daily_fields(self):
        client = client_returning(
            httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "7"}),
            max_retries=0,
        )
        with pytest.raises(RateLimited) as exc:
            client.get_match(1)
        assert exc.value.scope is None
        assert exc.value.resets_at is None
        assert exc.value.limit_per_day is None
        assert not isinstance(exc.value, AbuseThrottled)

    def test_daily_429_surfaces_scope_limit_and_resets_at(self):
        """The daily-cap 429 carries the absolute reset instant — an ISO
        timestamp derived from a local midnight, not a UTC midnight — and the
        SDK must hand it over parsed."""
        client = client_returning(
            httpx.Response(
                429,
                json={
                    "error": "rate_limited",
                    "scope": "day",
                    "limit_per_day": 100,
                    "resets_at": "2026-08-07T21:00:00Z",
                },
            ),
            max_retries=0,
        )
        with pytest.raises(RateLimited) as exc:
            client.get_match(1)
        assert exc.value.scope == "day"
        assert exc.value.limit_per_day == 100
        assert exc.value.resets_at == datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc)
        assert "resets at" in str(exc.value)

    def test_unparseable_resets_at_stays_reachable_in_the_body(self):
        client = client_returning(
            httpx.Response(429, json={"error": "rate_limited", "scope": "day", "resets_at": "soon"}),
            max_retries=0,
        )
        with pytest.raises(RateLimited) as exc:
            client.get_match(1)
        assert exc.value.resets_at is None
        assert exc.value.body["resets_at"] == "soon"

    def test_abuse_throttled_is_its_own_type(self):
        client = client_returning(
            httpx.Response(429, json={"error": "abuse_throttled", "retry_at_epoch": 1786571000}),
            max_retries=0,
        )
        with pytest.raises(AbuseThrottled) as exc:
            client.get_match(1)
        assert isinstance(exc.value, RateLimited)  # except RateLimited still catches it
        assert exc.value.error_code == "abuse_throttled"
        assert exc.value.retry_at_epoch == 1786571000
        assert exc.value.retry_at == datetime.fromtimestamp(1786571000, tz=timezone.utc)
        assert "fix the retry loop" in str(exc.value)

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

    def test_abuse_throttled_is_never_retried(self):
        """A 24h abuse block cannot clear inside any backoff window — and
        retrying it is the very behaviour that earns it."""
        client = client_returning(
            httpx.Response(429, json={"error": "abuse_throttled", "retry_at_epoch": 1786571000}),
            max_retries=3,
        )
        with pytest.raises(AbuseThrottled):
            client.get_match(1)
        assert len(client.requests) == 1

    def test_daily_429_is_never_retried(self):
        client = client_returning(
            httpx.Response(429, json={"error": "rate_limited", "scope": "day", "resets_at": "2026-08-07T21:00:00Z"}),
            max_retries=3,
        )
        with pytest.raises(RateLimited):
            client.get_match(1)
        assert len(client.requests) == 1

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

    def test_player_filter_is_repeated_not_comma_joined(self):
        # The API reads `?player=1&player=2`; `player=1,2` is a 400.
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_matches(player=[925, 1137])
        assert client.requests[0].url.params.get_list("player") == ["925", "1137"]

    def test_single_player_id_needs_no_list(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_matches(player=925)
        assert client.requests[0].url.params["player"] == "925"

    def test_date_bounds_map_to_from_and_to(self):
        # `from` is a Python keyword, so the argument is `from_` — but the wire
        # parameter must stay `from`.
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_matches(from_="2026-07-01", to="2026-07-31", country="ned")
        params = client.requests[0].url.params
        assert params["from"] == "2026-07-01"
        assert params["to"] == "2026-07-31"
        assert params["country"] == "ned"
        assert "from_" not in params

    def test_history_filters_pass_through(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_completed_matches(tour="itf", player=925, coverage="from_start")
        params = client.requests[0].url.params
        assert params["tour"] == "itf"
        assert params["player"] == "925"
        assert params["coverage"] == "from_start"

    def test_draw_filter_reaches_every_listing_that_takes_it(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_matches(draw="singles")
        client.list_completed_matches(tour="itf", draw="doubles")
        client.list_tournaments(draw="singles")
        client.list_fixtures(tour="itf", draw="singles")
        assert client.requests[0].url.params["draw"] == "singles"
        assert client.requests[1].url.params["draw"] == "doubles"
        assert client.requests[2].url.params["draw"] == "singles"
        # list_fixtures took NO filters before 1.6.0 — tour must ride too.
        assert client.requests[3].url.params["tour"] == "itf"
        assert client.requests[3].url.params["draw"] == "singles"

    def test_draw_is_omitted_when_not_given(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_matches()
        client.list_fixtures()
        assert "draw" not in client.requests[0].url.params
        assert "draw" not in client.requests[1].url.params
        assert "tour" not in client.requests[1].url.params

    def test_bad_draw_is_a_bad_request_with_the_allowed_list_reachable(self):
        # The server owns draw validation; the SDK passes the value through
        # and surfaces the refusal with its vocabulary intact.
        client = client_returning(
            httpx.Response(400, json={"error": "bad_draw", "allowed": ["singles", "doubles"]}),
            max_retries=0,
        )
        with pytest.raises(BadRequest) as exc:
            client.list_matches(draw="mixed")
        assert exc.value.error_code == "bad_draw"
        assert exc.value.body["allowed"] == ["singles", "doubles"]

    def test_tournament_paths(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_tournaments("wimbledon", tour="atp")
        assert str(client.requests[0].url).startswith(f"{BASE}/tournaments?")
        assert client.requests[0].url.params["search"] == "wimbledon"
        client.get_tournament("atp-wimbledon")
        assert str(client.requests[1].url).startswith(f"{BASE}/tournaments/atp-wimbledon")

    def test_archive_paths_and_filters(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_archive_matches(tour="atp", name="borg", round="F", level="G", from_="1976-01-01")
        assert str(client.requests[0].url).startswith(f"{BASE}/history/archive/matches?")
        params = client.requests[0].url.params
        assert params["name"] == "borg"
        assert params["round"] == "F"
        assert params["level"] == "G"
        assert params["from"] == "1976-01-01"
        client.get_archive_match(123456)
        assert str(client.requests[1].url).startswith(f"{BASE}/history/archive/matches/123456")
        client.list_archive_players("navratilova", tour="wta")
        assert str(client.requests[2].url).startswith(f"{BASE}/history/archive/players?")
        client.get_archive_career("borg")
        assert str(client.requests[3].url).startswith(f"{BASE}/history/archive/career?name=borg")

    def test_h2h_sends_both_names(self):
        client = client_returning(httpx.Response(200, json={}))
        client.get_h2h("federer", "nadal")
        params = client.requests[0].url.params
        assert params["p1"] == "federer"
        assert params["p2"] == "nadal"

    def test_tape_path_and_sequence(self):
        client = client_returning(httpx.Response(200, json={}))
        client.get_match_tape(18953)
        assert str(client.requests[0].url).startswith(f"{BASE}/history/matches/18953")
        assert "sequence" not in client.requests[0].url.params
        client.get_match_tape(18953, sequence="clean")
        assert client.requests[1].url.params["sequence"] == "clean"

    def test_statistics_path(self):
        client = client_returning(httpx.Response(200, json={}))
        client.get_match_statistics(18953)
        assert str(client.requests[0].url).startswith(f"{BASE}/matches/18953/statistics")

    def test_rankings_params_are_repeated_not_comma_joined(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_rankings(player=[925, 1137], system=["atp", "wta"], as_of="2026-07-01")
        params = client.requests[0].url.params
        assert params.get_list("player") == ["925", "1137"]
        assert params.get_list("system") == ["atp", "wta"]
        assert params["as_of"] == "2026-07-01"

    def test_rankings_listing_mode_sends_no_player(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_rankings(system="atp", limit=100)
        params = client.requests[0].url.params
        assert "player" not in params
        assert params["system"] == "atp"

    def test_rankings_elo_params_ride_along(self):
        """The Elo companion params reach the wire as given — and only when given."""
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_rankings(
            system="elo",
            tour="atp",
            surface="clay",
            archive_player=[104925, 103819],
            min_matches=20,
            activity_weeks=52,
        )
        params = client.requests[0].url.params
        assert params["system"] == "elo"
        assert params["tour"] == "atp"
        assert params["surface"] == "clay"
        assert params.get_list("archive_player") == ["104925", "103819"]
        assert params["min_matches"] == "20"
        assert params["activity_weeks"] == "52"

        client.list_rankings(system="atp")
        params = client.requests[1].url.params
        for name in ("tour", "surface", "archive_player", "min_matches", "activity_weeks"):
            assert name not in params

    def test_package_paths_and_params(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_history_packages(kind="rankings", year="2025")
        assert str(client.requests[0].url).startswith(f"{BASE}/history/packages?")
        assert client.requests[0].url.params["kind"] == "rankings"
        assert client.requests[0].url.params["year"] == "2025"
        client.get_history_package("2026-07")
        assert str(client.requests[1].url).startswith(f"{BASE}/history/packages/2026-07")

    def test_rally_paths_and_filters(self):
        client = client_returning(httpx.Response(200, json={"data": []}))
        client.list_rally_matches(player="federer", from_="2008-01-01", to="2008-12-31", gender="M")
        assert str(client.requests[0].url).startswith(f"{BASE}/rally/matches?")
        params = client.requests[0].url.params
        assert params["player"] == "federer"
        assert params["from"] == "2008-01-01"
        assert params["gender"] == "M"
        client.get_rally_match(4242, limit=100, offset=200)
        assert str(client.requests[1].url).startswith(f"{BASE}/rally/matches/4242")
        assert client.requests[1].url.params["offset"] == "200"
        client.get_match_rally(18953)
        assert str(client.requests[2].url).startswith(f"{BASE}/history/matches/18953/rally")

    def test_charting_paths(self):
        client = client_returning(httpx.Response(200, json={}))
        client.get_charting_player("sampras", gender="men")
        assert str(client.requests[0].url).startswith(f"{BASE}/charting/players?")
        assert client.requests[0].url.params["name"] == "sampras"
        assert client.requests[0].url.params["gender"] == "men"
        client.get_charting_match(777)
        assert str(client.requests[1].url).startswith(f"{BASE}/charting/matches/777")

    def test_ws_token_and_usage_paths(self):
        client = client_returning(httpx.Response(200, json={}))
        client.get_ws_token()
        assert str(client.requests[0].url).startswith(f"{BASE}/ws-token")
        client.get_usage()
        assert str(client.requests[1].url).startswith(f"{BASE}/usage")

    def test_not_charted_is_a_distinguishable_404(self):
        client = client_returning(
            httpx.Response(404, json={"error": "not_charted"}), max_retries=0
        )
        with pytest.raises(NotFound) as exc:
            client.get_match_rally(18953)
        assert exc.value.error_code == "not_charted"


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


class TestMatchPoints:
    """/matches/{id}/points pages on a SEQUENCE cursor, not an offset — the
    iterator must follow has_more/last_seq and never trust page length."""

    @staticmethod
    def page_body(seqs, *, has_more, last_seq=None, **extra):
        body = {
            "match_id": 18953,
            "pbp_coverage": "point",
            "quality": "clean",
            "points": [
                {"seq": s, "set": 1, "game": 1, "number": s, "server": 1,
                 "winner": 1 + (s % 2), "score": {"p1": "15", "p2": "0"},
                 "sets": [0, 0], "games": [[0], [0]], "ts": "2026-08-17T10:00:00Z"}
                for s in seqs
            ],
            "last_seq": last_seq if last_seq is not None else (seqs[-1] if seqs else None),
            "has_more": has_more,
        }
        body.update(extra)
        return body

    def test_path_and_after_seq_param(self):
        client = client_returning(httpx.Response(200, json=self.page_body([], has_more=False)))
        client.get_match_points(18953)
        assert str(client.requests[0].url).startswith(f"{BASE}/matches/18953/points")
        assert client.requests[0].url.params["after_seq"] == "0"
        client.get_match_points(18953, after_seq=120)
        assert client.requests[1].url.params["after_seq"] == "120"

    def test_page_parses_points_and_flags(self):
        from datetime import datetime

        client = client_returning(
            httpx.Response(200, json=self.page_body([1, 2], has_more=True, covers_from_start=True))
        )
        page = client.get_match_points(18953)
        assert page.match_id == 18953
        assert page.pbp_coverage == "point"
        assert page.quality == "clean"
        assert page.covers_from_start is True
        assert page.has_more is True
        assert page.last_seq == 2
        assert len(page) == 2
        first = page.points[0]
        assert first.seq == 1
        assert first.score == {"p1": "15", "p2": "0"}
        assert isinstance(first.ts, datetime)

    def test_absent_covers_from_start_reads_none_not_false(self):
        """Older servers omit the field; None means 'not stated', never 'no'."""
        client = client_returning(httpx.Response(200, json=self.page_body([1], has_more=False)))
        assert client.get_match_points(18953).covers_from_start is None

    def test_iterator_follows_last_seq_not_page_length(self):
        # The first page is SHORT (3 < 500) yet has_more says keep going —
        # a live match's newest page is routinely short.
        client = client_returning(
            httpx.Response(200, json=self.page_body([1, 2, 3], has_more=True)),
            httpx.Response(200, json=self.page_body([4, 5], has_more=False)),
        )
        seqs = [p.seq for p in client.iter_match_points(18953)]
        assert seqs == [1, 2, 3, 4, 5]
        assert len(client.requests) == 2
        assert client.requests[0].url.params["after_seq"] == "0"
        assert client.requests[1].url.params["after_seq"] == "3"

    def test_iterator_starts_at_the_given_cursor(self):
        client = client_returning(httpx.Response(200, json=self.page_body([121], has_more=False)))
        assert [p.seq for p in client.iter_match_points(18953, after_seq=120)] == [121]
        assert client.requests[0].url.params["after_seq"] == "120"

    def test_iterator_never_loops_on_a_page_with_no_forward_progress(self):
        # A broken page: has_more says more, but the cursor cannot advance.
        client = client_returning(
            httpx.Response(200, json=self.page_body([1, 2, 3], has_more=True)),
            httpx.Response(200, json=self.page_body([], has_more=True, last_seq=3)),
        )
        seqs = [p.seq for p in client.iter_match_points(18953)]
        assert seqs == [1, 2, 3]
        assert len(client.requests) == 2  # it stopped instead of hammering

    async def test_async_iterator_mirrors_sync(self):
        pages = [
            httpx.Response(200, json=self.page_body([1, 2], has_more=True)),
            httpx.Response(200, json=self.page_body([3], has_more=False)),
        ]
        seen = []

        def handler(request):
            seen.append(request)
            return pages.pop(0) if len(pages) > 1 else pages[0]

        async with AsyncLiveTennisAPI("k", transport=httpx.MockTransport(handler)) as client:
            seqs = [p.seq async for p in client.iter_match_points(18953)]
        assert seqs == [1, 2, 3]
        assert seen[1].url.params["after_seq"] == "2"


class TestHistoryCoverage:
    """/history/coverage is one measured object, not a list — and its 503 is
    an answer ("not built yet"), which must stay distinguishable."""

    BODY = {
        "as_of": "2026-08-18T04:15:00Z",
        "method": "ledger",
        "buckets": {
            "itf_singles": {
                "completed": 1000,
                "any_tape": 980,
                "point_complete": 511,
                "complete_on_default_read": 440,
                "share": 0.511,
            },
        },
        "totals": {
            "completed": 1000,
            "any_tape": 980,
            "point_complete": 511,
            "complete_on_default_read": 440,
            "share": 0.511,
        },
    }

    def test_path_and_shape(self):
        client = client_returning(httpx.Response(200, json=self.BODY))
        cov = client.get_history_coverage()
        assert str(client.requests[0].url).startswith(f"{BASE}/history/coverage")
        assert isinstance(cov.as_of, datetime)
        assert cov.method == "ledger"
        assert cov.buckets["itf_singles"]["point_complete"] == 511
        assert cov.totals["complete_on_default_read"] == 440

    def test_403_names_basic(self):
        client = client_returning(
            httpx.Response(403, json={"error": "upgrade_required"}), max_retries=0
        )
        with pytest.raises(UpgradeRequired) as exc:
            client.get_history_coverage()
        assert exc.value.required_tier == "BASIC"

    def test_unbuilt_artifact_is_a_503_with_the_code_readable(self):
        client = client_returning(
            httpx.Response(503, json={"error": "coverage_unavailable"}), max_retries=0
        )
        with pytest.raises(ServiceUnavailable) as exc:
            client.get_history_coverage()
        assert exc.value.error_code == "coverage_unavailable"

    async def test_async_mirrors_sync(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=self.BODY))
        async with AsyncLiveTennisAPI("k", transport=transport) as client:
            cov = await client.get_history_coverage()
        assert cov.buckets["itf_singles"]["completed"] == 1000
        assert isinstance(cov.as_of, datetime)


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

    async def test_async_new_surface_mirrors_sync(self):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"system": "atp", "rank": 1, "previous_rank": 2}]})
        )
        async with AsyncLiveTennisAPI("k", transport=transport) as client:
            page = await client.list_rankings(system="atp")
        assert page[0].rank == 1
        assert page[0].previous_rank == 2

    async def test_async_draw_filter_mirrors_sync(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": []})

        async with AsyncLiveTennisAPI("k", transport=httpx.MockTransport(handler)) as client:
            await client.list_matches(draw="doubles")
            await client.list_fixtures(tour="atp", draw="singles")
        assert seen[0].url.params["draw"] == "doubles"
        assert seen[1].url.params["tour"] == "atp"
        assert seen[1].url.params["draw"] == "singles"

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
            "list_tournaments", "get_tournament",
            "list_archive_matches", "get_archive_match",
            "list_archive_players", "get_archive_career", "get_h2h",
            "get_match_tape", "get_match_statistics", "list_rankings",
            "get_history_coverage",
            "get_match_points", "iter_match_points",
            "list_history_packages", "get_history_package",
            "list_rally_matches", "get_rally_match", "get_match_rally",
            "get_charting_player", "get_charting_match",
            "get_ws_token", "get_usage",
        }
        for name in endpoints:
            assert hasattr(LiveTennisAPI, name), f"sync client missing {name}"
            assert hasattr(AsyncLiveTennisAPI, name), f"async client missing {name}"
