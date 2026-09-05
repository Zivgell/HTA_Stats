"""Fold cached per-match records into season aggregates, clean sheets and deltas.

Because every match is stored individually, aggregates are recomputed from scratch on
each run. That makes the numbers self-healing: correcting a cached match file and
re-running fixes the season totals, with no incremental state to drift.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.common import DATA, MATCHES, load_config, read_json, setup_logging, write_json

LOG = setup_logging("aggregate")

GOALKEEPER = "שוער"

COUNTERS = (
    "apps", "starts", "bench_apps", "subbed_off", "minutes", "goals", "assists",
    "yellow", "second_yellow", "red", "clean_sheets", "goals_conceded", "saves",
    "unused_sub",
)


def _blank(player: dict) -> dict:
    row = {
        "player_id": player["player_id"],
        "name": player["name"],
        "jersey": player.get("jersey"),
        "position": player.get("position"),
        "xg": 0.0,
        "_rating_sum": 0.0,
        "_rating_count": 0,
    }
    row.update({key: 0 for key in COUNTERS})
    return row


def _is_clean_sheet(player: dict, match: dict, min_minutes: int) -> bool:
    """Goalkeepers need only to have played; outfielders need the standard 60 minutes."""
    if not match.get("team_clean_sheet"):
        return False
    minutes = player.get("minutes") or 0
    if minutes <= 0:
        return False
    if player.get("position") == GOALKEEPER:
        return True
    return minutes >= min_minutes


def accumulate(matches: list[dict], cfg: dict) -> dict:
    min_minutes = cfg["schedule"]["clean_sheet_min_minutes"]
    names_he = cfg["competitions"]["names_he"]

    buckets: dict[str, dict[int, dict]] = {"total": {}}
    competitions: dict[str, dict] = {}
    timeline: dict[int, list[dict]] = {}

    for match in sorted(matches, key=lambda m: m.get("start_time") or ""):
        comp_id = str(match.get("competition_id"))
        competitions.setdefault(
            comp_id,
            {
                "id": match.get("competition_id"),
                "name": names_he.get(comp_id) or match.get("competition_name"),
                "matches": 0,
                "w": 0, "d": 0, "l": 0,
                "goals_for": 0, "goals_against": 0, "clean_sheets": 0,
            },
        )
        comp = competitions[comp_id]
        comp["matches"] += 1
        if match.get("result") == "W":
            comp["w"] += 1
        elif match.get("result") == "D":
            comp["d"] += 1
        elif match.get("result") == "L":
            comp["l"] += 1
        comp["goals_for"] += match.get("team_score") or 0
        comp["goals_against"] += match.get("opponent_score") or 0
        if match.get("team_clean_sheet"):
            comp["clean_sheets"] += 1

        buckets.setdefault(comp_id, {})

        for player in match["players"]:
            pid = player["player_id"]
            played = (player.get("minutes") or 0) > 0 or player.get("started")
            clean = _is_clean_sheet(player, match, min_minutes)

            for key in ("total", comp_id):
                row = buckets[key].setdefault(pid, _blank(player))
                row["name"] = player["name"]
                if player.get("jersey") is not None:
                    row["jersey"] = player["jersey"]
                if player.get("position"):
                    row["position"] = player["position"]

                if played:
                    row["apps"] += 1
                    row["starts"] += 1 if player.get("started") else 0
                    row["bench_apps"] += 1 if player.get("bench_used") else 0
                    row["subbed_off"] += 1 if player.get("sub_off_minute") is not None else 0
                    row["minutes"] += player.get("minutes") or 0
                    row["goals"] += player.get("goals") or 0
                    row["assists"] += player.get("assists") or 0
                    row["yellow"] += player.get("yellow") or 0
                    row["second_yellow"] += player.get("second_yellow") or 0
                    row["red"] += player.get("red") or 0
                    row["saves"] += player.get("saves") or 0
                    row["goals_conceded"] += player.get("goals_conceded") or 0
                    row["xg"] += player.get("xg") or 0.0
                    row["clean_sheets"] += 1 if clean else 0
                    # 365scores uses -1 as "no rating given"; averaging it drags
                    # a real rating below zero, so only positive ratings count.
                    rating = player.get("rating")
                    if rating is not None and float(rating) > 0:
                        row["_rating_sum"] += float(rating)
                        row["_rating_count"] += 1
                elif player.get("unused_sub"):
                    row["unused_sub"] += 1

            if played:
                timeline.setdefault(pid, []).append(
                    {
                        "game_id": match["game_id"],
                        "date": (match.get("start_time") or "")[:10],
                        "competition": names_he.get(comp_id) or match.get("competition_name"),
                        "opponent": match.get("opponent"),
                        "is_home": match.get("is_home"),
                        "score": f"{match.get('team_score')}-{match.get('opponent_score')}",
                        "result": match.get("result"),
                        "started": player.get("started"),
                        "minutes": player.get("minutes"),
                        "goals": player.get("goals"),
                        "assists": player.get("assists"),
                        "yellow": player.get("yellow"),
                        "second_yellow": player.get("second_yellow"),
                        "red": player.get("red"),
                        "clean_sheet": clean,
                        "rating": player.get("rating"),
                    }
                )

    def finish(rows: dict[int, dict]) -> list[dict]:
        out = []
        for row in rows.values():
            row = dict(row)
            count = row.pop("_rating_count")
            total = row.pop("_rating_sum")
            row["avg_rating"] = round(total / count, 2) if count else None
            row["xg"] = round(row["xg"], 2)
            row["minutes_per_goal"] = round(row["minutes"] / row["goals"]) if row["goals"] else None
            row["goal_involvements"] = row["goals"] + row["assists"]
            # The invariant that catches a parsing regression immediately.
            if row["apps"] != row["starts"] + row["bench_apps"]:
                raise AssertionError(
                    f"appearance mismatch for {row['name']}: "
                    f"apps={row['apps']} starts={row['starts']} bench={row['bench_apps']}"
                )
            out.append(row)
        out.sort(key=lambda r: (-r["minutes"], r["name"]))
        return out

    incomplete = [
        {
            "game_id": m["game_id"],
            "date": (m.get("start_time") or "")[:10],
            "opponent": m.get("opponent"),
            "competition": names_he.get(str(m.get("competition_id"))) or m.get("competition_name"),
            "derived": bool(m.get("stats_derived")),
        }
        for m in matches
        if not m.get("stats_complete", True)
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": load_config()["season"]["label"],
        "matches_counted": len(matches),
        "matches_missing_stats": incomplete,
        "competitions": list(competitions.values()),
        "total": finish(buckets["total"]),
        "by_competition": {cid: finish(rows) for cid, rows in buckets.items() if cid != "total"},
        "timeline": {str(pid): entries for pid, entries in timeline.items()},
    }


def build_delta(previous: dict | None, current: dict) -> dict:
    """What changed since the last aggregate - the 'what happened in that match' panel."""
    if not previous:
        return {"is_first_run": True, "changes": [], "new_matches": current["matches_counted"]}

    before = {r["player_id"]: r for r in previous.get("total", [])}
    tracked = ("goals", "assists", "minutes", "apps", "starts", "bench_apps",
               "yellow", "second_yellow", "red", "clean_sheets", "saves")

    changes = []
    for row in current["total"]:
        old = before.get(row["player_id"])
        diffs = {k: row[k] - (old[k] if old else 0) for k in tracked}
        diffs = {k: v for k, v in diffs.items() if v}
        if diffs:
            changes.append({"player_id": row["player_id"], "name": row["name"], "diff": diffs})

    return {
        "is_first_run": False,
        "new_matches": current["matches_counted"] - previous.get("matches_counted", 0),
        "changes": changes,
    }


HE_LABEL = {
    "goals": "שערים", "assists": "בישולים", "minutes": "דקות", "apps": "הופעות",
    "starts": "הרכב פותח", "bench_apps": "מהספסל", "yellow": "צהוב",
    "second_yellow": "צהוב שני", "red": "אדום", "clean_sheets": "שער נקי", "saves": "הצלות",
}


def append_changelog(delta: dict, current: dict) -> None:
    if delta.get("is_first_run"):
        line = f"טעינה ראשונית: {current['matches_counted']} משחקים נטענו."
        entries = []
    elif not delta["changes"]:
        return
    else:
        line = f"עודכנו {delta['new_matches']} משחקים חדשים."
        entries = [
            f"- {c['name']}: "
            + ", ".join(f"+{v} {HE_LABEL.get(k, k)}" if v > 0 else f"{v} {HE_LABEL.get(k, k)}"
                        for k, v in c["diff"].items())
            for c in delta["changes"]
        ]

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    path = DATA / "changelog.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# יומן עדכונים\n"
    block = f"\n## {stamp}\n\n{line}\n" + ("\n".join(entries) + "\n" if entries else "")
    path.write_text(existing + block, encoding="utf-8")


def write_notification(delta: dict, matches: list[dict], cfg: dict) -> None:
    """Compose the toast text here, in Python, not in PowerShell.

    Windows PowerShell 5.1 reads a .ps1 as ANSI unless the file carries a UTF-8 BOM,
    which mangles Hebrew string literals and breaks the parser outright. Keeping every
    Hebrew string on this side of the boundary avoids that class of bug entirely -
    run_update.ps1 just reads this JSON and passes it through.
    """
    path = DATA / "notification.json"
    changes = delta.get("changes") or []
    if delta.get("is_first_run") or not changes:
        write_json(path, {"show": False})
        return

    goals = sum(c["diff"].get("goals", 0) for c in changes)
    scorers = [c["name"] for c in changes if c["diff"].get("goals", 0) > 0]

    newest = max(matches, key=lambda m: m.get("start_time") or "")
    home, away = (
        (cfg["team"]["name_he"], newest.get("opponent"))
        if newest.get("is_home")
        else (newest.get("opponent"), cfg["team"]["name_he"])
    )
    score = f"{newest.get('team_score')}-{newest.get('opponent_score')}"
    if not newest.get("is_home"):
        score = f"{newest.get('opponent_score')}-{newest.get('team_score')}"

    body = f"{home} {score} {away}"
    if scorers:
        body += " · " + ", ".join(scorers[:3])
    elif goals:
        body += f" · {goals} שערים"

    write_json(path, {"show": True, "title": "הפועל תל אביב — עודכן", "body": body})


def main() -> int:
    cfg = load_config()
    matches = [read_json(p) for p in MATCHES.glob("*.json")]
    matches = [m for m in matches if m]
    if not matches:
        LOG.error("no cached matches found - run hta_fetch.py first")
        return 1

    season_path = DATA / f"season_{cfg['season']['slug']}.json"
    previous = read_json(season_path)

    current = accumulate(matches, cfg)
    delta = build_delta(previous, current)

    write_json(season_path, current)
    write_json(DATA / "last_delta.json", delta)
    append_changelog(delta, current)
    write_notification(delta, matches, cfg)

    total_minutes = sum(r["minutes"] for r in current["total"])
    with_minutes = len([r for r in current["total"] if r["minutes"] > 0])
    LOG.info(
        "aggregated %d matches | %d players with minutes | %d total minutes | %d changes",
        current["matches_counted"], with_minutes, total_minutes, len(delta.get("changes", [])),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
