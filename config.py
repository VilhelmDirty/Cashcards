"""All app settings in one place.

Anything set in the .env file overrides the defaults below, so personal
paths and the API key never have to be written into the code.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

# The SQLite database file. The data/ folder is git-ignored.
DB_PATH = Path(os.getenv("DB_PATH") or PROJECT_DIR / "data" / "flashcards.db")

# The folder holding the flashcard files to import.
CARDS_DIR = Path(os.getenv("CARDS_DIR") or Path.home() / "Downloads" / "Flaschards")

# --- Spaced repetition ---
# How many never-seen cards to introduce per day. Each new card comes back
# several times in its first days, so a big number snowballs into big review piles.
NEW_CARDS_PER_DAY = 20
# FSRS schedules each card for when you have this chance of still remembering it.
# Higher = more frequent reviews. 0.9 (90%) is the FSRS default.
DESIRED_RETENTION = 0.9
# Seconds to think before the answer is revealed automatically.
TIME_LIMIT_SECONDS = 60

# --- AI grading (Claude API) ---
# The ONE place the model is named. Haiku is Anthropic's cheapest current model.
CLAUDE_MODEL = "claude-haiku-4-5"
# Price per million tokens for CLAUDE_MODEL, used only to show my running spend.
# Check https://www.anthropic.com/pricing if you change the model.
PRICE_PER_M_INPUT = 1.00
PRICE_PER_M_OUTPUT = 5.00
# Typing an explanation takes longer than recalling one, so typed answers get
# their own limit. At the limit, whatever I've typed is submitted.
TYPED_TIME_LIMIT_SECONDS = 60
# The Forgotten (0) -> Retained (100) slider, set by Claude or by me, is turned
# into one of FSRS's four grades behind the scenes. Highest band reached wins.
SCORE_TO_RATING = [
    (90, 4),  # 90-100 -> Easy
    (70, 3),  # 70-89  -> Good
    (50, 2),  # 50-69  -> Hard
    (0, 1),   # 0-49   -> Again
]

# --- Reminders (Windows) ---
# When Windows Task Scheduler runs remind.py each day. Re-run setup_reminders.py
# after changing these so the schedule is updated.
REMINDER_TIMES = ["09:00", "14:00", "19:00"]
# Stay quiet unless at least this many cards (due + new allowed today) are waiting.
REMINDER_MIN_CARDS = 1
# A reminder starts the app invisibly so "Study now" works; it quits after this idle time.
IDLE_SHUTDOWN_MINUTES = 120
APP_PORT = 5000

# Outgoing email for reminders. The account and its app password live in .env;
# the address emails are sent TO is set on the app's Settings page.
SMTP_HOST = os.getenv("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.getenv("SMTP_PORT") or 587)

# Which files to import, and the deck name each one gets in the app.
# Paths are relative to CARDS_DIR. Only the newest version of each set is listed;
# older copies and the interactive explainer pages are deliberately left out.
DECKS = [
    ("Corporate Finance: Capital Budgeting", "Corporate Finance/Corp.Fin.Capital.Budgetting.html"),
    ("Fabozzi: Chapters 2-4", "Fabozzi/fabozzi_ch2_ch3_ch4_flashcards.html"),
    ("Fabozzi: Chapters 5-6", "Fabozzi/Fabozzi_Ch5-6_Flashcards.md"),
    ("15.401: Fixed Income", "Finance Theory 15.401/15.401 fixed_income_flashcards.html"),
    ("15.401: Time Value of Money", "Finance Theory 15.401/tvm_flashcards (2).html"),
    ("Statistics: Foundations", "Statistics/stats_flashcards_foundational.html"),
    ("Statistics: Core", "Statistics/stats_flashcards_core.html"),
    ("Statistics: Part Two", "Statistics/stats_flashcards part two.html"),
    ("WSP: DCF", "WSP Quizzes/dcf_flashcards2.0.html"),
    ("WSP: Economic Growth", "WSP Quizzes/econ_growth_flashcards.html"),
    ("WSP: M&A", "WSP Quizzes/ma_flashcards2.0.html"),
    ("WSP: Private Equity & LBOs", "WSP Quizzes/PE_Flashcards_Q1_Q109_v5.html"),
    ("WSP: Trading Comps", "WSP Quizzes/trading_comps_new_cards 2.0.html"),
]

# Cards that only make sense together ("this same bond...", "Same company...").
# The importer merges each group into ONE card with parts a), b), c)...
# Listed by each card's number in the original file, in order.
MERGE_GROUPS = {
    "Fabozzi: Chapters 2-4": [
        ["Q2a", "Q2b", "Q2c"],       # FV, PV, then semiannual FV of the same bond
        ["Q4a", "Q4b"],              # "this instrument"
        ["Q17a", "Q17b", "Q17c"],    # floater / inverse floater built from one collateral
        ["Q18", "Q18b", "Q18c"],     # pricing the inverse floater, then a worked example
        ["Q21a", "Q21b"],            # "Same $400,000... more attractive than 21(a)?"
        ["Q52a", "Q52b", "Q52c"],    # Bond A vs Bond B convexity
    ],
    "15.401: Fixed Income": [
        ["Q13A", "Q13B", "Q13C"],    # 4-yr vs 30-yr bond as rates move
        ["Q22A", "Q22B", "Q22C"],    # spot rates -> YTM (3.66%) -> forward rate
    ],
    "WSP: DCF": [
        ["12A", "12B", "12C", "12D", "12E"],  # one full DCF, step by step
        ["28A", "28B", "28C"],       # "Same company... Adding to Q28B"
        ["60A", "60B"],              # "Same figures", intrinsic vs exit multiple
    ],
    "WSP: Trading Comps": [
        ["26", "27"],                # "From the same live comp set"
    ],
}
