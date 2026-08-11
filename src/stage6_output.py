# -*- coding: utf-8 -*-
"""
Stage 6 -- outputs.

Five artifacts:
  final_route.html            every stop, always visible, on a Leaflet map
  nearby_out_of_sequence.csv  stops you are standing near but scheduled late
  printable_route.html        RTL Hebrew delivery list, no arrival times
  navigation_links.html       Google Maps segments, 25 points each
  final_route.csv             the whole thing, UTF-8, Hebrew preserved

The map deliberately shows all stops at once. If the ordering is imperfect,
the driver needs to notice that stop #310 is across the street while working
stop #45 -- a map that only draws what comes next cannot show that.

No localStorage anywhere. No customer data leaves the machine.

    python -m src.stage6_output [--demo-host]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.stage2_geocode import haversine_meters
from src.stage4_matrix import fetch_route_geometry


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def build_route(stops: list[dict], order: list[int]) -> list[dict]:
    route = []
    cumulative = 0

    for position, stop_index in enumerate(order, start=1):
        stop = stops[stop_index]
        cumulative += stop["envelope_count"]
        route.append(
            {
                "route_order": position,
                "stop_id": stop["stop_id"],
                "normalized_address": stop["normalized_address"],
                "original_address": " | ".join(stop["original_addresses"]),
                "street": stop["street"],
                "house_number": stop["house_number"],
                "latitude": stop["lat"],
                "longitude": stop["lng"],
                "envelope_count": stop["envelope_count"],
                "cumulative_envelopes": cumulative,
                "customers": stop["customers"],
                "customer_names": ", ".join(
                    c["name"] for c in stop["customers"] if c["name"]
                ),
                "apartment_numbers": ", ".join(
                    c["apartment"] for c in stop["customers"] if c["apartment"]
                ),
                "entrance_numbers": ", ".join(
                    sorted({c["entrance"] for c in stop["customers"] if c["entrance"]})
                ),
                "flags": stop["flags"],
            }
        )
    return route


def find_opportunistic(route: list[dict]) -> dict[int, list[dict]]:
    """
    Stops that are physically close but scheduled much later.

    These are the cases where the driver is already standing next to
    something the ordering will send them back for.
    """
    nearby: dict[int, list[dict]] = {}

    for current in route:
        matches = []
        for other in route:
            gap = other["route_order"] - current["route_order"]
            if gap <= config.OPPORTUNISTIC_SEQUENCE_GAP:
                continue
            distance = haversine_meters(
                (current["latitude"], current["longitude"]),
                (other["latitude"], other["longitude"]),
            )
            if distance <= config.OPPORTUNISTIC_RADIUS_METERS:
                matches.append(
                    {
                        "route_order": other["route_order"],
                        "address": other["normalized_address"],
                        "envelope_count": other["envelope_count"],
                        "distance_meters": round(distance),
                        "sequence_gap": gap,
                    }
                )
        if matches:
            nearby[current["route_order"]] = sorted(
                matches, key=lambda m: m["distance_meters"]
            )

    return nearby


# --------------------------------------------------------------------------
# A. Map
# --------------------------------------------------------------------------

MAP_TEMPLATE = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>מסלול חלוקת מעטפות - בני ברק</title>
__LEAFLET_ASSETS__
<style>
  html, body { margin:0; padding:0; height:100%; font-family: Arial, "Segoe UI", sans-serif; }
  #map { height:100%; width:100%; }
  .stop-marker {
    display:flex; align-items:center; justify-content:center;
    border-radius:50%; border:2px solid rgba(0,0,0,.55);
    color:#fff; font-weight:700; text-shadow:0 1px 2px rgba(0,0,0,.8);
    box-shadow:0 1px 4px rgba(0,0,0,.4);
  }
  .leaflet-popup-content { direction:rtl; text-align:right; font-size:13px; min-width:230px; }
  .leaflet-popup-content h3 { margin:0 0 6px; font-size:15px; }
  .leaflet-popup-content table { border-collapse:collapse; width:100%; margin-top:6px; }
  .leaflet-popup-content td { padding:2px 4px; border-bottom:1px solid #eee; }
  .warn { color:#b30000; font-weight:700; }
  .legend {
    background:#fff; padding:10px 12px; border-radius:6px; line-height:1.6;
    box-shadow:0 1px 6px rgba(0,0,0,.3); direction:rtl; text-align:right; font-size:12px;
  }
  .legend .bar {
    height:10px; width:150px; border-radius:5px; margin:4px 0;
    background:linear-gradient(to left, #7b0d8f, #2b6be0, #12a48a, #d9a400, #d92b2b);
  }
  .legend .row { display:flex; justify-content:space-between; width:150px; }
</style>
</head>
<body>
<div id="map"></div>
<script>
const STOPS = __STOPS__;
const GEOMETRY = __GEOMETRY__;
const NEARBY = __NEARBY__;
const TOTAL = STOPS.length;

const map = L.map('map');
L.tileLayer(__TILE_URL__, { maxZoom: 19, attribution: __ATTRIBUTION__ }).addTo(map);

// Colour runs along the sequence, so a marker's colour tells you at a glance
// whether a nearby building is early or late in the order.
function sequenceColour(position) {
  const ratio = TOTAL > 1 ? (position - 1) / (TOTAL - 1) : 0;
  const hue = 0 + ratio * 290;          // red (first) -> purple (last)
  return `hsl(${hue}, 72%, 45%)`;
}

function markerSize(envelopes) {
  const maxEnvelopes = Math.max(...STOPS.map(s => s.envelope_count));
  const scale = maxEnvelopes > 1 ? (envelopes - 1) / (maxEnvelopes - 1) : 0;
  return Math.round(24 + scale * 20);
}

const routeLine = L.polyline(GEOMETRY, {
  color:'#2b6be0', weight:3, opacity:.55
});

const stopLayer = L.layerGroup();

STOPS.forEach(stop => {
  const size = markerSize(stop.envelope_count);
  const colour = sequenceColour(stop.route_order);

  const icon = L.divIcon({
    className:'',
    html:`<div class="stop-marker" style="width:${size}px;height:${size}px;
           background:${colour};font-size:${size > 32 ? 13 : 11}px;">${stop.route_order}</div>`,
    iconSize:[size, size],
    iconAnchor:[size/2, size/2]
  });

  const customers = stop.customers.map(c => `
    <tr><td>${c.name || '-'}</td>
        <td>${c.apartment ? 'דירה ' + c.apartment : ''}</td>
        <td>${c.entrance ? 'כניסה ' + c.entrance : ''}</td>
        <td>${c.floor ? 'קומה ' + c.floor : ''}</td></tr>`).join('');

  const nearby = (NEARBY[stop.route_order] || []).map(n =>
    `<li>#${n.route_order} — ${n.address} (${n.envelope_count} מעטפות, ${n.distance_meters} מ')</li>`
  ).join('');

  const flags = stop.flags.length
    ? `<p class="warn">שים לב: ${stop.flags.join(' · ')}</p>` : '';

  L.marker([stop.latitude, stop.longitude], { icon })
    .bindPopup(`
      <h3>#${stop.route_order} — ${stop.normalized_address}</h3>
      <div>מעטפות: <b>${stop.envelope_count}</b> · מצטבר: ${stop.cumulative_envelopes}</div>
      <div>כתובת מקורית: ${stop.original_address}</div>
      ${flags}
      <table>${customers}</table>
      ${nearby ? `<p><b>קרוב אך מאוחר במסלול:</b></p><ul>${nearby}</ul>` : ''}
    `)
    .addTo(stopLayer);
});

stopLayer.addTo(map);
routeLine.addTo(map);

map.fitBounds(L.latLngBounds(STOPS.map(s => [s.latitude, s.longitude])).pad(0.08));

L.control.layers(null, {
  'קו המסלול': routeLine,
  'נקודות עצירה': stopLayer
}, { collapsed:false, position:'topleft' }).addTo(map);

const legend = L.control({ position:'bottomright' });
legend.onAdd = function () {
  const div = L.DomUtil.create('div', 'legend');
  div.innerHTML = `
    <b>סדר המסלול</b>
    <div class="bar"></div>
    <div class="row"><span>ראשון</span><span>אחרון</span></div>
    <div style="margin-top:6px">גודל הסימון = מספר מעטפות</div>
    <div>סה"כ נקודות: ${TOTAL}</div>`;
  return div;
};
legend.addTo(map);
</script>
</body>
</html>
"""


