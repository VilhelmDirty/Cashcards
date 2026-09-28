"""Adding new cards: ones I write myself, and ones Claude drafts for me to approve.

Claude only ever *drafts*. Drafts wait in the card_drafts table until I edit,
approve or discard them on the Suggest page; nothing reaches my decks unapproved.
"""
import random
from datetime import datetime, timezone

from pydantic import BaseModel

import grader
from import_cards import card_key

MAX_QUESTION = 2000
MAX_ANSWER = 5000
EXAMPLES_SENT = 40  # how many existing cards Claude sees, to match style and avoid repeats

DRAFT_PROMPT = """\
You write flashcards for a finance graduate student preparing for job interviews. \
They practise by explaining each answer out loud in their own words.

You will get the cards already in one of their decks. Write {count} NEW cards for \
the same deck that take the material one step further: the "why" behind a rule, \
how two concepts connect, a short worked example with numbers, a common mistake, \
or the follow-up an interviewer would ask next.

Rules:
- Don't repeat or lightly reword an existing card.
- Every card must stand alone: never refer to another card, "the example above", \
"this same bond", and so on. Include any numbers the question needs.
- Answers: accurate, 1-4 sentences (a short worked calculation may be longer), \
plain text, no markdown. Only write cards you are confident are correct.
- topic: a short label; reuse one of the deck's existing topics when it fits.\
"""


class DraftCard(BaseModel):
    topic: str
    question: str
    answer: str


class DraftSet(BaseModel):
    cards: list[DraftCard]


class CardError(Exception):
    """A problem with a card I'm trying to save, in plain English."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_card(conn, deck, question, answer, topic=None, origin="manual"):
    """Save one new card. Returns its id. Raises CardError if it can't be saved."""
    deck, question, answer = deck.strip(), question.strip(), answer.strip()
    topic = (topic or "").strip() or None
    if not deck or len(deck) > 80:
        raise CardError("Give the deck a name (up to 80 characters).")
    if not question or not answer:
        raise CardError("A card needs both a question and an answer.")
    if len(question) > MAX_QUESTION or len(answer) > MAX_ANSWER:
        raise CardError("That card is very long. Keep questions under 2,000 characters "
                        "and answers under 5,000.")
    key = card_key(deck, question)  # same fingerprint the importer uses
    if conn.execute("SELECT 1 FROM cards WHERE card_key = ?", (key,)).fetchone():
        raise CardError(f"'{deck}' already has a card with that question.")
    cursor = conn.execute(
        "INSERT INTO cards (deck, source_ref, topic, question, answer, card_key, imported_at, origin) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?, ?)",
        (deck, topic, question, answer, key, _now(), origin),
    )
    conn.commit()
    return cursor.lastrowid


def deck_names(conn):
    return [row[0] for row in conn.execute("SELECT DISTINCT deck FROM cards ORDER BY deck")]


def draft_cards(conn, deck, count=5):
    """Ask Claude for `count` new cards for `deck`; save them as drafts. Returns how many."""
    existing = conn.execute("SELECT topic, question, answer FROM cards WHERE deck = ?",
                            (deck,)).fetchall()
    if not existing:
        raise CardError(f"There's no deck called '{deck}'.")
    shown = existing if len(existing) <= EXAMPLES_SENT else random.sample(existing, EXAMPLES_SENT)
    examples = "\n\n".join(
        f"[{c['topic'] or 'General'}]\nQ: {c['question'][:400]}\nA: {c['answer'][:500]}"
        for c in shown
    )
    prompt = (f"<deck name=\"{deck}\" total_cards=\"{len(existing)}\">\n{examples}\n</deck>\n\n"
              f"Write {count} new cards for this deck.")

    response = grader.call_claude(system=DRAFT_PROMPT.format(count=count), prompt=prompt,
                                  output_format=DraftSet, max_tokens=4000)
    grader.record_usage(conn, "draft_cards", response.usage.input_tokens,
                        response.usage.output_tokens)

    drafts = [d for d in response.parsed_output.cards if d.question.strip() and d.answer.strip()]
    for d in drafts[:count]:
        conn.execute("INSERT INTO card_drafts (deck, topic, question, answer, created_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (deck, d.topic.strip()[:80], d.question.strip(), d.answer.strip(), _now()))
    conn.commit()
    return min(len(drafts), count)


def pending_drafts(conn, deck):
    return conn.execute("SELECT * FROM card_drafts WHERE deck = ? ORDER BY id", (deck,)).fetchall()


def discard_drafts(conn, deck):
    conn.execute("DELETE FROM card_drafts WHERE deck = ?", (deck,))
    conn.commit()
