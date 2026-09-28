# Finance Flashcards — explain it in your own words

A personal study app that runs on my own computer. It shows me a finance
flashcard, I type the answer **in my own words**, and Claude grades my
explanation against the reference answer. The grade feeds a spaced-repetition
scheduler (FSRS), so cards I explain poorly come back sooner.

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

# 3. Start the study app - it opens http://127.0.0.1:5000 in your browser
.\.venv\Scripts\python app.py
#    Press Ctrl+C in the terminal to stop it.
```

Card files are read from `Downloads\Flaschards` by default. To use another
folder, set `CARDS_DIR` in `.env`. The list of files to import lives in `config.py`.

### Setup: API key

1. Copy `.env.example` to a new file called `.env`.
2. Paste your Anthropic API key after `ANTHROPIC_API_KEY=`.
3. Never commit `.env` — it is already listed in `.gitignore`.

## Design decisions

| Choice | Why |
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

_More rows added as each tool is introduced._

## Roadmap

- [x] Stage 1 — Import cards from HTML into SQLite (767 cards across 13 decks)
- [x] Stage 2 — Review screen with FSRS scheduling (self-rated)
- [ ] Stage 3 — Own-words grading with the Claude API
- [ ] Stage 4 — Due-card reminders
