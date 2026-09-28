"""The study app. Start it with:

    .\\.venv\\Scripts\\python app.py

It opens http://127.0.0.1:5000 in your browser. Press Ctrl+C in the terminal to stop.
"""
import sys
import threading
import webbrowser
from datetime import datetime, timezone

from flask import Flask, abort, g, redirect, render_template, request, url_for

import db
import srs

app = Flask(__name__)


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


@app.route("/")
def home():
    conn = get_conn()
    return render_template(
        "home.html",
        decks=srs.deck_summary(conn),
        counts=srs.queue_counts(conn),
        reviewed_today=srs.reviewed_today(conn),
    )


@app.route("/review")
def review():
    deck = request.args.get("deck") or None
    conn = get_conn()
    card = srs.next_card(conn, deck)
    if card is None:
        next_due = srs.next_due(conn, deck)
        wait = next_due - datetime.now(timezone.utc) if next_due else None
        return render_template("done.html", deck=deck, wait=wait,
                               wait_text=srs.format_interval(wait) if wait else None)
    return render_template(
        "review.html",
        card=card,
        deck=deck,
        counts=srs.queue_counts(conn, deck),
        previews=srs.preview(conn, card["id"]),
        version=srs.version(conn, card["id"]),
        ratings=srs.RATINGS,
    )


@app.post("/review/<int:card_id>")
def rate(card_id):
    rating = request.form.get("rating", type=int)
    if rating not in (1, 2, 3, 4):
        abort(400)
    conn = get_conn()
    if conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone() is None:
        abort(404)
    srs.record_review(conn, card_id, rating, request.form.get("version", ""))
    # Redirect after saving, so refreshing the page can't submit the rating twice.
    return redirect(url_for("review", deck=request.form.get("deck") or None))


if __name__ == "__main__":
    url = "http://127.0.0.1:5000"
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    print(f"Study app running at {url}  (press Ctrl+C to stop)")
    # 127.0.0.1 means "this computer only": nothing else on the network can reach it.
    app.run(host="127.0.0.1", port=5000, debug=False)
