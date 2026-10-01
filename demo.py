"""Public demo mode: the same app, safe to put on the internet.

Switched on with DEMO_MODE=1 (the live site) or `python app.py --demo` (to try it
locally on port 5001). Differences from the personal app:

  - Each visitor gets their OWN small database file, copied from a sample deck
    (demo/sample_deck.json). Nothing about one visitor's progress is shared with
    another, and none of the app's queries had to change to make that true.
  - Visitors are anonymous: a random ID in a signed browser cookie, no sign-up.
  - AI grading: a few free grades a day on the site owner's key, with a cap for the
    whole site; or unlimited with the visitor's own key, which lives only in their
    browser cookie and is never written to the server's disk.
  - Reminders, emails, Claude-drafted cards and template drafting are switched off.
  - Old visitor files are deleted, so the server's disk can't fill up.
"""
import json
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timezone

import config
import db
import variants
from card_writer import add_card

VISITOR_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")  # what secrets.token_urlsafe(16) produces
OWN_KEY = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,200}$")


# ------------------------------------------------------------------ visitors

def new_visitor_id():
    return secrets.token_urlsafe(16)


def is_valid_visitor(visitor_id):
    return bool(visitor_id and VISITOR_ID.match(visitor_id))


def visitor_db(visitor_id):
    """The visitor's own database file, created from the sample deck on first use."""
    if not is_valid_visitor(visitor_id):  # never build a file path from unchecked input
        raise ValueError("bad visitor id")
    path = config.DEMO_DIR / "visitors" / f"{visitor_id}.db"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _make_room()
        shutil.copyfile(template_db(), path)
    return path


def _make_room():
    """Delete visitors idle for a week, and the oldest ones beyond the cap."""
    folder = config.DEMO_DIR / "visitors"
    files = sorted(folder.glob("*.db"), key=lambda f: f.stat().st_mtime)  # oldest first
    cutoff = time.time() - config.DEMO_KEEP_DAYS * 86400
    kept = []
    for f in files:
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
        else:
            kept.append(f)
    while len(kept) >= config.DEMO_MAX_VISITORS:  # still too many: drop the least recent
        kept.pop(0).unlink(missing_ok=True)


# ------------------------------------------------------------------ the sample deck

def template_db():
    path = config.DEMO_DIR / "template.db"
    if not path.exists():
        build_template_db(path)
    return path


def build_template_db(path=None):
    """Build the starting database every visitor copies: the sample deck, with the
    fresh-number templates already approved. Raises if any template fails its checks,
    so a broken card can never reach the live site."""
    path = path or config.DEMO_DIR / "template.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    conn = db.connect(tmp)
    deck = json.loads((config.PROJECT_DIR / "demo" / "sample_deck.json").read_text(encoding="utf-8"))
    for card in deck["cards"]:
        spec = card.get("template")
        if spec:  # math card: its text comes from the template's original numbers
            values = variants.original_values(spec)
            question, answer = variants.fill(spec["question"], values), variants.fill(spec["answer"], values)
            last = spec["derived"][-1]["name"]
            spec = dict(spec, expected=[{"name": last, "value": values[last]}])
            ok, notes = variants.check(spec, {"question": question, "answer": answer})
            if not ok:
                raise ValueError(f"sample card '{question[:50]}' fails its check: {notes}")
        else:
            question, answer = card["question"], card["answer"]
        card_id = add_card(conn, card["deck"], question, answer, card.get("topic"), origin="demo")
        if spec:
            conn.execute("INSERT INTO card_templates (card_id, status, spec, notes, updated_at) "
                         "VALUES (?, 'approved', ?, '[]', ?)",
                         (card_id, json.dumps(spec), datetime.now(timezone.utc).isoformat()))
    db.set_setting(conn, "desktop_notifications", "0")
    conn.commit()
    conn.close()
    tmp.replace(path)
    return path


# ------------------------------------------------------------------ free AI grades

def _shared():
    """A small database shared by all visitors, only for counting free AI grades."""
    config.DEMO_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DEMO_DIR / "shared.db", timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS free_grades ("
                 "day TEXT, visitor TEXT, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, visitor))")
    return conn


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def free_grades_left(visitor_id):
    """Free AI grades this visitor can still use today (the site-wide cap counts too)."""
    with _shared() as conn:
        mine = conn.execute("SELECT used FROM free_grades WHERE day = ? AND visitor = ?",
                            (_today(), visitor_id)).fetchone()
        everyone = conn.execute("SELECT COALESCE(SUM(used), 0) FROM free_grades WHERE day = ?",
                                (_today(),)).fetchone()[0]
    return max(0, min(config.DEMO_FREE_GRADES - (mine[0] if mine else 0),
                      config.DEMO_DAILY_CAP - everyone))


def use_free_grade(visitor_id):
    with _shared() as conn:
        conn.execute("INSERT INTO free_grades (day, visitor, used) VALUES (?, ?, 1) "
                     "ON CONFLICT(day, visitor) DO UPDATE SET used = used + 1",
                     (_today(), visitor_id))


def looks_like_api_key(key):
    return bool(OWN_KEY.match(key or ""))
