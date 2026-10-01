"""Import many cards at once from a spreadsheet (CSV) or an Anki text export.

Accepted layouts:
  - A CSV with a header row naming the columns: "question" and "answer" (required),
    "deck" and "topic" (optional). Other columns are ignored. Common synonyms work
    too: front/back, term/definition, prompt/response.
  - No header row: column 1 is the question, column 2 the answer, column 3 (if any)
    the topic.
  - Anki: File > Export > "Notes in Plain Text". That's tab-separated, may start with
    "#separator:tab"-style lines, and may contain HTML (bold, line breaks); the HTML
    is turned into plain text.
Commas, semicolons and tabs are all recognised as separators. Rows without a deck
column go into the deck chosen on the page.

Like the progress restore, the file is treated as untrusted: it's size-capped (by
the app's upload limit), row-capped, each card goes through the same checks as one
typed in by hand, and everything is saved in one transaction.
"""
import csv
import io

from bs4 import BeautifulSoup

from card_writer import CardError, add_card

MAX_ROWS = 2000
HEADINGS = {
    "question": {"question", "front", "term", "prompt", "q"},
    "answer": {"answer", "back", "definition", "response", "a"},
    "deck": {"deck", "set", "subject"},
    "topic": {"topic", "tag", "tags", "section", "chapter", "category"},
}


class CardImportError(Exception):
    """The whole file can't be used, with the reason in plain English."""


def _text(raw):
    """Decode the upload: UTF-8 (with or without Excel's marker), else Windows' encoding."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")  # never fails


def _plain(value):
    """Anki fields can hold HTML; keep the words and line breaks, drop the tags."""
    if "<" in value and ">" in value:
        soup = BeautifulSoup(value, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        value = soup.get_text()
    return value.replace("\xa0", " ").strip()


def _rows(text):
    lines = [line for line in text.splitlines() if not line.startswith("#")]  # Anki headers
    body = "\n".join(lines)
    if not body.strip():
        raise CardImportError("That file is empty.")
    try:
        dialect = csv.Sniffer().sniff(body[:5000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in lines[0] else csv.excel
    return [row for row in csv.reader(io.StringIO(body), dialect) if any(cell.strip() for cell in row)]


def _columns(first_row):
    """Column positions from a header row, or None if the first row is a card."""
    found = {}
    for i, cell in enumerate(first_row):
        name = cell.strip().lower()
        for field, names in HEADINGS.items():
            if name in names and field not in found:
                found[field] = i
    if "question" in found and "answer" in found:
        return found
    return None


def import_cards(conn, raw, default_deck):
    """Add the cards in `raw` (bytes). Returns (added, skipped) where skipped is a list
    of "row N: reason" strings. Raises CardImportError if the file can't be used at all."""
    rows = _rows(_text(raw))
    columns = _columns(rows[0])
    has_header = columns is not None
    if has_header:
        rows = rows[1:]
    else:
        if len(rows[0]) < 2:
            raise CardImportError("Couldn't find two columns (question and answer). Check the file "
                               "is a CSV, or add a header row with 'question' and 'answer'.")
        columns = {"question": 0, "answer": 1, "topic": 2}
    if not rows:
        raise CardImportError("That file has a header row but no cards under it.")
    if len(rows) > MAX_ROWS:
        raise CardImportError(f"That file has {len(rows):,} cards; import at most {MAX_ROWS:,} at a time.")

    def cell(row, field):
        i = columns.get(field)
        return _plain(row[i]) if i is not None and i < len(row) else ""

    added, skipped = 0, []
    offset = 2 if has_header else 1  # spreadsheet row numbers, as the user sees them
    try:
        for n, row in enumerate(rows, start=offset):
            deck = cell(row, "deck") or default_deck
            try:
                add_card(conn, deck, cell(row, "question"), cell(row, "answer"),
                         cell(row, "topic")[:80] or None, commit=False)
                added += 1
            except CardError as err:
                skipped.append(f"row {n}: {err}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return added, skipped
