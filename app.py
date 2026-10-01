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
  --demo         run the public demo locally (sample deck, port 5001), see demo.py
"""
import json
import os
import random
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Flask, Response, abort, g, redirect, render_template, request, session, url_for

import backup
import card_writer
import config
import db
import demo
import grader
import mailer
import setup_reminders
import srs
import variants

app = Flask(__name__)
# Signs the browser cookie so it can't be tampered with. The live site sets SECRET_KEY;
# locally a fresh random key per run is fine.
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,      # page scripts can't read the cookie
    SESSION_COOKIE_SAMESITE="Lax",     # other sites' forms can't send it
    SESSION_COOKIE_SECURE=os.getenv("DEMO_MODE") == "1",  # HTTPS-only on the live site
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=config.BACKUP_MAX_BYTES,  # biggest upload accepted (a progress file)
)
PORT = 5001 if "--demo" in sys.argv else config.APP_PORT
URL = f"http://127.0.0.1:{PORT}"
_last_request = time.monotonic()

# Pages that don't exist in the public demo (reminders, email, and Claude drafting,
# which would spend the site owner's credit).
DEMO_OFF = {"save_reminders", "test_email", "change_schedule", "suggest", "make_suggestions",
            "save_drafts", "numbers", "draft_numbers", "decide_numbers"}
if config.DEMO_MODE:
    demo.build_template_db()  # rebuilt at every start, so deck edits go live on deploy


def is_running(port=None):
    """True if something is already answering on the app's port."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port or config.APP_PORT)) == 0


@app.before_request
def note_activity():
    global _last_request
    _last_request = time.monotonic()


def same_origin():
    """Was this form sent by one of our own pages? Browsers say where a form came from;
    a form on another website posting here would name that site instead (a "CSRF" attack)."""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True  # not sent by a browser form, so it can't be a forged one
    return urlparse(source).netloc == request.host


@app.before_request
def guard_requests():
    if request.method == "POST" and not same_origin():
        abort(403)
    if not config.DEMO_MODE or request.endpoint == "static":
        return
    if request.endpoint in DEMO_OFF:
        abort(404)
    visitor = session.get("visitor")
    if demo.is_valid_visitor(visitor):
        g.db_path = demo.visitor_db(visitor)
        return
    # A brand-new browser: give it an ID, and show the sample deck read-only until it
    # comes back with the cookie. Bots that ignore cookies never create a file.
    session["visitor"] = demo.new_visitor_id()
    session.permanent = True
    if request.endpoint != "home":
        return redirect(url_for("home"))
    g.db_path, g.read_only = demo.template_db(), True


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")  # no guessing file types
    response.headers.setdefault("X-Frame-Options", "DENY")            # can't be framed by other sites
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


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
    if "conn" not in g:  # in the demo, each visitor has their own database file
        g.conn = db.connect(g.get("db_path"), read_only=g.get("read_only", False))
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


def ai_available():
    """Can typed answers be graded? (The site's key, or a demo visitor's own key.)"""
    return grader.api_key_configured() or bool(visitor_key())


def visitor_key():
    """Demo only: the visitor's own API key, unlocked from their cookie (or None)."""
    if not config.DEMO_MODE:
        return None
    return demo.open_key(session.get("own_key"), app.secret_key)


def demo_api_key():
    """Demo only: the key to grade with. None = the site's key (uses a free grade).
    Raises GradingError when the visitor's free grades for today are used up."""
    if not config.DEMO_MODE:
        return None
    own = visitor_key()
    if own:
        return own
    if demo.free_grades_left(session["visitor"]) <= 0:
        raise grader.GradingError(
            "You've used today's free AI grades. Add your own Anthropic API key on the "
            "Settings page for unlimited grading, or rate yourself below.")
    return None


def current_mode():
    """'type' (AI grading) when grading is available, unless I picked otherwise."""
    mode = request.args.get("mode")
    if mode in ("type", "flip"):
        return mode
    return "type" if ai_available() else "flip"


@app.context_processor
def app_name():
    """Makes {{ app_name }}, {{ demo }} etc. available in every page template.
    studying: True on the study screens, which get a compact header without the tagline."""
    return {"app_name": config.APP_NAME, "app_tagline": config.APP_TAGLINE,
            "demo": config.DEMO_MODE, "github_url": config.GITHUB_URL,
            "studying": request.endpoint in STUDY_PAGES}


