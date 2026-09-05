"""Transfermarkt client - backup tier 1 and the independent cross-check.

Season aggregates only: no per-match detail, and no clean sheets. Its value is that it
is completely unrelated to 365scores, so agreement between the two is real evidence.
Cross-validated during design: both reported 21 players and ~7,908 minutes.

The squad table nests an `<table class="inline-table">` inside the player cell, so rows
must be taken as direct children of `tbody` and the nested table removed before the
cells are read - a naive row/cell scan silently produces garbage here.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from .common import BROWSER_UA, SourceError, http_get

NAME = "transfermarkt"
BASE = "https://www.transfermarkt.com"

HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en;q=0.9",
}

# Stat columns run in a fixed order at the tail of each row, after the identity columns.
# Reading them from the right survives changes to the leading columns.
TAIL_COLUMNS = [
    "apps",
    "goals",
    "assists",
    "yellow",
    "second_yellow",
    "red",
    "subs_on",
    "subs_off",
    "ppg",
    "minutes",
]


def _to_int(text: str) -> int:
    cleaned = (text or "").replace(".", "").replace(",", "").replace("'", "").strip()
    if cleaned in {"", "-"}:
        return 0
    try:
        return int(cleaned)
    except ValueError:
        return 0


class Transfermarkt:
    def __init__(self, config: dict, logger=None):
        self.club_id = config["team"]["transfermarkt_club_id"]
        self.season_id = config["season"]["transfermarkt_season_id"]
        self.logger = logger

    def _fetch(self, url: str) -> BeautifulSoup:
        resp = http_get(url, headers=HEADERS, retries=3, backoff=2.0, timeout=30, logger=self.logger)
        return BeautifulSoup(resp.text, "html.parser")

    def squad_stats(self, competition_code: str | None = None) -> list[dict]:
        """Season aggregates per player, optionally filtered to one competition."""
        if competition_code:
            url = (
                f"{BASE}/x/leistungsdaten/verein/{self.club_id}"
                f"/reldata/{competition_code}%26{self.season_id}/plus/1"
            )
        else:
            url = (
                f"{BASE}/x/leistungsdaten/verein/{self.club_id}"
                f"/plus/1?saison_id={self.season_id}"
            )

        soup = self._fetch(url)
        table = soup.find("table", class_="items")
        if table is None:
            raise SourceError("no squad table found on Transfermarkt page")

        body = table.find("tbody")
        if body is None:
            raise SourceError("squad table has no tbody")

        players = []
        for row in body.find_all("tr", recursive=False):
            if not row.get("class") or row.get("class")[0] not in ("odd", "even"):
                continue

            cells = row.find_all("td", recursive=False)
            if len(cells) < len(TAIL_COLUMNS) + 2:
                continue

            # The player cell holds a nested table (name + position). Pull the name out
            # of it, then drop it so the numeric tail lines up.
            name = None
            for cell in cells:
                inner = cell.find("table", class_="inline-table")
                if inner is not None:
                    link = inner.find("a", title=True)
                    name = link["title"] if link else inner.get_text(" ", strip=True)
                    break

            tail = cells[-len(TAIL_COLUMNS):]
            values = [c.get_text(strip=True) for c in tail]
            record = {"name": name or "?", "source": NAME}
            for key, raw in zip(TAIL_COLUMNS, values):
                record[key] = raw if key == "ppg" else _to_int(raw)

            record["starts"] = max(record["apps"] - record["subs_on"], 0)
            record["bench_apps"] = record["subs_on"]
            players.append(record)

        if not players:
            raise SourceError("Transfermarkt squad table parsed to zero players")
        return players

    def totals(self) -> dict:
        """Compact figures used by the cross-check."""
        players = self.squad_stats()
        with_minutes = [p for p in players if p["minutes"] > 0]
        return {
            "source": NAME,
            "players_with_minutes": len(with_minutes),
            "total_minutes": sum(p["minutes"] for p in with_minutes),
            "total_goals": sum(p["goals"] for p in players),
            "total_assists": sum(p["assists"] for p in players),
            "players": players,
        }
