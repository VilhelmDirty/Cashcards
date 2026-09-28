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

## How to run it

Requires **Python 3.10+** on Windows (commands below are for PowerShell).

```powershell
# 1. Create a private environment for this project's packages (one time)
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2. Import the flashcards (safe to re-run any time)
.venv\Scripts\python import_cards.py

#    ...and list cards that may need a manual check
.venv\Scripts\python import_cards.py --flagged
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
| **All settings in `config.py` / `.env`** | One place to change file lists, paths and (later) the Claude model name. |

_More rows added as each tool is introduced._

## Roadmap

- [x] Stage 1 — Import cards from HTML into SQLite (767 cards across 13 decks)
- [ ] Stage 2 — Review screen with FSRS scheduling (self-rated)
- [ ] Stage 3 — Own-words grading with the Claude API
- [ ] Stage 4 — Due-card reminders
