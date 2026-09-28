"""Spaced repetition: which card comes next, and what happens when I rate one.

All of the scheduling maths is done by the FSRS library (py-fsrs). This module
only saves and loads FSRS's per-card state in SQLite and decides queue order:
  1. cards whose review time has arrived, most overdue first;
  2. then never-seen cards, up to a daily limit, in random order
     (mixing topics tends to help memory more than studying one block at a time).
"""
from datetime import datetime, timedelta, timezone

import fsrs

import config

scheduler = fsrs.Scheduler(desired_retention=config.DESIRED_RETENTION)
# FSRS nudges long intervals by a random few percent ("fuzz") so cards learned
# together don't all come due on the same day. Good for real scheduling, but it
# makes button previews jumpy (Hard could show longer than Good), so previews
# use an identical scheduler with the randomness switched off.
_preview_scheduler = fsrs.Scheduler(desired_retention=config.DESIRED_RETENTION,
                                    enable_fuzzing=False)

RATINGS = [(r.value, r.name) for r in fsrs.Rating]  # [(1, "Again"), (2, "Hard"), ...]


# ---------------------------------------------------------------- time helpers
# All times are stored in UTC with a fixed format, so text comparison
# ("2026-09-28T10:00:00+00:00" <= "...") gives the right order.

def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _start_of_today():
    """Local midnight (my time zone), expressed in UTC."""
    local_now = datetime.now().astimezone()
    return _iso(local_now.replace(hour=0, minute=0, second=0, microsecond=0))


def _deck_filter(deck):
    return ("AND c.deck = ?", [deck]) if deck else ("", [])


def format_interval(delta):
    """timedelta -> short human text: '10 min', '5 h', '3 d', '2 mo', '1.5 y'."""
    minutes = delta.total_seconds() / 60
    if minutes < 60:
        return f"{max(1, round(minutes))} min"
    if minutes < 60 * 24:
        return f"{round(minutes / 60)} h"
    days = minutes / (60 * 24)
    if days < 30:
        return f"{round(days)} d"
    if days < 365:
        return f"{round(days / 30)} mo"
    return f"{days / 365:.1f} y"


# ---------------------------------------------------------------- loading state

def _load_fsrs_card(conn, card_id):
    """Return (fsrs.Card, version). A never-reviewed card gets a fresh FSRS card."""
    row = conn.execute("SELECT fsrs_card, due FROM review_state WHERE card_id = ?",
                       (card_id,)).fetchone()
    if row is None:
        return fsrs.Card(card_id=card_id), "new"
    return fsrs.Card.from_json(row["fsrs_card"]), row["due"]


# ---------------------------------------------------------------- the queue

def new_cards_left_today(conn):
    introduced = conn.execute(
        "SELECT COUNT(*) FROM review_log WHERE was_new = 1 AND reviewed_at >= ?",
        (_start_of_today(),),
    ).fetchone()[0]
    return max(0, config.NEW_CARDS_PER_DAY - introduced)


def next_card(conn, deck=None):
    """The card to show next, or None if there's nothing to study right now."""
    where, params = _deck_filter(deck)
    card = conn.execute(
        f"SELECT c.* FROM cards c JOIN review_state r ON r.card_id = c.id "
        f"WHERE r.due <= ? {where} ORDER BY r.due LIMIT 1",
        [_iso(_now()), *params],
    ).fetchone()
    if card is None and new_cards_left_today(conn) > 0:
        card = conn.execute(
            f"SELECT c.* FROM cards c LEFT JOIN review_state r ON r.card_id = c.id "
            f"WHERE r.card_id IS NULL {where} ORDER BY RANDOM() LIMIT 1",
            params,
        ).fetchone()
    return card


def queue_counts(conn, deck=None):
    """How many cards are waiting: due reviews, and new cards still allowed today."""
    where, params = _deck_filter(deck)
    due = conn.execute(
        f"SELECT COUNT(*) FROM cards c JOIN review_state r ON r.card_id = c.id "
        f"WHERE r.due <= ? {where}",
        [_iso(_now()), *params],
    ).fetchone()[0]
    unseen = conn.execute(
        f"SELECT COUNT(*) FROM cards c LEFT JOIN review_state r ON r.card_id = c.id "
        f"WHERE r.card_id IS NULL {where}",
        params,
    ).fetchone()[0]
    return {"due": due, "new": min(unseen, new_cards_left_today(conn))}


