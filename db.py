"""Opening the SQLite database and creating its tables."""
import sqlite3

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
    retention   INTEGER            -- slider position 0 (forgotten) - 100 (retained)
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
]


def connect():
    """Open the database, creating the file and tables on first use."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
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
}


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else SETTING_DEFAULTS[key]


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
