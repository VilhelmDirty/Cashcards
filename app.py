"""The study app. Start it with:

    .\\.venv\\Scripts\\python app.py

It opens http://127.0.0.1:5000 in your browser. Press Ctrl+C in the terminal to stop.
If the app is already running (e.g. started by a reminder), this just opens the browser.

Two ways to review a card:
  type  - type the answer in my own words; Claude grades it (Stage 3)
  flip  - reveal the answer and rate myself (Stage 2)

Options:
  --no-browser   don't open a browser tab
  --background   started invisibly by remind.py: log to data/app.log and quit
                 after config.IDLE_SHUTDOWN_MINUTES without any page requests
"""
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone

from flask import Flask, abort, g, redirect, render_template, request, url_for

import config
import db
import grader
import srs

app = Flask(__name__)
URL = f"http://127.0.0.1:{config.APP_PORT}"
_last_request = time.monotonic()


def is_running():
    """True if something is already answering on the app's port."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", config.APP_PORT)) == 0


@app.before_request
def note_activity():
    global _last_request
    _last_request = time.monotonic()


def _quit_when_idle():
    """Background mode only: stop the server after a long spell with no page requests."""
    limit = config.IDLE_SHUTDOWN_MINUTES * 60
    while True:
        time.sleep(60)
        if time.monotonic() - _last_request > limit:
            print(f"{datetime.now():%Y-%m-%d %H:%M} idle for {config.IDLE_SHUTDOWN_MINUTES} min, stopping")
            os._exit(0)


def get_conn():
    """One database connection per page request, closed automatically afterwards."""
    if "conn" not in g:
        g.conn = db.connect()
    return g.conn


@app.teardown_appcontext
def close_conn(_error):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


def get_card(conn, card_id):
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if card is None:
        abort(404)
    return card


def read_duration():
    """The browser measures thinking time. Treat it as untrusted input:
    ignore anything that isn't a sensible number of milliseconds."""
    duration_ms = request.form.get("duration_ms", type=int)
    if duration_ms is not None and not 0 <= duration_ms <= 10 * 60 * 1000:
        return None
    return duration_ms


def current_mode():
    """'type' (AI grading) when an API key is set up, unless I picked otherwise."""
    mode = request.args.get("mode")
    if mode in ("type", "flip"):
        return mode
    return "type" if grader.api_key_configured() else "flip"


@app.route("/")
def home():
    conn = get_conn()
    return render_template(
        "home.html",
        decks=srs.deck_summary(conn),
        counts=srs.queue_counts(conn),
        reviewed_today=srs.reviewed_today(conn),
        ai_ready=grader.api_key_configured(),
        model=config.CLAUDE_MODEL,
        spend=grader.total_spend(conn),
    )


@app.route("/review")
def review():
    deck = request.args.get("deck") or None
    mode = current_mode()
    conn = get_conn()
    card = srs.next_card(conn, deck)
    if card is None:
        next_due = srs.next_due(conn, deck)
        wait = next_due - datetime.now(timezone.utc) if next_due else None
        return render_template("done.html", deck=deck, wait=wait,
                               wait_text=srs.format_interval(wait) if wait else None)
    common = dict(card=card, deck=deck, mode=mode, counts=srs.queue_counts(conn, deck))
    if mode == "type":
        return render_template("type.html", **common,
                               time_limit=config.TYPED_TIME_LIMIT_SECONDS)
    return render_template(
        "review.html", **common,
        previews=srs.preview(conn, card["id"]),
        version=srs.version(conn, card["id"]),
        bands=config.SCORE_TO_RATING,
        initial=50,  # self-rating starts mid-way; I move it
        time_limit=config.TIME_LIMIT_SECONDS,
    )


