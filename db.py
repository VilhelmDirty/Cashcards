"""Opening the SQLite database and creating its tables."""
import sqlite3

import config
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY,
    deck        TEXT NOT NULL,          -- e.g. "WSP: DCF"
    source_ref  TEXT,                   -- the card's number in the original file, e.g. "Q12"
    topic       TEXT,                   -- section/chapter label from the original file
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,          -- the reference answer, as plain text
    note        TEXT,                   -- extra context some sets include (e.g. "insight")
    flag        TEXT,                   -- why this card may need a manual check, or NULL
    card_key    TEXT NOT NULL UNIQUE,   -- fingerprint of deck + question; stops duplicates
    imported_at TEXT NOT NULL,
    updated_at  TEXT
);

-- Where each card stands in its review schedule. A card with no row here is "new".
CREATE TABLE IF NOT EXISTS review_state (
    card_id   INTEGER PRIMARY KEY REFERENCES cards(id),
    fsrs_card TEXT NOT NULL,   -- the FSRS library's Card object, saved as JSON text
    due       TEXT NOT NULL,   -- copy of the due time (UTC) so the queue can sort by it
    state     TEXT NOT NULL    -- Learning / Review / Relearning
);
CREATE INDEX IF NOT EXISTS idx_review_state_due ON review_state(due);

-- One row per review, ever. Useful for stats, and FSRS can later be tuned on it.
CREATE TABLE IF NOT EXISTS review_log (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES cards(id),
    rating      INTEGER NOT NULL,  -- 1 Again, 2 Hard, 3 Good, 4 Easy
    reviewed_at TEXT NOT NULL,     -- UTC
    was_new     INTEGER NOT NULL,  -- 1 if this was the card's first-ever review
    graded_by   TEXT NOT NULL,     -- 'self' now; 'claude' from Stage 3
    duration_ms INTEGER,           -- thinking time: card shown -> answer revealed
    timed_out   INTEGER NOT NULL DEFAULT 0, -- 1 if the time limit ran out
    feedback_id INTEGER REFERENCES ai_feedback(id), -- the AI grade behind this rating, if any
    retention   INTEGER,           -- slider position 0 (forgotten) - 100 (retained)
    custom_due  TEXT               -- my own next-review date, if I overrode FSRS (UTC)
);

-- One row per study session: I click Study, and it runs for `target` questions.
CREATE TABLE IF NOT EXISTS study_sessions (
    id         INTEGER PRIMARY KEY,
    deck       TEXT,              -- NULL = all decks
    target     INTEGER NOT NULL,  -- smaller of my session size and the cards waiting
    started_at TEXT NOT NULL      -- UTC
);

-- Math cards turned into templates so they come up with fresh numbers (variants.py).
CREATE TABLE IF NOT EXISTS card_templates (
    card_id    INTEGER PRIMARY KEY REFERENCES cards(id),
    status     TEXT NOT NULL,   -- draft (passed the check) / failed / unsuitable / approved / off
    spec       TEXT NOT NULL,   -- variables, formulas, question and answer with blanks (JSON)
    notes      TEXT NOT NULL,   -- why the check failed, if it did (JSON list)
    updated_at TEXT NOT NULL
);

-- Per-deck choices. A deck with no row here uses the defaults.
CREATE TABLE IF NOT EXISTS deck_settings (
    deck   TEXT PRIMARY KEY,
    remind INTEGER NOT NULL DEFAULT 1   -- 1 = include this deck in reminders
);

-- App-wide choices made on the Settings page (e.g. where reminder emails go).
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- New cards Claude has drafted for a deck, waiting for me to edit/approve/discard.
CREATE TABLE IF NOT EXISTS card_drafts (
    id         INTEGER PRIMARY KEY,
    deck       TEXT NOT NULL,
    topic      TEXT,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Claude usage other than grading (grades keep their tokens in ai_feedback).
CREATE TABLE IF NOT EXISTS ai_usage (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    purpose       TEXT NOT NULL,     -- e.g. 'draft_cards'
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL
);

-- Claude's grade of a typed answer. Saved before I confirm the rating, so the
-- feedback is never lost (and never paid for twice) if the page is refreshed.
CREATE TABLE IF NOT EXISTS ai_feedback (
    id            INTEGER PRIMARY KEY,
    card_id       INTEGER NOT NULL REFERENCES cards(id),
    created_at    TEXT NOT NULL,     -- UTC
    user_answer   TEXT NOT NULL,
    score         INTEGER NOT NULL,  -- 0-100
    missed        TEXT NOT NULL,     -- JSON list: key points I left out
    wrong         TEXT NOT NULL,     -- JSON list: things I got wrong
    rewrite       TEXT NOT NULL,     -- my answer, tightened
    model         TEXT,              -- NULL when no API call was needed (blank answer)
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER,
    timed_out     INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added after the first release. A database created earlier won't have
# them, and CREATE TABLE IF NOT EXISTS won't change an existing table, so they're
# added here if missing. (Adding columns this way keeps all existing rows.)
MIGRATIONS = [
    ("review_log", "duration_ms", "INTEGER"),
    ("review_log", "timed_out", "INTEGER NOT NULL DEFAULT 0"),
    ("review_log", "feedback_id", "INTEGER REFERENCES ai_feedback(id)"),
    ("review_log", "retention", "INTEGER"),
    ("review_log", "custom_due", "TEXT"),
    ("cards", "origin", "TEXT NOT NULL DEFAULT 'import'"),  # 'import', 'manual' or 'claude'
    ("review_log", "session_id", "INTEGER REFERENCES study_sessions(id)"),
    ("review_log", "variant_seed", "INTEGER"),  # which fresh numbers I was shown, if any
    ("ai_feedback", "variant_seed", "INTEGER"),
]


def connect(path=None, read_only=False):
    """Open the database, creating the file and tables on first use.
    `path` and `read_only` are only used by the public demo, where each visitor has
    their own file and brand-new visitors see the shared sample deck read-only."""
    path = path or DB_PATH
    if read_only:  # SQLite refuses any write through this connection
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row  # lets code read columns by name: row["question"]
    conn.execute("PRAGMA foreign_keys = ON")  # refuse reviews that point at a missing card
    # We keep SQLite's default "rollback journal" mode rather than WAL mode.
    # WAL keeps extra side files open, which sync tools like OneDrive handle badly.
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


SETTING_DEFAULTS = {
    "desktop_notifications": "1",   # "1" on, "0" off
    "email_to": "",                 # where reminder emails go; empty = no emails
    "email_frequency": "daily",     # "off", "daily" (at most one a day), "every" (each reminder time)
    "last_email_date": "",          # local date of the last reminder email, for "daily"
    "reminder_times": ",".join(config.REMINDER_TIMES),  # "HH:MM,HH:MM" local time
    "study_days": "0,1,2,3,4,5,6",  # weekdays reminders may fire: 0 = Monday ... 6 = Sunday
    "timer_seconds": "0",           # optional countdown per card; "0" = no timer
    "session_size": "25",           # questions per study session (see config.SESSION_SIZES)
}


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else SETTING_DEFAULTS[key]


def reminder_times(conn):
    return [t for t in get_setting(conn, "reminder_times").split(",") if t]


def study_days(conn):
    """Weekday numbers (0 = Monday) on which reminders may fire."""
    return {int(d) for d in get_setting(conn, "study_days").split(",") if d != ""}


def set_setting(conn, key, value):
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def _migrate(conn):
    for table, column, definition in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()
