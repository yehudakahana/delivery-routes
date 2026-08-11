# -*- coding: utf-8 -*-
"""
Stage 3 -- collapse customers into physical stops.

One building = one stop = one minute of service time, however many envelopes
go into the lobby mailboxes. The merge is what turns 450 customers into ~280
stops, and getting it wrong is the quietest way to ruin the route: under-merge
and the driver visits the same lobby four times; over-merge and four buildings
collapse into one coordinate that is wrong for three of them.

Primary key is the normalized street + house number. Proximity merging is a
fallback for spelling variants only, and is refused unless the two addresses
plausibly denote the same building -- neighbouring buildings sit comfortably
inside 15 m, so distance alone proves nothing.

    python -m src.stage3_collapse
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.stage2_geocode import haversine_meters


def load_inputs() -> tuple[dict, dict]:
    if not config.STAGE1_PARSED.exists():
        raise SystemExit("Run stage 1 first.")
    if not config.STAGE2_RESULTS.exists():
        raise SystemExit("Run stage 2 first.")

    parsed = json.loads(config.STAGE1_PARSED.read_text(encoding="utf-8"))
    geocode = json.loads(config.STAGE2_RESULTS.read_text(encoding="utf-8"))
    return parsed, geocode["results"]


def plausibly_same_building(a: dict, b: dict) -> tuple[bool, str]:
    """
    Gate for proximity merging.

    Requires the same house number and street names that are variants of one
    another -- one a token-subset of the other, or a strong token overlap.
    "כהנמן 34" and "רבי יהושע כהנמן 34" pass. "נחמיה 21" and "עזרא 21" do not,
    however close their coordinates happen to be.
    """
    if a["house_number"] != b["house_number"]:
        return False, "different_house_number"

    tokens_a = set(a["street"].split())
    tokens_b = set(b["street"].split())
    if not tokens_a or not tokens_b:
        return False, "empty_street"

    if tokens_a <= tokens_b or tokens_b <= tokens_a:
        return True, "street_token_subset"

    overlap = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    if overlap >= 0.5:
        return True, "street_token_overlap"

    return False, "streets_unrelated"


def build_stops(parsed: dict, geocode: dict) -> tuple[list[dict], list[dict]]:
    """Returns (stops, excluded_records)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    excluded: list[dict] = []

    for record in parsed["records"]:
        if not record["geocodable"]:
            excluded.append({**record, "exclusion_reason": "not_parseable"})
            continue

        address = record["parsed"]["normalized_address"]
        result = geocode.get(address)
        if not result:
            excluded.append({**record, "exclusion_reason": "geocode_rejected_or_failed"})
            continue

        groups[record["merge_key"]].append(record)

    stops: list[dict] = []
    for merge_key, records in groups.items():
        first = records[0]["parsed"]
        address = first["normalized_address"]
        result = geocode[address]

        stops.append(
            {
                "stop_id": None,
                "normalized_address": address,
                "merge_keys": [merge_key],
                "original_addresses": sorted({r["original_address"] for r in records}),
                "lat": result["lat"],
                "lng": result["lng"],
                "street": first["street"],
                "house_number": first["house_number"],
                "customers": [
                    {
                        "name": r["customer_name"],
                        "apartment": r["parsed"]["apartment"],
                        "entrance": r["parsed"]["entrance"],
                        "floor": r["parsed"]["floor"],
                        "envelope_count": r["envelope_count"],
                        "row_index": r["row_index"],
                        "original_address": r["original_address"],
                    }
                    for r in records
                ],
                "envelope_count": sum(r["envelope_count"] for r in records),
                "merge_method": "normalized_address",
                "geocode_location_type": result.get("location_type"),
                "partial_match": result.get("partial_match", False),
                "validation": result.get("validation"),
                "flags": [],
            }
        )

    stops = apply_proximity_merges(stops)

    for index, stop in enumerate(sorted(stops, key=lambda s: (s["street"], s["house_number"])), start=1):
        stop["stop_id"] = index

    return sorted(stops, key=lambda s: s["stop_id"]), excluded


def apply_proximity_merges(stops: list[dict]) -> list[dict]:
    """Second pass: fold spelling variants of the same building together."""
    merged: list[dict] = []
    consumed: set[int] = set()

    for i, stop in enumerate(stops):
        if i in consumed:
            continue

        current = stop
        for j in range(i + 1, len(stops)):
            if j in consumed:
                continue
            other = stops[j]

            distance = haversine_meters(
                (current["lat"], current["lng"]), (other["lat"], other["lng"])
            )
            if distance > config.STOP_MERGE_RADIUS_METERS:
                continue

            ok, reason = plausibly_same_building(current, other)
            if not ok:
                # Close but not the same building. Record it so the pair can
                # be eyeballed, and keep them separate.
                current["flags"].append(
                    f"near_neighbour_not_merged:{other['normalized_address']}"
                    f"@{distance:.0f}m:{reason}"
                )
                continue

            current = fold(current, other, distance, reason)
            consumed.add(j)

        merged.append(current)

    return merged


