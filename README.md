# Prefix-Optimal Delivery Routing

Takes an Excel list of customers in Bnei Brak and produces a delivery order
where **every prefix of the route is worth as much as possible** — for a driver
who will run out of time before running out of stops.

The driver works one shift, drives down the list, and stops when the shift
ends. Whether that is at stop 180 or stop 340 is not known in advance and does
not matter. What matters is that the stops already visited are the most
valuable ones that could have been reached in that time.

## Why this is not a TSP

A travelling-salesman solver minimizes the time to finish **all** stops. It
produces an efficient loop and says nothing about the order value arrives in.
Drive half of an optimal TSP tour and you may have collected half the value —
or a quarter.

This solves the **weighted minimum latency problem** instead:

```
minimize   Σ  weight[i] × arrival_time[i]
           i
```

Weighting by `weight[i]` (envelopes per building) pulls high-value stops toward
the front. The cumulative-arrival term keeps the route geographically coherent,
so it does not teleport between distant valuable stops.

OR-Tools' `RoutingModel` cannot express this objective — `SetSpanCostCoefficient`
minimizes route *span*, which is a different thing. So OR-Tools builds a seed
tour, and a local search then optimizes the real objective directly: Or-opt
moves, accepted only when weighted latency drops.

## Setup (once)

```bash
pip install pandas openpyxl requests ortools
python tools/fetch_leaflet.py          # vendor Leaflet for offline maps
cp .env.example .env                   # then put the Google Geocoding key in it
```

**OSRM must be running for stages 4 and 6.** Bring up the local instance from
the project root:

```bash
docker run -t -v "%cd%/osrm:/data" ghcr.io/project-osrm/osrm-backend \
    osrm-extract -p /opt/car.lua /data/israel-and-palestine-latest.osm.pbf
docker run -t -v "%cd%/osrm:/data" ghcr.io/project-osrm/osrm-backend \
    osrm-partition /data/israel-and-palestine-latest.osrm
docker run -t -v "%cd%/osrm:/data" ghcr.io/project-osrm/osrm-backend \
    osrm-customize /data/israel-and-palestine-latest.osrm

docker run -p 5000:5000 -v "%cd%/osrm:/data" ghcr.io/project-osrm/osrm-backend \
    osrm-routed --algorithm mld --max-table-size 1000 \
    /data/israel-and-palestine-latest.osrm
```

`--max-table-size` must exceed the stop count or `/table` refuses the request.

Stages 4 and 6 accept `--demo-host` to use the public OSRM server instead.
**Synthetic data only** — real customer coordinates must stay on the local
instance.

## Running a new customer file

### 1. Put the file where stage 1 will find it

Stage 1 scans the project root for **exactly one** spreadsheet and fails loudly
if there is more than one. Either replace the existing file, or name yours
explicitly in `config.py`:

```python
INPUT_FILE = "newClients.xlsx"     # or an absolute path; None = auto-detect
```

### 2. Confirm the column mapping

```bash
python -m src.stage1_parse --inspect
```

Prints the headers, five sample rows, and a suggested mapping. Compare it
against `config.COLUMN_MAPPING`, which is currently confirmed for the headers
`לקוח | שם לקוח | כתובת | טלפון | נייד | פקס | Email`:

```python
COLUMN_MAPPING = {
    "customer_name":  "שם לקוח",
    "full_address":   "כתובת",   # one free-text column: "כהנמן 72 בני ברק"
    "envelope_count": None,      # no quantity column -> 1 envelope per row
    "street": None, "house_number": None, "apartment": None, "entrance": None,
}
```

Same headers, change nothing. Different headers — separate `רחוב` / `מספר בית`
columns, a `מעטפות` quantity column, a `דירה` column — fill those names in.
Stage 1 refuses to run rather than guess at a mapping.

### 3. Run the six stages

```bash
python -m src.stage1_parse
python -m src.stage2_geocode
python -m src.stage3_collapse
python -m src.stage4_matrix       # needs OSRM up
python -m src.stage5_order
python -m src.stage6_output       # needs OSRM up
```

No cache needs clearing between runs. The geocode cache is keyed by address, so
a new file only pays for addresses it has never seen. Stage 4 always rebuilds
the matrix; stage 5 refuses to run if the matrix does not match the current
stops; stage 6 re-fetches road geometry unless the stop order is identical.

### 4. Read the reports before trusting the route

Stages 1–3 are the ones that fail silently. The worst outcome is not a crash —
it is a clean-looking route built on bad address grouping.

