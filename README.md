# Prefix-Optimal Delivery Routing

Order a set of delivery stops so that **every prefix of the route is worth as
much as possible** — for anyone who will run out of time before running out of
stops.

The driver works one shift, drives down the list, and stops when the shift
ends. Whether that is at stop 180 or stop 340 is not known in advance and does
not matter. What matters is that the stops already visited are the most
valuable ones that could have been reached in that time.

## Why this is not a TSP

A travelling-salesman solver minimizes the time to finish **all** stops. It
produces an efficient loop and says nothing about the order value arrives in.
Drive half of an optimal TSP tour and you may have collected half the value —
or a quarter.

This solves the **weighted minimum latency problem** (weighted travelling
repairman) instead:

```
minimize   Σ  weight[i] × arrival_time[i]
           i
```

Weighting by `weight[i]` (here: envelopes per building) pulls high-value stops
toward the front. The cumulative-arrival term keeps the route geographically
coherent, so it does not teleport between distant valuable stops. Every prefix
gets as good as it can be.

`OR-Tools' RoutingModel cannot express this objective.`
`SetSpanCostCoefficient` minimizes route *span*, which is a different thing.
So OR-Tools builds a seed tour, and a local search then optimizes the real
objective directly — Or-opt moves, accepted only when weighted latency drops.

## Pipeline

Six stages, each reading the previous stage's artifact and writing its own, so
any stage can be re-run alone.

| Stage | Does | Writes |
|---|---|---|
| 1 | Parse input, normalize addresses, split off unit identifiers | `parsed_addresses.json`, `malformed_rows.csv` |
| 2 | Geocode, validate, cache | `geocode_results.json`, `geocoding_review.json` |
| 3 | Collapse customers into physical stops | `stops.json` |
| 4 | Asymmetric travel-time matrix from OSRM | `travel_time_matrix.json` |
| 5 | Order by weighted latency + baseline comparison | `route_order.json` |
| 6 | Map, printable list, navigation links, CSV | `final_route.html`, … |

Stages 1–3 are the ones that fail silently, so each stops and reports rather
than guessing. The worst outcome is not a crash — it is a clean-looking route
built on bad address grouping.

## Quick start

```bash
pip install pandas openpyxl requests ortools
python tools/fetch_leaflet.py          # vendor Leaflet for offline maps

python -m src.stage1_parse --inspect   # show schema, confirm column mapping
# put the confirmed mapping into config.COLUMN_MAPPING, then:
python -m src.stage1_parse
python -m src.stage2_geocode
python -m src.stage3_collapse
python -m src.stage4_matrix
python -m src.stage5_order
python -m src.stage6_output
```

Add `--demo-host` to stages 4 and 6 to use the public OSRM server instead of a
local one. **Synthetic data only** — real addresses must stay on a local
instance (setup commands are in `config.py`, above `OSRM_HOST`).

Try it without any real data:

```bash
python tools/make_demo_data.py         # 30 invented customers
python tests/test_normalize.py         # normalization self-checks
```

## Configuration

Everything tunable lives in `config.py`. No magic numbers in code.

The parameters that change the answer most:

- `BASE_SERVICE_TIME_MINUTES` / `PER_ENVELOPE_TIME_MINUTES` — service cost per
  stop. When reward is free (all envelopes in one lobby mailbox), the second
  is `0.0` and travel time dominates.
- `STREET_REVISIT_PENALTY_MINUTES` — charged when the route re-enters a street
  it abandoned with stops remaining. Re-entering a congested street is the
  expensive mistake, not leaving one.
- `DETOUR_RETURN_THRESHOLD_STOPS` — a branch that returns within this many
  stops is a cheap side-street detour and is deliberately **not** penalized.
- `ARTERIAL_STREETS` / `ARTERIAL_PENALTY_MULTIPLIER` — bias away from
  congested main roads toward quiet interior streets.
- `STOP_MERGE_RADIUS_METERS` / `MAX_PROXIMITY_MERGE_PERCENT` — how aggressively
  addresses collapse into one building, and when to stop and ask instead.

## Adapting it elsewhere

The optimization is domain-agnostic. Anything with **stops, weights, and a
travel-time matrix** works: parcel drops, meter reading, leaflet campaigns,
restocking, inspections, canvassing. Replace "envelopes" with whatever the
value at a stop is.

What is portable as-is:

- Stages 3–6 — collapsing, matrix, solver, outputs. No locale assumptions.
- Stage 2 — swap `GEOCODE_COMPONENTS` and `BNEI_BRAK_BBOX` for your region.

What is locale-specific:

- **`src/normalize.py` is written for Hebrew Israeli addresses** — street
  prefixes (`רח'`, `שד'`), acronym aliases (`חזו"א` → `חזון איש`), niqqud, RTL
  control characters, `דירה`/`כניסה`/`קומה` unit tokens.

To move to another language, replace that one module. It has a narrow
interface — `parse_address()` returning street, house number, unit fields, and
a merge key — and `tests/test_normalize.py` shows the contract. Everything
downstream consumes only that output.

The design rule there is worth keeping whatever the language: **under-normalize
rather than over-normalize.** Missing a variant costs one duplicate stop.
Fusing two distinct streets corrupts the route with no error message.

## Privacy

Customer data is never committed. `.gitignore` covers the input file, `.env`,
the geocode cache, every intermediate artifact, and all generated route files.

Data leaves the machine in exactly two places: the geocoding API, and OSRM
(coordinates only — use the local instance and it leaves nowhere). Nothing is
sent to any other service. Generated pages use no `localStorage` and no
external calls beyond map tiles.

Note that `final_route.html` and `printable_route.html` contain names,
addresses and apartment numbers in plain text. Sharing those files shares the
customer list.

## Layout

```
config.py                  every tunable parameter
src/normalize.py           address normalization (locale-specific)
src/stage1..6_*.py         pipeline stages
src/env.py                 .env loader
tools/fetch_leaflet.py     vendor Leaflet for self-contained maps
tools/make_demo_data.py    synthetic test data
tests/test_normalize.py    normalization self-checks
vendor/                    inlined Leaflet assets
output/                    artifacts (git-ignored)
```
