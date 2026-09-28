"""The reminder email: what it says and how it looks.

Subject lines and openers rotate (random.choice) so the emails don't go stale,
and the nudge reacts to what I actually did yesterday. The email is sent as
HTML (templates/email.html, with inline styles because email apps ignore most
CSS) plus a plain-text version for apps that don't show HTML.
"""
import random
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import config

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),  # card text is escaped, never treated as HTML
)

SUBJECTS_MANY = [  # 10+ reviews due
    "Margin call: {due} cards due",
    "Your {due} cards are accruing interest",
    "Your memory's been marked to market. {due} cards due.",
    "{due} cards, zero excuses",
]
SUBJECTS_FEW = [   # 1-9 reviews due
    "Just {due} card{s}. Smaller than your coffee order.",
    "{due} quick card{s} before the market closes",
    "A light session today: {due} card{s} due",
]
SUBJECTS_NEW = [   # nothing due, but new cards to learn
    "Fresh issuance: {new} new cards",
    "New listings: {new} cards just hit the market",
    "IPO day: {new} new cards",
]
OPENERS_DUE = [
    "Compounding works both ways. Review now and it grows; skip it and, well, you know.",
    "Somewhere out there an interviewer is polishing a DCF question. Be ready.",
    "Knowledge depreciates if you let it. Let's not let it.",
    "Small, regular deposits. That's the whole trick.",
]
OPENERS_NEW = [
    "No reviews due, just fresh material waiting. First-mover advantage is yours.",
    "Your review pile is clear, so it's a good day to take on some new positions.",
]


def plural(n):
    return "" if n == 1 else "s"


def subject(counts, test=False):
    due, new = counts["due"], counts["new"]
    if due >= 10:
        line = random.choice(SUBJECTS_MANY)
    elif due:
        line = random.choice(SUBJECTS_FEW)
    elif new:
        line = random.choice(SUBJECTS_NEW)
    else:
        return f"{config.APP_NAME}: test email. The pipes work."
    text = line.format(due=due, new=new, s=plural(due))
    return f"{text} (test)" if test else text


def nudge(history):
    """One line reacting to yesterday, or None for a brand-new user."""
    if not history["ever"]:
        return None
    if history["streak"] >= 3:
        return f"You're on a {history['streak']}-day streak. Don't break the chain now."
    if history["yesterday"]:
        n = history["yesterday"]
        return f"Yesterday you cleared {n} card{plural(n)}. Keep the returns compounding."
    return "Yesterday's volume: 0 cards. Bold strategy. Let's see if it pays off."


def build(counts, warm_up, history, test=False):
    """Returns (subject, plain_text, html) for one reminder email."""
    opener = random.choice(OPENERS_DUE if counts["due"] else OPENERS_NEW)
    context = {
        "app_name": config.APP_NAME,
        "opener": opener,
        "nudge": nudge(history),
        "counts": counts,
        "warm_up": warm_up,
    }

    lines = [opener, ""]
    if context["nudge"]:
        lines += [context["nudge"], ""]
    lines.append("TODAY'S STATEMENT")
    lines.append(f"  Reviews due:   {counts['due']}")
    lines += [f"    {deck}: {n}" for deck, n in counts["by_deck"]]
    lines.append(f"  New cards:     {counts['new']}")
    lines.append("")
    if warm_up:
        deck, question = warm_up
        lines += [f"POP QUIZ ({deck}). Answer it in your head, no peeking:", "", question, ""]
    lines += [f"Open {config.APP_NAME} on your laptop to check yourself.", "",
              f"- {config.APP_NAME}, your most persistent study buddy", "",
              "Too many emails? Change how often on the Settings page. "
              "We won't take it personally. (We will a little.)"]

    html = _templates.get_template("email.html").render(**context)
    return subject(counts, test), "\n".join(lines), html
