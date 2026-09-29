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
import random
import re
import socket
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone

from flask import Flask, abort, g, redirect, render_template, request, url_for

import card_writer
import config
import db
import grader
import mailer
import setup_reminders
import srs
import variants

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


@app.context_processor
def app_name():
    """Makes {{ app_name }} available in every page template."""
    return {"app_name": config.APP_NAME}


@app.route("/")
def home():
    conn = get_conn()
    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return render_template(
        "home.html",
        greeting=greeting,
        decks=srs.deck_summary(conn),
        counts=srs.queue_counts(conn),
        reviewed_today=srs.reviewed_today(conn),
        ai_ready=grader.api_key_configured(),
        math_cards=variants.candidate_counts(conn),
    )


@app.post("/reminders")
def save_reminders():
    """The 'Remind me' checkboxes on the home page: ticked decks trigger reminders."""
    conn = get_conn()
    known = {row[0] for row in conn.execute("SELECT DISTINCT deck FROM cards")}
    chosen = set(request.form.getlist("remind")) & known  # ignore anything unexpected
    srs.set_reminded_decks(conn, chosen)
    return redirect(url_for("home"))


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # 24-hour "HH:MM"


def schedule_status():
    """The Windows schedule's state, or an error message if it can't be read."""
    try:
        return setup_reminders.status()
    except setup_reminders.ScheduleError as err:
        return {"installed": False, "error": str(err)}


def render_settings(conn, message=None, error=None):
    days = db.study_days(conn)
    outlook, unseen = srs.upcoming(conn, days)
    return render_template(
        "settings.html",
        email_to=db.get_setting(conn, "email_to"),
        email_frequency=db.get_setting(conn, "email_frequency"),
        desktop=db.get_setting(conn, "desktop_notifications") == "1",
        sender=mailer.sender(), email_ready=mailer.email_configured(),
        times=db.reminder_times(conn), study_days=days, weekdays=WEEKDAYS,
        outlook=outlook, unseen=unseen, schedule=schedule_status(),
        timer_seconds=int(db.get_setting(conn, "timer_seconds")), timer_choices=config.TIMER_CHOICES,
        session_size=int(db.get_setting(conn, "session_size")), session_sizes=config.SESSION_SIZES,
        reminded=[d["deck"] for d in srs.deck_summary(conn) if d["remind"]],
        message=message, error=error,
    )


@app.route("/settings")
def settings():
    return render_settings(get_conn(), message=request.args.get("msg"))


@app.post("/settings")
def save_settings():
    conn = get_conn()
    email_to = request.form.get("email_to", "").strip()
    if email_to and not mailer.looks_like_email(email_to):
        return render_settings(conn, error=f"'{email_to}' doesn't look like an email address.")
    frequency = request.form.get("email_frequency")
    if frequency not in ("off", "daily", "every"):
        abort(400)
    times = sorted({t.strip() for t in request.form.getlist("reminder_time") if t.strip()})
    if not times or len(times) > 6 or not all(TIME_PATTERN.match(t) for t in times):
        return render_settings(conn, error="Choose between 1 and 6 reminder times.")
    days = sorted({int(d) for d in request.form.getlist("study_day") if d in "0123456" and d})
    timer_text = request.form.get("timer_seconds", "0")
    if not timer_text.isdigit() or int(timer_text) not in config.TIMER_CHOICES:
        abort(400)  # only the lengths offered on the page
    timer = int(timer_text)
    size_text = request.form.get("session_size", db.get_setting(conn, "session_size"))
    if not size_text.isdigit() or int(size_text) not in config.SESSION_SIZES:
        abort(400)

    old_times = db.reminder_times(conn)
    db.set_setting(conn, "email_to", email_to)
    db.set_setting(conn, "email_frequency", frequency)
    db.set_setting(conn, "desktop_notifications", "1" if request.form.get("desktop") else "0")
    db.set_setting(conn, "reminder_times", ",".join(times))
    db.set_setting(conn, "study_days", ",".join(map(str, days)))
    db.set_setting(conn, "timer_seconds", str(timer))
    db.set_setting(conn, "session_size", size_text)

    message = "Saved."
    if times != old_times and schedule_status().get("installed"):
        try:  # keep the Windows schedule in step with the new times
            setup_reminders.install(times)
            message = "Saved, and the Windows reminder schedule was updated."
        except setup_reminders.ScheduleError as err:
            return render_settings(conn, error=f"Saved, but updating the schedule failed: {err}")
    return redirect(url_for("settings", msg=message))


