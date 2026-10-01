"""Stage 3: grade my own-words answer against the reference answer with Claude.

One API call per graded card. Claude returns a fixed JSON shape (enforced by the
API's "structured outputs" feature), so the app never has to guess at free text:
    score (0-100), missed points, wrong points, and a tightened rewrite.
"""
import json
import os
from datetime import datetime, timezone

import anthropic
from pydantic import BaseModel

import config

SYSTEM_PROMPT = """\
You grade answers for a finance student preparing for job interviews. They are \
practising explaining finance concepts in their own words, the way they would \
out loud to an interviewer.

You will get a flashcard question, the reference answer, and the student's answer.

Grade substance, not wording: an answer that says the same thing differently \
deserves full credit. Finance concepts often have several correct formulations \
(e.g. enterprise value = operating assets - operating liabilities = equity value + \
net debt); credit any correct alternative even if the reference uses a different \
one. Only list something under "wrong" if it is actually incorrect finance, not \
merely different from the reference; an imprecision (e.g. "debt" where "net debt" \
is meant) is a missed point, not an error. Don't penalise spelling, grammar or \
brevity unless meaning is lost. If the question has several parts (a, b, c...), \
grade all of them together.

score (0-100):
  90-100  every key point, correct, clear enough to say in an interview
  70-89   the core idea is right; a minor point missing or loosely put
  50-69   partly right; an important point missing or muddled
  0-49    mostly missing, wrong, or blank

missed: key points from the reference answer that the student left out. \
Short phrases. Empty list if nothing is missing.
wrong: statements in the student's answer that are incorrect, each with the \
correction. Empty list if nothing is wrong.
rewrite: the student's own answer, tightened into what they should say in an \
interview. Keep their wording and structure where it works; fix errors; add \
anything important they missed; cut filler. Plain text, no markdown, no longer \
than the reference answer needs. If their answer was blank or unrelated, write \
a concise model answer instead.\
"""


class Grade(BaseModel):
    score: int
    missed: list[str]
    wrong: list[str]
    rewrite: str


class GradingError(Exception):
    """A problem the user should see in plain English (no key, no internet...)."""


def api_key_configured():
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def score_to_rating(score):
    for threshold, rating in config.SCORE_TO_RATING:
        if score >= threshold:
            return rating
    return 1


def _client(api_key=None):
    # Without api_key, reads ANTHROPIC_API_KEY from the environment (loaded from .env).
    # timeout: give up after 60 s; max_retries: the SDK retries brief outages itself.
    if api_key:
        return anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=2)
    return anthropic.Anthropic(timeout=60.0, max_retries=2)


def key_problem(api_key):
    """Check a visitor's own key with a free call (listing models). None if it works,
    otherwise a plain-English problem."""
    try:
        _client(api_key).models.list()
        return None
    except anthropic.AuthenticationError:
        return "Anthropic rejected that key. Check you copied all of it."
    except anthropic.PermissionDeniedError:
        return "That key isn't allowed to use the API."
    except anthropic.APIConnectionError:
        return "Couldn't reach Anthropic to check the key. Try again shortly."
    except anthropic.APIStatusError as err:
        return f"Anthropic had a problem checking the key ({err.status_code}). Try again shortly."


def call_claude(system, prompt, output_format, max_tokens, api_key=None):
    """One Claude request with a structured reply. Shared by grading and card drafting.

    Returns the SDK response (response.parsed_output is the validated object).
    Raises GradingError with a plain-English message for anything that goes wrong.
    """
    if not api_key and not api_key_configured():
        raise GradingError("No API key found. Add ANTHROPIC_API_KEY to the .env file "
                           "in the project folder, then restart the app.")
    try:
        response = _client(api_key).messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=output_format,
        )
    except anthropic.AuthenticationError:
        if api_key:
            raise GradingError("Your API key was rejected. Check it on the Settings page.")
        raise GradingError("The API key was rejected. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.PermissionDeniedError:
        raise GradingError("This API key isn't allowed to use that model.")
    except anthropic.NotFoundError:
        raise GradingError(f"Model '{config.CLAUDE_MODEL}' wasn't found. "
                           "Check CLAUDE_MODEL in config.py.")
    except anthropic.RateLimitError:
        raise GradingError("Too many requests, or your API credit has run out. "
                           "Wait a minute, or check your balance at console.anthropic.com.")
    except anthropic.BadRequestError as err:
        if "credit" in str(err).lower():
            raise GradingError("Your API credit balance is too low. "
                               "Top up at console.anthropic.com.")
        raise GradingError(f"The request was rejected: {err.message}")
    except anthropic.APIConnectionError:
        raise GradingError("Couldn't reach the Claude API. Check your internet connection.")
    except anthropic.APIStatusError as err:
        raise GradingError(f"The Claude API had a problem ({err.status_code}). Try again shortly.")

    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise GradingError("Claude's reply was cut off. Try again.")
    return response


def grade_answer(card, user_answer, api_key=None):
    """Ask Claude to grade one answer. Returns (Grade, input_tokens, output_tokens)."""
    reference = card["answer"]
    if card["note"]:
        reference += f"\n\nKey insight: {card['note']}"
    prompt = (
        f"<question>\n{card['question']}\n</question>\n\n"
        f"<reference_answer>\n{reference}\n</reference_answer>\n\n"
        f"<student_answer>\n{user_answer}\n</student_answer>"
    )

    response = call_claude(system=SYSTEM_PROMPT, prompt=prompt, output_format=Grade,
                           max_tokens=2000, api_key=api_key)

    grade = response.parsed_output
    grade.score = max(0, min(100, grade.score))  # keep it in range whatever comes back
    return grade, response.usage.input_tokens, response.usage.output_tokens


def blank_grade():
    """A blank answer scores 0 with no API call (and no cost)."""
    return Grade(score=0, missed=["No answer given"], wrong=[], rewrite="")


def save_feedback(conn, card_id, user_answer, grade, model, input_tokens, output_tokens,
                  duration_ms, timed_out, variant_seed=None):
    cursor = conn.execute(
        "INSERT INTO ai_feedback (card_id, created_at, user_answer, score, missed, wrong, "
        "rewrite, model, input_tokens, output_tokens, duration_ms, timed_out, variant_seed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (card_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), user_answer,
         grade.score, json.dumps(grade.missed), json.dumps(grade.wrong), grade.rewrite,
         model, input_tokens, output_tokens, duration_ms, timed_out, variant_seed),
    )
    conn.commit()
    return cursor.lastrowid


def record_usage(conn, purpose, input_tokens, output_tokens):
    """Log Claude usage that isn't a grade (grades keep their tokens in ai_feedback)."""
    conn.execute(
        "INSERT INTO ai_usage (created_at, purpose, model, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), purpose,
         config.CLAUDE_MODEL, input_tokens, output_tokens),
    )
    conn.commit()


def total_spend(conn):
    """Estimated dollars spent on Claude so far (grading + card drafting)."""
    tokens_in, tokens_out = conn.execute(
        "SELECT COALESCE(SUM(i), 0), COALESCE(SUM(o), 0) FROM ("
        "  SELECT input_tokens AS i, output_tokens AS o FROM ai_feedback"
        "  UNION ALL SELECT input_tokens, output_tokens FROM ai_usage)"
    ).fetchone()
    return (tokens_in * config.PRICE_PER_M_INPUT + tokens_out * config.PRICE_PER_M_OUTPUT) / 1e6
