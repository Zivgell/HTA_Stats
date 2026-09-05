"""Results-driven ingestion.

The fixture list is only ever used to decide *when* to run. Ingestion is keyed off the
results feed, so a cup tie that was drawn, scheduled and played entirely between two
runs still gets picked up. A missed fixture delays an update; it never loses data.

Each finished match is cached under data/matches/<gameId>.json and never re-fetched, so
365scores throttling only bites during the first backfill.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.api365 import Api365
from sources.common import (
    DATA,
    MATCHES,
    SourceError,
    load_config,
    read_json,
    setup_logging,
    write_json,
)
from sources.transfermarkt import Transfermarkt

LOG = setup_logging("fetch")


def _parse_time(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def relevant(game: dict, cfg: dict) -> bool:
    """Current-season, non-excluded competitions only."""
    excluded = set(cfg["competitions"]["excluded_ids"])
    if game.get("competitionId") in excluded:
        return False
    start = _parse_time(game.get("startTime"))
    season_start = _parse_time(cfg["season"]["start_date"] + "T00:00:00+00:00")
    return bool(start and season_start and start >= season_start)


def ingest_matches(api: Api365, cfg: dict, *, force: bool = False) -> dict:
    """Fetch every finished match we do not already hold. Returns a run summary."""
    MATCHES.mkdir(parents=True, exist_ok=True)

    games = api.results()
    finished = [g for g in games if api.is_final(g) and relevant(g, cfg)]
    LOG.info("results feed: %d games, %d finished and in scope", len(games), len(finished))

    # A match is only "done" once it is final AND carries real player stats. Matches
    # cached without stats stay eligible for re-fetching in case they fill in later.
    cached, incomplete = set(), set()
    for path in MATCHES.glob("*.json"):
        cached.add(path.stem)
        record = read_json(path) or {}
        if not record.get("stats_complete", True):
            incomplete.add(path.stem)
    if incomplete:
        LOG.info("%d cached match(es) still missing player stats, will retry: %s",
                 len(incomplete), ", ".join(sorted(incomplete)))

    # A match re-pulled because it was missing stats is NOT news - counting it as new
    # would fire the notification on every single run, forever.
    new_ids, refreshed_ids, failed = [], [], []

    for game in finished:
        gid = str(game.get("id"))
        if gid in cached and gid not in incomplete and not force:
            continue
        try:
            record = api.parse_game(api.game(int(gid)))
        except Exception as exc:  # noqa: BLE001
            # One bad match - throttling, or a malformed payload - must not sink the
            # whole run. It stays uncached and is retried on the next run.
            LOG.error("could not ingest game %s: %s", gid, exc)
            failed.append(gid)
            continue
        write_json(MATCHES / f"{gid}.json", record)
        (refreshed_ids if gid in cached else new_ids).append(gid)
        LOG.info(
            "ingested %s | %s %s-%s %s | %d players",
            gid,
            record["competition_name"],
            record["team_score"],
            record["opponent_score"],
            record["opponent"],
            len(record["players"]),
        )

    return {
        "finished_in_scope": len(finished),
        "already_cached": len(cached),
        "newly_ingested": new_ids,
        "refreshed_incomplete": refreshed_ids,
        "failed": failed,
    }


def refresh_fixtures(api: Api365, cfg: dict) -> dict:
    """Store upcoming fixtures plus a per-run history of each kickoff time.

    The history is what lets the scheduler tell a confirmed kickoff from a placeholder:
    a time that has held steady across runs is trusted, one that just moved is not.
    """
    games = api.fixtures()
    names = cfg["competitions"]["names_he"]
    excluded = set(cfg["competitions"]["excluded_ids"])

    fixtures = []
    for game in games:
        if game.get("competitionId") in excluded:
            continue
        fixtures.append(
            {
                "game_id": game.get("id"),
                "competition_id": game.get("competitionId"),
                "competition_name": names.get(str(game.get("competitionId")))
                or game.get("competitionDisplayName"),
                "round": game.get("roundNum"),
                "start_time": game.get("startTime"),
                "home": (game.get("homeCompetitor") or {}).get("name"),
                "away": (game.get("awayCompetitor") or {}).get("name"),
                "status_text": game.get("statusText"),
            }
        )
    fixtures.sort(key=lambda f: f["start_time"] or "")

    now = datetime.now(timezone.utc).isoformat()
    write_json(DATA / "fixtures.json", {"fetched_at": now, "fixtures": fixtures})

    history = read_json(DATA / "fixture_history.json", default={}) or {}
    for fixture in fixtures:
        gid = str(fixture["game_id"])
        seen = history.setdefault(gid, [])
        if not seen or seen[-1]["start_time"] != fixture["start_time"]:
            seen.append({"seen_at": now, "start_time": fixture["start_time"]})
        # Keep the tail only; we just need "has this time been stable".
        history[gid] = seen[-5:]
    write_json(DATA / "fixture_history.json", history)

    LOG.info("fixtures refreshed: %d upcoming", len(fixtures))
    return {"count": len(fixtures), "fixtures": fixtures}


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Hapoel Tel Aviv match data")
    parser.add_argument("--force", action="store_true", help="re-fetch already cached matches")
    parser.add_argument("--fixtures-only", action="store_true", help="refresh fixtures and exit")
    args = parser.parse_args()

    cfg = load_config()
    api = Api365(cfg, LOG)
    status = {"ran_at": datetime.now(timezone.utc).isoformat(), "source": "365scores"}

    try:
        fixture_info = refresh_fixtures(api, cfg)
        status["fixtures"] = fixture_info["count"]
        status["fixtures_ok"] = True
    except SourceError as exc:
        LOG.error("fixture refresh failed: %s", exc)
        status["fixtures_ok"] = False
        status["fixtures_error"] = str(exc)

    if args.fixtures_only:
        write_json(DATA / "fetch_status.json", status)
        return 0 if status.get("fixtures_ok") else 1

    try:
        summary = ingest_matches(api, cfg, force=args.force)
        status.update(summary)
        status["ingest_ok"] = True
    except SourceError as exc:
        # Tier 1 is down entirely. Fall back so the run still reports something useful.
        LOG.error("365scores ingestion failed: %s", exc)
        status["ingest_ok"] = False
        status["ingest_error"] = str(exc)
        try:
            LOG.warning("falling back to Transfermarkt aggregates")
            totals = Transfermarkt(cfg, LOG).totals()
            write_json(DATA / "fallback_transfermarkt.json", totals)
            status["source"] = "transfermarkt"
            status["fallback_players"] = totals["players_with_minutes"]
            status["fallback_minutes"] = totals["total_minutes"]
        except SourceError as exc2:
            LOG.error("Transfermarkt fallback also failed: %s", exc2)
            status["fallback_error"] = str(exc2)

    write_json(DATA / "fetch_status.json", status)
    LOG.info("fetch complete: %s", {k: v for k, v in status.items() if k != "fixtures"})
    return 0 if status.get("ingest_ok") or status.get("source") == "transfermarkt" else 1


if __name__ == "__main__":
    raise SystemExit(main())