CDN_FALLBACK = (
    f'<link rel="stylesheet" '
    f'href="https://unpkg.com/leaflet@{config.LEAFLET_VERSION}/dist/leaflet.css">\n'
    f'<script src="https://unpkg.com/leaflet@{config.LEAFLET_VERSION}/dist/leaflet.js">'
    f'</script>'
)


def leaflet_assets() -> str:
    """
    Leaflet inlined into the page.

    The map file is meant to be a single attachment that works on its own, so
    the library travels with it instead of being fetched from a CDN at open
    time. Marker and control images are already data URIs inside the vendored
    CSS, so nothing but the map tiles touches the network.
    """
    if not config.INLINE_LEAFLET:
        return CDN_FALLBACK

    if not (config.LEAFLET_CSS.exists() and config.LEAFLET_JS.exists()):
        print("  ! vendored Leaflet missing; falling back to the CDN. "
              "Run: python tools/fetch_leaflet.py")
        return CDN_FALLBACK

    css = config.LEAFLET_CSS.read_text(encoding="utf-8")
    javascript = config.LEAFLET_JS.read_text(encoding="utf-8")

    # A literal </script> inside the library text would close the tag early.
    javascript = javascript.replace("</script>", "<\\/script>")

    return (
        f"<style>\n{css}\n</style>\n"
        f"<script>\n{javascript}\n</script>"
    )