@app.post("/settings/schedule")
def change_schedule():
    """The Install / Remove buttons for the Windows reminder schedule."""
    conn = get_conn()
    try:
        if request.form.get("action") == "install":
            setup_reminders.install(db.reminder_times(conn))
            message = "Reminder schedule installed."
        elif request.form.get("action") == "remove":
            setup_reminders.remove()
            message = "Reminder schedule removed. No more reminders until you install it again."
        else:
            abort(400)
    except setup_reminders.ScheduleError as err:
        return render_settings(conn, error=f"Windows schedule problem: {err}")
    return redirect(url_for("settings", msg=message))


@app.post("/settings/test-email")
def test_email():
    """Send a real reminder email right now, to check the setup works."""
    import remind  # imported here because remind.py itself imports this file
    conn = get_conn()
    to = db.get_setting(conn, "email_to")
    if not to:
        return render_settings(conn, error="Save an email address first.")
    try:
        remind.send_reminder_email(conn, srs.reminder_counts(conn), to, test=True)
    except mailer.MailError as err:
        return render_settings(conn, error=str(err))
    return render_settings(conn, message=f"Test email sent to {to}. Check your inbox (and spam).")


@app.route("/cards/new")
def new_card():
    conn = get_conn()
    return render_template("new_card.html", decks=card_writer.deck_names(conn),
                           deck=request.args.get("deck"), message=request.args.get("msg"),
                           error=None, form={})


@app.post("/cards/new")
def save_new_card():
    conn = get_conn()
    form = request.form
    deck = form.get("new_deck", "") if form.get("deck") == "__new__" else form.get("deck", "")
    try:
        card_writer.add_card(conn, deck, form.get("question", ""), form.get("answer", ""),
                             form.get("topic"))
    except card_writer.CardError as err:
        return render_template("new_card.html", decks=card_writer.deck_names(conn),
                               deck=form.get("deck"), message=None, error=str(err), form=form)
    return redirect(url_for("new_card", deck=deck.strip(),
                            msg="Card added. It joins your reviews as a new card."))


def render_suggest(conn, deck, message=None, error=None):
    summary = next((d for d in srs.deck_summary(conn) if d["deck"] == deck), None)
    if summary is None:
        abort(404)
    return render_template("suggest.html", deck=deck, summary=summary,
                           drafts=card_writer.pending_drafts(conn, deck),
                           ai_ready=grader.api_key_configured(), model=config.CLAUDE_MODEL,
                           message=message, error=error)


@app.route("/decks/suggest")
def suggest():
    return render_suggest(get_conn(), request.args.get("deck", ""), message=request.args.get("msg"))


@app.post("/decks/suggest")
def make_suggestions():
    """Ask Claude to draft new cards; they wait on the page for my approval."""
    conn = get_conn()
    deck = request.form.get("deck", "")
    count = request.form.get("count", type=int)
    if count not in (3, 5, 10):
        abort(400)
    try:
        drafted = card_writer.draft_cards(conn, deck, count)
    except (grader.GradingError, card_writer.CardError) as err:
        return render_suggest(conn, deck, error=str(err))
    # Redirect so refreshing the page doesn't pay for another batch.
    return redirect(url_for("suggest", deck=deck, msg=f"Claude drafted {drafted} cards. "
                            "Check each one, edit if needed, and tick the ones to keep."))


