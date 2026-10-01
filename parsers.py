"""Read flashcard files and turn them into plain Python dictionaries.

My card files come in three formats:
  1. HTML pages with the cards stored as a JavaScript list:  const CARDS = [{q: ..., a: ...}, ...]
  2. HTML pages with the cards written out as page elements: <div class="card">...
  3. Markdown notes:  ### Q1 — Title / **Q:** ... / **A:** ...

Whatever the format, every card comes out in the same shape:
    {"ref": "Q12", "topic": "Duration", "question": "...", "answer": "...",
     "note": None, "has_visual": False}

parse_file() returns (cards, warnings), where warnings lists anything skipped.
"""
import json
import re
from pathlib import Path

import json5
from bs4 import BeautifulSoup

# The variable names my HTML files use for their list of cards.
CARD_LIST_START = re.compile(r"\b(?:CARDS|allCards|cards|defaultCards)\s*=\s*\[")
TAG_LABELS_START = re.compile(r"\bTAG_LABELS\s*=\s*\{")


def parse_file(path: Path):
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".md":
        return parse_markdown(text)
    if CARD_LIST_START.search(text):
        return parse_js_cards(text)
    return parse_html_elements(text)


# ---------------------------------------------------------------- format 1

def parse_js_cards(text):
    """Cards stored as a JavaScript list inside the HTML page."""
    match = CARD_LIST_START.search(text)
    js_list = _backticks_to_quotes(_extract_bracketed(text, match.end() - 1))
    # JavaScript object syntax is looser than JSON (unquoted keys, single quotes,
    # comments). The json5 library understands that looser syntax.
    items = json5.loads(js_list)

    # Some files map short tags ("def") to readable labels ("Definitions").
    tag_labels = {}
    labels_match = TAG_LABELS_START.search(text)
    if labels_match:
        tag_labels = json5.loads(_extract_bracketed(text, labels_match.end() - 1))

    cards, warnings = [], []
    for i, item in enumerate(items, start=1):
        card = _card_from_js_item(item, tag_labels)
        if card["question"] and card["answer"]:
            cards.append(card)
        else:
            warnings.append(f"item {i}: missing question or answer, skipped")
    return cards, warnings


def _card_from_js_item(item, tag_labels):
    if "front" in item:  # e.g. {"q": "Q1", "front": question, "back": answer}
        question, answer, ref = item.get("front"), item.get("back"), item.get("q")
    else:                # e.g. {id: 1, q: question, a: answer}
        question, answer, ref = item.get("q"), item.get("a"), None

    ref = ref or _first(item, "id", "num", "n")
    tag = item.get("tag")
    # In the M&A set the "tag" is the card number ("Q2a"), not a topic.
    if ref is None and tag and re.fullmatch(r"Q\d+\w*", str(tag)):
        ref, tag = tag, None

    topic = _first(item, "catLabel", "topic", "section", "block", "cat")
    if topic is None and tag:
        topic = tag_labels.get(tag, tag)
    if topic is None and "ch" in item:
        topic = f"Chapter {item['ch']}"

    return {
        "ref": str(ref) if ref is not None else None,
        "topic": topic,
        "question": html_to_text(question or ""),
        "answer": html_to_text(answer or ""),
        "note": html_to_text(item["insight"]) if item.get("insight") else None,
        "has_visual": bool(item.get("visual")),
    }


def _first(item, *keys):
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


def _extract_bracketed(text, start):
    """Return text[start:end] where text[start] is '[' or '{' and end is its partner.

    Brackets inside strings or comments are ignored, so an answer containing "]"
    can't end the list early.
    """
    depth, i, n = 0, start, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            i = _skip_string(text, i)
            continue
        if text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if text.startswith("/*", i):
            i = text.find("*/", i) + 2
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise ValueError("Card list never closes - is the file complete?")


