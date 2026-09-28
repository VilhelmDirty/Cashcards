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
