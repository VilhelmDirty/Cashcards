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
    graded_by   TEXT NOT NULL      -- 'self' now; 'claude' from Stage 3
);
"""


def connect():
    """Open the database, creating the file and tables on first use."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets code read columns by name: row["question"]
    conn.execute("PRAGMA foreign_keys = ON")  # refuse reviews that point at a missing card
    # We keep SQLite's default "rollback journal" mode rather than WAL mode.
    # WAL keeps extra side files open, which sync tools like OneDrive handle badly.
    conn.executescript(SCHEMA)
    return conn
