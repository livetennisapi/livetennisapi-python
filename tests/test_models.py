"""Model behaviour — above all, forward compatibility.

The API ships additive changes within v1, so the single most important property
of these models is that a field they have never heard of does not break them.
"""

from datetime import date, datetime

import pytest

from livetennisapi.models import (
    ArchiveMatch,
    ArchiveParticipant,
    ArchivePlayerBio,
    Fixture,
    HeadToHead,
    Market,
    Match,
    Page,
    Player,
    Score,
    Tournament,
)


class TestForwardCompatibility:
    """A new server field must never break an old client."""

    def test_unknown_fields_do_not_raise(self):
        match = Match.from_dict(
            {"id": 1, "tournament": "ATP Wimbledon", "a_field_invented_next_year": {"nested": True}}
        )
        assert match.id == 1
        assert match.tournament == "ATP Wimbledon"

    def test_unknown_fields_are_readable_as_attributes(self):
        match = Match.from_dict({"id": 1, "future_field": "hello"})
        assert match.future_field == "hello"

    def test_unknown_fields_are_in_raw(self):
        match = Match.from_dict({"id": 1, "future_field": "hello"})
        assert match.raw["future_field"] == "hello"

    def test_raw_preserves_the_exact_payload(self):
        payload = {"id": 1, "tournament": "X", "extra": [1, 2, 3]}
        assert Match.from_dict(payload).to_dict() == payload

    def test_missing_fields_become_none(self):
        match = Match.from_dict({"id": 1})
        assert match.tournament is None
        assert match.score is None

    def test_genuinely_absent_attribute_still_raises(self):
        """Forward compatibility must not turn every typo into a silent None."""
        match = Match.from_dict({"id": 1})
        with pytest.raises(AttributeError, match="definitely_not_a_field"):
            _ = match.definitely_not_a_field

    def test_none_in_none_out(self):
        assert Match.from_dict(None) is None

    def test_unexpected_type_is_preserved_not_coerced(self):
        # The server sent a list where an object was documented.
        match = Match.from_dict(["not", "an", "object"])
        assert match is not None
        assert match.raw["_unexpected"] == ["not", "an", "object"]


class TestScore:
    """`games` is player-major. This is the API's sharpest edge."""

    def test_games_is_player_major(self):
        # 6-4, 3-6, 2-1
        score = Score.from_dict({"games": [[6, 3, 2], [4, 6, 1]], "sets": [1, 1]})
        assert score.games_for_set(0) == (6, 4)
        assert score.games_for_set(1) == (3, 6)
        assert score.games_for_set(2) == (2, 1)

    def test_games_for_set_out_of_range(self):
        score = Score.from_dict({"games": [[6], [4]]})
        assert score.games_for_set(5) == (None, None)

    def test_games_for_set_with_no_games(self):
        assert Score.from_dict({}).games_for_set(0) == (None, None)

    def test_ragged_games_do_not_raise(self):
        # A set in progress can leave the two sides different lengths.
        score = Score.from_dict({"games": [[6, 3, 2], [4, 6]]})
        assert score.games_for_set(2) == (2, None)

    def test_timestamp_is_parsed(self):
        score = Score.from_dict({"timestamp": "2026-07-18T14:30:00Z"})
        assert isinstance(score.timestamp, datetime)
        assert score.timestamp.year == 2026

    def test_unparseable_timestamp_is_left_alone(self):
        score = Score.from_dict({"timestamp": "not a timestamp"})
        assert score.timestamp == "not a timestamp"

    def test_model_fields_absent_below_ultra(self):
        score = Score.from_dict({"sets": [1, 0]})
        assert score.win_probability_p1 is None
        assert score.danger is None


class TestMatch:
    def test_nested_players_become_models(self):
        match = Match.from_dict(
            {"id": 1, "players": {"p1": {"id": 10, "name": "A"}, "p2": {"id": 11, "name": "B"}}}
        )
        assert isinstance(match.p1, Player)
        assert match.p1.name == "A"
        assert match.p2.name == "B"

    def test_missing_players_object(self):
        match = Match.from_dict({"id": 1})
        assert match.p1 is None
        assert match.p2 is None

    def test_nested_score_becomes_a_model(self):
        match = Match.from_dict({"id": 1, "score": {"sets": [1, 0]}})
        assert isinstance(match.score, Score)
        assert match.score.sets == [1, 0]

    def test_market_and_analysis_absent_below_tier(self):
        match = Match.from_dict({"id": 1})
        assert match.market is None
        assert match.analysis is None

    def test_scheduled_time_is_parsed(self):
        match = Match.from_dict({"id": 1, "scheduled_time": "2026-07-18T14:30:00Z"})
        assert isinstance(match.scheduled_time, datetime)


class TestMarket:
    def test_prices_become_models(self):
        market = Market.from_dict(
            {"id": 1, "prices": [{"side": 1, "mid": 0.62}, {"side": 2, "mid": 0.38}]}
        )
        assert len(market.prices) == 2
        assert market.prices[0].mid == 0.62

    def test_missing_prices_is_an_empty_list(self):
        assert Market.from_dict({"id": 1}).prices == []


