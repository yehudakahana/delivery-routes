# -*- coding: utf-8 -*-
"""
Stage 1 -- parsing and Hebrew normalization.

Reads the customer workbook, splits every address into a physical address
(street + house number) and unit identifiers (apartment / entrance / floor /
PO box), and reports everything that looked wrong.

Nothing is discarded. Rows that cannot be parsed are written to
malformed_rows.csv and excluded from geocoding, but they stay in the record
so they can be corrected and re-run.

    python -m src.stage1_parse --inspect     show schema + 5 rows, then exit
    python -m src.stage1_parse               parse using the confirmed mapping
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.normalize import (
    ISSUE_EMPTY,
    ISSUE_NO_HOUSE_NUMBER,
    ISSUE_NO_STREET,
    clean_text,
    merge_key,
    parse_address,
)

# Header keywords used to *suggest* a column mapping. Suggestions are never
# applied automatically -- the user confirms them into config.COLUMN_MAPPING.
HEADER_HINTS = {
    "full_address": ("כתובת", "כתובת מלאה", "address"),
    "street": ("רחוב", "רח", "street"),
    "house_number": ("מספר בית", "מס בית", "מספר", "בית", "house"),
    "apartment": ("דירה", "דירות", "apt", "apartment"),
    "entrance": ("כניסה", "entrance"),
    "customer_name": ("שם", "שם לקוח", "לקוח", "name", "customer"),
    "envelope_count": ("מעטפות", "מעטפה", "כמות", "envelopes", "qty"),
}


def locate_input_file() -> Path:
    """Use config.INPUT_FILE, or find exactly one spreadsheet in the root."""
    if config.INPUT_FILE:
        path = Path(config.INPUT_FILE)
        if not path.is_absolute():
            path = config.PROJECT_ROOT / path
        if not path.exists():
            raise SystemExit(f"INPUT_FILE not found: {path}")
        return path

    candidates = [
        p
        for pattern in ("*.xlsx", "*.xls", "*.csv")
        for p in config.PROJECT_ROOT.glob(pattern)
        if not p.name.startswith("~$")
    ]
    if not candidates:
        raise SystemExit("No .xlsx/.xls/.csv found. Set config.INPUT_FILE.")
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise SystemExit(f"Multiple candidate inputs ({names}). Set config.INPUT_FILE.")
    return candidates[0]


def load_frame(path: Path) -> tuple[pd.DataFrame, str]:
    """Detect format and load. Returns (frame, format_label)."""
    suffix = path.suffix.lower()

    if suffix in (".xlsx", ".xls"):
        frame = pd.read_excel(path, dtype=str)
        return frame.fillna(""), f"excel ({suffix})"

    if suffix == ".csv":
        # Hebrew CSVs are exported as UTF-8, UTF-8-BOM, or cp1255.
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "cp1255", "iso-8859-8"):
            try:
                frame = pd.read_csv(path, dtype=str, encoding=encoding)
                return frame.fillna(""), f"csv ({encoding})"
            except (UnicodeDecodeError, pd.errors.ParserError) as error:
                last_error = error
        raise SystemExit(f"Could not decode CSV {path.name}: {last_error}")

    raise SystemExit(f"Unsupported format: {suffix}")


def suggest_mapping(columns: list[str]) -> dict[str, str | None]:
    """Best-guess column mapping, for the user to confirm or correct."""
    suggestion: dict[str, str | None] = {key: None for key in HEADER_HINTS}
    normalized = {column: clean_text(column) for column in columns}

    for role, hints in HEADER_HINTS.items():
        best: tuple[int, str] | None = None
        for column, header in normalized.items():
            for rank, hint in enumerate(hints):
                if header == hint:
                    score = 100 - rank
                elif hint in header:
                    score = 50 - rank
                else:
                    continue
                if best is None or score > best[0]:
                    best = (score, column)
        if best:
            suggestion[role] = best[1]

    # A single free-text address column makes street/house columns redundant;
    # keep both suggestions so the user can see what was found.
    return suggestion


def inspect(path: Path) -> None:
    frame, fmt = load_frame(path)
    print(f"file    : {path.name}")
    print(f"format  : {fmt}")
    print(f"rows    : {len(frame)}")
    print(f"columns : {list(frame.columns)}\n")

    print("-- first 5 rows " + "-" * 50)
    for position, (_, row) in enumerate(frame.head(5).iterrows(), start=1):
        print(f"  [{position}]")
        for column in frame.columns:
            print(f"      {column} = {row[column]!r}")
    print()

    print("-- suggested mapping (CONFIRM BEFORE USE) " + "-" * 24)
    for role, column in suggest_mapping(list(frame.columns)).items():
        print(f"  {role:<16} -> {column}")
    print("\nCopy the confirmed mapping into config.COLUMN_MAPPING, then re-run.")


def mapped(row: pd.Series, role: str) -> str:
    column = config.COLUMN_MAPPING.get(role)
    if not column:
        return ""
    return str(row.get(column, "") or "")


def envelope_count(row: pd.Series) -> tuple[int, bool]:
    """Returns (count, was_defaulted)."""
    column = config.COLUMN_MAPPING.get("envelope_count")
    if not column:
        return config.DEFAULT_ENVELOPES_PER_ROW, True
    raw = clean_text(row.get(column, ""))
    if not raw:
        return config.DEFAULT_ENVELOPES_PER_ROW, True
    try:
        value = int(float(raw))
    except ValueError:
        return config.DEFAULT_ENVELOPES_PER_ROW, True
    return (value, False) if value > 0 else (config.DEFAULT_ENVELOPES_PER_ROW, True)


def parse(path: Path) -> dict:
    if not any(config.COLUMN_MAPPING.get(k) for k in ("full_address", "street")):
        raise SystemExit(
            "config.COLUMN_MAPPING has no address column. Run --inspect and "
            "confirm the mapping before parsing."
        )

    frame, fmt = load_frame(path)
    records: list[dict] = []

    for index, row in frame.iterrows():
        count, defaulted = envelope_count(row)
        parsed = parse_address(
            raw_address=mapped(row, "full_address"),
            raw_street=mapped(row, "street"),
            raw_house_number=mapped(row, "house_number"),
            raw_apartment=mapped(row, "apartment"),
            raw_entrance=mapped(row, "entrance"),
        )
        records.append(
            {
                "row_index": int(index) + 2,   # +2 = 1-based, past the header
                "customer_name": clean_text(mapped(row, "customer_name")),
                "original_address": mapped(row, "full_address")
                or f"{mapped(row, 'street')} {mapped(row, 'house_number')}".strip(),
                "envelope_count": count,
                "envelope_count_defaulted": defaulted,
                "parsed": parsed.to_dict(),
                "merge_key": merge_key(parsed),
                "geocodable": parsed.is_geocodable(),
            }
        )

    return {"source_file": path.name, "format": fmt, "records": records}


def build_report(payload: dict) -> tuple[str, list[dict]]:
    records = payload["records"]
    total = len(records)

    empty = [r for r in records if ISSUE_EMPTY in r["parsed"]["issues"]]
    no_house = [r for r in records if ISSUE_NO_HOUSE_NUMBER in r["parsed"]["issues"]]
    no_street = [r for r in records if ISSUE_NO_STREET in r["parsed"]["issues"]]
    ambiguous = [r for r in records if r["parsed"]["ambiguous"]]
    rewritten = [r for r in records if r["parsed"]["changed_by_normalization"]]
    cleaned_only = [r for r in records if r["parsed"]["text_cleaned"]]
    aliased = [r for r in records if r["parsed"]["street_alias_applied"]]
    malformed = [r for r in records if not r["geocodable"]]

    # Exact duplicates: same customer and same raw address text.
    exact_seen: Counter = Counter(
        (r["customer_name"], clean_text(r["original_address"])) for r in records
    )
    exact_duplicates = [r for r in records
                        if exact_seen[(r["customer_name"],
                                       clean_text(r["original_address"]))] > 1]

    # Duplicate normalized addresses -- these are the future merges, and are
    # expected rather than alarming.
    normalized_counts: Counter = Counter(
        r["merge_key"] for r in records if r["merge_key"]
    )
    duplicate_normalized = sum(c for c in normalized_counts.values() if c > 1)

    issue_counts: Counter = Counter(
        issue for r in records for issue in r["parsed"]["issues"]
    )

    lines = [
        "=" * 64,
        "שלב 1 - ניתוח כתובות ונרמול עברית",
        "=" * 64,
        f"קובץ מקור                : {payload['source_file']} ({payload['format']})",
        f"סה\"כ שורות               : {total}",
        f"כתובות ריקות             : {len(empty)}",
        f"חסר מספר בית             : {len(no_house)}",
        f"חסר שם רחוב              : {len(no_street)}",
        f"כפילויות מדויקות         : {len(exact_duplicates)}",
        f"שורות בכתובת מנורמלת כפולה: {duplicate_normalized}",
        f"כתובות פגומות (לא לגיאוקוד): {len(malformed)}",
        f"שורות שהנרמול שינה בהן טקסט: {len(rewritten)}",
        f"שורות שנוקו ברמת תווים    : {len(cleaned_only)}",
        f"שורות עם כינוי רחוב שהוחלף: {len(aliased)}",
        f"שורות עם פרשנות מעורפלת   : {len(ambiguous)}",
        f"כתובות ייחודיות לגיאוקוד  : {len(normalized_counts)}",
        "",
        "פירוט בעיות:",
    ]
    for issue, count in issue_counts.most_common():
        lines.append(f"   {issue:<32} {count}")

    if aliased:
        lines += ["", "כינויי רחוב שהוחלפו:"]
        for record in aliased:
            lines.append(
                f"   שורה {record['row_index']:>3}: "
                f"{record['original_address']}  ->  "
                f"{record['parsed']['normalized_address']}"
            )

    if ambiguous:
        lines += ["", "שורות מעורפלות (דורשות עין אנושית):"]
        for record in ambiguous:
            lines.append(
                f"   שורה {record['row_index']:>3}: "
                f"{record['original_address']!r} "
                f"[{', '.join(record['parsed']['issues'])}]"
            )

    lines.append("=" * 64)
    return "\n".join(lines), malformed


def write_malformed(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding=config.CSV_ENCODING, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["row_index", "customer_name", "original_address",
             "parsed_street", "parsed_house_number", "issues"]
        )
        for record in rows:
            writer.writerow(
                [
                    record["row_index"],
                    record["customer_name"],
                    record["original_address"],
                    record["parsed"]["street"],
                    record["parsed"]["house_number"] or "",
                    "; ".join(record["parsed"]["issues"]),
                ]
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true",
                        help="show schema and 5 sample rows, then exit")
    args = parser.parse_args()

    path = locate_input_file()
    if args.inspect:
        inspect(path)
        return

    config.OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)

    payload = parse(path)
    report, malformed = build_report(payload)

    config.STAGE1_PARSED.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_malformed(malformed, config.STAGE1_MALFORMED)
    config.STAGE1_REPORT.write_text(report, encoding="utf-8")

    print(report)
    print(f"\nwrote {config.STAGE1_PARSED.name}, {config.STAGE1_MALFORMED.name}")


if __name__ == "__main__":
    main()
