"""Build a throwaway page that points the live panel at some other club's match.

Why this exists: the live panel can only be proven against a match that is actually being
played, and Hapoel play about once a week. This borrows someone else's live match so the
whole path - browser polls 365scores, normalises the game, renders the panel - can be
watched working on the real public site, on a real phone, without waiting for match day.

Deliberately a SEPARATE script that only READS output/hta_dashboard.html and writes a
DIFFERENT file. Nothing that builds the real dashboard is touched, so this cannot damage
the Hapoel stats: no ingestion runs, no data file is written, and the page it reads is
left exactly as it found it.

    python scripts/make_live_test.py 131 "ריאל מדריד"

Season tables are hidden on the output, because they would still be Hapoel's numbers and
showing them beside another club's scoreline is worse than showing nothing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "output" / "hta_dashboard.html"
TARGET = ROOT / "output" / "hta_live_test.html"

BANNER = """
<div class="banner warn" style="margin:0 0 14px">
  <span class="icon">⚠️</span>
  <div><strong>עמוד בדיקה — לא נתוני הפועל תל אביב.</strong><br>
  העמוד הזה עוקב אחר משחק חי של __TEAM__ כדי לבדוק שהפאנל החי עובד כמצופה.
  הנתונים מתעדכנים בדפדפן עצמו, ללא שרת. הטבלאות העונתיות מוסתרות כאן בכוונה.<br>
  הדף האמיתי: <a href="./">חזרה לסטטיסטיקת העונה</a></div>
</div>
"""

# Hide everything that is Hapoel season data. The live card and the banners stay.
HIDE_CSS = """
<style>
/* Test page only: the season figures below belong to a different club than the live panel
   above, so they are hidden rather than shown misleadingly. */
.wrap > section.card:not(.live-card),
.wrap > .chart-grid,
.tiles,
#deltaCard { display: none !important; }
</style>
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: make_live_test.py <competitor_id> [team name]", file=sys.stderr)
        return 2
    team_id = sys.argv[1].strip()
    if not team_id.isdigit():
        print(f"competitor id must be numeric, got {team_id!r}", file=sys.stderr)
        return 2
    team_name = sys.argv[2] if len(sys.argv) > 2 else team_id

    if not SOURCE.exists():
        print(f"no built dashboard at {SOURCE} - run hta_build.py first", file=sys.stderr)
        return 1

    html = SOURCE.read_text(encoding="utf-8")

    # Swap only the live poller's team. Matched narrowly, and asserted, so a change to the
    # payload shape fails loudly here instead of silently producing a page that quietly
    # follows the wrong club.
    pattern = r'"team_id":\s*\d+'
    if len(re.findall(pattern, html)) != 1:
        print(f"expected exactly one team_id in the payload, found "
              f"{len(re.findall(pattern, html))} - refusing to guess", file=sys.stderr)
        return 1
    html = re.sub(pattern, f'"team_id": {team_id}', html)

    # The renderer hard-codes "our" club's name, which is right on the real dashboard and
    # wrong here - without this the panel reads "הפועל תל אביב 1-0 אינטר". Patched only in
    # this throwaway copy; the dashboard's own code is deliberately left alone.
    ours = "'הפועל תל אביב'"
    if html.count(ours) < 2:
        print(f"expected the renderer's hard-coded club name at least twice, found "
              f"{html.count(ours)} - refusing to guess", file=sys.stderr)
        return 1
    html = html.replace(ours, f"'{team_name}'")

    html = html.replace("</head>", HIDE_CSS + "</head>", 1)
    html = html.replace(
        '<div id="banners"></div>',
        '<div id="banners"></div>' + BANNER.replace("__TEAM__", team_name),
        1,
    )
    html = html.replace("<title>", "<title>[בדיקה] ", 1)

    TARGET.write_text(html, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.name} following competitor {team_id} ({team_name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
