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
deserves full credit. Don't penalise spelling, grammar or brevity unless meaning \
is lost. If the question has several parts (a, b, c...), grade all of them together.

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


def _client():
    # Reads ANTHROPIC_API_KEY from the environment (loaded from .env by config.py).
    # timeout: give up after 60 s; max_retries: the SDK retries brief outages itself.
    return anthropic.Anthropic(timeout=60.0, max_retries=2)


def grade_answer(card, user_answer):
    """Ask Claude to grade one answer. Returns (Grade, input_tokens, output_tokens)."""
    if not api_key_configured():
        raise GradingError("No API key found. Add ANTHROPIC_API_KEY to the .env file "
                           "in the project folder, then restart the app.")

    reference = card["answer"]
    if card["note"]:
        reference += f"\n\nKey insight: {card['note']}"
    prompt = (
        f"<question>\n{card['question']}\n</question>\n\n"
        f"<reference_answer>\n{reference}\n</reference_answer>\n\n"
        f"<student_answer>\n{user_answer}\n</student_answer>"
    )

    try:
        response = _client().messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=Grade,
        )
    except anthropic.AuthenticationError:
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

    grade = response.parsed_output
    grade.score = max(0, min(100, grade.score))  # keep it in range whatever comes back
    return grade, response.usage.input_tokens, response.usage.output_tokens


def blank_grade():
    """A blank answer scores 0 with no API call (and no cost)."""
    return Grade(score=0, missed=["No answer given"], wrong=[], rewrite="")


def save_feedback(conn, card_id, user_answer, grade, model, input_tokens, output_tokens,
                  duration_ms, timed_out):
    cursor = conn.execute(
        "INSERT INTO ai_feedback (card_id, created_at, user_answer, score, missed, wrong, "
        "rewrite, model, input_tokens, output_tokens, duration_ms, timed_out) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (card_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), user_answer,
         grade.score, json.dumps(grade.missed), json.dumps(grade.wrong), grade.rewrite,
         model, input_tokens, output_tokens, duration_ms, timed_out),
    )
    conn.commit()
    return cursor.lastrowid


def total_spend(conn):
    """Estimated dollars spent on grading so far."""
    tokens_in, tokens_out = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) FROM ai_feedback"
    ).fetchone()
    return (tokens_in * config.PRICE_PER_M_INPUT + tokens_out * config.PRICE_PER_M_OUTPUT) / 1e6
