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

_Filled in as each stage is built._

## How to run it

_Filled in once Stage 1 is working._

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

_More rows added as each tool is introduced._

## Roadmap

- [ ] Stage 1 — Import cards from HTML into SQLite
- [ ] Stage 2 — Review screen with FSRS scheduling (self-rated)
- [ ] Stage 3 — Own-words grading with the Claude API
- [ ] Stage 4 — Due-card reminders
