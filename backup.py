"""Download all my progress as one file, and restore it later.

The file is plain JSON: every card, where each stands in its review schedule, the
review history, Claude's feedback and my study settings. It's what lets a demo
visitor keep their progress (the demo's server forgets everyone eventually), and
it doubles as a one-click backup for the personal app.

Restoring treats the uploaded file as untrusted: it could be damaged, from a
different version, or deliberately tampered with. So every table and column must
be one the app knows, every value must be plain text or a number, sizes are
capped, each card's schedule must load in the FSRS library, and the whole restore
happens in one transaction. If anything is wrong, nothing changes.
"""
import json
from datetime import datetime, timezone

import fsrs

import config
import db
import srs
import variants

FORMAT = 1
# Order matters: a row may only point at rows already restored (e.g. a review at its card).
TABLES = ["cards", "study_sessions", "ai_feedback", "review_state", "review_log",
          "card_templates", "deck_settings", "app_settings"]
MAX_ROWS = 200_000      # across all tables; my own 745-card database has about 1,100
MAX_TEXT = 20_000       # characters in any one value
# Settings a backup may carry, and which values each may take. Anything else is dropped.
SETTINGS = {
    "timer_seconds": lambda v: v.isdigit() and int(v) in config.TIMER_CHOICES,
    "session_size": lambda v: v.isdigit() and int(v) in config.SESSION_SIZES,
}
if not config.DEMO_MODE:  # reminder settings only mean something in the personal app
    SETTINGS |= {key: lambda v: True for key in
                 ("desktop_notifications", "email_to", "email_frequency", "last_email_date",
                  "reminder_times", "study_days")}


class BackupError(Exception):
    """A file that can't be restored, with the reason in plain English."""


def export(conn):
    """Everything worth keeping, as a dict ready for json.dumps."""
    tables = {t: [dict(row) for row in conn.execute(f"SELECT * FROM {t}")] for t in TABLES}
    return {"app": config.APP_NAME, "format": FORMAT,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tables": tables}


def filename():
    return f"{config.APP_NAME.lower()}-progress-{datetime.now():%Y-%m-%d}.json"


def restore(conn, raw, keep_templates_from=None):
    """Replace everything in `conn` with the backup in `raw` (bytes). Returns the
    number of my own cards restored. Raises BackupError, leaving the database untouched.

    keep_templates_from: the demo passes its sample-deck database here. Fresh-number
    templates are then taken from that trusted copy (matched by card), never from the
    file, because they contain formulas the server would run."""
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupError("That file isn't a progress file (it isn't readable JSON).")
    if not isinstance(data, dict) or data.get("app") != config.APP_NAME or not isinstance(data.get("tables"), dict):
        raise BackupError(f"That file isn't a {config.APP_NAME} progress file.")
    if data.get("format") != FORMAT:
        raise BackupError("That progress file is from a different version of the app.")

    tables = {t: _rows(data["tables"], t) for t in TABLES}
    if sum(map(len, tables.values())) > MAX_ROWS:
        raise BackupError("That progress file is too large to restore.")
    if not tables["cards"]:
        raise BackupError("That progress file has no cards in it.")
    tables["review_state"] = [_checked_state(r) for r in tables["review_state"]]
    tables["app_settings"] = [r for r in tables["app_settings"]
                              if r.get("key") in SETTINGS and isinstance(r.get("value"), str)
                              and SETTINGS[r["key"]](r["value"])]
    if keep_templates_from is not None:
        tables["card_templates"] = _trusted_templates(keep_templates_from, tables["cards"])
    else:
        tables["card_templates"] = [_checked_template(r) for r in tables["card_templates"]]

    try:
        for t in reversed(TABLES):  # children first, so no row is left pointing at nothing
            conn.execute(f"DELETE FROM {t}")
        for t in TABLES:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({t})")}
            for row in tables[t]:
                row = {k: v for k, v in row.items() if k in columns}  # names come from the
                if row:                                                # schema, never the file
                    conn.execute(f"INSERT INTO {t} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                                 list(row.values()))
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("That progress file is inconsistent (some rows point at missing cards).")
        conn.commit()
    except BackupError:
        conn.rollback()
        raise
    except (db.sqlite3.Error, OverflowError) as err:
        conn.rollback()
        raise BackupError("That progress file is damaged, so nothing was changed.") from err
    return sum(1 for card in tables["cards"] if card.get("origin") != "demo")  # my own cards


def _rows(tables, name):
    rows = tables.get(name, [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise BackupError(f"That progress file is damaged (the {name} section).")
    for row in rows:
        for value in row.values():
            if not (value is None or isinstance(value, (int, float, str))) or isinstance(value, bool):
                raise BackupError(f"That progress file is damaged (an odd value in {name}).")
            if isinstance(value, str) and len(value) > MAX_TEXT:
                raise BackupError("That progress file has a value that's far too long.")
    return rows


def _checked_state(row):
    """A card's schedule must load in FSRS; the copies used for sorting are rebuilt from it."""
    try:
        card = fsrs.Card.from_json(row["fsrs_card"])
    except Exception:
        raise BackupError("That progress file has a card schedule the app can't read.")
    return {"card_id": row.get("card_id"), "fsrs_card": card.to_json(),
            "due": srs._iso(card.due), "state": card.state.name}


def _checked_template(row):
    """Templates are run by the app's safe calculator; a broken one is switched off."""
    try:
        spec = json.loads(row["spec"])
        variants.validate(spec)
        variants.sample(spec, 1)
    except Exception:
        row = {**row, "status": "off"}
    return row


def _trusted_templates(path, cards):
    """The sample deck's templates, for the cards in this backup that came from it."""
    ids = {c.get("card_key"): c.get("id") for c in cards}
    source = db.connect(path, read_only=True)
    try:
        rows = source.execute("SELECT c.card_key, t.* FROM card_templates t "
                              "JOIN cards c ON c.id = t.card_id").fetchall()
    finally:
        source.close()
    return [{**{k: r[k] for k in r.keys() if k != "card_key"}, "card_id": ids[r["card_key"]]}
            for r in rows if r["card_key"] in ids]