| Check | Look for |
|---|---|
| `output/stage1_report.txt` | malformed rows, street aliases applied, ambiguous addresses |
| `output/malformed_rows.csv` | rows excluded from the route — fix them and re-run |
| `output/geocoding_review.json` | addresses Google answered suspiciously. **Eyeball these.** |
| `output/stage3_report.txt` | how customers collapsed into buildings |
| `output/baseline_comparison.txt` | what the ordering bought over a naive route |

### 5. Outputs

| File | Is |
|---|---|
| `output/final_route.html` | every stop on a Leaflet map, self-contained |
| `output/printable_route.html` | RTL Hebrew delivery list for the driver |
| `output/navigation_links.html` | Google Maps segments, 25 points each |
| `output/final_route.csv` | the whole route, UTF-8, Hebrew preserved |
| `output/nearby_out_of_sequence.csv` | stops you pass near but are scheduled late |

## Grouped customer workbook

Separate from the routing. Produces the same rows and cells as the input file,
grouped by street and formatted for printing:

```bash
python tools/make_grouped_workbook.py     # run after stage 1
```

Writes `output/clients_by_street.xlsx` and verifies it cell-for-cell against
the source, exiting non-zero if anything differs. It uses stage 1's canonical
street names, so `ר' עקיבא`, `ר עקיבא` and `רבי עקיבא` land in one group.

## Configuration

Everything tunable lives in `config.py`. No magic numbers in code.

Tuned for the current customer list, and worth revisiting for a new one:

- `GEOCODE_KNOWN_WRONG` / `GEOCODE_CONFIRMED_IN_CITY` — hand-verified decisions
  about specific addresses. Harmless if those addresses are absent, but a new
  file will need its own entries once `geocoding_review.json` has been read.
- `EXPECTED_STOP_RATIO_RANGE` — widened to `0.50–0.95` for a list of businesses,
  one customer per building. Tighten toward `0.50–0.78` for a residential list
  where one lobby mailbox serves a dozen customers.

The parameters that change the answer most:

- `BASE_SERVICE_TIME_MINUTES` / `PER_ENVELOPE_TIME_MINUTES` — service cost per
  stop. When reward is free (all envelopes in one lobby mailbox), the second is
  `0.0` and travel time dominates.
- `STREET_REVISIT_PENALTY_MINUTES` — charged when the route re-enters a street
  it abandoned with stops remaining. Re-entering a congested street is the
  expensive mistake, not leaving one.
- `DETOUR_RETURN_THRESHOLD_STOPS` — a branch that returns within this many stops
  is a cheap side-street detour and is deliberately **not** penalized.
- `ARTERIAL_STREETS` / `ARTERIAL_PENALTY_MULTIPLIER` — bias away from congested
  main roads toward quiet interior streets.
- `STOP_MERGE_RADIUS_METERS` / `MAX_PROXIMITY_MERGE_PERCENT` — how aggressively
  addresses collapse into one building, and when to stop and ask instead.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Multiple candidate inputs (…)` | more than one spreadsheet in the root — set `INPUT_FILE` |
| `COLUMN_MAPPING has no address column` | run `--inspect` and confirm the mapping first |
| Connection refused on stage 4 or 6 | OSRM container is not running |
| `/table` request rejected | `--max-table-size` is below the stop count |
| Stage 5: matrix does not match stops | stage 3 changed — re-run stage 4 |
| Stage 3 stops on the ratio band | normalization broke, or the list is not residential — see `EXPECTED_STOP_RATIO_RANGE` |

## Privacy

Customer data is never committed. `.gitignore` covers the input file, `.env`,
the geocode cache, every intermediate artifact, and all generated route files.

Data leaves the machine in exactly two places: the geocoding API, and OSRM
(coordinates only — use the local instance and it leaves nowhere). Generated
pages use no `localStorage` and no external calls beyond map tiles.

`final_route.html` and `printable_route.html` contain names, addresses and
apartment numbers in plain text. Sharing those files shares the customer list.

## Layout

```
config.py                       every tunable parameter
src/normalize.py                Hebrew address normalization
src/stage1..6_*.py              pipeline stages
src/env.py                      .env loader
tools/fetch_leaflet.py          vendor Leaflet for self-contained maps
tools/make_grouped_workbook.py  street-grouped copy of the customer workbook
tools/make_demo_data.py         synthetic test data
tests/test_normalize.py         normalization self-checks
vendor/                         inlined Leaflet assets
output/                         artifacts (git-ignored)
```

Try it without any real data:

```bash
python tools/make_demo_data.py         # 30 invented customers
python tests/test_normalize.py         # normalization self-checks
```
<img width="1080" height="1853" alt="Screenshot_20260812_181608_Chrome" src="https://github.com/user-attachments/assets/4730aaa9-185d-4b7a-8650-b9b241c87c65" />