@app.post("/drafts/save")
def save_drafts():
    conn = get_conn()
    deck = request.form.get("deck", "")
    if request.form.get("action") == "discard":
        card_writer.discard_drafts(conn, deck)
        return redirect(url_for("suggest", deck=deck, msg="Drafts discarded."))

    saved, problems = 0, []
    for draft in card_writer.pending_drafts(conn, deck):
        n = draft["id"]
        if not request.form.get(f"keep_{n}"):
            continue
        try:
            card_writer.add_card(conn, deck, request.form.get(f"question_{n}", ""),
                                 request.form.get(f"answer_{n}", ""),
                                 request.form.get(f"topic_{n}"), origin="claude")
            saved += 1
        except card_writer.CardError as err:
            problems.append(str(err))
    card_writer.discard_drafts(conn, deck)  # anything not ticked is thrown away
    message = f"Added {saved} new card{'' if saved == 1 else 's'} to {deck}."
    if problems:
        message += f" Skipped {len(problems)}: {problems[0]}"
    return redirect(url_for("suggest", deck=deck, msg=message))


def render_caught_up(conn, deck, session=None):
    """Nothing to study right now: say when the next card comes due."""
    next_due = srs.next_due(conn, deck)
    wait = next_due - datetime.now(timezone.utc) if next_due else None
    return render_template("done.html", deck=deck, session=session, wait=wait,
                           wait_text=srs.format_interval(wait) if wait else None)


@app.route("/study")
def start_session():
    """Every Study button lands here: start a session of the smaller of my
    session size (Settings) and the cards actually waiting, then begin."""
    deck = request.args.get("deck") or None
    conn = get_conn()
    session_id = srs.start_session(conn, deck, int(db.get_setting(conn, "session_size")))
    if session_id is None:
        return render_caught_up(conn, deck)
    return redirect(url_for("review", deck=deck, s=session_id, mode=request.args.get("mode")))


def render_numbers(conn, deck, message=None, error=None):
    rows = variants.templates_for_deck(conn, deck)
    for row in rows:  # a random example for each usable template
        row["example"] = None
        if row["template"] and row["template"]["status"] in ("draft", "approved"):
            try:
                row["example"] = variants.preview(row["spec"], random.randrange(1, 2**31))
            except variants.TemplateError as err:
                row["example_error"] = str(err)
    return render_template("numbers.html", deck=deck, rows=rows,
                           ai_ready=grader.api_key_configured(),
                           message=message, error=error)


@app.route("/decks/numbers")
def numbers():
    """Fresh numbers for a deck's math cards: draft templates, check them, approve them."""
    return render_numbers(get_conn(), request.args.get("deck", ""), message=request.args.get("msg"))


@app.post("/decks/numbers/draft")
def draft_numbers():
    """Ask Claude for templates: one card, or every math card in the deck without one."""
    conn = get_conn()
    deck = request.form.get("deck", "")
    only = request.form.get("card_id", type=int)
    rows = variants.templates_for_deck(conn, deck)
    if only:  # "Try again" on one card
        todo = [r["card"] for r in rows if r["card"]["id"] == only]
    else:     # every math card that doesn't have a template yet
        todo = [r["card"] for r in rows if r["template"] is None]
    results = {}
    try:
        for card in todo:
            status = variants.draft_template(conn, card)
            results[status] = results.get(status, 0) + 1
    except grader.GradingError as err:
        return render_numbers(conn, deck, error=f"Stopped early: {err}")
    summary = ", ".join(f"{n} {label}" for label, n in (
        ("passed the check", results.get("draft", 0)), ("failed it", results.get("failed", 0)),
        ("not suitable", results.get("unsuitable", 0))) if n)
    return redirect(url_for("numbers", deck=deck, msg=f"Drafted {len(todo)}: {summary or 'nothing to do'}."))


@app.post("/decks/numbers/decide")
def decide_numbers():
    conn = get_conn()
    deck = request.form.get("deck", "")
    card_id = request.form.get("card_id", type=int)
    action = request.form.get("action")
    row = conn.execute("SELECT status FROM card_templates WHERE card_id = ?", (card_id,)).fetchone()
    if row is None or action not in ("approve", "reject", "off"):
        abort(400)
    if action == "approve" and row["status"] != "draft":
        abort(400)  # only templates that passed the automatic check
    variants.set_status(conn, card_id, {"approve": "approved", "reject": "rejected", "off": "draft"}[action])
    return redirect(url_for("numbers", deck=deck) + f"#card-{card_id}")