def next_due(conn, deck=None):
    """When the next card comes due (datetime, UTC), or None if nothing is scheduled."""
    where, params = _deck_filter(deck)
    row = conn.execute(
        f"SELECT MIN(r.due) FROM cards c JOIN review_state r ON r.card_id = c.id "
        f"WHERE 1 = 1 {where}",
        params,
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row[0] else None


# ---------------------------------------------------------------- reviewing

def preview(conn, card_id):
    """FSRS's suggested next review for each grade, e.g. {1: 'in 1 min', 4: 'Thu 2 Oct (in 7 d)'}.

    Runs FSRS on the card without saving anything. The real review adds a
    little fuzz, so the saved interval can differ from the preview by a few percent.
    """
    card, _ = _load_fsrs_card(conn, card_id)
    now = _now()
    result = {}
    for value, _name in RATINGS:
        updated, _log = _preview_scheduler.review_card(card, fsrs.Rating(value),
                                                       review_datetime=now)
        result[value] = describe_due(updated.due, now)
    return result


def describe_due(due, now):
    """'in 10 min' for same-session steps, otherwise 'Thu 2 Oct (in 3 d)'."""
    delta = due - now
    if delta < timedelta(hours=12):
        return f"in {format_interval(delta)}"
    local = due.astimezone()
    return f"{local:%a} {local.day} {local:%b} (in {format_interval(delta)})"


def local_date_to_due(day):
    """A calendar date I picked -> the moment it becomes due: local midnight, in UTC."""
    return datetime(day.year, day.month, day.day).astimezone().astimezone(timezone.utc)


def version(conn, card_id):
    """A marker of the card's current schedule, sent with the rating form."""
    return _load_fsrs_card(conn, card_id)[1]


def record_review(conn, card_id, rating, expected_version, graded_by="self",
                  duration_ms=None, timed_out=False, feedback_id=None, retention=None,
                  due_override=None):
    """Apply a rating (1-4) via FSRS and save the result.

    expected_version guards against double-submits: if the card's schedule has
    changed since the page was shown (e.g. a double-click already rated it),
    the second rating is ignored. Returns True if the review was saved.

    duration_ms is my thinking time. FSRS records it but schedules on the rating
    alone, so time only changes the schedule where it's built into the rating
    (multiple choice, later).
    """
    card, current_version = _load_fsrs_card(conn, card_id)
    if current_version != expected_version:
        return False

    now = _now()
    card, _log = scheduler.review_card(card, fsrs.Rating(rating), review_datetime=now,
                                       review_duration=duration_ms)
    # My own date wins over FSRS's suggestion. FSRS still learns from the rating,
    # and at the next review it uses the real time elapsed, so the model stays honest.
    if due_override is not None:
        card.due = due_override

    conn.execute(
        "INSERT INTO review_state (card_id, fsrs_card, due, state) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(card_id) DO UPDATE SET "
        "fsrs_card = excluded.fsrs_card, due = excluded.due, state = excluded.state",
        (card_id, card.to_json(), _iso(card.due), card.state.name),
    )
    conn.execute(
        "INSERT INTO review_log (card_id, rating, reviewed_at, was_new, graded_by, "
        "duration_ms, timed_out, feedback_id, retention, custom_due) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (card_id, rating, _iso(now), current_version == "new", graded_by,
         duration_ms, timed_out, feedback_id, retention,
         _iso(due_override) if due_override else None),
    )
    conn.commit()
    return True


# ---------------------------------------------------------------- reminders

# A deck counts for reminders unless I've switched it off (no row = on).
REMINDED = "COALESCE((SELECT s.remind FROM deck_settings s WHERE s.deck = c.deck), 1) = 1"