def write_map(route: list[dict], geometry: list[list[float]], nearby: dict) -> None:
    payload = [
        {
            key: stop[key]
            for key in (
                "route_order", "normalized_address", "original_address",
                "latitude", "longitude", "envelope_count",
                "cumulative_envelopes", "customers", "flags",
            )
        }
        for stop in route
    ]

    html = (
        MAP_TEMPLATE
        .replace("__LEAFLET_ASSETS__", leaflet_assets())
        .replace("__STOPS__", json.dumps(payload, ensure_ascii=False))
        .replace("__GEOMETRY__", json.dumps(geometry))
        .replace("__NEARBY__", json.dumps(nearby, ensure_ascii=False))
        .replace("__TILE_URL__", json.dumps(config.MAP_TILE_URL))
        .replace("__ATTRIBUTION__", json.dumps(config.MAP_TILE_ATTRIBUTION))
    )
    config.FINAL_MAP.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------
# B. Opportunistic pickups
# --------------------------------------------------------------------------

def write_opportunistic(route: list[dict], nearby: dict) -> int:
    by_order = {stop["route_order"]: stop for stop in route}
    rows = 0

    with config.FINAL_OPPORTUNISTIC.open(
        "w", encoding=config.CSV_ENCODING, newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["route_order", "address", "nearby_route_order", "nearby_address",
             "nearby_envelope_count", "distance_meters", "sequence_gap"]
        )
        for position in sorted(nearby):
            for match in nearby[position]:
                writer.writerow(
                    [
                        position,
                        by_order[position]["normalized_address"],
                        match["route_order"],
                        match["address"],
                        match["envelope_count"],
                        match["distance_meters"],
                        match["sequence_gap"],
                    ]
                )
                rows += 1
    return rows


# --------------------------------------------------------------------------
# C. Printable list
# --------------------------------------------------------------------------

