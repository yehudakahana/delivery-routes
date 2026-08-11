# -*- coding: utf-8 -*-
"""
Central configuration for the Bnei Brak envelope-delivery routing pipeline.

Every tunable parameter lives here. No magic numbers anywhere else in the code.

Note on Hebrew string constants: values below are written in the *canonical*
form produced by src/normalize.py (ASCII apostrophe/quote, NFC, no niqqud).
Consumers should still pass them through normalize.canonicalize_street()
before comparing, so that a hand-edit here cannot silently stop matching.
"""

from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent

# Input workbook. If None, the parser auto-detects a single .xlsx/.xls/.csv
# file in PROJECT_ROOT and fails loudly if there is not exactly one.
INPUT_FILE = None

OUTPUT_DIRECTORY = PROJECT_ROOT / "output"
CACHE_DIRECTORY = PROJECT_ROOT / "cache"

# Stage artifacts. Each stage reads the previous stage's file and writes its
# own, so any stage can be re-run in isolation.
STAGE1_PARSED = OUTPUT_DIRECTORY / "parsed_addresses.json"
STAGE1_MALFORMED = OUTPUT_DIRECTORY / "malformed_rows.csv"
STAGE1_REPORT = OUTPUT_DIRECTORY / "stage1_report.txt"

GEOCODE_CACHE = CACHE_DIRECTORY / "geocode_cache.json"
STAGE2_RESULTS = OUTPUT_DIRECTORY / "geocode_results.json"
STAGE2_FAILURES = OUTPUT_DIRECTORY / "geocoding_failures.json"
STAGE2_REVIEW = OUTPUT_DIRECTORY / "geocoding_review.json"
STAGE2_REPORT = OUTPUT_DIRECTORY / "stage2_report.txt"

STAGE3_STOPS = OUTPUT_DIRECTORY / "stops.json"
STAGE3_REPORT = OUTPUT_DIRECTORY / "stage3_report.txt"

STAGE4_MATRIX = CACHE_DIRECTORY / "travel_time_matrix.json"
STAGE4_GEOMETRY = CACHE_DIRECTORY / "route_geometry.json"

STAGE5_ORDER = OUTPUT_DIRECTORY / "route_order.json"
STAGE5_COMPARISON = OUTPUT_DIRECTORY / "baseline_comparison.txt"

FINAL_MAP = OUTPUT_DIRECTORY / "final_route.html"
FINAL_PRINTABLE = OUTPUT_DIRECTORY / "printable_route.html"
FINAL_NAVIGATION = OUTPUT_DIRECTORY / "navigation_links.html"
FINAL_CSV = OUTPUT_DIRECTORY / "final_route.csv"
FINAL_OPPORTUNISTIC = OUTPUT_DIRECTORY / "nearby_out_of_sequence.csv"

# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

# Name of the environment variable holding the Google Geocoding API key.
# The key itself is read from .env at runtime and is never written to any
# artifact, log line, or cache entry.
GOOGLE_API_KEY_ENV_NAME = "GOOGLE_GEOCODING_API_KEY"

DOTENV_PATH = PROJECT_ROOT / ".env"

# --------------------------------------------------------------------------
# Column mapping (stage 1)
# --------------------------------------------------------------------------

# Filled in only after the user explicitly confirms the mapping against the
# real headers. Left as None so that stage 1 refuses to run on a guess.
COLUMN_MAPPING = {
    "street": None,
    "house_number": None,
    "apartment": None,
    "entrance": None,
    "customer_name": None,
    "full_address": None,   # optional: single free-text address column
    "envelope_count": None,  # optional: if absent, one envelope per row
}

# If the workbook has no explicit quantity column, each data row counts as
# exactly this many envelopes.
DEFAULT_ENVELOPES_PER_ROW = 1

# --------------------------------------------------------------------------
# Geocoding
# --------------------------------------------------------------------------

GEOCODE_COMPONENTS = {"country": "IL", "locality": "Bnei Brak"}
GEOCODE_LANGUAGE = "iw"
GEOCODE_REGION = "il"

# Appended to the normalized address when querying Google.
GEOCODE_CITY_SUFFIX = "בני ברק"

GEOCODE_MAX_RETRIES = 5
GEOCODE_BACKOFF_BASE_SECONDS = 1.0
GEOCODE_BACKOFF_MAX_SECONDS = 60.0
GEOCODE_REQUEST_TIMEOUT_SECONDS = 15
# Polite pacing between live API calls (cache hits are not delayed).
GEOCODE_MIN_INTERVAL_SECONDS = 0.05

# location_type handling. APPROXIMATE is never usable: Google returns the
# city centroid for addresses it cannot resolve, which would silently pile
# dozens of unrelated customers onto one phantom coordinate.
GEOCODE_ACCEPTED_LOCATION_TYPES = ("ROOFTOP", "RANGE_INTERPOLATED")
GEOCODE_SUSPICIOUS_LOCATION_TYPES = ("GEOMETRIC_CENTER",)
GEOCODE_REJECTED_LOCATION_TYPES = ("APPROXIMATE",)

# --------------------------------------------------------------------------
# Geographic validation
# --------------------------------------------------------------------------