def reminder_counts(conn):
    """Cards waiting in the decks I want reminders for.

    Returns {"due": n, "new": n, "by_deck": [(deck, due), ...]} with the
    busiest decks first. New cards share the one daily allowance.
    """
    now = _iso(_now())
    by_deck = conn.execute(
        f"SELECT c.deck, COUNT(*) FROM cards c JOIN review_state r ON r.card_id = c.id "
        f"WHERE r.due <= ? AND {REMINDED} GROUP BY c.deck ORDER BY COUNT(*) DESC, c.deck",
        (now,),
    ).fetchall()
    unseen = conn.execute(
        f"SELECT COUNT(*) FROM cards c LEFT JOIN review_state r ON r.card_id = c.id "
        f"WHERE r.card_id IS NULL AND {REMINDED}"
    ).fetchone()[0]
    return {
        "due": sum(n for _, n in by_deck),
        "new": min(unseen, new_cards_left_today(conn)),
        "by_deck": [(deck, n) for deck, n in by_deck],
    }


def upcoming(conn, study_days, days=14):
    """Day-by-day reminder outlook for the next `days` days, from FSRS's due dates.

    Each day: {"date", "due", "study_day", "reminder"}. Cards due on a day I don't
    study roll forward to my next study day, because that's when I'll see them.
    Overdue cards count towards today. "reminder" is True when a reminder will fire
    that day (a study day with cards due, or with new cards left to learn).
    """
    today = datetime.now().astimezone().date()
    per_day = {}
    for (due,) in conn.execute(
        f"SELECT r.due FROM cards c JOIN review_state r ON r.card_id = c.id WHERE {REMINDED}"
    ):
        day = max(datetime.fromisoformat(due).astimezone().date(), today)
        per_day[day] = per_day.get(day, 0) + 1
    unseen = conn.execute(
        f"SELECT COUNT(*) FROM cards c LEFT JOIN review_state r ON r.card_id = c.id "
        f"WHERE r.card_id IS NULL AND {REMINDED}"
    ).fetchone()[0]

    outlook, carried = [], 0
    for offset in range(days):
        day = today + timedelta(days=offset)
        due = per_day.get(day, 0) + carried
        study = day.weekday() in study_days
        carried = 0 if study else due
        outlook.append({"date": day, "due": due if study else per_day.get(day, 0),
                        "study_day": study, "reminder": study and (due > 0 or unseen > 0)})
    return outlook, unseen


def warm_up_question(conn):
    """One question from a reminded deck, for the reminder email: the most
    overdue card, or a random new card if nothing is due. None if neither."""
    row = conn.execute(
        f"SELECT c.deck, c.question FROM cards c JOIN review_state r ON r.card_id = c.id "
        f"WHERE r.due <= ? AND {REMINDED} ORDER BY r.due LIMIT 1",
        (_iso(_now()),),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"SELECT c.deck, c.question FROM cards c LEFT JOIN review_state r ON r.card_id = c.id "
            f"WHERE r.card_id IS NULL AND {REMINDED} ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    return (row["deck"], row["question"]) if row else None


def set_reminded_decks(conn, chosen):
    """Switch reminders on for the decks in `chosen` and off for every other deck."""
    for (deck,) in conn.execute("SELECT DISTINCT deck FROM cards").fetchall():
        conn.execute(
            "INSERT INTO deck_settings (deck, remind) VALUES (?, ?) "
            "ON CONFLICT(deck) DO UPDATE SET remind = excluded.remind",
            (deck, int(deck in chosen)),
        )
    conn.commit()


# ---------------------------------------------------------------- dashboard

def deck_summary(conn):
    """Per deck: total, unseen, due now, reminders on/off, average thinking time (s)."""
    return conn.execute(
        "SELECT c.deck AS deck, COUNT(*) AS total, "
        "       SUM(r.card_id IS NULL) AS unseen, "
        "       SUM(r.due <= ?) AS due, "
        f"      MAX({REMINDED}) AS remind, "
        "       (SELECT AVG(l.duration_ms) / 1000.0 FROM review_log l "
        "        JOIN cards c2 ON c2.id = l.card_id "
        "        WHERE c2.deck = c.deck AND l.duration_ms IS NOT NULL) AS avg_seconds "
        "FROM cards c LEFT JOIN review_state r ON r.card_id = c.id "
        "GROUP BY c.deck ORDER BY c.deck",
        (_iso(_now()),),
    ).fetchall()


def reviewed_today(conn):
    return conn.execute("SELECT COUNT(*) FROM review_log WHERE reviewed_at >= ?",
                        (_start_of_today(),)).fetchone()[0]