@app.route("/review")
def review():
    deck = request.args.get("deck") or None
    mode = current_mode()
    conn = get_conn()
    session = srs.session_progress(conn, request.args.get("s", type=int) or 0)
    if session is None:  # an old link or bookmark: start a proper session
        return redirect(url_for("start_session", deck=deck, mode=request.args.get("mode")))
    if session["done"] >= session["target"]:
        return render_template("session_done.html", deck=deck, session=session, mode=mode)
    card = srs.next_card(conn, deck)
    if card is None:
        return render_caught_up(conn, deck, session)
    card, variant_seed = variants.present(conn, card)  # fresh numbers for templated math cards
    common = dict(card=card, deck=deck, mode=mode, session=session, variant_seed=variant_seed,
                  counts=srs.queue_counts(conn, deck))
    timer_limit = int(db.get_setting(conn, "timer_seconds"))  # 0 = no countdown
    if mode == "type":
        return render_template("type.html", **common, time_limit=timer_limit)
    return render_template(
        "review.html", **common,
        previews=srs.preview(conn, card["id"]),
        version=srs.version(conn, card["id"]),
        bands=config.SCORE_TO_RATING,
        initial=50,  # self-rating starts mid-way; I move it
        time_limit=timer_limit,
    )


@app.post("/grade/<int:card_id>")
def grade(card_id):
    """Send my typed answer to Claude, save the grade, then show the feedback page."""
    conn = get_conn()
    variant_seed = request.form.get("v", type=int)
    card, variant_seed = variants.present(conn, get_card(conn, card_id), variant_seed or None)
    deck = request.form.get("deck") or None
    session_id = request.form.get("s", type=int)
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
                duration_ms=duration_ms, timed_out=timed_out, session_id=session_id,
                variant_seed=variant_seed,
            )

    feedback_id = grader.save_feedback(conn, card_id, answer, result, model,
                                       tokens_in, tokens_out, duration_ms, timed_out, variant_seed)
    # Redirect so refreshing the feedback page doesn't pay for a second grade.
    return redirect(url_for("feedback", feedback_id=feedback_id, deck=deck, s=session_id))


@app.route("/feedback/<int:feedback_id>")
def feedback(feedback_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM ai_feedback WHERE id = ?", (feedback_id,)).fetchone()
    if row is None:
        abort(404)
    deck = request.args.get("deck") or None
    session_id = request.args.get("s", type=int)
    already_rated = conn.execute("SELECT 1 FROM review_log WHERE feedback_id = ?",
                                 (feedback_id,)).fetchone()
    if already_rated:  # e.g. the Back button after rating
        return redirect(url_for("review", deck=deck, s=session_id, mode="type"))
    card, variant_seed = variants.present(conn, get_card(conn, row["card_id"]),
                                          row["variant_seed"])
    return render_template(
        "feedback.html", card=card, deck=deck, error=None,
        feedback=row, missed=json.loads(row["missed"]), wrong=json.loads(row["wrong"]),
        user_answer=row["user_answer"], suggested=grader.score_to_rating(row["score"]),
        initial=row["score"],  # Claude's assessment sets the slider
        bands=config.SCORE_TO_RATING, previews=srs.preview(conn, card["id"]),
        version=srs.version(conn, card["id"]),
        duration_ms=row["duration_ms"], timed_out=row["timed_out"], session_id=session_id,
        variant_seed=variant_seed,
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

    # Optional: my own next-review date instead of FSRS's suggestion.
    due_override = None
    if request.form.get("custom_due"):
        try:
            chosen = date.fromisoformat(request.form["custom_due"])
        except ValueError:
            abort(400)
        today = datetime.now().date()
        if not today < chosen <= today + timedelta(days=365):
            abort(400)  # from tomorrow up to a year ahead
        due_override = srs.local_date_to_due(chosen)

    session_id = request.form.get("s", type=int)
    if session_id is not None and srs.session_progress(conn, session_id) is None:
        abort(400)

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
        due_override=due_override,
        session_id=session_id,
        variant_seed=request.form.get("v", type=int),
    )
    # Redirect after saving, so refreshing the page can't submit the rating twice.
    mode = request.form.get("mode")
    return redirect(url_for("review", deck=request.form.get("deck") or None, s=session_id,
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