def fold(target: dict, other: dict, distance: float, reason: str) -> dict:
    """Merge `other` into `target`, losing no customer information."""
    target = dict(target)
    target["customers"] = target["customers"] + other["customers"]
    target["envelope_count"] += other["envelope_count"]
    target["original_addresses"] = sorted(
        set(target["original_addresses"]) | set(other["original_addresses"])
    )
    target["merge_keys"] = sorted(set(target["merge_keys"]) | set(other["merge_keys"]))
    target["merge_method"] = "proximity"
    target["flags"] = target["flags"] + [
        f"proximity_merged:{other['normalized_address']}@{distance:.0f}m:{reason}"
    ]
    if other["street"] != target["street"]:
        target["flags"].append(f"merged_street_variant:{other['street']}")
    return target


def flag_anomalies(stops: list[dict]) -> None:
    for stop in stops:
        if len(stop["customers"]) > config.SUSPICIOUS_CUSTOMERS_PER_STOP:
            stop["flags"].append(f"high_customer_count:{len(stop['customers'])}")
        if stop["partial_match"]:
            stop["flags"].append("geocode_partial_match")
        if stop["geocode_location_type"] in config.GEOCODE_SUSPICIOUS_LOCATION_TYPES:
            stop["flags"].append("geocode_geometric_center")
        house_numbers = {key.split("|")[1] for key in stop["merge_keys"]}
        if len(house_numbers) > 1:
            stop["flags"].append(f"spans_house_numbers:{sorted(house_numbers)}")


def build_report(stops: list[dict], parsed: dict, excluded: list[dict]) -> tuple[str, bool]:
    total_rows = len(parsed["records"])
    routed_rows = sum(len(s["customers"]) for s in stops)
    envelopes = sum(s["envelope_count"] for s in stops)
    counts = [s["envelope_count"] for s in stops]
    distribution = Counter(counts)

    proximity_merges = sum(1 for s in stops if s["merge_method"] == "proximity")
    proximity_percent = (proximity_merges / len(stops) * 100) if stops else 0.0
    ratio = len(stops) / routed_rows if routed_rows else 0.0

    low, high = config.EXPECTED_STOP_RATIO_RANGE
    ratio_checked = routed_rows >= config.STOP_RATIO_CHECK_MIN_ROWS
    ratio_ok = low <= ratio <= high

    must_stop = proximity_percent > config.MAX_PROXIMITY_MERGE_PERCENT or (
        ratio_checked and not ratio_ok
    )

    lines = [
        "=" * 64,
        "שלב 3 - איחוד לנקודות עצירה פיזיות",
        "=" * 64,
        f"שורות בקובץ המקור        : {total_rows}",
        f"לקוחות בניתוב            : {routed_rows}",
        f"שורות שהוצאו             : {len(excluded)}",
        f"נקודות עצירה             : {len(stops)}",
        f"סה\"כ מעטפות              : {envelopes}",
        f"יחס נקודות/לקוחות        : {ratio:.2f}  (צפוי {low}-{high}"
        + ("" if ratio_checked else ", לא נבדק - מדגם קטן") + ")",
        f"איחוד לפי כתובת מנורמלת  : {len(stops) - proximity_merges}",
        f"איחוד לפי קרבה גיאוגרפית : {proximity_merges} ({proximity_percent:.1f}%)",
        "",
        f"ממוצע מעטפות לנקודה      : {mean(counts):.2f}" if counts else "",
        f"חציון מעטפות לנקודה      : {median(counts):.1f}" if counts else "",
        "",
        "התפלגות מעטפות לנקודה:",
    ]
    for envelope_count in sorted(distribution):
        bar = "#" * distribution[envelope_count]
        lines.append(f"   {envelope_count:>2} מעטפות : {distribution[envelope_count]:>3}  {bar}")

    lines += ["", "בניינים צפופים ביותר:"]
    for stop in sorted(stops, key=lambda s: -s["envelope_count"])[:10]:
        lines.append(
            f"   {stop['envelope_count']:>2} מעטפות  "
            f"{stop['normalized_address']:<26} "
            f"({len(stop['customers'])} לקוחות)"
        )

    flagged = [s for s in stops if s["flags"]]
    if flagged:
        lines += ["", "נקודות מסומנות לבדיקה:"]
        for stop in flagged:
            lines.append(f"   {stop['normalized_address']}")
            for flag in stop["flags"]:
                lines.append(f"      - {flag}")

    if excluded:
        lines += ["", "שורות שלא נכנסו לניתוב:"]
        for record in excluded:
            lines.append(
                f"   שורה {record['row_index']:>3}: "
                f"{record['original_address']!r} [{record['exclusion_reason']}]"
            )

    if must_stop:
        lines += ["", "*** עצור: תוצאות האיחוד חשודות ***"]
        if proximity_percent > config.MAX_PROXIMITY_MERGE_PERCENT:
            lines.append(f"    איחוד לפי קרבה חורג מהמותר "
                         f"({proximity_percent:.1f}% > {config.MAX_PROXIMITY_MERGE_PERCENT}%)")
        if ratio_checked and not ratio_ok:
            lines.append(f"    יחס נקודות/לקוחות חורג מהטווח הצפוי ({ratio:.2f})")

    lines.append("=" * 64)
    return "\n".join(line for line in lines if line != ""), must_stop


def main() -> None:
    parsed, geocode = load_inputs()
    stops, excluded = build_stops(parsed, geocode)
    flag_anomalies(stops)

    config.OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    config.STAGE3_STOPS.write_text(
        json.dumps({"stops": stops, "excluded": excluded}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report, must_stop = build_report(stops, parsed, excluded)
    config.STAGE3_REPORT.write_text(report, encoding="utf-8")
    print(report)

    if must_stop:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