@app.post("/grade/<int:card_id>")
def grade(card_id):
    """Send my typed answer to Claude, save the grade, then show the feedback page."""
    conn = get_conn()
    card = get_card(conn, card_id)
    deck = request.form.get("deck") or None
    answer = request.form.get("answer", "").strip()[:5000]  # cap length: cost control
    duration_ms = read_duration()
    timed_out = request.form.get("timed_out") == "1"

    if not answer:
        result, model, tokens_in, tokens_out = grader.blank_grade(), None, 0, 0
    else:
        try:
            result, tokens_in, tokens_out = grader.grade_answer(card, answer)
            model = config.CLAUDE_MODEL
        except grader.GradingError as err:
            # Grading failed: show the reference answer so I can still rate myself.
            return render_template(
                "feedback.html", card=card, deck=deck, feedback=None, error=str(err),
                user_answer=answer, suggested=None, initial=50,
                bands=config.SCORE_TO_RATING,
                previews=srs.preview(conn, card_id), version=srs.version(conn, card_id),
                duration_ms=duration_ms, timed_out=timed_out,
            )

    feedback_id = grader.save_feedback(conn, card_id, answer, result, model,
                                       tokens_in, tokens_out, duration_ms, timed_out)
    # Redirect so refreshing the feedback page doesn't pay for a second grade.
    return redirect(url_for("feedback", feedback_id=feedback_id, deck=deck))


@app.route("/feedback/<int:feedback_id>")
def feedback(feedback_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM ai_feedback WHERE id = ?", (feedback_id,)).fetchone()
    if row is None:
        abort(404)
    deck = request.args.get("deck") or None
    already_rated = conn.execute("SELECT 1 FROM review_log WHERE feedback_id = ?",
                                 (feedback_id,)).fetchone()
    if already_rated:  # e.g. the Back button after rating
        return redirect(url_for("review", deck=deck))
    card = get_card(conn, row["card_id"])
    return render_template(
        "feedback.html", card=card, deck=deck, error=None,
        feedback=row, missed=json.loads(row["missed"]), wrong=json.loads(row["wrong"]),
        user_answer=row["user_answer"], suggested=grader.score_to_rating(row["score"]),
        initial=row["score"],  # Claude's assessment sets the slider
        bands=config.SCORE_TO_RATING, previews=srs.preview(conn, card["id"]),
        version=srs.version(conn, card["id"]),
        duration_ms=row["duration_ms"], timed_out=row["timed_out"],
    )


@app.post("/review/<int:card_id>")
def rate(card_id):
    # The Forgotten (0) -> Retained (100) slider; FSRS needs one of 4 grades.
    retention = request.form.get("retention", type=int)
    if retention is None or not 0 <= retention <= 100:
        abort(400)
    rating = grader.score_to_rating(retention)
    conn = get_conn()
    get_card(conn, card_id)

    graded_by, feedback_id = "self", request.form.get("feedback_id", type=int)
    if feedback_id is not None:
        row = conn.execute("SELECT score FROM ai_feedback WHERE id = ? AND card_id = ?",
                           (feedback_id, card_id)).fetchone()
        if row is None:
            abort(400)
        # Did I keep Claude's assessment, or move the slider?
        graded_by = "claude" if retention == row["score"] else "override"

    srs.record_review(
        conn, card_id, rating, request.form.get("version", ""),
        graded_by=graded_by,
        duration_ms=read_duration(),
        timed_out=request.form.get("timed_out") == "1",
        feedback_id=feedback_id,
        retention=retention,
    )
    # Redirect after saving, so refreshing the page can't submit the rating twice.
    mode = request.form.get("mode")
    return redirect(url_for("review", deck=request.form.get("deck") or None,
                            mode=mode if mode in ("type", "flip") else None))


if __name__ == "__main__":
    background = "--background" in sys.argv
    open_browser = not background and "--no-browser" not in sys.argv

    if is_running():  # one copy is enough
        if open_browser:
            webbrowser.open(URL)
        print(f"The app is already running at {URL}")
        sys.exit(0)

    if background:
        # Started invisibly (no terminal), so send messages to a log file instead.
        log_path = config.PROJECT_DIR / "data" / "app.log"
        log_path.parent.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(log_path, "a", encoding="utf-8", buffering=1)
        print(f"{datetime.now():%Y-%m-%d %H:%M} started in background by a reminder")
        threading.Thread(target=_quit_when_idle, daemon=True).start()
    elif open_browser:
        threading.Timer(1.0, webbrowser.open, args=[URL]).start()

    print(f"Study app running at {URL}  (press Ctrl+C to stop)")
    # 127.0.0.1 means "this computer only": nothing else on the network can reach it.
    app.run(host="127.0.0.1", port=config.APP_PORT, debug=False)