PRINTABLE_TEMPLATE = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<title>רשימת חלוקה - בני ברק</title>
<style>
  body { font-family:"David","Times New Roman",serif; direction:rtl; margin:18px;
         font-size:13px; color:#111; }
  h1 { font-size:19px; margin:0 0 4px; }
  .meta { color:#555; font-size:12px; margin-bottom:12px; }
  table { border-collapse:collapse; width:100%; }
  th, td { border:1px solid #999; padding:5px 7px; text-align:right;
           vertical-align:top; }
  th { background:#e8e8e8; }
  tr:nth-child(even) td { background:#f7f7f7; }
  .num { text-align:center; width:44px; font-weight:700; }
  .env { text-align:center; width:60px; }
  .cum { text-align:center; width:70px; color:#555; }
  .units { font-size:12px; color:#333; }
  .dense td { background:#fff3cd !important; }
  @media print {
    body { margin:8mm; font-size:11px; }
    thead { display:table-header-group; }
    tr { page-break-inside:avoid; }
  }
</style>
</head>
<body>
<h1>רשימת חלוקת מעטפות - בני ברק</h1>
<div class="meta">
  סה"כ נקודות: __STOPS__ &nbsp;|&nbsp; סה"כ מעטפות: __ENVELOPES__ &nbsp;|&nbsp;
  הרשימה מסודרת לפי סדר הנסיעה. אין זמני הגעה משוערים.
</div>
<table>
<thead>
<tr><th class="num">מספר</th><th>כתובת</th><th>דירות / יחידות</th>
    <th class="env">מעטפות</th><th class="cum">מצטבר מעטפות</th></tr>
</thead>
<tbody>
__ROWS__
</tbody>
</table>
</body>
</html>
"""


def escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def write_printable(route: list[dict]) -> None:
    rows = []
    for stop in route:
        units = []
        for customer in stop["customers"]:
            parts = [customer["name"] or ""]
            if customer["apartment"]:
                parts.append(f"דירה {customer['apartment']}")
            if customer["entrance"]:
                parts.append(f"כניסה {customer['entrance']}")
            if customer["floor"]:
                parts.append(f"קומה {customer['floor']}")
            units.append(" · ".join(p for p in parts if p))

        highlight = ' class="dense"' if stop["envelope_count"] >= 4 else ""
        rows.append(
            f'<tr{highlight}>'
            f'<td class="num">{stop["route_order"]}</td>'
            f'<td>{escape(stop["original_address"])}</td>'
            f'<td class="units">{escape("<br>".join(units)).replace("&lt;br&gt;", "<br>")}</td>'
            f'<td class="env">{stop["envelope_count"]}</td>'
            f'<td class="cum">{stop["cumulative_envelopes"]}</td>'
            f'</tr>'
        )

    html = (
        PRINTABLE_TEMPLATE
        .replace("__ROWS__", "\n".join(rows))
        .replace("__STOPS__", str(len(route)))
        .replace("__ENVELOPES__", str(route[-1]["cumulative_envelopes"] if route else 0))
    )
    config.FINAL_PRINTABLE.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------
# D. Navigation links
# --------------------------------------------------------------------------

def build_navigation_segments(route: list[dict]) -> list[dict]:
    """
    Split into Google Maps Directions URLs of at most 25 points.

    Segments overlap by one stop -- the end of one is the start of the next --
    so no leg goes unnavigated. optimize:true is deliberately never set:
    Google would resequence the waypoints and destroy the ordering that this
    entire pipeline exists to produce.
    """
    limit = config.MAX_POINTS_PER_NAVIGATION_LINK
    segments = []
    start = 0

    while start < len(route) - 1:
        end = min(start + limit, len(route))
        chunk = route[start:end]

        origin = chunk[0]
        destination = chunk[-1]
        waypoints = chunk[1:-1]

        url = (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={origin['latitude']},{origin['longitude']}"
            f"&destination={destination['latitude']},{destination['longitude']}"
            "&travelmode=driving"
        )
        if waypoints:
            joined = "|".join(
                f"{w['latitude']},{w['longitude']}" for w in waypoints
            )
            url += f"&waypoints={quote(joined, safe='|,')}"

        segments.append(
            {
                "index": len(segments) + 1,
                "from_order": origin["route_order"],
                "to_order": destination["route_order"],
                "from_address": origin["normalized_address"],
                "to_address": destination["normalized_address"],
                "point_count": len(chunk),
                "waypoint_count": len(waypoints),
                "url": url,
            }
        )
        start = end - config.NAVIGATION_SEGMENT_OVERLAP

    return segments


def validate_segments(segments: list[dict], route: list[dict]) -> list[str]:
    problems = []
    limit = config.MAX_POINTS_PER_NAVIGATION_LINK

    for segment in segments:
        if segment["point_count"] > limit:
            problems.append(f"segment {segment['index']} has "
                            f"{segment['point_count']} points (max {limit})")
        if segment["waypoint_count"] > limit - 2:
            problems.append(f"segment {segment['index']} has "
                            f"{segment['waypoint_count']} waypoints (max {limit - 2})")
        if "optimize:true" in segment["url"]:
            problems.append(f"segment {segment['index']} sets optimize:true")
        if not segment["url"].startswith("https://www.google.com/maps/dir/?api=1"):
            problems.append(f"segment {segment['index']} has a malformed URL")

    for earlier, later in zip(segments, segments[1:]):
        if earlier["to_order"] != later["from_order"]:
            problems.append(f"gap between segments {earlier['index']} and "
                            f"{later['index']}: {earlier['to_order']} -> "
                            f"{later['from_order']}")

    if segments:
        if segments[0]["from_order"] != 1:
            problems.append("first segment does not start at stop 1")
        if segments[-1]["to_order"] != len(route):
            problems.append("last segment does not end at the final stop")

    return problems


NAVIGATION_TEMPLATE = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>קישורי ניווט - בני ברק</title>
<style>
  body { font-family:Arial, sans-serif; direction:rtl; margin:16px; font-size:14px; }
  h1 { font-size:19px; }
  .note { background:#fff3cd; border:1px solid #e0c65a; padding:8px 10px;
          border-radius:4px; margin-bottom:14px; font-size:13px; }
  a.seg { display:block; padding:11px 13px; margin-bottom:8px; background:#f2f6ff;
          border:1px solid #b9ccf0; border-radius:5px; text-decoration:none;
          color:#123; }
  a.seg:hover { background:#e3ecff; }
  .range { font-weight:700; font-size:15px; }
  .detail { color:#555; font-size:12.5px; margin-top:3px; }
</style>
</head>
<body>
<h1>קישורי ניווט - Google Maps</h1>
<div class="note">
  כל קטע מכיל עד __LIMIT__ נקודות. סוף קטע = תחילת הקטע הבא, כך שאין קטע חסר.
  הסדר קבוע ואינו משתנה - Google לא מסדר מחדש את הנקודות.
</div>
__SEGMENTS__
</body>
</html>
"""


def write_navigation(segments: list[dict]) -> None:
    blocks = [
        f'<a class="seg" href="{s["url"]}" target="_blank" rel="noopener">'
        f'<div class="range">קטע {s["index"]}: עצירה {s["from_order"]} ← {s["to_order"]}</div>'
        f'<div class="detail">{escape(s["from_address"])} ← {escape(s["to_address"])} '
        f'· {s["point_count"]} נקודות</div></a>'
        for s in segments
    ]
    html = (
        NAVIGATION_TEMPLATE
        .replace("__SEGMENTS__", "\n".join(blocks))
        .replace("__LIMIT__", str(config.MAX_POINTS_PER_NAVIGATION_LINK))
    )
    config.FINAL_NAVIGATION.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------
# E. CSV
# --------------------------------------------------------------------------

CSV_FIELDS = [
    "route_order", "stop_id", "normalized_address", "original_address",
    "street", "house_number", "latitude", "longitude", "envelope_count",
    "cumulative_envelopes", "customer_names", "apartment_numbers",
    "entrance_numbers",
]


def write_csv(route: list[dict]) -> None:
    with config.FINAL_CSV.open("w", encoding=config.CSV_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for stop in route:
            writer.writerow(stop)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-host", action="store_true")
    parser.add_argument("--refresh-geometry", action="store_true",
                        help="re-fetch road geometry instead of reusing the cache")
    args = parser.parse_args()

    if not config.STAGE5_ORDER.exists():
        raise SystemExit("Run stage 5 first.")

    stops = json.loads(config.STAGE3_STOPS.read_text(encoding="utf-8"))["stops"]
    order = json.loads(config.STAGE5_ORDER.read_text(encoding="utf-8"))["order"]

    route = build_route(stops, order)
    nearby = find_opportunistic(route)

    host = config.OSRM_DEMO_HOST if args.demo_host else config.OSRM_HOST
    cached = None
    if config.STAGE4_GEOMETRY.exists() and not args.refresh_geometry:
        payload = json.loads(config.STAGE4_GEOMETRY.read_text(encoding="utf-8"))
        # Only reuse geometry that was drawn for this exact ordering.
        if payload.get("stop_ids") == [s["stop_id"] for s in route]:
            cached = payload["geometry"]

    if cached is not None:
        geometry = cached
        print(f"reusing cached road geometry ({len(geometry)} points)")
    else:
        print(f"fetching road geometry from {host}")
        geometry = fetch_route_geometry(
            host, [(s["latitude"], s["longitude"]) for s in route]
        )
        config.STAGE4_GEOMETRY.write_text(
            json.dumps({"stop_ids": [s["stop_id"] for s in route],
                        "geometry": geometry}),
            encoding="utf-8",
        )

    if not geometry:
        raise SystemExit("No road geometry returned; refusing to draw straight lines.")

    segments = build_navigation_segments(route)
    problems = validate_segments(segments, route)
    if problems:
        print("\n*** NAVIGATION LINK VALIDATION FAILED ***")
        for problem in problems:
            print(f"    - {problem}")
        raise SystemExit(1)

    write_map(route, geometry, nearby)
    opportunistic_rows = write_opportunistic(route, nearby)
    write_printable(route)
    write_navigation(segments)
    write_csv(route)

    print(f"""
נכתבו קבצי הפלט:
  {config.FINAL_MAP.name:<28} {len(route)} נקודות, {len(geometry)} נקודות גיאומטריה
  {config.FINAL_PRINTABLE.name:<28} רשימה להדפסה (RTL)
  {config.FINAL_NAVIGATION.name:<28} {len(segments)} קטעי ניווט
  {config.FINAL_CSV.name:<28} {len(route)} שורות
  {config.FINAL_OPPORTUNISTIC.name:<28} {opportunistic_rows} הזדמנויות איסוף
""")


if __name__ == "__main__":
    main()
