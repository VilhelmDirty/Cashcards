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
"""


def connect():
    """Open the database, creating the file and tables on first use."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets code read columns by name: row["question"]
    # We keep SQLite's default "rollback journal" mode rather than WAL mode.
    # WAL keeps extra side files open, which sync tools like OneDrive handle badly.
    conn.executescript(SCHEMA)
    return conn