def _backticks_to_quotes(js):
    """Rewrite `backtick strings` as "double-quoted" ones, which json5 can read.

    JavaScript allows three quote styles; json5 only understands two.
    """
    out, i = [], 0
    while i < len(js):
        ch = js[i]
        if js.startswith("//", i) or js.startswith("/*", i):  # copy comments untouched
            end = js.find("\n", i) if js[i + 1] == "/" else js.find("*/", i) + 2
            end = len(js) if end <= 0 else end
            out.append(js[i:end])
            i = end
            continue
        if ch in "\"'`":
            end = _skip_string(js, i)
            if ch == "`":
                content = js[i + 1:end - 1]
                if "${" in content:
                    raise ValueError("Card text uses ${...} placeholders, which can't be imported")
                out.append(json.dumps(content.replace("\\`", "`"), ensure_ascii=False))
            else:
                out.append(js[i:end])
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _skip_string(text, i):
    quote, i = text[i], i + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2  # skip escaped character, e.g. \' or \"
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    raise ValueError("Unclosed string in card list")


# ---------------------------------------------------------------- format 2

def parse_html_elements(text):
    """Cards written as <div class="card"><div class="front">…</div><div class="back">…"""
    soup = BeautifulSoup(text, "html.parser")
    cards, warnings, topic = [], [], None
    # Walk section headings and cards in page order, so each card
    # picks up the most recent heading as its topic.
    for el in soup.select(".section-label, .card"):
        if "section-label" in el.get("class", []):
            topic = el.get_text(strip=True)
            continue
        front, back = el.select_one(".front"), el.select_one(".back")
        if not front or not back:
            warnings.append("card without front/back, skipped")
            continue
        number = front.select_one(".qnum")
        ref = number.get_text(strip=True) if number else None
        if number:
            number.decompose()  # remove "Q1" so it isn't part of the question text
        cards.append({
            "ref": ref,
            "topic": topic,
            "question": html_to_text(front.decode_contents()),
            "answer": html_to_text(back.decode_contents()),
            "note": None,
            "has_visual": False,
        })
    return cards, warnings


# ---------------------------------------------------------------- format 3

def parse_markdown(text):
    """### Q1 — Title / **Q:** question / **A:** answer, grouped under ## CHAPTER headings."""
    cards, warnings = [], []
    topic = None
    for block in re.split(r"^(?=#{2,3} )", text, flags=re.MULTILINE):
        heading, _, body = block.partition("\n")
        if heading.startswith("## "):
            topic = heading[3:].strip().replace("CHAPTER", "Chapter")
            continue
        if not heading.startswith("### "):
            continue
        ref = re.split(r"\s+[—-]\s+", heading[4:].strip(), maxsplit=1)[0]
        body = re.sub(r"^---\s*$", "", body, flags=re.MULTILINE)
        qa = re.search(r"\*\*Q:\*\*(.*?)\*\*A:\*\*(.*)", body, flags=re.DOTALL)
        if not qa:
            warnings.append(f"{ref}: no **Q:**/**A:** pair, skipped")
            continue
        cards.append({
            "ref": ref,
            "topic": topic,
            "question": _tidy(qa.group(1).replace("**", "")),
            "answer": _tidy(qa.group(2).replace("**", "")),
            "note": None,
            "has_visual": False,
        })
    return cards, warnings


# ---------------------------------------------------------------- helpers

def html_to_text(html):
    """Strip HTML formatting (<strong>, <br>, lists) but keep line breaks readable."""
    if "<" not in html and "&" not in html:
        return _tidy(html)
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n• ")
    for block in soup.find_all(["p", "div", "ul", "ol"]):  # blocks sit on their own lines
        block.insert_before("\n")
        block.insert_after("\n")
    return _tidy(soup.get_text())


def _tidy(text):
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---------------------------------------------------------------- review flags

# Phrases suggesting the card only makes sense right after the card before it.
DEPENDS_ON_PREVIOUS = re.compile(
    r"\b(this same|from the same|the same (bond|company|firm|deal|comp set|data ?set|"
    r"scenario|project|portfolio|example)|previous (question|card))\b",
    re.IGNORECASE,
)
# Phrases suggesting the original page showed a chart the text doesn't include.
MENTIONS_VISUAL = re.compile(
    r"\b(shown (below|above)|the (chart|graph|figure|diagram|table) (below|above))\b",
    re.IGNORECASE,
)


def review_flag(card):
    """Return a reason this card may need a manual check, or None."""
    reasons = []
    # A merged multi-part card already contains the part it depends on.
    if not card.get("merged") and DEPENDS_ON_PREVIOUS.search(card["question"]):
        reasons.append("may depend on the previous card")
    if card["has_visual"] or MENTIONS_VISUAL.search(card["question"]):
        reasons.append("original showed a chart/picture")
    return "; ".join(reasons) or None
