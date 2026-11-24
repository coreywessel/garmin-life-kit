"""
A card in Fitness for each new Garmin activity, so the day updates as it happens instead of
waiting for the evening recap. No Claude here, it's all counting.

    python -m kit.cards

Seen activity ids live in data/seen-activities.json. The first run only records what's already
there, so it never floods the topic with old activities. An id is added to the seen list only
after its card was sent, so a failed send is retried next run.

A Garmin problem is printed and skipped (exit 0): this runs every 2 hours and the morning recap
already alerts once a day with the fix. A dry run always shows the newest fixture activity
and never saves the seen list.
"""
import json
import sys

from kit import fetch, garmin, metrics, numbers, profile, telegram
from kit.telegram import esc

KEEP = 500


def seen_path():
    return profile.data_dir() / "seen-activities.json"


def load_seen():
    """None on the first run (no file yet)."""
    try:
        return set(json.loads(seen_path().read_text(encoding="utf-8"))["ids"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError):
        print("WARNING: seen-activities.json is unreadable - treating this as a first run", file=sys.stderr)
        return None


def save_seen(ids):
    path = seen_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ids": sorted(ids)[-KEEP:]}) + "\n", encoding="utf-8")


def card(a, units=None, on=None):
    """Everything worth knowing about one activity (a slim dict), readable without opening Garmin.
    Zones, training effect, load and VO2 max appear only when their metric is on and present."""
    if units is None or on is None:
        p = profile.load()
        units, on = units or p["units"], metrics.enabled(p) if on is None else on
    n = numbers.activity(a, units, on, detail=True)
    lines = [f"<b>{esc(n['name'])}</b>" + (f" · {n['start']}" if n.get("start") else "")]
    main = [n.get(k) for k in ("distance", "time", "pace", "avg_speed")]
    if any(main):
        lines.append(" · ".join(x for x in main if x))
    if numbers.is_snow(a):
        ride = [f"{n['vertical']} vertical" if n.get("vertical") else None,
                f"top {n['top_speed']}" if n.get("top_speed") else None,
                f"{n['runs']} runs" if n.get("runs") else None]
        if any(ride):
            lines.append(" · ".join(x for x in ride if x))
    hr = [f"HR {n['avg_hr']} avg" + (f" / {n['max_hr']} max" if n.get("max_hr") else "") if n.get("avg_hr") else None,
          f"+{n['climb']}" if n.get("climb") else None]
    if any(hr):
        lines.append(" · ".join(x for x in hr if x))
    if n.get("zones"):
        lines.append(f"Zones: {n['zones']}")
    te = [f"Effect {n['effect']}" if n.get("effect") else None,
          f"load {n['load']}" if n.get("load") else None,
          f"VO2 max {n['vo2max']}" if n.get("vo2max") else None,
          f"{a['calories']:.0f} cal" if a.get("calories") else None]
    if any(te):
        lines.append(" · ".join(x for x in te if x))
    return "\n".join(lines)


def run():
    try:
        p = profile.load()
    except profile.ProfileError as e:
        profile.fail(str(e))
    if not p["features"]["activity_cards"]:
        print("activity cards are off in profile.toml - nothing to do")
        return 0
    try:
        acts = [fetch.slim(a) for a in garmin.connect().latest_activities(20)]
    except garmin.GarminError as e:
        print(f"::warning::activity cards skipped: {e}")
        return 0
    acts = [a for a in acts if a.get("activityId")]
    seen = load_seen()
    if profile.dry():
        newest = max(acts, key=lambda a: a.get("startTimeLocal", ""), default=None)
        seen = {a["activityId"] for a in acts if a is not newest}
    if seen is None:
        save_seen({a["activityId"] for a in acts})
        print(f"first run: recorded {len(acts)} existing activities, posted nothing")
        return 0
    new = sorted((a for a in acts if a["activityId"] not in seen), key=lambda a: a.get("startTimeLocal", ""))
    if not new:
        print(f"no new activities ({len(acts)} checked)")
        return 0
    for a in new:
        try:
            telegram.send("fitness", card(a, p["units"], metrics.enabled(p)), silent=True)
        except telegram.TelegramError as e:
            profile.fail(f"Activity card not sent. {e}")
        seen.add(a["activityId"])
        if not profile.dry():
            save_seen(seen)                      # after each confirmed send, never before
    print(f"posted {len(new)} card(s)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
