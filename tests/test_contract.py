"""Contract tests — run against the real API.

A valid spec is not proof of behaviour. These assert that what the production
API *actually sends* matches what these models expect, which is the only way to
catch drift between the two.

    LIVETENNISAPI_KEY=twjp_… pytest -m contract

Skipped automatically when no key is set, so CI stays green for contributors
without credentials. The health check runs regardless — it needs no auth.

Everything here is read-only and tolerant of an empty slate: at 3am there may be
no live matches, and that is not a failure.
"""

from __future__ import annotations

import os
from datetime import date, datetime

import pytest

from livetennisapi import (
    LiveTennisAPI,
    Match,
    NotFound,
    Player,
    Score,
    UpgradeRequired,
)

KEY = os.environ.get("LIVETENNISAPI_KEY", "").strip()
TIER = os.environ.get("LIVETENNISAPI_TIER", "").strip().lower()  # optional: basic|pro|ultra

needs_key = pytest.mark.skipif(not KEY, reason="set LIVETENNISAPI_KEY to run contract tests")

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def client():
    with LiveTennisAPI(KEY or None) as c:
        yield c


# -- unauthenticated ----------------------------------------------------------


def test_health_needs_no_key():
    with LiveTennisAPI("") as anon:
        health = anon.health()
    assert health.get("status") == "ok"
    assert health.get("version") == "v1"


# -- BASIC --------------------------------------------------------------------


@needs_key
def test_list_matches_shape(client):
    page = client.list_matches(status="live", limit=5)
    assert page.meta is None or page.meta.limit is not None

    for match in page:
        assert isinstance(match, Match)
        assert match.id is not None, "every match must carry an id"
        assert match.status in ("live", "upcoming", "completed", "cancelled", None)
        if match.score is not None:
            assert isinstance(match.score, Score)
        if match.players:
            for side in ("p1", "p2"):
                if match.players.get(side) is not None:
                    assert isinstance(match.players[side], Player)


@needs_key
def test_score_games_are_player_major(client):
    """The layout the whole SDK depends on: [games_p1, games_p2]."""
    for match in client.list_matches(status="live", limit=10):
        score = match.score
        if not score or not score.games:
            continue
        assert len(score.games) == 2, (
            f"expected games to be [p1, p2] (player-major), got {len(score.games)} "
            f"entries: {score.games!r}"
        )
        assert all(isinstance(side, list) for side in score.games)
        # The two sides describe the same match, so their lengths agree within
        # one (the in-progress set can land on one side first).
        assert abs(len(score.games[0]) - len(score.games[1])) <= 1
        return
    pytest.skip("no live match with games on the board right now")


@needs_key
def test_timestamps_are_utc_and_parsed(client):
    for match in client.list_matches(status="live", limit=10):
        if match.score is not None and match.score.timestamp is not None:
            assert isinstance(match.score.timestamp, datetime), "timestamp did not parse"
            assert match.score.timestamp.tzinfo is not None, "timestamp is not tz-aware"
            return
    pytest.skip("no timestamped live score right now")


@needs_key
def test_get_match_round_trips(client):
    page = client.list_matches(status="live", limit=1) or client.list_completed_matches(limit=1)
    if not len(page):
        pytest.skip("no matches available")
    match_id = page[0].id
    detail = client.get_match(match_id)
    assert detail is not None
    assert detail.id == match_id


@needs_key
def test_get_match_score(client):
    page = client.list_matches(status="live", limit=1)
    if not len(page):
        pytest.skip("no live matches")
    score = client.get_match_score(page[0].id)
    assert score is None or isinstance(score, Score)


@needs_key
def test_search_players(client):
    page = client.search_players("a", limit=5)
    for player in page:
        assert isinstance(player, Player)
        assert player.id is not None
        if player.birthday is not None:
            assert isinstance(player.birthday, date)


@needs_key
def test_get_player(client):
    page = client.search_players("a", limit=1)
    if not len(page):
        pytest.skip("no players matched")
    player = client.get_player(page[0].id)
    assert player is not None and player.id == page[0].id


@needs_key
def test_fixtures(client):
    for fixture in client.list_fixtures(limit=5):
        assert fixture.id is not None
        if fixture.event_date is not None:
            assert isinstance(fixture.event_date, date)


@needs_key
def test_history_has_derived_winner(client):
    page = client.list_completed_matches(limit=5)
    for match in page:
        assert match.status in ("completed", None)
        assert match.winner in (1, 2, None)


@needs_key
def test_unknown_match_id_is_not_found(client):
    with pytest.raises(NotFound):
        client.get_match(999_999_999)


@needs_key
def test_limit_is_respected(client):
    assert len(client.list_matches(status="completed", limit=3)) <= 3


# -- tier boundaries ----------------------------------------------------------
#
# These prove the 403 path is wired correctly. Which assertion applies depends
# on the key's tier, so each is expressed as "either it works, or it raises
# UpgradeRequired naming the right tier" — true regardless of tier, and it
# still catches a 403 that surfaces as the wrong exception.


@needs_key
def test_events_are_pro_gated(client):
    page = client.list_matches(status="live", limit=1) or client.list_completed_matches(limit=1)
    if not len(page):
        pytest.skip("no matches available")
    try:
        events = client.list_match_events(page[0].id, limit=5)
    except UpgradeRequired as exc:
        assert exc.required_tier == "PRO"
        assert exc.error_code == "upgrade_required"
    else:
        for event in events:
            assert event.player in (1, 2, None)


@needs_key
def test_markets_are_pro_gated(client):
    page = client.list_matches(status="live", limit=1)
    if not len(page):
        pytest.skip("no live matches")
    try:
        markets = client.list_markets(page[0].id)
    except UpgradeRequired as exc:
        assert exc.required_tier == "PRO"
    else:
        for market in markets:
            assert market.id is not None


@needs_key
def test_analysis_is_ultra_gated(client):
    page = client.list_matches(status="live", limit=1) or client.list_completed_matches(limit=1)
    if not len(page):
        pytest.skip("no matches available")
    try:
        analysis = client.get_match_analysis(page[0].id)
    except UpgradeRequired as exc:
        assert exc.required_tier == "ULTRA"
    except NotFound:
        pass  # entitled, but this match has no analysis
    else:
        if analysis is not None:
            assert analysis.thesis is None or isinstance(analysis.thesis, dict)
            assert analysis.profile is None or isinstance(analysis.profile, dict)


@needs_key
@pytest.mark.skipif(TIER not in ("", "ultra"), reason="model fields need ULTRA")
def test_model_fields_present_on_ultra(client):
    """win_probability_p1 / danger ride along on ULTRA scores."""
    for match in client.list_matches(status="live", limit=10):
        if match.score is not None and match.score.win_probability_p1 is not None:
            assert 0.0 <= match.score.win_probability_p1 <= 1.0
            return
    pytest.skip("no live match exposed a win probability (needs ULTRA and a live match)")


# -- forward compatibility against production ---------------------------------


@needs_key
def test_no_unmodelled_field_breaks_parsing(client):
    """If production has fields we don't declare, report them — never fail.

    A new field is expected and must not break the client. This test exists to
    surface drift in the log so the models can be updated deliberately.
    """
    declared = {f for f in Match.__dataclass_fields__ if f != "raw"}
    unknown: set[str] = set()
    for match in client.list_matches(status="live", limit=10):
        unknown |= set(match.raw) - declared
    if unknown:
        print(f"\nNOTE: production sends undeclared Match fields: {sorted(unknown)}")
    # Deliberately no assertion: parsing survived, which is the contract.
