"""Stage 1: import flashcards from my HTML/Markdown files into SQLite.

Run it with:
    .\\.venv\\Scripts\\python import_cards.py              (import + summary)
    .\\.venv\\Scripts\\python import_cards.py --flagged    (also list cards needing a check)

Safe to run again at any time: a card already in the database is updated
if its answer changed in the source file, and never duplicated.
"""
import hashlib
import re
import sys
from datetime import datetime, timezone

import config
import db
from parsers import parse_file, review_flag


def card_key(deck, question):
    """A fingerprint of deck + question. Same card in, same key out, on every run."""
    normalised = re.sub(r"\s+", " ", question).strip().lower()
    return hashlib.sha1(f"{deck}\n{normalised}".encode("utf-8")).hexdigest()


def merge_parts(deck, cards, problems):
    """Replace each group in config.MERGE_GROUPS with one card: a) ... b) ... c) ...

    Returns (cards, parts) where parts are the original cards that were merged,
    so their old separate copies can be removed from the database.
    """
    by_ref = {}
    for card in cards:
        by_ref.setdefault(card["ref"], []).append(card)

    replacements, dropped, all_parts = {}, set(), []
    for refs in config.MERGE_GROUPS.get(deck, []):
        matches = [by_ref.get(ref, []) for ref in refs]
        if any(len(m) != 1 for m in matches):
            problems.append(f"{deck}: can't merge {' / '.join(refs)} "
                            "(a card number is missing or used twice)")
            continue
        parts = [m[0] for m in matches]
        label = lambda i, text: f"{'abcdefghij'[i]}) {text}"
        merged = {
            "ref": f"{parts[0]['ref']}–{parts[-1]['ref']}",
            "topic": parts[0]["topic"],
            "question": "\n\n".join(label(i, p["question"]) for i, p in enumerate(parts)),
            "answer": "\n\n".join(label(i, p["answer"]) for i, p in enumerate(parts)),
            "note": "\n".join(p["note"] for p in parts if p["note"]) or None,
            "has_visual": any(p["has_visual"] for p in parts),
            "merged": True,
        }
        replacements[id(parts[0])] = merged      # merged card takes the first part's place
        dropped.update(id(p) for p in parts[1:])
        all_parts += parts

    merged_cards = [replacements.get(id(c), c) for c in cards if id(c) not in dropped]
    return merged_cards, all_parts


def remove_card(conn, deck, question):
    """Delete a card, and its review history, that a merged card has replaced."""
    row = conn.execute("SELECT id FROM cards WHERE card_key = ?",
                       (card_key(deck, question),)).fetchone()
    if row is None:
        return False
    for table in ("review_log", "review_state"):
        conn.execute(f"DELETE FROM {table} WHERE card_id = ?", (row["id"],))
    conn.execute("DELETE FROM cards WHERE id = ?", (row["id"],))
    return True


def save_card(conn, deck, card, now):
    """Insert a new card or update a changed one. Returns 'new', 'updated' or 'unchanged'."""
    key = card_key(deck, card["question"])
    fields = {
        "source_ref": card["ref"],
        "topic": card["topic"],
        "answer": card["answer"],
        "note": card["note"],
        "flag": review_flag(card),
    }
    existing = conn.execute(
        "SELECT source_ref, topic, answer, note, flag FROM cards WHERE card_key = ?", (key,)
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO cards (deck, question, card_key, imported_at, "
            "source_ref, topic, answer, note, flag) "
            "VALUES (:deck, :question, :card_key, :now, "
            ":source_ref, :topic, :answer, :note, :flag)",
            {**fields, "deck": deck, "question": card["question"], "card_key": key, "now": now},
        )
        return "new"
    if dict(existing) != fields:
        conn.execute(
            "UPDATE cards SET source_ref = :source_ref, topic = :topic, answer = :answer, "
            "note = :note, flag = :flag, updated_at = :now WHERE card_key = :card_key",
            {**fields, "now": now, "card_key": key},
        )
        return "updated"
    return "unchanged"


def main():
    show_flagged = "--flagged" in sys.argv
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db.connect()

    print(f"Reading cards from: {config.CARDS_DIR}")
    print(f"Saving to:          {config.DB_PATH}\n")
    header = f"{'Deck':<38}{'Found':>6}{'New':>6}{'Upd':>6}{'Same':>6}{'Dupe':>6}{'Flag':>6}"
    print(header)
    print("-" * len(header))

    totals = dict(found=0, new=0, updated=0, unchanged=0, dupe=0, flag=0)
    problems, merged_notes = [], []

    for deck, relative_path in config.DECKS:
        path = config.CARDS_DIR / relative_path
        if not path.exists():
            problems.append(f"{deck}: file not found ({path})")
            continue
        try:
            cards, warnings = parse_file(path)
        except Exception as err:  # one bad file shouldn't stop the others
            problems.append(f"{deck}: could not read file ({err})")
            continue
        problems += [f"{deck}: {w}" for w in warnings]

        cards, parts = merge_parts(deck, cards, problems)
        removed = sum(remove_card(conn, deck, p["question"]) for p in parts)
        if removed:
            merged_notes.append(f"{deck}: replaced {removed} separate cards with "
                                f"{sum(1 for c in cards if c.get('merged'))} multi-part cards")

        counts = dict(found=len(cards), new=0, updated=0, unchanged=0, dupe=0, flag=0)
        seen = set()
        for card in cards:
            key = card_key(deck, card["question"])
            if key in seen:  # same question twice in one file
                counts["dupe"] += 1
                continue
            seen.add(key)
            counts[save_card(conn, deck, card, now)] += 1
            counts["flag"] += review_flag(card) is not None

        conn.commit()  # save each deck as soon as it's done
        for k in totals:
            totals[k] += counts[k]
        print(f"{deck:<38}{counts['found']:>6}{counts['new']:>6}{counts['updated']:>6}"
              f"{counts['unchanged']:>6}{counts['dupe']:>6}{counts['flag']:>6}")

    print("-" * len(header))
    print(f"{'TOTAL':<38}{totals['found']:>6}{totals['new']:>6}{totals['updated']:>6}"
          f"{totals['unchanged']:>6}{totals['dupe']:>6}{totals['flag']:>6}")
    in_db = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    print(f"\nCards now in the database: {in_db}")

    if merged_notes:
        print("\nMerged multi-part cards:")
        for note in merged_notes:
            print(f"  - {note}")

    if problems:
        print("\nSkipped / problems:")
        for p in problems:
            print(f"  - {p}")

    if show_flagged:
        print("\nCards flagged for a manual check:")
        for row in conn.execute(
            "SELECT deck, source_ref, question, flag FROM cards WHERE flag IS NOT NULL ORDER BY id"
        ):
            q = row["question"].replace("\n", " ")
            print(f"  [{row['deck']} {row['source_ref'] or ''}] {q[:90]}{'…' if len(q) > 90 else ''}")
            print(f"      -> {row['flag']}")
    elif totals["flag"]:
        print("\nRun with --flagged to list cards that may need a manual check.")

    conn.close()


if __name__ == "__main__":
    main()
