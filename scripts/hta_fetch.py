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
from datetime import datetime, timedelta, timezone
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


PRESERVE_ON_REFETCH = ("rating", "xg")


def merge_preserving(fresh: dict, cached: dict | None) -> dict:
    """Let a re-fetch add or correct data, but never erase it.

    365scores drops player ratings for older matches - querying the July fixture returns
    no ranking field at all, while September still has all of them. Because a re-fetch
    overwrites the cached record wholesale, running --force after a rating expired would
    silently strip a value we had captured while it was still published, and it is not
    recoverable from anywhere.
    """
    if not cached:
        return fresh

    previous = {p.get("player_id"): p for p in cached.get("players") or []}
    for player in fresh.get("players") or []:
        old = previous.get(player.get("player_id"))
        if not old:
            continue
        for field in PRESERVE_ON_REFETCH:
            if player.get(field) in (None, 0, 0.0) and old.get(field):
                player[field] = old[field]
    return fresh


# Roughly 20 scheduled runs. Long enough to survive a quiet night or a source outage,
# short enough that a match whose ratings are never coming stops costing a request.
RATING_RETRY_HOURS = 48


def has_ratings(record: dict) -> bool:
    """Does this record carry a usable set of ratings, not just the first few?

    Deliberately not `any()`. Man of the match is the highest rating in the match, so a
    half-published set can crown the wrong player and then be frozen in place by the very
    caching this check guards. Requiring a majority of the players who actually took the
    field costs at most a few extra requests inside the retry window and cannot pick a
    winner from a partial field. Unrated cameos are normal and expected, which is why this
    is a majority and not "all".
    """
    played = [p for p in record.get("players") or [] if (p.get("minutes") or 0) > 0]
    if not played:
        return False
    rated = [p for p in played if (p.get("rating") or 0) > 0]
    return len(rated) * 2 >= len(played)


def within_retry_window(record: dict, now: datetime) -> bool:
    """Is this match still young enough to be worth asking about again?

    A record with no usable kickoff time is treated as too old. Retrying is the
    unbounded option, and a malformed file must not buy itself a request on every run
    from now until forever.
    """
    start = _parse_time(record.get("start_time"))
    return bool(start and now - start < timedelta(hours=RATING_RETRY_HOURS))


def merge_feeds(*feeds: list[dict]) -> list[dict]:
    """One game list, deduplicated by id, with later feeds winning.

    results() and recent() overlap heavily but each sees things the other does not:
    results() covers the whole season, recent() covers the last fortnight and includes
    matches results() has not caught up with yet. Later feeds win, so the fresher view of
    a game's status is the one that survives.
    """
    merged: dict[str, dict] = {}
    for feed in feeds:
        for game in feed or []:
            gid = game.get("id")
            if gid is not None:
                merged[str(gid)] = game
    return list(merged.values())


LIVE_PATH = DATA / "live_match.json"


def capture_live(api: Api365, live_games: list[dict], already_handled: set[str]) -> str | None:
    """Snapshot a match that is under way, well away from the season totals.

    Written to its own file rather than data/matches/, so hta_aggregate.py cannot see it
    and the season figures stay full-time-only. That is the point: a 0-0 at halftime is
    not a clean sheet and a 1-0 lead is not a win, so מאזן and שערים נקיים must not move
    until the whistle. Only goals, assists and cards are meaningful in-play, and only
    those are rendered.

    The file is always written - {} when nothing is live - rather than deleted, so the
    committed path is stable and a finished match reliably clears the panel.
    """
    for game in live_games:
        gid = str(game.get("id"))
        if gid in already_handled:
            continue  # went final between reading the feed and now; the real record wins
        try:
            record = api.parse_game(api.game(int(gid)))
        except Exception as exc:  # noqa: BLE001
            LOG.error("could not read live game %s: %s", gid, exc)
            continue
        write_json(LIVE_PATH, record)
        LOG.info("live: %s %s-%s %s (%s)", record["competition_name"], record["team_score"],
                 record["opponent_score"], record["opponent"], record.get("status_text"))
        return gid

    write_json(LIVE_PATH, {})
    return None


def ingest_matches(api: Api365, cfg: dict, *, force: bool = False) -> dict:
    """Fetch every finished match we do not already hold. Returns a run summary."""
    MATCHES.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    # Two feeds, because one is not enough. See Api365.recent(): the plain results feed
    # silently omitted a league match that had finished an hour earlier, and the fixtures
    # feed had already dropped it, so it was invisible to ingestion despite its own game
    # record being complete.
    games = merge_feeds(api.results(), api.recent())
    in_scope = [g for g in games if relevant(g, cfg)]
    finished = [g for g in in_scope if api.is_final(g)]
    # statusGroup == 3, now read off real live games rather than inferred. The earlier
    # "not final and kicked off" test would have called a postponed or abandoned match
    # live, and shown a frozen 0-0 as though it were in progress.
    live_games = [g for g in in_scope if api.is_live(g)]
    LOG.info("feeds: %d games, %d in scope, %d finished, %d in play",
             len(games), len(in_scope), len(finished), len(live_games))

    # A match is only "done" once it is final AND carries real player stats. Matches
    # cached without stats stay eligible for re-fetching in case they fill in later.
    #
    # Ratings need the same treatment, for a subtler reason. 365scores marks a match final
    # and publishes minutes at the whistle, but computes player ratings some minutes after.
    # A run landing in that gap would cache a "complete" record with no ratings and never
    # look again, losing man of the match for that game permanently - and the routine now
    # fires about two minutes after full time, squarely inside the gap.
    #
    # Both retries are bounded by match age, because neither absence is always temporary:
    # the July cup tie has never carried stats, and ratings for the July league games have
    # since expired upstream. Unbounded, those would each cost a request on every run for
    # the rest of the season.
    cached, incomplete, awaiting_ratings = set(), set(), set()
    for path in MATCHES.glob("*.json"):
        cached.add(path.stem)
        record = read_json(path) or {}
        if not within_retry_window(record, now):
            continue
        if not record.get("stats_complete", True):
            incomplete.add(path.stem)
        elif not has_ratings(record):
            awaiting_ratings.add(path.stem)
    if incomplete:
        LOG.info("%d cached match(es) still missing player stats, will retry: %s",
                 len(incomplete), ", ".join(sorted(incomplete)))
    if awaiting_ratings:
        LOG.info("%d cached match(es) still missing player ratings, will retry: %s",
                 len(awaiting_ratings), ", ".join(sorted(awaiting_ratings)))
    retry = incomplete | awaiting_ratings

    # A match re-pulled because it was missing stats is NOT news - counting it as new
    # would fire the notification on every single run, forever.
    new_ids, refreshed_ids, failed = [], [], []

    for game in finished:
        gid = str(game.get("id"))
        if gid in cached and gid not in retry and not force:
            continue
        try:
            record = merge_preserving(
                api.parse_game(api.game(int(gid))),
                read_json(MATCHES / f"{gid}.json"),
            )
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

    live_id = capture_live(api, live_games, already_handled={*new_ids, *refreshed_ids})

    return {
        "finished_in_scope": len(finished),
        "already_cached": len(cached),
        "live_match": live_id,
        "newly_ingested": new_ids,
        "refreshed_incomplete": refreshed_ids,
        # Matches still waiting on ratings AFTER this run - i.e. ones that will be asked
        # about again. A match topped up successfully this run drops out of the set.
        "awaiting_ratings": sorted(
            gid for gid in awaiting_ratings
            if not has_ratings(read_json(MATCHES / f"{gid}.json") or {})
        ),
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
