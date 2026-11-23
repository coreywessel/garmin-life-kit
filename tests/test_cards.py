"""Activity cards: no flood on the first run, no lost card on a failed send, snow-day numbers."""
import json

import pytest

from kit import cards, fetch, garmin


class FakeGarmin:
    def __init__(self, acts):
        self.acts = acts

    def latest_activities(self, limit=20):
        return self.acts[:limit]


def act(aid, name="Run", key="running", start="2026-01-14 07:00:00", **kw):
    return {"activityId": aid, "activityName": name, "activityType": {"typeKey": key},
            "startTimeLocal": start, "distance": 5000.0, "duration": 1500.0, "averageSpeed": 3.33, **kw}


def use(monkeypatch, acts):
    monkeypatch.setattr(garmin, "connect", lambda: FakeGarmin(acts))


def seen():
    return set(json.loads(cards.seen_path().read_text(encoding="utf-8"))["ids"])


def test_first_run_seeds_without_posting(monkeypatch, sent):
    """The first run must never flood Fitness with every old activity."""
    use(monkeypatch, [act(1), act(2)])
    assert cards.run() == 0
    assert sent == []
    assert seen() == {1, 2}


def test_new_activity_is_posted_then_marked_seen(monkeypatch, sent):
    """A new activity gets one silent card, and is marked seen so it isn't posted twice."""
    use(monkeypatch, [act(1)])
    cards.run()
    use(monkeypatch, [act(2, start="2026-01-14 18:00:00"), act(1)])
    cards.run()
    assert [t for t, _ in sent] == ["fitness"]
    assert seen() == {1, 2}
    cards.run()
    assert len(sent) == 1


def test_seen_list_not_saved_when_send_fails(monkeypatch, sent):
    """A failed send must leave the activity unseen, so the next run posts it."""
    use(monkeypatch, [act(1)])
    cards.run()
    use(monkeypatch, [act(2), act(1)])
    sent.fail_on = "fitness"
    with pytest.raises(SystemExit):
        cards.run()
    assert seen() == {1}


def test_garmin_problem_skips_quietly(monkeypatch, sent):
    """Every 2 hours must not spam Alerts; the morning recap alerts once with the fix."""
    def boom():
        raise garmin.RateLimited("Garmin rate-limited the API key (HTTP 429)")
    monkeypatch.setattr(garmin, "connect", boom)
    assert cards.run() == 0
    assert sent == []


def snow():
    return fetch.slim(act(9, name="Pine Ridge Snowboard (12 Runs)", key="resort_snowboarding",
                          elevationLoss=2566.0, elevationGain=2540.0, maxSpeed=18.3, averageSpeed=1.75,
                          distance=31250.0, duration=17880.0))


def test_snowboard_card_imperial():
    """Snow days: vertical is the descent, top speed is maxSpeed, runs come from the name, lift climb hidden."""
    text = cards.card(snow(), "imperial")
    assert "8,419 ft vertical" in text
    assert "top 41 mph" in text
    assert "12 runs" in text
    assert "8,333 ft" not in text           # elevationGain is the lifts
    assert "/mi" not in text                 # no running pace on a snowboard


def test_snowboard_card_metric():
    """The same day in metric units."""
    text = cards.card(snow(), "metric")
    assert "2,566 m vertical" in text and "top 66 km/h" in text and "31.25 km" in text


def test_no_runs_figure_without_one_in_the_name():
    """No run count in the name means no runs figure, never a guess."""
    a = snow()
    a["activityName"] = "Snowboarding"
    assert "runs" not in cards.card(a, "imperial")


def test_run_card_has_pace_and_zones():
    """A run card shows pace and the zone split."""
    a = fetch.slim(act(3, hrTimeInZone_1=60, hrTimeInZone_2=540, hrTimeInZone_3=300, hrTimeInZone_4=100))
    text = cards.card(a, "imperial")
    assert "8:03/mi" in text and "Zones: Z1-2 60% · Z3 30% · Z4-5 10%" in text