class TestPlayer:
    def test_birthday_is_parsed_as_a_date(self):
        player = Player.from_dict({"id": 1, "birthday": "1987-05-22"})
        assert isinstance(player.birthday, date)
        assert player.birthday.year == 1987

    def test_null_birthday(self):
        assert Player.from_dict({"id": 1, "birthday": None}).birthday is None


class TestFixture:
    def test_event_date_is_parsed(self):
        fixture = Fixture.from_dict({"id": 1, "event_date": "2026-07-20"})
        assert isinstance(fixture.event_date, date)

    def test_start_time_is_parsed_and_nullable(self):
        # A date-only fixture is a real state: start_time stays None until the
        # order of play assigns a time.
        fixture = Fixture.from_dict({"id": 1, "start_time": "2026-08-03T11:00:00Z"})
        assert isinstance(fixture.start_time, datetime)
        assert Fixture.from_dict({"id": 1, "start_time": None}).start_time is None

    def test_player_ids_and_round_code(self):
        fixture = Fixture.from_dict({"id": 1, "player1_id": 925, "player2_id": None, "round_code": "QF"})
        assert fixture.player1_id == 925
        assert fixture.player2_id is None
        assert fixture.round_code == "QF"


class TestMatchNewFields:
    def test_tour_and_tournament_id(self):
        match = Match.from_dict({"id": 1, "tour": "itf", "tournament_id": "itf-m15-wuning"})
        assert match.tour == "itf"
        assert match.tournament_id == "itf-m15-wuning"

    def test_round_code_and_withdrew(self):
        match = Match.from_dict(
            {"id": 1, "round_code": "R16", "event_status": "Retired", "winner": 2, "withdrew": 1}
        )
        assert match.round_code == "R16"
        assert match.withdrew == 1

    def test_withdrew_absent_means_none(self):
        # Absent means "not a withdrawal, or no evidence" — never a guess.
        assert Match.from_dict({"id": 1}).withdrew is None


class TestTournament:
    def test_fields(self):
        t = Tournament.from_dict(
            {"id": "atp-wimbledon", "name": "Wimbledon", "tour": "atp", "surface": "grass",
             "indoor": False, "city": "London", "country": "GB", "category": "grand_slam"}
        )
        assert t.id == "atp-wimbledon"
        assert t.category == "grand_slam"

    def test_uncurated_location_is_none(self):
        t = Tournament.from_dict({"id": "x", "name": "Y", "city": None, "country": None, "category": None})
        assert t.city is None
        assert t.category is None


class TestArchiveMatch:
    def test_winner_and_loser_become_participants(self):
        m = ArchiveMatch.from_dict(
            {
                "id": 1, "tour": "atp", "event_date": "1980-06-23", "round": "F",
                "winner": {"name": "Bjorn Borg", "rank": 1, "player_id": 100437, "entry": None},
                "loser": {"name": "John McEnroe", "rank": 2, "player_id": 100581, "seed": 2},
                "score": "1-6 7-5 6-3 6-7(16) 8-6", "outcome": "completed",
            }
        )
        assert isinstance(m.winner, ArchiveParticipant)
        assert m.winner.name == "Bjorn Borg"
        assert m.loser.player_id == 100581
        assert isinstance(m.event_date, date)

    def test_pre_1991_stats_stay_none(self):
        # The corpus records serve stats from 1991; the None is honest.
        m = ArchiveMatch.from_dict({"id": 1, "event_date": "1975-01-01", "stats": None})
        assert m.stats is None


class TestArchivePlayerBio:
    def test_dates_are_parsed(self):
        bio = ArchivePlayerBio.from_dict(
            {"id": 100437, "tour": "atp", "name": "Bjorn Borg", "dob": "1956-06-06",
             "career_high_rank": 1, "career_high_date": "1977-08-23"}
        )
        assert isinstance(bio.dob, date)
        assert isinstance(bio.career_high_date, date)
        assert bio.career_high_rank == 1

    def test_the_eras_silence_is_none(self):
        bio = ArchivePlayerBio.from_dict({"id": 1, "tour": "wta", "hand": None, "height_cm": None})
        assert bio.hand is None
        assert bio.height_cm is None


class TestHeadToHead:
    def test_shape(self):
        h2h = HeadToHead.from_dict(
            {
                "players": {"p1": {"name": "Roger Federer"}, "p2": {"name": "Rafael Nadal"}},
                "totals": {"p1_wins": 16, "p2_wins": 24, "meetings": 40, "undecided": 0},
                "by_surface": {"clay": {"p1": 2, "p2": 14}},
                "meetings": [{"era": "current", "match_id": 18453, "winner": 2, "outcome": "completed"}],
            }
        )
        assert h2h.totals["p2_wins"] == 24
        assert h2h.meetings[0]["era"] == "current"

    def test_no_match_is_an_empty_record_not_a_crash(self):
        h2h = HeadToHead.from_dict({"players": None, "totals": {"meetings": 0}})
        assert h2h.players is None
        assert h2h.meetings == []


class TestPage:
    def test_page_is_iterable_and_sized(self):
        page = Page(data=[1, 2, 3])
        assert len(page) == 3
        assert list(page) == [1, 2, 3]
        assert page[0] == 1
        assert page.count == 3
