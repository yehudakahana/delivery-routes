# -*- coding: utf-8 -*-
"""
Stage 4 -- travel-time matrix from OSRM.

Straight-line distance is never used, here or anywhere downstream. Bnei Brak
is a grid of one-way streets: two buildings 40 m apart can be a four-minute
drive, and a Euclidean matrix produces a route that cannot physically be
driven.

The matrix is asymmetric and stays that way. A->B and B->A are different
numbers because they are different drives. It is never symmetrized, never
averaged, and missing entries are never backfilled with anything -- an
unreachable pair is reported as unreachable.

    python -m src.stage4_matrix                 local OSRM (production)
    python -m src.stage4_matrix --demo-host     public OSRM (synthetic data only)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config


def coordinate_string(points: list[tuple[float, float]]) -> str:
    """OSRM wants lng,lat -- the reverse of the usual order."""
    return ";".join(f"{lng},{lat}" for lat, lng in points)


def osrm_table(
    host: str,
    sources: list[tuple[float, float]],
    destinations: list[tuple[float, float]],
) -> list[list[float | None]]:
    """One /table call. Returns durations in seconds, None where unreachable."""
    points = sources + destinations
    source_indices = list(range(len(sources)))
    destination_indices = list(range(len(sources), len(points)))

    url = f"{host}/table/v1/{config.OSRM_PROFILE}/{coordinate_string(points)}"
    response = requests.get(
        url,
        params={
            "annotations": "duration",
            "sources": ";".join(map(str, source_indices)),
            "destinations": ";".join(map(str, destination_indices)),
        },
        timeout=config.OSRM_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("code") != "Ok":
        raise SystemExit(f"OSRM /table failed: {payload.get('code')} "
                         f"{payload.get('message', '')}")

    return payload["durations"]


def build_matrix(host: str, points: list[tuple[float, float]]) -> list[list[float | None]]:
    size = len(points)
    matrix: list[list[float | None]] = [[None] * size for _ in range(size)]
    chunk = config.OSRM_TABLE_CHUNK_SIZE

    for source_start in range(0, size, chunk):
        source_slice = slice(source_start, min(source_start + chunk, size))
        for dest_start in range(0, size, chunk):
            dest_slice = slice(dest_start, min(dest_start + chunk, size))

            block = osrm_table(host, points[source_slice], points[dest_slice])
            for local_row, row in enumerate(block):
                for local_col, value in enumerate(row):
                    matrix[source_start + local_row][dest_start + local_col] = value

            time.sleep(config.OSRM_MIN_INTERVAL_SECONDS)

    return matrix


def fetch_route_geometry(host: str, points: list[tuple[float, float]]) -> list[list[float]]:
    """
    Real road geometry along the ordered points, stitched from chunked /route
    calls. Returns [[lat, lng], ...]. Used for drawing, never for costing.
    """
    geometry: list[list[float]] = []
    chunk = config.OSRM_ROUTE_CHUNK_SIZE
    start = 0

    while start < len(points) - 1:
        end = min(start + chunk, len(points))
        segment = points[start:end]

        url = f"{host}/route/v1/{config.OSRM_PROFILE}/{coordinate_string(segment)}"
        response = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson", "continue_straight": "false"},
            timeout=config.OSRM_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("code") == "Ok" and payload.get("routes"):
            coordinates = payload["routes"][0]["geometry"]["coordinates"]
            piece = [[lat, lng] for lng, lat in coordinates]
            # Drop the duplicated joint between consecutive chunks.
            geometry.extend(piece[1:] if geometry else piece)
        else:
            print(f"  ! /route failed for segment {start}-{end}: {payload.get('code')}")

        # Overlap by one so consecutive chunks join at a shared waypoint.
        start = end - 1
        time.sleep(config.OSRM_MIN_INTERVAL_SECONDS)

    return geometry


def verify(matrix: list[list[float | None]], size: int) -> tuple[list[str], dict]:
    """Structural checks. Returns (problems, stats)."""
    problems: list[str] = []

    if len(matrix) != size or any(len(row) != size for row in matrix):
        problems.append(f"matrix is not {size}x{size}")

    bad_diagonal = [i for i in range(size) if matrix[i][i] not in (0, 0.0)]
    if bad_diagonal:
        problems.append(f"non-zero diagonal at rows {bad_diagonal[:5]}")

    unreachable = [
        (i, j)
        for i in range(size)
        for j in range(size)
        if i != j and matrix[i][j] is None
    ]

    asymmetric_pairs = 0
    max_asymmetry = 0.0
    for i in range(size):
        for j in range(i + 1, size):
            forward, backward = matrix[i][j], matrix[j][i]
            if forward is None or backward is None:
                continue
            if forward != backward:
                asymmetric_pairs += 1
                max_asymmetry = max(max_asymmetry, abs(forward - backward))

    if config.ASSERT_MATRIX_ASYMMETRY and size > 2 and asymmetric_pairs == 0:
        # A perfectly symmetric matrix over a one-way grid means the profile
        # ignored direction -- the route would be undrivable.
        problems.append("matrix is fully symmetric; one-way streets were ignored")

    return problems, {
        "unreachable_pairs": len(unreachable),
        "unreachable_examples": unreachable[:10],
        "asymmetric_pairs": asymmetric_pairs,
        "max_asymmetry_seconds": round(max_asymmetry, 1),
        "total_pairs": size * (size - 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-host", action="store_true",
                        help="use the public OSRM instance (synthetic data only)")
    args = parser.parse_args()

    if not config.STAGE3_STOPS.exists():
        raise SystemExit("Run stage 3 first.")

    stops = json.loads(config.STAGE3_STOPS.read_text(encoding="utf-8"))["stops"]
    points = [(s["lat"], s["lng"]) for s in stops]

    host = config.OSRM_DEMO_HOST if args.demo_host else config.OSRM_HOST
    if args.demo_host:
        print("!! PUBLIC OSRM HOST. Synthetic data only -- never real customers. !!\n")

    print(f"building {len(points)}x{len(points)} matrix via {host}")
    matrix = build_matrix(host, points)

    problems, stats = verify(matrix, len(points))

    print(f"  asymmetric pairs   : {stats['asymmetric_pairs']}/"
          f"{stats['total_pairs'] // 2}")
    print(f"  max A->B vs B->A   : {stats['max_asymmetry_seconds']}s")
    print(f"  unreachable pairs  : {stats['unreachable_pairs']}")

    if problems:
        print("\n*** MATRIX VERIFICATION FAILED ***")
        for problem in problems:
            print(f"    - {problem}")
        raise SystemExit(1)

    config.CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    config.STAGE4_MATRIX.write_text(
        json.dumps(
            {
                "host": host,
                "profile": config.OSRM_PROFILE,
                "stop_count": len(points),
                # Identifies the stop set this matrix belongs to, so a stale
                # matrix cannot be silently reused after stage 3 changes.
                "stop_ids": [s["stop_id"] for s in stops],
                "stop_addresses": [s["normalized_address"] for s in stops],
                "units": "seconds",
                "stats": stats,
                "durations": matrix,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {config.STAGE4_MATRIX.name}")


if __name__ == "__main__":
    main()
