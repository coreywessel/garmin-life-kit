"""
Expiry warnings for the Alerts topic, from setup.json "expiries" (written by setup).

    warnings(today) -> list of HTML messages: 30, 14 and 7 days out, then daily from 3 days
    python -m kit.expiry          print today's warnings (nothing when there's nothing to say)
    python -m kit.expiry --list   print every key with its date and fix
"""
import sys
from datetime import date

from kit import profile
from kit.telegram import esc

WARN_DAYS = {30, 14, 7}


def rows(setup=None):
    out = []
    for e in (setup if setup is not None else profile.setup()).get("expiries") or []:
        try:
            out.append((e, date.fromisoformat(e["expires"])))
        except (KeyError, TypeError, ValueError):
            print(f"WARNING: setup.json expiry entry has no valid date: {e.get('name', e)}")
    return out


def message(e, left):
    when = f"in {left} days" if left > 1 else "tomorrow" if left == 1 else "TODAY" if left == 0 else f"{-left} days ago"
    return (f"<b>{esc(e['name'])} expires {when}</b> ({e['expires']}).\n"
            f"Breaks: {esc(e.get('breaks', 'see SETUP.md'))}.\nFix: {esc(e.get('fix', 'see SETUP.md'))}")


def warnings(today=None, setup=None):
    today = today or profile.today()
    out = []
    for e, expires in rows(setup):
        left = (expires - today).days
        if left in WARN_DAYS or left <= 3:
            out.append(message(e, left))
    return out


def main(argv):
    today = profile.today()
    if "--list" in argv:
        items = [message(e, (d - today).days) for e, d in rows()]
        print("\n\n".join(items) if items else "setup.json has no expiry dates yet.")
        return 0
    for w in warnings(today):
        print(w + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