STUDY_PAGES = {"review", "grade", "feedback", "rate", "start_session"}


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
        ai_ready=ai_available(),
        own_key=bool(visitor_key()),
        free_left=demo.free_grades_left(session["visitor"]) if config.DEMO_MODE else None,
        math_cards={} if config.DEMO_MODE else variants.candidate_counts(conn),
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
    common = dict(
        timer_seconds=int(db.get_setting(conn, "timer_seconds")), timer_choices=config.TIMER_CHOICES,
        session_size=int(db.get_setting(conn, "session_size")), session_sizes=config.SESSION_SIZES,
        message=message, error=error,
    )
    if config.DEMO_MODE:  # just the study settings, plus "use your own API key"
        return render_template("settings.html", **common, own_key=bool(visitor_key()),
                               free_left=demo.free_grades_left(session["visitor"]),
                               free_per_day=config.DEMO_FREE_GRADES)
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
        reminded=[d["deck"] for d in srs.deck_summary(conn) if d["remind"]],
        **common,
    )


@app.route("/about")
def about():
    """What the app is, where the name comes from, and how it works."""
    return render_template("about.html")


@app.route("/settings")
def settings():
    return render_settings(get_conn(), message=request.args.get("msg"), error=request.args.get("error"))


@app.post("/settings")
def save_settings():
    conn = get_conn()
    if config.DEMO_MODE:  # the demo only has the study settings
        timer_text = request.form.get("timer_seconds", "0")
        size_text = request.form.get("session_size", "25")
        if (not timer_text.isdigit() or int(timer_text) not in config.TIMER_CHOICES
                or not size_text.isdigit() or int(size_text) not in config.SESSION_SIZES):
            abort(400)
        db.set_setting(conn, "timer_seconds", timer_text)
        db.set_setting(conn, "session_size", size_text)
        return redirect(url_for("settings", msg="Saved."))
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


@app.post("/settings/key")
def own_key():
    """Demo only: a visitor's own Anthropic key, for unlimited grading. It is kept in
    their browser cookie, encrypted, and sent with their requests; never saved on the server."""
    if not config.DEMO_MODE:
        abort(404)
    conn = get_conn()
    if request.form.get("action") == "remove":
        session.pop("own_key", None)
        return redirect(url_for("settings", msg="Your key was removed from this browser."))
    key = request.form.get("key", "").strip()
    if not demo.looks_like_api_key(key):
        return render_settings(conn, error="That doesn't look like an Anthropic API key "
                                           "(they start with sk-ant-).")
    problem = grader.key_problem(key)
    if problem:
        return render_settings(conn, error=problem)
    session["own_key"] = demo.seal_key(key, app.secret_key)
    return redirect(url_for("settings", msg="Key saved in this browser. Grading is now unlimited."))


@app.get("/progress")
def download_progress():
    """All my cards and progress as one JSON file (see backup.py)."""
    body = json.dumps(backup.export(get_conn()), ensure_ascii=False)
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": f'attachment; filename="{backup.filename()}"'})


@app.post("/progress")
def restore_progress():
    """Replace everything with an uploaded progress file. In the personal app the
    current database is copied to data/backups first, in case I pick the wrong file."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return redirect(url_for("settings", error="Choose a progress file to restore."))
    conn = get_conn()
    if not config.DEMO_MODE:
        folder = config.DB_PATH.parent / "backups"
        folder.mkdir(exist_ok=True)
        with db.sqlite3.connect(folder / f"before-restore-{datetime.now():%Y%m%d-%H%M%S}.db") as copy:
            conn.backup(copy)
    try:
        count = backup.restore(conn, upload.read(),
                               keep_templates_from=demo.template_db() if config.DEMO_MODE else None)
    except backup.BackupError as err:
        return redirect(url_for("settings", error=str(err)))
    return redirect(url_for("settings", msg=f"Restored {count} cards and your progress."))


@app.errorhandler(413)
def upload_too_large(_error):
    return redirect(url_for("settings", error="That file is too large to be a progress file."))


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
    action = request.form.get("action")
    if action == "approve_all":  # every template in the deck that passed the check
        ready = [r["card"]["id"] for r in variants.templates_for_deck(conn, deck)
                 if r["template"] and r["template"]["status"] == "draft"]
        for card_id in ready:
            variants.set_status(conn, card_id, "approved")
        return redirect(url_for("numbers", deck=deck, msg=f"Approved {len(ready)} template"
                                f"{'' if len(ready) == 1 else 's'}. Turn any off below."))
    card_id = request.form.get("card_id", type=int)
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
            api_key = demo_api_key()  # demo: the visitor's own key, or a free grade
            result, tokens_in, tokens_out = grader.grade_answer(card, answer, api_key=api_key)
            model = config.CLAUDE_MODEL
            if config.DEMO_MODE and not api_key:
                demo.use_free_grade(session["visitor"])
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

    if is_running(PORT):  # one copy is enough
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

    print(f"{'Public demo' if config.DEMO_MODE else 'Study app'} running at {URL}  (press Ctrl+C to stop)")
    # 127.0.0.1 means "this computer only": nothing else on the network can reach it.
    # (The live site runs under gunicorn instead; see render.yaml.)
    app.run(host="127.0.0.1", port=PORT, debug=False)
