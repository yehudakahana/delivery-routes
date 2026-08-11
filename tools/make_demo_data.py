# -*- coding: utf-8 -*-
"""
Generate a synthetic demo workbook: 30 fictional customers in Bnei Brak.

NOT REAL DATA. Names are invented, addresses are real Bnei Brak streets with
arbitrary house numbers. The point is to exercise the pipeline end to end,
including the cases that break it:

  - the same building written several different ways
  - dropped/expanded street prefixes and acronym aliases
  - unit tokens mixed into a free-text address column
  - a slash form, a house range, a missing house number, a blank row
  - an exact duplicate row
  - stray niqqud, gershayim, and an invisible RTL mark

Output columns are Hebrew, mirroring what a real customer export looks like.

Run:  python tools/make_demo_data.py
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUTPUT = Path(__file__).resolve().parent.parent / "demo_addresses.xlsx"

# (customer, free-text address, apartment, entrance, envelopes)
ROWS = [
    # --- one building, four customers: the core collapsing case -------------
    ("אברהם כהן",        "רח' רבי עקיבא 25", "5", "א", 1),
    ("שרה לוי",          "רבי עקיבא 25", "12", "א", 1),
    ("יוסף פרידמן",      "ר' עקיבא 25 דירה 3", "", "ב", 1),
    ("מרים גולדשטיין",   "רחוב ר' עקיבא 25, קומה 4", "9", "ב", 1),

    # --- acronym aliases: all three are חזון איש 12 -------------------------
    ("דוד רוזנברג",      'חזו"א 12', "7", "", 1),
    ("רחל וייס",         'החזו"א 12 דירה 2', "", "", 1),
    ("שמעון ברגר",       "חזון איש 12", "15", "ג", 1),

    # --- 12א is a DIFFERENT building from 12 -------------------------------
    ("אסתר מנדלסון",     'חזו"א 12א', "4", "", 1),

    # --- רשב"י alias, plus a house range -----------------------------------
    ("יעקב שטרן",        'רשב"י 8', "3", "א", 1),
    ("חנה אדלר",         "רבי שמעון בר יוחאי 8 כניסה ב קומה 2", "6", "", 1),
    ("נתן הורוביץ",      'רשב"י 14-16', "2", "", 1),

    # --- ש"ך alias, plus the slash form ------------------------------------
    ("מלכה זילברמן",     'רח\' ש"ך 40/12', "", "", 1),
    ("אליעזר קליין",     "הרב שך 40 דירה 8", "", "א", 1),
    ("ברוך שפירא",       "הרב שך 22", "1", "", 1),

    # --- arterial street ----------------------------------------------------
    ("טובה גרינברג",     "ז'בוטינסקי 61", "10", "ב", 1),
    ("משה אקשטיין",      "ז׳בוטינסקי 61 דירה 14", "", "ב", 1),
    ("פנינה רוט",        "ז'בוטינסקי 88", "5", "", 1),

    # --- quiet interior streets --------------------------------------------
    ("שלמה נוימן",       "רבי יהושע כהנמן 34", "8", "א", 1),
    ("רבקה בלוי",        "כהנמן 34 קומה 3", "11", "א", 1),
    ("יצחק דויטש",       "אהרונוביץ 17", "2", "", 1),
    ("לאה פישר",         "אהרונוביץ 17 דירה 9", "", "ב", 1),
    ("מנחם הרשקוביץ",    "השומר 5", "3", "", 1),
    ("דבורה לנדאו",      "רחוב חפץ חיים 29", "7", "א", 1),
    ("אהרן שוורץ",       "אבני נזר 6", "4", "", 1),
    ("גיטל וסרמן",       "נחמיה 21 כניסה א", "5", "", 1),
    ("ישראל פרנקל",      "הרב קוק 12", "6", "ב", 1),
    ("חיים באומגרטן",    "סוקולוב 44", "2", "", 1),

    # --- messy rows: niqqud, invisible RTL mark, exact duplicate ------------
    ("צירל רייזמן",      "רַבִּי עֲקִיבָא 88", "13", "ג", 1),
    ("יהודה אשכנזי",     "‏הרב קוק 12‎", "10", "ב", 1),
    ("חיים באומגרטן",    "סוקולוב 44", "2", "", 1),   # exact duplicate

    # --- rows that must NOT be silently dropped ----------------------------
    ("זלמן טאובר",       "ז'בוטינסקי", "3", "", 1),    # no house number
    ("", "", "", "", 1),                               # blank row
]

COLUMNS = {
    "customer": "שם לקוח",
    "address": "כתובת",
    "apartment": "דירה",
    "entrance": "כניסה",
    "envelopes": "מעטפות",
}


def main() -> None:
    frame = pd.DataFrame(
        [
            {
                COLUMNS["customer"]: name,
                COLUMNS["address"]: address,
                COLUMNS["apartment"]: apartment,
                COLUMNS["entrance"]: entrance,
                COLUMNS["envelopes"]: envelopes,
            }
            for name, address, apartment, entrance, envelopes in ROWS
        ]
    )
    frame.to_excel(OUTPUT, index=False)
    print(f"wrote {OUTPUT.name}: {len(frame)} rows, columns = {list(frame.columns)}")


if __name__ == "__main__":
    main()
