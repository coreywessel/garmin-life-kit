"""
Send to the Telegram group. All output is HTML (<b>, <i>) with text escaped.

- The bot token comes from TELEGRAM_BOT_TOKEN only. Errors carry Telegram's description,
  never the request URL (it contains the token).
- Every send checks ok=true. A job that exits 0 on a failed send looks exactly like one that worked.
- A dry run prints to stdout instead.

    python -m kit.telegram alerts "<b>Hello</b>"            send text to a topic (or a thread id)
    python -m kit.telegram alerts --failed "Morning recap"  the workflow failure alert: what failed,
                                                            the reason from out/error.txt, the log link
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from kit import profile

LIMIT = 4096          # Telegram's cap, counted on visible text in UTF-16 units
TOPICS = ("today", "fitness", "log", "alerts")
NO_IDS = "setup.json has no telegram ids yet - run setup step 6"


class TelegramError(RuntimeError):
    """A send didn't go through. The message says why and what to do."""


def esc(s):
    return html.escape(str(s), quote=False)


def plain(s):
    """HTML -> the visible text, for the resend when Telegram can't parse our HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def tg_len(s):
    return len(plain(s).encode("utf-16-le")) // 2


def split(text, limit=LIMIT):
    """Split on blank lines so a section is never cut in half. A single block that is still
    too long is cut on line breaks, then hard."""
    if tg_len(text) <= limit:
        return [text]
    room = limit - 32
    blocks = []
    for block in text.split("\n\n"):
        if tg_len(block) <= room:
            blocks.append(block)
            continue
        cur = ""
        for line in block.split("\n"):
            while tg_len(line) > room:           # one enormous line: hard cut
                if cur:
                    blocks.append(cur)
                    cur = ""
                blocks.append(line[:room])
                line = line[room:]
            cand = line if not cur else cur + "\n" + line
            if tg_len(cand) > room and cur:
                blocks.append(cur)
                cur = line
            else:
                cur = cand
        if cur:
            blocks.append(cur)
    parts, cur = [], ""
    for block in blocks:
        cand = block if not cur else cur + "\n\n" + block
        if tg_len(cand) > room and cur:
            parts.append(cur)
            cur = block
        else:
            cur = cand
    if cur:
        parts.append(cur)
    return parts


def token():
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not t:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set. Fix: ask Claude to run "
                            "`uv run setup/kit.py secret TELEGRAM_BOT_TOKEN`, then `uv run setup/kit.py secrets`")
    return t


def call(method, **fields):
    """One Bot API call. Always returns a dict with ok; never raises, never shows the URL."""
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                   for k, v in fields.items() if v is not None}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token()}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return {"ok": False, "description": f"network failure ({type(e).__name__})"}


def resolve(target):
    """Topic name, thread id, or ''/None for General -> (chat_id, thread_id, label)."""
    tg = profile.setup().get("telegram") or {}
    if not tg.get("chat_id"):
        raise TelegramError(NO_IDS)
    if target in (None, ""):
        return tg["chat_id"], None, "general"
    if isinstance(target, int) or str(target).lstrip("-").isdigit():
        return tg["chat_id"], int(target), f"thread {target}"
    thread = (tg.get("topics") or {}).get(target)
    if not thread:
        raise TelegramError(f"setup.json has no '{target}' topic yet - run setup step 6")
    return tg["chat_id"], thread, target


def send(target, text, silent=False):
    """Send HTML to a topic ("today", "fitness", "log", "alerts"), a thread id, or General ("").
    Returns the message ids. Raises TelegramError if Telegram didn't accept every part."""
    chat, thread, label = resolve(target)
    parts = split(text.strip())
    if profile.dry():
        print(f"--- [{label}]{' (silent)' if silent else ''} ---")
        print("\n\n".join(parts))
        print()
        return []
    ids = []
    for part in parts:
        common = dict(chat_id=chat, message_thread_id=thread, disable_web_page_preview="true",
                      disable_notification="true" if silent else None)
        res = call("sendMessage", text=part, parse_mode="HTML", **common)
        if not res.get("ok") and "can't parse entities" in res.get("description", ""):
            # Broken HTML (usually from Claude). Plain text beats not sending at all.
            res = call("sendMessage", text=plain(part), **common)
        if not res.get("ok"):
            raise TelegramError(f"Telegram didn't take the message for {label}: "
                                f"{res.get('description', 'no reason given')}. "
                                "Fix: if it says 'bot was kicked' or 'chat not found', re-run setup step 6; "
                                "'Unauthorized' means the bot token changed")
        ids.append(res["result"]["message_id"])
    return ids


def failed_text(what):
    """The workflow failure alert: what failed, why (out/error.txt if a step wrote it), log link."""
    try:
        reason = (profile.OUT / "error.txt").read_text(encoding="utf-8").strip()
    except OSError:
        reason = ""
    lines = [f"<b>{esc(what)} failed.</b>"]
    lines.append(esc(reason) if reason else "The log says which step broke.")
    if os.environ.get("RUN_URL"):
        lines.append(f"Log: {esc(os.environ['RUN_URL'])}")
    return "\n".join(lines)


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    target = argv[0]
    text = failed_text(argv[2] if len(argv) > 2 else "A job") if argv[1] == "--failed" else argv[1]
    try:
        send(target, text)
    except TelegramError as e:
        raise SystemExit(f"FAILED: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
