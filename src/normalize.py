# -*- coding: utf-8 -*-
"""
Hebrew address normalization for Bnei Brak delivery routing.

Two rules govern everything here:

1.  The original string is never mutated. Normalization always produces a
    separate field, so a bad rule can be spotted and reverted.
2.  Under-normalizing costs one duplicate stop. Over-normalizing silently
    fuses two distinct streets into one and corrupts the whole route. When a
    rule is not certainly safe, it is not applied -- the address is flagged
    ambiguous instead.

The output of parse_address() is the input to geocoding (physical address
only) and to stop collapsing (street + house number as the merge key). Unit
tokens -- apartment, entrance, floor, PO box -- are split off and carried
along untouched for the printed list.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Optional

# --------------------------------------------------------------------------
# Issue codes -- collected per address, counted in the stage 1 report
# --------------------------------------------------------------------------

ISSUE_EMPTY = "empty_address"
ISSUE_NO_HOUSE_NUMBER = "missing_house_number"
ISSUE_NO_STREET = "missing_street"
ISSUE_HOUSE_RANGE = "house_number_range"
ISSUE_SLASH_AMBIGUOUS = "slash_apartment_ambiguous"
ISSUE_NUMBER_FIRST = "number_before_street"
ISSUE_MULTIPLE_NUMBERS = "multiple_candidate_numbers"
ISSUE_UNPARSED_REMAINDER = "unparsed_remainder"
ISSUE_LATIN_CHARS = "contains_latin_characters"
ISSUE_PO_BOX_ONLY = "po_box_without_street_address"

# --------------------------------------------------------------------------
# Character-level cleaning
# --------------------------------------------------------------------------

# Zero-width, bidi controls, BOM. These are invisible in Excel and will
# happily split two identical addresses into two stops.
_INVISIBLE_CHARS = (
    "​‌‍‎‏"      # ZWSP, ZWNJ, ZWJ, LRM, RLM
    "‪‫‬‭‮"      # LRE, RLE, PDF, LRO, RLO
    "⁦⁧⁨⁩"            # LRI, RLI, FSI, PDI
    "﻿­"                        # BOM, soft hyphen
)
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE_CHARS}]")

# Hebrew points and cantillation marks. Deliberately excludes U+05F3 geresh
# and U+05F4 gershayim, which are punctuation and carry meaning in acronyms.
_NIQQUD_RE = re.compile(r"[֑-ׇֽֿׁׂׅׄ]")

# Every apostrophe-ish and quote-ish glyph collapses to the ASCII form, so
# ז'בוטינסקי and ז׳בוטינסקי and ז’בוטינסקי become one string.
_APOSTROPHE_VARIANTS = "׳‘’ʼʹ´`"
_QUOTE_VARIANTS = "״“”„″ʺ"

_APOSTROPHE_RE = re.compile(f"[{_APOSTROPHE_VARIANTS}]")
_QUOTE_RE = re.compile(f"[{_QUOTE_VARIANTS}]")

# Dash variants -> ASCII hyphen (house ranges: 25–27).
_DASH_RE = re.compile(r"[‐-―−]")

_WHITESPACE_RE = re.compile(r"\s+")

_HEBREW_LETTER = r"א-ת"
_LATIN_RE = re.compile(r"[A-Za-z]")


def clean_text(value: object) -> str:
    """Character-level cleanup only. Does not touch words or word order."""
    if value is None:
        return ""
    text = str(value)
    text = unicodedata.normalize("NFC", text)
    text = _INVISIBLE_RE.sub("", text)
    text = _NIQQUD_RE.sub("", text)
    text = _APOSTROPHE_RE.sub("'", text)
    text = _QUOTE_RE.sub('"', text)
    text = _DASH_RE.sub("-", text)
    # Commas and periods act as separators between address parts; keep the
    # period only where it belongs to an acronym such as ת.ד.
    text = text.replace("،", ",")     # Arabic comma, occasionally pasted
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


# --------------------------------------------------------------------------
# Street prefixes
# --------------------------------------------------------------------------

# Dropped entirely: "רחוב X", "רח' X" and bare "X" are the same street.
_DROPPABLE_PREFIXES = (
    "רחוב",
    "רח'",
    "רח",
)

# Expanded, never dropped. "שדרות ירושלים" is not the same street as a
# hypothetical "ירושלים", and "ר' עקיבא" only means "רבי עקיבא" because the
# honorific is part of the name.
_EXPANDED_PREFIXES = (
    ("שדרות", "שדרות"),
    ("שד'", "שדרות"),
    ("שד", "שדרות"),
    ("רבי", "רבי"),
    ("ר'", "רבי"),
)


# Prefixes stack in real data: "רח' ר' עקיבא" needs the drop and the
# expansion. Iterate until stable, with a hard bound so a self-mapping rule
# (רבי -> רבי) cannot spin.
_MAX_PREFIX_PASSES = 3


def _apply_street_prefix_rules(name: str) -> str:
    result = name

    for _ in range(_MAX_PREFIX_PASSES):
        tokens = result.split()
        if len(tokens) < 2:
            break

        head = tokens[0]
        rewritten = result

        # Only drop when something follows -- a street literally named רחוב
        # does not exist, but an empty result would be worse than a no-op.
        if head in _DROPPABLE_PREFIXES:
            rewritten = " ".join(tokens[1:])
        else:
            for prefix, canonical in _EXPANDED_PREFIXES:
                if head == prefix:
                    rewritten = " ".join([canonical] + tokens[1:])
                    break

        if rewritten == result:
            break
        result = rewritten

    return result


# --------------------------------------------------------------------------
# Street aliases
# --------------------------------------------------------------------------

# Maps a known variant to its canonical street name. Keys are matched after
# clean_text() + prefix rules, so they are written in ASCII-quote form.
#
# This table is intentionally explicit rather than rule-based. A general
# "strip the ה prefix" or "expand any acronym" rule would eventually collapse
# two real streets into one, which is the failure mode with no error message.
_STREET_ALIASES = {
    # רבי עקיבא -- ר' עקיבא is already handled by the prefix expansion, but
    # the acronym forms are not.
    "ר עקיבא": "רבי עקיבא",
    "רע\"ק": "רבי עקיבא",

    # חזון איש
    "חזו\"א": "חזון איש",
    "החזו\"א": "חזון איש",
    "החזון איש": "חזון איש",
    "חזון אי\"ש": "חזון איש",

    # רבי שמעון בר יוחאי
    "רשב\"י": "רבי שמעון בר יוחאי",
    "הרשב\"י": "רבי שמעון בר יוחאי",
    "רבי שמעון בן יוחאי": "רבי שמעון בר יוחאי",
    "שמעון בר יוחאי": "רבי שמעון בר יוחאי",

    # הרב שך
    "ש\"ך": "הרב שך",
    "הש\"ך": "הרב שך",
    "שך": "הרב שך",
    "רב שך": "הרב שך",
    "הרב שך": "הרב שך",
}

# Acronyms are frequently typed without the gershayim (חזוא, רשבי). Only the
# acronym-bearing keys get a quote-stripped twin; plain names are excluded so
# that nothing unexpected joins the table.
_STREET_ALIASES_NO_QUOTES = {
    key.replace('"', "").replace("'", ""): value
    for key, value in _STREET_ALIASES.items()
    if '"' in key or "'" in key
}


def canonicalize_street(raw_street: str) -> tuple[str, bool]:
    """
    Return (canonical_street, was_rewritten).

    Applies character cleaning, prefix rules, then the alias table. Anything
    not in the table is returned as-is: an unknown street name is a normal
    situation, not an error.
    """
    cleaned = clean_text(raw_street)
    if not cleaned:
        return "", False

    original = cleaned
    cleaned = _apply_street_prefix_rules(cleaned)

    if cleaned in _STREET_ALIASES:
        cleaned = _STREET_ALIASES[cleaned]
    else:
        stripped = cleaned.replace('"', "").replace("'", "")
        if stripped in _STREET_ALIASES_NO_QUOTES:
            cleaned = _STREET_ALIASES_NO_QUOTES[stripped]

    return cleaned, cleaned != original


def streets_match(a: str, b: str) -> bool:
    """Canonical-form equality, for arterial lookups and merge checks."""
    return canonicalize_street(a)[0] == canonicalize_street(b)[0]


# --------------------------------------------------------------------------
# Unit identifiers -- apartment / entrance / floor / PO box
# --------------------------------------------------------------------------

# Each pattern captures the value in group "val". Patterns are applied to the
# cleaned string and the matched span is removed from the physical address.
#
# Values may be Hebrew letters (כניסה א) or digits (דירה 12), and may carry a
# trailing geresh (כניסה א').
_UNIT_VALUE = rf"(?:[{_HEBREW_LETTER}]'?|\d+)"

_UNIT_PATTERNS = (
    ("apartment", re.compile(rf"\bדירה\s*(?:מס'?\s*)?(?P<val>{_UNIT_VALUE})")),
    ("apartment", re.compile(rf"\bדיר'\s*(?P<val>{_UNIT_VALUE})")),
    ("apartment", re.compile(rf"\bדר'\s*(?P<val>{_UNIT_VALUE})")),
    ("apartment", re.compile(rf"\bד'\s*(?P<val>\d+)")),
    ("entrance", re.compile(rf"\bכניסה\s*(?P<val>{_UNIT_VALUE})")),
    ("entrance", re.compile(rf"\bכנ'\s*(?P<val>{_UNIT_VALUE})")),
    ("entrance", re.compile(rf"\bפתח\s*(?P<val>{_UNIT_VALUE})")),
    ("floor", re.compile(rf"\bקומה\s*(?P<val>{_UNIT_VALUE})")),
    ("floor", re.compile(rf"\bקומ'\s*(?P<val>{_UNIT_VALUE})")),
    ("floor", re.compile(rf"\bק'\s*(?P<val>\d+)")),
    ("po_box", re.compile(r"\bת\.?\s*ד\.?\s*(?P<val>\d+)")),
    ("po_box", re.compile(r'\bת"ד\s*(?P<val>\d+)')),
)


@dataclass
class UnitInfo:
    apartment: Optional[str] = None
    entrance: Optional[str] = None
    floor: Optional[str] = None
    po_box: Optional[str] = None
    tokens: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.apartment or self.entrance or self.floor or self.po_box)


def extract_unit_tokens(text: str) -> tuple[str, UnitInfo]:
    """
    Split unit identifiers off the physical address.

    Returns (remaining_text, UnitInfo). The remaining text is what gets
    geocoded; the UnitInfo is preserved verbatim for the printed list.
    """
    units = UnitInfo()
    remaining = text

    for slot, pattern in _UNIT_PATTERNS:
        match = pattern.search(remaining)
        if not match:
            continue
        if getattr(units, slot) is not None:
            # Already filled by an earlier pattern; leave the text alone so
            # the leftover shows up as an unparsed remainder rather than
            # vanishing.
            continue
        setattr(units, slot, match.group("val").strip())
        units.tokens.append(match.group(0).strip())
        remaining = (remaining[: match.start()] + " " + remaining[match.end():])

    remaining = _WHITESPACE_RE.sub(" ", remaining).strip(" ,.-")
    return remaining, units


# --------------------------------------------------------------------------
# House number
# --------------------------------------------------------------------------

# "25", "25א", "25 א", "25/3", "25-27", "מס' 25".
# A trailing Hebrew letter is part of the building identity in Bnei Brak
# (25א is a different building from 25) and is kept in the house number.
# It is NOT treated as an entrance -- entrances are written "כניסה א".
_HOUSE_NUMBER_RE = re.compile(
    rf"(?:\bמס'?\s*)?"
    rf"(?P<num>\d{{1,4}})"
    rf"\s*(?P<letter>[{_HEBREW_LETTER}](?![{_HEBREW_LETTER}]))?"
    rf"(?:\s*(?P<sep>[-/])\s*(?P<second>\d{{1,4}}))?"
    rf"\s*$"
)

_LEADING_NUMBER_RE = re.compile(rf"^(?P<num>\d{{1,4}})\s*(?P<letter>[{_HEBREW_LETTER}](?![{_HEBREW_LETTER}]))?\s+")
_ANY_NUMBER_RE = re.compile(r"\d+")


@dataclass
class HouseNumber:
    value: Optional[str] = None          # "25" or "25א"
    number: Optional[int] = None         # 25
    letter: Optional[str] = None         # "א"
    range_end: Optional[int] = None      # 27, when the input said 25-27
    implied_apartment: Optional[str] = None   # the 3 in "25/3"


def extract_house_number(text: str) -> tuple[str, HouseNumber, list[str]]:
    """
    Pull the house number off the end of the address string.

    Returns (street_part, HouseNumber, issues).
    """
    issues: list[str] = []
    house = HouseNumber()

    match = _HOUSE_NUMBER_RE.search(text)
    if match:
        street_part = text[: match.start()].strip(" ,.-")
    else:
        # Fall back to a leading number ("25 רבי עקיבא"), which is unusual in
        # Hebrew addresses and worth flagging.
        match = _LEADING_NUMBER_RE.match(text)
        if match:
            issues.append(ISSUE_NUMBER_FIRST)
            street_part = text[match.end():].strip(" ,.-")
        else:
            return text.strip(" ,.-"), house, [ISSUE_NO_HOUSE_NUMBER]

    house.number = int(match.group("num"))
    house.letter = (match.group("letter") or "").strip() or None
    house.value = f"{house.number}{house.letter}" if house.letter else str(house.number)

    groups = match.groupdict()
    if groups.get("second"):
        if groups.get("sep") == "-":
            # "25-27": a building range. Anchor on the first number and flag.
            house.range_end = int(groups["second"])
            issues.append(ISSUE_HOUSE_RANGE)
        else:
            # "25/3": in Bnei Brak this is almost always house/apartment, but
            # it can also be a sub-building. Take it as an apartment and mark
            # the row ambiguous so it appears in the review artifact.
            house.implied_apartment = groups["second"]
            issues.append(ISSUE_SLASH_AMBIGUOUS)

    # More digits left in the street part means the split is not obvious.
    if _ANY_NUMBER_RE.search(street_part):
        issues.append(ISSUE_MULTIPLE_NUMBERS)

    return street_part, house, issues


# --------------------------------------------------------------------------
# Full address parsing
# --------------------------------------------------------------------------

# Stripped before parsing: the city itself adds nothing and would confuse the
# street/number split if it trailed the number.
_CITY_TOKENS = ("בני ברק", "בנ\"ב", "ב\"ב", "בני-ברק")


def _strip_city(text: str) -> str:
    result = text
    for token in _CITY_TOKENS:
        result = re.sub(rf"(^|[\s,]){re.escape(token)}($|[\s,])", " ", result)
    return _WHITESPACE_RE.sub(" ", result).strip(" ,.-")


@dataclass
class ParsedAddress:
    """One input row's address, split into physical address and unit info."""

    original: str = ""
    cleaned: str = ""

    street_raw: str = ""
    street: str = ""
    house_number: Optional[str] = None
    house_number_int: Optional[int] = None
    house_letter: Optional[str] = None

    apartment: Optional[str] = None
    entrance: Optional[str] = None
    floor: Optional[str] = None
    po_box: Optional[str] = None
    unit_tokens: list[str] = field(default_factory=list)

    # "רבי עקיבא 25" -- the geocoding key and the stop-merge key.
    normalized_address: str = ""

    # True when character-level cleaning altered the raw text (niqqud,
    # invisible marks, punctuation variants, doubled spaces).
    text_cleaned: bool = False
    # True when the street/number text itself was rewritten -- a prefix
    # dropped, an alias resolved, a number reformatted. This is the flag
    # worth reviewing by hand.
    changed_by_normalization: bool = False
    street_alias_applied: bool = False
    ambiguous: bool = False
    issues: list[str] = field(default_factory=list)

    def is_geocodable(self) -> bool:
        return bool(self.street and self.house_number)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_address(
    raw_address: str,
    raw_street: Optional[str] = None,
    raw_house_number: Optional[str] = None,
    raw_apartment: Optional[str] = None,
    raw_entrance: Optional[str] = None,
) -> ParsedAddress:
    """
    Parse one address.

    Works from either a single free-text address column or from separate
    street / house-number / apartment / entrance columns -- whichever the
    confirmed column mapping provides. When both are present, the structured
    columns win and the free-text column is used only to fill gaps.
    """
    parsed = ParsedAddress(original=str(raw_address or ""))

    structured_street = clean_text(raw_street)
    structured_house = clean_text(raw_house_number)

    combined_source = clean_text(raw_address)
    if not combined_source and (structured_street or structured_house):
        combined_source = f"{structured_street} {structured_house}".strip()

    parsed.cleaned = combined_source

    if not combined_source and not structured_street:
        parsed.issues.append(ISSUE_EMPTY)
        parsed.ambiguous = True
        return parsed

    if _LATIN_RE.search(combined_source):
        parsed.issues.append(ISSUE_LATIN_CHARS)

    working = _strip_city(combined_source)
    working, units = extract_unit_tokens(working)

    parsed.apartment = units.apartment
    parsed.entrance = units.entrance
    parsed.floor = units.floor
    parsed.po_box = units.po_box
    parsed.unit_tokens = list(units.tokens)

    # Explicit apartment/entrance columns override anything scraped from the
    # free-text address.
    column_apartment = clean_text(raw_apartment)
    column_entrance = clean_text(raw_entrance)
    if column_apartment:
        parsed.apartment = column_apartment
    if column_entrance:
        parsed.entrance = column_entrance

    if structured_street and structured_house:
        street_part = structured_street
        street_part, house, house_issues = extract_house_number(
            f"{structured_street} {structured_house}"
        )
    else:
        street_part, house, house_issues = extract_house_number(working)

    parsed.issues.extend(house_issues)

    if house.implied_apartment and not parsed.apartment:
        parsed.apartment = house.implied_apartment

    parsed.street_raw = street_part
    canonical, alias_applied = canonicalize_street(street_part)
    parsed.street = canonical
    parsed.street_alias_applied = alias_applied

    parsed.house_number = house.value
    parsed.house_number_int = house.number
    parsed.house_letter = house.letter

    if not parsed.street:
        parsed.issues.append(ISSUE_NO_STREET)
    if not parsed.house_number and ISSUE_NO_HOUSE_NUMBER not in parsed.issues:
        parsed.issues.append(ISSUE_NO_HOUSE_NUMBER)
    if parsed.po_box and not parsed.street:
        parsed.issues.append(ISSUE_PO_BOX_ONLY)

    parsed.normalized_address = (
        f"{parsed.street} {parsed.house_number}".strip()
        if parsed.house_number
        else parsed.street
    )

    pre_canonical = (
        f"{parsed.street_raw} {parsed.house_number}".strip()
        if parsed.house_number
        else parsed.street_raw
    )
    parsed.text_cleaned = parsed.cleaned != str(raw_address or "").strip()
    parsed.changed_by_normalization = parsed.normalized_address != pre_canonical
    parsed.ambiguous = bool(
        {
            ISSUE_EMPTY,
            ISSUE_NO_STREET,
            ISSUE_NO_HOUSE_NUMBER,
            ISSUE_HOUSE_RANGE,
            ISSUE_SLASH_AMBIGUOUS,
            ISSUE_NUMBER_FIRST,
            ISSUE_MULTIPLE_NUMBERS,
            ISSUE_PO_BOX_ONLY,
        }
        & set(parsed.issues)
    )

    return parsed


def merge_key(parsed: ParsedAddress) -> Optional[str]:
    """
    Primary stop-collapsing key: canonical street + house number.

    None when the address is not complete enough to merge on, in which case
    stage 3 must not guess.
    """
    if not parsed.street or not parsed.house_number:
        return None
    return f"{parsed.street}|{parsed.house_number}"
