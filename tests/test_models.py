"""Model behaviour — above all, forward compatibility.

The API ships additive changes within v1, so the single most important property
of these models is that a field they have never heard of does not break them.
"""

from datetime import date, datetime

import pytest

from livetennisapi.models import Fixture, Market, Match, Page, Player, Score


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


class TestPage:
    def test_page_is_iterable_and_sized(self):
        page = Page(data=[1, 2, 3])
        assert len(page) == 3
        assert list(page) == [1, 2, 3]
        assert page[0] == 1
        assert page.count == 3
