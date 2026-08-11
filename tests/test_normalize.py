# -*- coding: utf-8 -*-
"""
Self-checks for src/normalize.py.

Uses synthetic addresses only -- no customer data. Run with:
    python tests/test_normalize.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.normalize import (  # noqa: E402
    ISSUE_HOUSE_RANGE,
    ISSUE_NO_HOUSE_NUMBER,
    ISSUE_SLASH_AMBIGUOUS,
    canonicalize_street,
    clean_text,
    extract_unit_tokens,
    merge_key,
    parse_address,
)

failures = []


def check(label, actual, expected):
    if actual != expected:
        failures.append(f"{label}\n     got: {actual!r}\n  wanted: {expected!r}")


# -- character cleaning ----------------------------------------------------

check("niqqud stripped", clean_text("רַבִּי עֲקִיבָא"), "רבי עקיבא")
check("gershayim -> ascii", clean_text('חזו״א'), 'חזו"א')
check("geresh -> ascii", clean_text("ז׳בוטינסקי"), "ז'בוטינסקי")
check("RLM stripped", clean_text("‏רבי עקיבא‎"), "רבי עקיבא")
check("doubled spaces", clean_text("  רבי   עקיבא  "), "רבי עקיבא")
check("en dash -> hyphen", clean_text("25–27"), "25-27")

# -- street canonicalization ----------------------------------------------

check("drop רחוב", canonicalize_street("רחוב חזון איש")[0], "חזון איש")
check("drop רח'", canonicalize_street("רח' הרב שך")[0], "הרב שך")
check("expand ר'", canonicalize_street("ר' עקיבא")[0], "רבי עקיבא")
check("keep רבי", canonicalize_street("רבי עקיבא")[0], "רבי עקיבא")
check("expand שד'", canonicalize_street("שד' ירושלים")[0], "שדרות ירושלים")
check("alias חזו\"א", canonicalize_street('חזו"א')[0], "חזון איש")
check("alias החזו\"א", canonicalize_street('החזו"א')[0], "חזון איש")
check("alias unquoted חזוא", canonicalize_street("חזוא")[0], "חזון איש")
check("alias רשב\"י", canonicalize_street('רשב"י')[0], "רבי שמעון בר יוחאי")
check("alias ש\"ך", canonicalize_street('ש"ך')[0], "הרב שך")
check("alias הרב שך", canonicalize_street("רח' ש\"ך")[0], "הרב שך")

# -- must NOT over-normalize ----------------------------------------------

check("distinct streets kept apart",
      canonicalize_street("הרב קוק")[0] == canonicalize_street("הרב שך")[0],
      False)
check("unknown street passes through", canonicalize_street("אבן גבירול")[0], "אבן גבירול")
check("שדרות not dropped", canonicalize_street("שדרות ירושלים")[0] == "ירושלים", False)

# -- unit tokens -----------------------------------------------------------

remaining, units = extract_unit_tokens("רבי עקיבא 25 דירה 5 כניסה א קומה 3")
check("units removed from address", remaining, "רבי עקיבא 25")
check("apartment", units.apartment, "5")
check("entrance", units.entrance, "א")
check("floor", units.floor, "3")

remaining, units = extract_unit_tokens('חזון איש 12 ת.ד. 401')
check("po box removed", remaining, "חזון איש 12")
check("po box value", units.po_box, "401")

# -- full parsing ----------------------------------------------------------

p = parse_address("רח' רבי עקיבא 25, דירה 5, כניסה א, בני ברק")
check("street", p.street, "רבי עקיבא")
check("house", p.house_number, "25")
check("apartment", p.apartment, "5")
check("entrance", p.entrance, "א")
check("normalized", p.normalized_address, "רבי עקיבא 25")
check("merge key", merge_key(p), "רבי עקיבא|25")

# Spelling variants of one building must produce one merge key.
variants = [
    "רבי עקיבא 25",
    "ר' עקיבא 25",
    "רחוב רבי עקיבא 25",
    "רח' ר' עקיבא 25 דירה 8",
    "‏רבי  עקיבא 25‎",
]
keys = {merge_key(parse_address(v)) for v in variants}
check("variants collapse to one key", keys, {"רבי עקיבא|25"})

# Building letter suffix is part of the building, not an entrance.
p = parse_address('חזו"א 12א')
check("letter suffix street", p.street, "חזון איש")
check("letter suffix house", p.house_number, "12א")
check("letter suffix is not entrance", p.entrance, None)
check("12א distinct from 12", merge_key(p) == merge_key(parse_address('חזו"א 12')), False)

# Slash form: taken as apartment, flagged ambiguous.
p = parse_address("הרב שך 40/12")
check("slash house", p.house_number, "40")
check("slash apartment", p.apartment, "12")
check("slash flagged", ISSUE_SLASH_AMBIGUOUS in p.issues, True)

# Range form: anchored on the first number, flagged.
p = parse_address("רשב\"י 8-10")
check("range street", p.street, "רבי שמעון בר יוחאי")
check("range house", p.house_number, "8")
check("range flagged", ISSUE_HOUSE_RANGE in p.issues, True)

# Missing house number: flagged, not guessed.
p = parse_address("ז'בוטינסקי")
check("no house number flagged", ISSUE_NO_HOUSE_NUMBER in p.issues, True)
check("no house number is not geocodable", p.is_geocodable(), False)
check("no merge key without house number", merge_key(p), None)

# Empty input survives.
p = parse_address("")
check("empty is not geocodable", p.is_geocodable(), False)

# Structured columns.
p = parse_address("", raw_street="שד' ירושלים", raw_house_number="14",
                  raw_apartment="7", raw_entrance="ב")
check("structured street", p.street, "שדרות ירושלים")
check("structured house", p.house_number, "14")
check("structured apartment", p.apartment, "7")
check("structured entrance", p.entrance, "ב")

# -- report ----------------------------------------------------------------

if failures:
    print(f"FAILED: {len(failures)}\n")
    for f in failures:
        print("  - " + f)
    sys.exit(1)

print("all normalization checks passed")