# Generous box around Bnei Brak; anything outside is flagged for review,
# never auto-discarded.
BNEI_BRAK_BBOX = {
    "min_lat": 32.070,
    "max_lat": 32.115,
    "min_lng": 34.810,
    "max_lng": 34.865,
}

# Known centroid of Bnei Brak. Any geocode landing within this many metres of
# it is treated as a probable "Google gave up" fallback and flagged, even if
# the reported location_type looks acceptable.
BNEI_BRAK_CENTROID = (32.0853, 34.8338)
CENTROID_SUSPICION_RADIUS_METERS = 60

# --------------------------------------------------------------------------
# Stop collapsing (stage 3)
# --------------------------------------------------------------------------

# Proximity merging is a fallback for spelling variants of the same building
# only. Neighbouring buildings sit well inside 15 m, so proximity alone is
# never sufficient grounds for a merge.
STOP_MERGE_RADIUS_METERS = 15

# If more than this share of stops were merged by proximity rather than by
# normalized address, normalization is broken. Stop and ask.
MAX_PROXIMITY_MERGE_PERCENT = 8.0

# Sanity band for the collapsed stop count, as a fraction of input rows.
# ~450 rows is expected to yield roughly 250-320 stops. A result near the
# raw row count means normalization failed.
EXPECTED_STOP_COUNT_RANGE = (230, 340)

# A single stop holding more than this many customers is flagged for eyeball
# review -- it is the signature of a geocoding or merge failure.
SUSPICIOUS_CUSTOMERS_PER_STOP = 12

# --------------------------------------------------------------------------
# Travel-time matrix (stage 4)
# --------------------------------------------------------------------------

OSRM_HOST = "http://127.0.0.1:5000"
OSRM_PROFILE = "driving"
OSRM_TABLE_CHUNK_SIZE = 100      # nodes per /table request slice
OSRM_REQUEST_TIMEOUT_SECONDS = 120

# The matrix is asymmetric by construction (one-way streets). It is never
# symmetrized, averaged, or backfilled with straight-line distance. This flag
# exists only so the verification step can assert the intent.
ASSERT_MATRIX_ASYMMETRY = True

# --------------------------------------------------------------------------
# Service time (measured: lobby mailbox, no apartment visits)
# --------------------------------------------------------------------------

BASE_SERVICE_TIME_MINUTES = 1.0
PER_ENVELOPE_TIME_MINUTES = 0.0

# --------------------------------------------------------------------------
# Street continuity
# --------------------------------------------------------------------------

# Charged when the route re-enters a street it had already left while stops
# on that street were still unvisited. Re-entry is the expensive mistake in
# Bnei Brak -- not leaving.
STREET_REVISIT_PENALTY_MINUTES = 3.0

# A departure that returns to the same street within this many stops is a
# short side-street branch: cheap, desirable, and explicitly not penalized.
DETOUR_RETURN_THRESHOLD_STOPS = 3

# Congested arterials. Traversal time on legs touching these is multiplied,
# pushing the solver toward quiet interior streets.
ARTERIAL_STREETS = [
    "רבי עקיבא",
    "ז'בוטינסקי",
    "חזון איש",
]
ARTERIAL_PENALTY_MULTIPLIER = 1.6

# --------------------------------------------------------------------------
# Solver (stage 5)
# --------------------------------------------------------------------------

ROUTE_MODE = "open"          # open route: solver picks both endpoints

# Fixed starting point, if the user has one. None means a genuinely open
# route. Format: (lat, lng).
FIXED_START_COORDINATE = None

SOLVER_TIME_LIMIT_SECONDS = 60          # OR-Tools seed tour budget
LATENCY_LOCAL_SEARCH_ITERATIONS = 200_000
LATENCY_LOCAL_SEARCH_TIME_LIMIT_SECONDS = 120

# Or-opt segment lengths tried when relocating. Orientation is preserved,
# which is what makes these moves safe on an asymmetric matrix -- unlike
# 2-opt, whose segment reversal re-costs every internal arc.
OR_OPT_SEGMENT_LENGTHS = (1, 2, 3)

# Include a restricted, orientation-reversing 2-opt pass. Off by default:
# on an asymmetric matrix it is both expensive to evaluate and prone to
# producing routes that fight the one-way grid.
ENABLE_TWO_OPT = False

# Seeds fed to the latency local search; the best final result wins.
SOLVER_SEEDS = ("ortools_tsp", "greedy_ratio")

RANDOM_SEED = 20260811

# Prefix sizes reported in the baseline comparison table.
BASELINE_REPORT_PREFIXES = (100, 200, 300)

# --------------------------------------------------------------------------
# Opportunistic pickup (stage 6)
# --------------------------------------------------------------------------

OPPORTUNISTIC_RADIUS_METERS = 150
OPPORTUNISTIC_SEQUENCE_GAP = 50

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

# Google Maps Directions allows 1 origin + 23 waypoints + 1 destination.
MAX_POINTS_PER_NAVIGATION_LINK = 25

# Consecutive navigation segments overlap by one stop: the end of a segment
# is the start of the next.
NAVIGATION_SEGMENT_OVERLAP = 1

MAP_DEFAULT_ZOOM = 15
MAP_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
MAP_TILE_ATTRIBUTION = "© OpenStreetMap contributors"

CSV_ENCODING = "utf-8-sig"   # so Excel opens the Hebrew correctly
