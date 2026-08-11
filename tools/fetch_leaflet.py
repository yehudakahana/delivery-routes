# -*- coding: utf-8 -*-
"""
Vendor Leaflet into vendor/ so final_route.html can travel as a single file.

Downloads the pinned Leaflet build plus the three images its stylesheet
references, and rewrites those references as data URIs. The result,
vendor/leaflet.inlined.css, has no external dependencies at all.

Run once, or whenever LEAFLET_VERSION changes:
    python tools/fetch_leaflet.py
"""

import base64
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

BASE_URL = f"https://unpkg.com/leaflet@{config.LEAFLET_VERSION}/dist"
CSS_IMAGES = ("layers-2x.png", "layers.png", "marker-icon.png")


def download(url: str) -> bytes:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def main() -> None:
    config.VENDOR_DIRECTORY.mkdir(parents=True, exist_ok=True)

    javascript = download(f"{BASE_URL}/leaflet.js")
    config.LEAFLET_JS.write_bytes(javascript)
    print(f"leaflet.js          {len(javascript):>8,} bytes")

    css = download(f"{BASE_URL}/leaflet.css").decode("utf-8")
    (config.VENDOR_DIRECTORY / "leaflet.css").write_text(css, encoding="utf-8")

    for name in CSS_IMAGES:
        image = download(f"{BASE_URL}/images/{name}")
        (config.VENDOR_DIRECTORY / name).write_bytes(image)
        encoded = base64.b64encode(image).decode()
        css = css.replace(f"url(images/{name})", f"url(data:image/png;base64,{encoded})")
        print(f"  inlined {name:<18} {len(image):>7,} bytes")

    leftover = re.findall(r"url\(images/[^)]+\)", css)
    if leftover:
        raise SystemExit(f"unresolved image references remain: {leftover}")

    config.LEAFLET_CSS.write_text(css, encoding="utf-8")
    print(f"leaflet.inlined.css {len(css.encode()):>8,} bytes")
    print(f"\nvendored Leaflet {config.LEAFLET_VERSION} into {config.VENDOR_DIRECTORY.name}/")


if __name__ == "__main__":
    main()
