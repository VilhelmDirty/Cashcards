"""Stage 4: remind me when flashcards are waiting - desktop notification and/or email.

Windows Task Scheduler runs this a few times a day (set up by setup_reminders.py).
You can also run it by hand:

    .\\.venv\\Scripts\\python remind.py          remind only if cards are waiting
    .\\.venv\\Scripts\\python remind.py --test   always remind (to try it out)

Only decks ticked "Remind me" on the app's home page count. The Settings page
chooses the channels. If cards are waiting it:
  - desktop: starts the study app invisibly (if it isn't running) so the
    notification's "Study now" button has something to open, then shows a
    notification using Windows' built-in notification system;
  - email: sends a short email (at most once a day, unless set to every time)
    with the counts and one warm-up question.
One channel failing doesn't stop the other. Each run adds a line to
data/reminders.log, since scheduled runs have no window.
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import config
import db
import mailer
import srs
from app import URL, is_running

# Windows only shows notifications from apps it knows about. Borrowing Windows
# PowerShell's built-in app ID avoids registering our own (a registry change).
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# PowerShell can reach Windows' notification system directly, so no extra package
# is needed. The notification's XML is passed in through an environment variable,
# which avoids any problems with quotes inside the text.
SHOW_NOTIFICATION = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:FLASHCARDS_TOAST_XML)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:FLASHCARDS_APP_ID).Show($toast)
"""

NO_WINDOW = 0x08000000           # don't flash a console window
BREAKAWAY_FROM_JOB = 0x01000000  # keep the app alive after the scheduled task finishes

LOG = config.PROJECT_DIR / "data" / "reminders.log"


def log(text):
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def short_deck(name):
    """'WSP: DCF' -> 'DCF', '15.401: Fixed Income' -> 'Fixed Income' (notifications are small)."""
    return name.split(": ", 1)[-1]


def waiting_message(counts):
    parts = []
    if counts["due"]:
        busiest = ", ".join(f"{short_deck(d)} {n}" for d, n in counts["by_deck"][:3])
        more = " …" if len(counts["by_deck"]) > 3 else ""
        parts.append(f"{plural(counts['due'], 'review')} due ({busiest}{more})")
    if counts["new"]:
        parts.append(f"{plural(counts['new'], 'new card')} ready")
    return " · ".join(parts) or "Nothing is due right now. (This is a test notification.)"


def start_app_in_background():
    if is_running():
        return "already running"
    # pythonw.exe is the windowless version of Python.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else sys.executable)
    command = [exe, str(config.PROJECT_DIR / "app.py"), "--background"]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(command, cwd=config.PROJECT_DIR, creationflags=flags | BREAKAWAY_FROM_JOB)
    except OSError:  # breakaway not permitted here; start it normally
        subprocess.Popen(command, cwd=config.PROJECT_DIR, creationflags=flags)
    for _ in range(40):  # wait up to ~10 s so "Study now" works straight away
        if is_running():
            return "started"
        time.sleep(0.25)
    return "slow to start (see data/app.log)"


def show_notification(text):
    xml = f"""<toast activationType="protocol" launch={quoteattr(URL + "/")}>
  <visual>
    <binding template="ToastGeneric">
      <text>Finance Flashcards</text>
      <text>{escape(text)}</text>
    </binding>
  </visual>
  <actions>
    <action content="Study now" activationType="protocol" arguments={quoteattr(URL + "/review")}/>
    <action content="Later" activationType="system" arguments="dismiss"/>
  </actions>
</toast>"""
    env = {**os.environ, "FLASHCARDS_TOAST_XML": xml, "FLASHCARDS_APP_ID": POWERSHELL_APP_ID}
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SHOW_NOTIFICATION],
        env=env, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"PowerShell exit code {result.returncode}")


def email_subject(counts):
    if counts["due"]:
        return f"Finance Flashcards: {plural(counts['due'], 'review')} due"
    if counts["new"]:
        return f"Finance Flashcards: {plural(counts['new'], 'new card')} ready"
    return "Finance Flashcards: test email"


def email_body(counts, warm_up):
    lines = [waiting_message(counts), ""]
    if counts["by_deck"]:
        lines.append("Due by deck:")
        lines += [f"  - {deck}: {n}" for deck, n in counts["by_deck"]]
        lines.append("")
    if warm_up:
        deck, question = warm_up
        lines += [f"Warm-up question ({deck}) - answer it in your head now:", "",
                  question, "", "Then check yourself in the app on your laptop.", ""]
    lines += ["---", "Sent by your Finance Flashcards app. Change the address, how often "
              "emails come, or which decks count on the app's Settings and home pages."]
    return "\n".join(lines)


def send_reminder_email(conn, counts, to):
    """Send one reminder email. Raises mailer.MailError on failure."""
    mailer.send_email(to, email_subject(counts), email_body(counts, srs.warm_up_question(conn)))
    db.set_setting(conn, "last_email_date", datetime.now().date().isoformat())


def main():
    test = "--test" in sys.argv
    conn = db.connect()
    try:
        counts = srs.reminder_counts(conn)  # only decks ticked "Remind me" on the home page
        waiting = counts["due"] + counts["new"]
        if waiting < config.REMINDER_MIN_CARDS and not test:
            log(f"nothing waiting in reminded decks (due {counts['due']}, new {counts['new']}), "
                "no reminder")
            return

        text = waiting_message(counts)
        failures = 0

        # --- desktop notification ---
        if db.get_setting(conn, "desktop_notifications") == "1":
            try:
                app_status = start_app_in_background()
                show_notification(text)
                log(f"desktop: {text} (app {app_status})")
                print(f"Desktop notification shown: {text}")
            except Exception as err:  # never crash silently in a scheduled run: write it down
                failures += 1
                log(f"desktop FAILED: {err}")

        # --- email ---
        to = db.get_setting(conn, "email_to")
        frequency = db.get_setting(conn, "email_frequency")
        already_today = db.get_setting(conn, "last_email_date") == datetime.now().date().isoformat()
        if not to or frequency == "off":
            pass
        elif frequency == "daily" and already_today and not test:
            log("email: skipped, already sent one today")
        else:
            try:
                send_reminder_email(conn, counts, to)
                log(f"email: sent to {to}")
                print(f"Email sent to {to}")
            except mailer.MailError as err:
                failures += 1
                log(f"email FAILED: {err}")
                print(f"Email failed: {err}")
    finally:
        conn.close()

    if failures:
        sys.exit(1)  # shows as an error in Task Scheduler / setup_reminders.py --status


if __name__ == "__main__":
    main()
