# Wheelwright — flashcards you explain in your own words

*The hand-feel's not included.*

**Wheelwright** is a study app for remembering what you learn. It shows a flashcard,
you type the answer **in your own words**, and Claude grades the explanation against
the reference answer. The grade feeds a spaced-repetition scheduler (FSRS), so cards
you explain poorly come back sooner. I use it on my own computer with my finance
cards; a public demo runs the same code with a sample deck on general subjects.

> **The name.** In the *Zhuangzi*, Wheelwright Bian tells Duke Huan that the books he is
> reading are only "the dregs of the men of old": the feel of a craft can't be put into
> words, so Bian couldn't even pass his skill on to his own son. Reading isn't knowing.
> The app can give you the cards; the hand-feel only comes from practice.

> **Status:** in progress — being built in stages (see [Roadmap](#roadmap)).

## Why I built it

I'm a finance grad student preparing for interviews. In an interview nobody
asks me to recognise the right answer — they ask me to *explain* it: "Walk me
through a DCF", "Why do PE firms pay lower multiples than strategics?".
Classic flashcards only test recognition. This app makes me produce the
explanation, then tells me what I missed and shows a tighter way to say it.

## What it does

1. **Imports** my existing flashcard sets (HTML files) into a local SQLite database.
2. **Schedules** reviews with FSRS, a modern spaced-repetition algorithm.
3. **Grades** my typed answer with the Claude API: a score, what I missed or got
   wrong, and a tightened rewrite of my wording. The score maps to an FSRS
   rating (Again / Hard / Good / Easy).
4. **Reminds** me when cards are due.

## Screenshots

_Coming soon._

<!-- ![Review screen](docs/screenshots/review.png) -->
<!-- ![AI feedback](docs/screenshots/feedback.png) -->

## How it works

### 1. Importing cards (`import_cards.py`, `parsers.py`)

My flashcards started life as self-contained HTML study pages. The cards
aren't visible text in those pages — they're stored as a JavaScript list:

```js
const CARDS = [
  { id: 1, tag: 'def', q: 'What is an asset?', a: 'Business entity, PP&E, <strong>patents</strong>…' },
  ...
];
```

The importer:

1. **Finds the card list** in each file and cuts it out, tracking brackets while
   ignoring any that appear inside strings or comments.
2. **Parses it** with `json5`, which reads JavaScript's looser object syntax.
3. **Normalises** the different field names (`q`/`a`, `front`/`back`, `topic`/`section`/`block`…)
   into one shape, and strips HTML formatting (`<strong>`, `<br>`) into plain text.
4. **Saves** each card to SQLite. Every card gets a fingerprint (a SHA-1 hash of
   deck + question), so re-running the importer updates changed cards instead of
   duplicating them — and never touches review history.
5. **Flags** cards that may not stand alone in a shuffled review (e.g. "this same
   bond…", or questions built around a chart).

Two less common formats are handled too: cards written as HTML elements
(`<div class="card">`), and a Markdown file (`**Q:** … **A:** …`).

### 2. Reviewing with spaced repetition (`srs.py`, `app.py`)

**Spaced repetition** means reviewing a card just before you'd forget it. Each
successful recall pushes the next review further out (10 minutes → 3 days →
2 weeks → …); a failed recall brings it back soon.

The scheduling maths is done by **FSRS** (Free Spaced Repetition Scheduler),
via the official `fsrs` Python library. FSRS models each card with two numbers —
*stability* (how long the memory lasts) and *difficulty* — and schedules the
next review for when my chance of remembering drops to 90%.

My code around FSRS is small:

- **State storage.** FSRS's per-card state is saved as JSON in a `review_state`
  table, with the due time copied into its own column so the queue can sort by it.
  Every rating is also appended to a `review_log` table.
- **Queue order.** Overdue cards first (most overdue first), then new cards in
  random order — capped at 20 new cards a day so reviews don't snowball.
- **Previews.** Each rating button shows what it would do ("Good · next: 3 d"),
  computed by running FSRS on the card without saving.
- **Double-submit guard.** The rating form carries the card's current due time;
  if a double-click already rated the card, the second rating is ignored.

The screens are a small **Flask** web app that runs on my own computer and opens
in the browser. Keyboard shortcuts: <kbd>Space</kbd> shows the answer,
<kbd>1</kbd>–<kbd>4</kbd> rate it Again / Hard / Good / Easy.

**Study sessions.** Every *Study* button starts a session of the smaller of my
session size (25, 50, 75 or 100, chosen in Settings) and the cards actually
waiting, so a deck with 12 due cards gives a 12-question session. The card shows
my progress ("7 / 25"), and the session ends on a summary: questions answered,
average retention and total thinking time, with an "Another round" button.
A card I forget and see again later counts as another question. Each review
records its session (`study_sessions` table, `review_log.session_id`).

**Timing.** How long I take on each card is always measured quietly and stored per
review; the home page shows my average per deck, because in an interview speed of
recall matters too. An optional **countdown** (off by default; 30 seconds to 5 minutes,
set on the Settings page) adds a timer bar: when it runs out, the answer is revealed,
or in typed mode what I've written is submitted, and the attempt is marked as over
time. Time never changes the schedule directly; the retention rating does.

### 3. Own-words grading with Claude (`grader.py`)

In **typed mode** I explain the answer in my own words, as I would to an
interviewer, then Claude grades it against the reference answer. One API call
per card returns:

- a **score** out of 100,
- what I **missed** and what I got **wrong**,
- a **tightened rewrite** of my own wording.

The score sets a **Forgotten → Retained slider** (0–100). <kbd>Enter</kbd> accepts
Claude's assessment; I can drag the slider if I disagree (logged as `override`),
because a grader can be wrong. Self-rating mode uses the same slider.

Under the slider, FSRS's **suggested next review** is shown as a date
("Thu 2 Oct (in 3 d)"). A **change** link lets me pick my own date instead
(date picker, or Tomorrow / This weekend). My date wins; FSRS still learns from
the rating, and at the next review it uses the real time elapsed.

Behind the scenes the slider is converted to one of FSRS's four grades
(90+ Easy, 70–89 Good, 50–69 Hard, below 50 Again), because FSRS's memory model
is built on exactly four. The raw 0–100 value is stored with every review too.

How the call is built:

- **Structured outputs.** The response must match a fixed schema (a Pydantic
  model), so the app never parses free-form text.
- **Grade substance, not wording.** The prompt tells Claude that paraphrasing
  deserves full credit — the whole point is to *not* memorise the card's wording.
- **Model in one setting.** `CLAUDE_MODEL` in `config.py`; currently Claude Haiku 4.5,
  Anthropic's cheapest current model. Measured: ~700–850 input and 60–140 output
  tokens per grade, about **$0.0012 per graded answer** (~800 grades per $1), 3–4 s each.
- **Tested on real answers.** A good own-words answer scored 92, a half-right one 60,
  a wrong one 15. The first half-right test scored 45 because Claude marked a
  correct *alternative* formula (EV = equity + net debt) as wrong, so the prompt now
  tells it to credit correct alternatives and treat imprecision as "missed", not "wrong".
- **Cost controls.** Blank / "I don't know" answers skip the API entirely; answers
  are capped at 5,000 characters; token usage is stored with every grade and draft
  (`grader.total_spend()` adds it up), but kept out of the interface.
- **Failure-safe.** No key, no internet, or no credit shows a plain-English message
  plus the reference answer, so I can still self-rate the card.
- **Pay once.** Each grade is saved before I rate it, and the page redirects
  afterwards, so refreshing never triggers a second (paid) grade.

### 4. Reminders (`remind.py`, `setup_reminders.py`) — Windows

**Smart reminders, on my schedule.** On the **Settings** page I choose the time(s)
of day and which weekdays are study days, and switch the schedule on or off.
The page also shows the next week's outlook, computed from FSRS's due dates
(e.g. "Thu: 12 due · Sat: rest day"); cards due on a rest day roll forward to my
next study day. Changing the times updates the Windows schedule automatically.

**Windows Task Scheduler** runs `remind.py` at the chosen times. The script skips
days that aren't study days, then:

1. counts cards waiting (due reviews + new cards still allowed today) **in the decks
   I've ticked "Remind me" on the home page**, and stays silent if there are none;
2. starts the study app invisibly in the background if it isn't already running,
   so the notification's **Study now** button has something to open — that copy
   shuts itself down after 2 hours without use;
3. shows a native Windows notification naming the busiest decks
   ("7 reviews due (DCF 5, M&A 2) · 20 new cards ready")
   through Windows' own notification API, called from PowerShell — no extra package.

4. sends a **reminder email** too, if I've entered an address on the **Settings**
   page — at most once a day by default, or at every reminder time. It's written
   to be read, not ignored (`emails.py`, `templates/email.html`): rotating
   finance-pun subject lines ("Margin call: 14 cards due", "IPO day: 20 new cards"),
   a "Today's statement" of what's due by deck, a **pop quiz** from a due card, and
   a nudge based on yesterday ("Yesterday's volume: 0 cards. Bold strategy." or
   "You're on a 4-day streak"). It's a styled HTML email with a plain-text version,
   sent through a Gmail account using an app password kept in `.env` (Python's
   built-in `smtplib`, encrypted with STARTTLS).

Email and desktop notifications are independent: if one fails (wrong password,
no internet), the other still goes out, and the failure is logged. Because the
reminder runs on my laptop, emails only go out while it's on.

The task catches up after the computer was off ("start when available"), runs only
while I'm logged in, needs no admin rights, and is removed with one command.
Each run writes a line to `data/reminders.log`, because scheduled runs have no window.

### 5. Adding new cards (`card_writer.py`)

- **Add card** (top bar): pick a deck or create a new one, then type the question
  and reference answer. New cards join the review queue as new cards.
- **New cards from Claude**: once I've seen every card in a deck, a *New cards*
  link appears. Claude reads up to 40 of the deck's cards and drafts 3, 5 or 10
  new ones that go a step further (the "why", worked examples, interviewer
  follow-ups), each self-contained. **Nothing is saved until I approve it:** drafts
  wait on the page, where I edit, tick or discard each one.
- Why the approval step matters, measured on a real batch of 5 DCF drafts: 3 were
  good, 1 had a real error (UFCF without the after-tax interest add-back), and
  1 was muddled. About $0.009 per batch of 5 with Haiku.
- Cards record where they came from (`origin`: import / manual / claude), and the
  importer never touches cards I added.

### 6. Fresh numbers for math cards (`variants.py`)

Re-solving a DCF problem with new numbers is better practice than remembering
"the answer is $900". A math card can get a **template**: its question and
answer with the numbers replaced by named blanks, realistic ranges for each input,
and the formulas that produce the answer. Each time the card comes up, the app picks
new numbers and **computes the answer itself**, so the reference answer Claude
grades against is exact. Feedback and the review log use the same numbers I saw.

- **Claude drafts each template once**; it is never trusted on its own.
- **An automatic check** rebuilds the card with its *original* numbers and rejects
  the template unless it reproduces the card's *original* answer and question, then
  tries 25 random sets of numbers (no impossible values, no garbled numbers like "1.7.5").
- **Rules keep conclusions true**: if the answer concludes "bondholders convert", the
  template carries a constraint (conversion price < share price) so random numbers
  never contradict the wording.
- **I approve each template** on the deck's *Fresh numbers* page, which shows a random
  example next to the original.
- **Claude's formulas are never run as Python.** They go through a small calculator
  (`evaluate`, built on Python's `ast` module) that only allows arithmetic, comparisons
  and a few math functions; anything else, like file or system access, is refused.

**Results across all 13 decks (216 math-looking cards, about $1 of Claude usage):**
76 templates pass every check and wait for approval, 30 failed, and 110 cards keep
their numbers (mostly statistics questions about one specific data set).

What the checks caught, and how they were improved, is the most useful part of this
feature: formulas that didn't reproduce the original answer, a "$10,000,000M" slip,
rates glued onto text ("1.7.5"), a conclusion ("convert") that stopped being true, and
original figures left behind in the working ("× 1.08⁵" under a 10% growth question),
found by comparing several random versions. The check was also *too strict* at first,
rejecting "$8,000,000" for "$8M", and was loosened with evidence. A final manual read of
14 random passing templates still found wording slips in about 1 in 3 (for example
"−2.13 SDs above the mean"), which is why every template still needs human approval.

### Merged multi-part cards

Some cards only made sense after the previous one ("…of this same bond").
They're listed in `MERGE_GROUPS` in `config.py` and merged at import into one
card with parts a), b), c) — 34 cards became 12.

### 7. Public demo (`demo.py`, `render.yaml`)

The same code runs as a public demo with `DEMO_MODE=1`, built so strangers can use it
safely without touching my own cards:

- **Sample deck, not my cards.** 53 original cards across 14 subjects (memory, physics, biology,
  medicine, law, history, philosophy, economics, psychology, earth science, language, computing,
  maths and money) in `demo/sample_deck.json` (my own
  finance cards come from courses and stay private). The 6 maths cards are written as
  templates, so even their first version is generated from formulas, and every one is
  checked at start-up; a broken card stops the site from starting rather than going live.
- **Each visitor gets their own database file**, copied from the sample deck, keyed by a
  random ID in a signed cookie. No sign-up, and no query in the app had to change to keep
  visitors apart. A brand-new browser sees the deck read-only until it proves it keeps
  cookies, so bots can't fill the disk; idle visitors are deleted after a week.
- **AI grading costs are capped**: 5 free grades per visitor per day and 150 for the whole
  site per day (about $0.20 at most), counted in a small shared database. Visitors can paste
  their own Anthropic key for unlimited grading; it is checked with a free API call, kept
  only in their signed cookie, and never written to the server's disk.
- **Web security basics**: forged cross-site form posts are refused (the browser's `Origin`
  header must match), cookies are HTTP-only, SameSite and HTTPS-only, standard protective
  headers are sent, and personal-only features (reminders, email, Claude drafting) return 404.
- **Hosting**: Render, configured by `render.yaml`, running the app under gunicorn. Every
  `git push` redeploys it. The free tier sleeps when idle, which also resets demo progress.

Try the demo locally (it uses port 5001 and its own data folder):

```powershell
.\.venv\Scripts\python app.py --demo
```

## How to run it

Requires **Python 3.10+** on Windows (commands below are for PowerShell).

```powershell
# 1. Create a private environment for this project's packages (one time)
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

# 2. Import the flashcards (safe to re-run any time)
.\.venv\Scripts\python import_cards.py

#    ...and list cards that may need a manual check
.\.venv\Scripts\python import_cards.py --flagged

# 3. (Optional) For AI grading, add your key to .env - see "Setup: API key" below

# 4. Start the study app - it opens http://127.0.0.1:5000 in your browser
.\.venv\Scripts\python app.py
#    Press Ctrl+C in the terminal to stop it.

# 5. Reminders (Windows): try one, then install the daily schedule
.\.venv\Scripts\python remind.py --test
.\.venv\Scripts\python setup_reminders.py            # install / update
.\.venv\Scripts\python setup_reminders.py --status   # next and last run
.\.venv\Scripts\python setup_reminders.py --remove   # uninstall
```

Card files are read from `Downloads\Flaschards` by default. To use another
folder, set `CARDS_DIR` in `.env`. The list of files to import lives in `config.py`.

### Setup: API key

1. Copy `.env.example` to a new file called `.env`.
2. Paste your Anthropic API key after `ANTHROPIC_API_KEY=`.
3. Never commit `.env` — it is already listed in `.gitignore`.

## Design decisions

| Public demo choice | Why |
|---|---|
| **One database file per visitor** | Complete separation between visitors with zero changes to the app's queries; the risky part of a multi-user app (forgetting a "which user?" filter) can't happen. |
| **Anonymous signed-cookie IDs, no accounts** | Nothing to sign up for, and no passwords or emails to protect. |
| **Free grades on my key, capped; or bring your own key** | Recruiters can see the AI feedback without an API key, and the worst-case daily cost has a hard ceiling. |
| **Origin check instead of CSRF tokens** | Modern browsers always send `Origin` on form posts; checking it blocks forged posts without changing every form. |
| **Render + gunicorn** | Deploys straight from GitHub with HTTPS included; gunicorn is a production-grade server where Flask's built-in one is not. |

| Personal app choice | Why |
|---|---|
| **Python** | Readable, already installed, and widely used in finance. |
| **SQLite** | A full database stored in one file — no server to install or run. |
| **API key in `.env`, ignored by git** | Keeps the secret out of the code and off GitHub. |
| **Read the JavaScript data, not the page layout** | The card list is structured data; scraping the rendered page would break whenever the page design changed. |
| **`json5`** | JavaScript objects aren't valid JSON (unquoted keys, single quotes, comments); json5 reads them without me writing a parser. |
| **Beautiful Soup** | The standard Python library for reading HTML; used to strip formatting and read the one element-based file. |
| **Fingerprint (hash) per card** | Makes the import *idempotent* — running it twice gives the same result — so I can re-import after editing a source file. |
| **SQLite default journal mode (not WAL)** | The database lives in a OneDrive-synced folder; WAL mode keeps extra side files open that sync tools handle badly. |
| **All settings in `config.py` / `.env`** | One place to change file lists, paths, the daily new-card limit and (later) the Claude model name. |
| **`fsrs` library, not my own algorithm** | FSRS is research-backed and used by Anki; reimplementing it would add bugs, not value. |
| **Flask** | The smallest mainstream Python web framework: one file of routes, no build step, and a browser gives me a UI for free. |
| **Runs on `127.0.0.1` only** | "This computer only" — nothing else on the network can reach the app. |
| **Scheduling logic in `srs.py`, separate from the web code** | The AI grader (Stage 3) and reminders (Stage 4) reuse it without touching the web pages. |
| **20 new cards/day cap** | Each new card returns several times in its first days; without a cap, 767 cards would bury me in reviews. |
| **Previews without FSRS's random "fuzz"** | Fuzz spreads reviews out (good) but made previews jumpy — Hard could show a longer interval than Good. |
| **Redirect after each rating (Post/Redirect/Get)** | Refreshing the page can't re-submit a rating. |
| **All times stored in UTC** | One unambiguous clock; converted to local time only for "today" limits. |
| **Timer measured in the browser, checked on the server** | Only the browser knows when I revealed the answer; the server rejects values that aren't a sensible number of milliseconds. |
| **Small migration step in `db.py`** | Adding columns to an existing table keeps my review history when the schema grows. |
| **Claude Haiku 4.5 for grading** | Cheapest current Claude model; comparing a short answer to a reference is a well-bounded task. Swappable in one line. |
| **Official `anthropic` SDK + structured outputs** | Typed errors, automatic retries, and guaranteed-valid JSON instead of hand-parsing text. |
| **Score → rating mapping in config** | The AI's grade drives FSRS, and the bands are easy to tune. |
| **AI suggests, I confirm** | A grader can be wrong; overrides are logged so I could later measure how often. |
| **Forgotten → Retained slider instead of grade buttons** | Memory isn't four boxes; a continuum is more honest. FSRS still gets its four grades via configurable bands. |
| **Windows Task Scheduler for reminders** | Built into Windows, survives reboots, catches up missed times, and uses no memory between reminders — unlike an always-running background program. |
| **Notifications via PowerShell + Windows' notification API** | Native Windows pop-ups with a "Study now" button and no extra dependency. |
| **Reminder starts the app; app quits when idle** | "Study now" always works, without leaving a server running forever. |
| **Email via Gmail SMTP + app password** | Email is the notification I check most. `smtplib` is built into Python; an app password can only send mail and can be revoked without touching my real password. |
| **Recipient on the Settings page, sender in `.env`** | The address I receive at is a preference (stored locally, never in git); the sending password is a secret (`.env`). |
| **Once-a-day email by default** | Three reminder times shouldn't mean three emails; configurable. |
| **Single-instance check on startup** | Launching the app while a reminder already started it just opens the browser instead of crashing on a busy port. |
| **Design: a woodworking workshop, painted** | Matches the name: an ink-wash wheel signed with a small vermilion seal for a logo, and faint ink-wash mountains in mist fixed along the bottom of the page (drawn with gradients, not blur, which bands into stripes on dark backgrounds); charred-oak dark theme with copper, brass and an olive for "retained" (a linen-and-walnut light theme is kept in the stylesheet); Spectral for headings, Work Sans for text, IBM Plex Mono for figures; squared-off corners like cut timber. Dark for everyone, whatever the system setting. |
| **App name in one setting (`APP_NAME`)** | Pages, emails, notifications and the Windows task all read it, so renaming is one line. |
| **Email copy in its own module + template** | Wording and layout change without touching the scheduling code; card text is HTML-escaped. |
| **Math templates computed by code, not by Claude at study time** | Language models make arithmetic slips; formulas checked against the original answer and evaluated locally are exact, instant and free. |
| **A restricted calculator instead of `eval`** | Never execute AI-written text as code; parse it and allow only arithmetic. |
| **Claude drafts, I approve** | AI-written study material can be confidently wrong (1 in 5 drafts in testing); a human check before anything enters a deck. |
| **Drafts stored before review, then redirect** | Refreshing the page never pays for a second batch, and closing the tab doesn't lose them. |
| **One shared `call_claude` helper** | Grading and drafting get the same plain-English error handling (no key, no credit, offline). |
| **Suggested next date + optional override** | FSRS knows the maths, I know my calendar. |
| **Merge dependent cards at import, listed in config** | A shuffled "part b" can't be answered alone; an explicit list is transparent and editable. |

_More rows added as each tool is introduced._

## Roadmap

- [x] Stage 1 — Import cards from HTML into SQLite (767 cards across 13 decks)
- [x] Stage 2 — Review screen with FSRS scheduling (self-rated)
- [x] Answer timing (average time per deck; optional countdown of 30 s to 5 min)
- [x] Study sessions of 25 / 50 / 75 / 100 questions, with a summary at the end
- [x] Fresh numbers for math cards (piloted on DCF)
- [x] Fresh numbers drafted for all decks (76 templates ready to review)
- [x] Public demo mode (general-knowledge sample deck, per-visitor data, capped AI grading)
- [ ] Deploy the demo to Render and link it here
- [x] Stage 3 — Own-words grading with the Claude API
- [x] Stage 4 — Due-card reminders (Windows desktop + email)
- [x] Forgotten → Retained slider, suggested next date with override
- [x] Reminder times and study days chosen in the app
- [x] Add cards manually, or approve Claude-drafted ones
- [x] Renamed Wheelwright, with a workshop-style redesign
- [ ] Multiple choice with Claude-written wrong options (time feeds the rating)

## License

[MIT](LICENSE). My flashcard *content* isn't part of this repository: the app
imports cards from files on my own computer, and the database stays local.
