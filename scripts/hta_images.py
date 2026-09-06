"""Download and cache the club crest and player headshots.

These are cached to resources/images/ and COMMITTED, unlike every other generated file.
That is deliberate: the page embeds them as data URIs, and the build fingerprint hashes
the page payload. If the Windows PC and the Linux CI runner each downloaded their own
copies, any difference in bytes would change the fingerprint and make the cloud routine
republish the artifact on every single run. Committing the images makes both machines
embed identical bytes.

Images are fetched at 64px. Measured alternatives, per photo and for a 28-player squad:
    PNG 128@2x  16.8 KB  ->  630 KB   (the source default - far too heavy)
    PNG  64@1x   2.3 KB  ->   87 KB   <- chosen
    WebP 48@2x   3.6 KB  ->  134 KB
64px displayed at ~30px stays crisp, and keeps the page near 220 KB instead of 750 KB.

A download failure is never fatal: the player simply has no cached file and the page
falls back to an initials avatar.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.common import BROWSER_UA, RESOURCES, SourceError, http_get, load_config, read_json, setup_logging

LOG = setup_logging("images")

IMAGES = RESOURCES / "images"
BASE = "https://imagecache.365scores.com/image/upload"

# Round crop centred on the face, 64px, PNG.
ATHLETE_TRANSFORM = (
    "f_png,w_64,h_64,c_limit,q_auto:eco,dpr_1,"
    "d_Athletes:default.png,r_max,c_thumb,g_face,z_0.65"
)
CREST_TRANSFORM = "f_png,w_96,h_96,c_limit,q_auto:eco,dpr_1"

HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "image/png,image/*;q=0.8",
    "Referer": "https://www.365scores.com/",
}


def _save(url: str, path: Path) -> bool:
    try:
        resp = http_get(url, headers=HEADERS, retries=2, backoff=1.5, timeout=20, logger=LOG)
    except SourceError as exc:
        LOG.warning("could not fetch %s: %s", path.name, exc)
        return False
    if not resp.content or not resp.headers.get("Content-Type", "").startswith("image/"):
        LOG.warning("%s did not return an image", path.name)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return True


def main() -> int:
    cfg = load_config()
    season_path = RESOURCES.parent / "data" / f"season_{cfg['season']['slug']}.json"
    season = read_json(season_path)
    if not season:
        LOG.error("no season aggregate found - run hta_aggregate.py first")
        return 1

    IMAGES.mkdir(parents=True, exist_ok=True)
    fetched = skipped = failed = 0

    crest = IMAGES / "crest.png"
    if crest.exists():
        skipped += 1
    else:
        team_id = cfg["team"]["competitor_id"]
        if _save(f"{BASE}/{CREST_TRANSFORM}/v5/Competitors/{team_id}", crest):
            fetched += 1
        else:
            failed += 1

    # Every player who has appeared in the squad list, not just those with minutes -
    # an unused substitute still shows in the roster table.
    seen: set[int] = set()
    for row in season.get("total", []):
        athlete = row.get("athlete_id")
        pid = row.get("player_id")
        if not athlete or athlete in seen:
            continue
        seen.add(athlete)

        path = IMAGES / "players" / f"{athlete}.png"
        if path.exists():
            skipped += 1
            continue

        version = row.get("image_version") or 1
        url = f"{BASE}/{ATHLETE_TRANSFORM}/v{version}/Athletes/{athlete}"
        if _save(url, path):
            fetched += 1
            LOG.info("photo for %s (athlete %s)", row.get("name"), athlete)
        else:
            failed += 1
            LOG.info("no photo for %s - will fall back to initials", row.get("name"))

    total_kb = sum(p.stat().st_size for p in IMAGES.rglob("*.png")) / 1024
    LOG.info(
        "images: %d fetched, %d already cached, %d unavailable | %d files, %.0f KB on disk",
        fetched, skipped, failed, len(list(IMAGES.rglob("*.png"))), total_kb,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
