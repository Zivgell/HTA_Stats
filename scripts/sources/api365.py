"""365scores client - the primary source.

Returns Hebrew natively (langId=2), gives per-match player detail, and updates live.
All parsing keys off stable numeric ids rather than Hebrew display strings:

  eventType.id   1 = goal, 2 = yellow card, 3 = red card, 12 = woodwork, 1000 = substitution
  lineup status  1 = Starting, 2 = Substitute, 3 = Missing, 4 = Management
  game.statusGroup == 4 -> the match is over (covers full time, AET and penalties)
"""
from __future__ import annotations

import time

from .common import BROWSER_UA, SourceError, http_get

NAME = "365scores"

EVENT_GOAL = 1
EVENT_YELLOW = 2
EVENT_RED = 3
EVENT_PENALTY_MISS = 6
EVENT_SUBSTITUTION = 1000

# A shootout is encoded past the end of extra time (121' upward, even when no extra
# time was played). Shootout penalties are never goals, so anything beyond this is
# excluded from goal and assist counts.
SHOOTOUT_AFTER_MINUTE = 120

STATUS_STARTING = 1
STATUS_SUBSTITUTE = 2
STATUS_MISSING = 3

STATUS_GROUP_FINISHED = 4

HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
    "Accept-Language": "he-IL,he;q=0.9",
    "Referer": "https://www.365scores.com/",
}


class Api365:
    def __init__(self, config: dict, logger=None):
        cfg = config["api365"]
        self.base = cfg["base_url"].rstrip("/")
        self.params = dict(cfg["params"])
        self.retries = cfg["retries"]
        self.backoff = cfg["backoff_seconds"]
        self.delay = cfg["delay_between_calls"]
        self.timeout = cfg["timeout_seconds"]
        self.stat_types = config["stat_types"]
        self.team_id = config["team"]["competitor_id"]
        self.logger = logger

    def _get(self, path: str, extra: dict) -> dict:
        params = {**self.params, **extra}
        resp = http_get(
            f"{self.base}/{path}",
            headers=HEADERS,
            params=params,
            retries=self.retries,
            backoff=self.backoff,
            timeout=self.timeout,
            logger=self.logger,
        )
        time.sleep(self.delay)
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"non-JSON response from {path}: {exc}") from exc

    # ---------------------------------------------------------------- feeds

    def results(self) -> list[dict]:
        """Every played match. This feed drives ingestion, not the fixture list."""
        payload = self._get("games/results/", {"competitors": str(self.team_id)})
        return payload.get("games", []) or []

    def fixtures(self) -> list[dict]:
        """Upcoming matches with exact UTC kickoff times, used only for scheduling."""
        payload = self._get("games/fixtures/", {"competitors": str(self.team_id)})
        return payload.get("games", []) or []

    def game(self, game_id: int) -> dict:
        payload = self._get("game/", {"gameId": str(game_id)})
        game = payload.get("game")
        if not game:
            raise SourceError(f"no game object for gameId={game_id}")
        return game

    # ---------------------------------------------------------------- parsing

    @staticmethod
    def is_final(game_summary: dict) -> bool:
        return game_summary.get("statusGroup") == STATUS_GROUP_FINISHED

    def _stat(self, member: dict, key: str):
        wanted = self.stat_types[key]
        for stat in member.get("stats") or []:
            if stat.get("type") == wanted:
                return stat.get("value")
        return None

    @staticmethod
    def _as_number(value, cast=int):
        """Minutes arrive as \"90'\"; percentages and ratios as \"15/16 (94%)\"."""
        if value is None:
            return None
        text = str(value).replace("'", "").strip()
        if not text:
            return None
        try:
            return cast(float(text))
        except ValueError:
            return None

    def _regulation_end(self, game: dict, all_events: list[dict]) -> int:
        """90, or 120 when extra time was played."""
        for stage in game.get("stages") or []:
            if "הארכה" in (stage.get("name") or ""):
                return 120
        for event in all_events:
            minute = self._as_number(event.get("gameTime"), int)
            if minute is not None and 90 < minute <= SHOOTOUT_AFTER_MINUTE:
                return 120
        return 90

    def derive_from_events(self, players, game, all_events, sub_on, sub_off, reg_end):
        """Reconstruct minutes, goals, assists and concessions from the event feed.

        Used only when the source published a lineup but no per-player stats (seen on
        the July Toto Cup tie). Minutes come from the recorded substitutions, which is
        how minutes are calculated in the first place - nothing is invented. What
        genuinely cannot be recovered (ratings, xG, goalkeeper saves) stays empty
        rather than being guessed, and the match is marked as derived.
        """
        our_goals, our_assists = {}, {}
        opponent_goal_minutes = []

        for event in all_events:
            etype = (event.get("eventType") or {}).get("id")
            minute = self._as_number(event.get("gameTime"), int)
            if etype != EVENT_GOAL or minute is None or minute > SHOOTOUT_AFTER_MINUTE:
                continue  # shootout penalties are not goals
            if event.get("competitorId") == self.team_id:
                our_goals[event.get("playerId")] = our_goals.get(event.get("playerId"), 0) + 1
                for assister in event.get("extraPlayers") or []:
                    our_assists[assister] = our_assists.get(assister, 0) + 1
            else:
                opponent_goal_minutes.append(minute)

        for player in players:
            pid = player["player_id"]
            on = sub_on.get(pid)
            off = sub_off.get(pid)

            if player["started"]:
                start = 0
                end = off if off is not None else reg_end
            elif on is not None:
                start = on
                end = off if off is not None else reg_end
            else:
                continue  # named on the bench but never came on

            player["minutes"] = max(end - start, 0)
            player["bench_used"] = (not player["started"]) and player["minutes"] > 0
            player["unused_sub"] = False
            player["goals"] = our_goals.get(pid, 0)
            player["assists"] = our_assists.get(pid, 0)
            player["goals_conceded"] = sum(1 for m in opponent_goal_minutes if start < m <= end)

        return players

    def parse_game(self, game: dict) -> dict:
        """Reduce a ~130 KB payload to the Hapoel-side record we actually store."""
        home, away = game["homeCompetitor"], game["awayCompetitor"]
        if home.get("id") == self.team_id:
            side, opponent, is_home = home, away, True
        elif away.get("id") == self.team_id:
            side, opponent, is_home = away, home, False
        else:
            raise SourceError(f"team {self.team_id} not in game {game.get('id')}")

        # Some payloads carry member entries with no id at all (seen on the Toto Cup
        # penalties tie), so this cannot assume the key is present.
        names = {m["id"]: m for m in game.get("members") or [] if m.get("id") is not None}

        def score(comp):
            raw = comp.get("score")
            return None if raw is None or raw < 0 else int(raw)

        team_score, opp_score = score(side), score(opponent)

        # Cards and substitution minutes come from the event feed; minutes, goals,
        # assists, saves and goals-conceded come from each member's stats block.
        cards: dict[int, list[int]] = {}
        reds: set[int] = set()
        sub_on: dict[int, int] = {}
        sub_off: dict[int, int] = {}
        events_out = []

        for ev in game.get("events") or []:
            if ev.get("competitorId") != self.team_id:
                continue
            etype = (ev.get("eventType") or {}).get("id")
            pid = ev.get("playerId")
            minute = self._as_number(ev.get("gameTime"), int)
            extras = ev.get("extraPlayers") or []

            if etype == EVENT_YELLOW:
                cards.setdefault(pid, []).append(minute)
            elif etype == EVENT_RED:
                reds.add(pid)
            elif etype == EVENT_SUBSTITUTION:
                # playerId comes on, extraPlayers[0] goes off
                if pid is not None:
                    sub_on[pid] = minute
                if extras:
                    sub_off[extras[0]] = minute

            if etype in (EVENT_GOAL, EVENT_YELLOW, EVENT_RED, EVENT_SUBSTITUTION):
                events_out.append(
                    {
                        "minute": minute,
                        "type_id": etype,
                        "type": (ev.get("eventType") or {}).get("name"),
                        # Marked so a shootout conversion is never read back as a goal.
                        "shootout": minute is not None and minute > SHOOTOUT_AFTER_MINUTE,
                        "player_id": pid,
                        "player": (names.get(pid) or {}).get("name"),
                        "extra_player_id": extras[0] if extras else None,
                        "extra_player": (names.get(extras[0]) or {}).get("name") if extras else None,
                    }
                )

        players, missing = [], []
        for member in (side.get("lineups") or {}).get("members") or []:
            status = member.get("status")
            pid = member.get("id")
            if pid is None:
                continue
            info = names.get(pid) or {}
            name = info.get("name") or f"#{pid}"
            position = (member.get("position") or {}).get("name")

            if status == STATUS_MISSING:
                suspension = (member.get("suspension") or {}).get("name")
                injury = member.get("injury") or {}
                missing.append(
                    {
                        "player_id": pid,
                        "name": name,
                        "position": position,
                        "reason": suspension or injury.get("reason"),
                        "expected_return": injury.get("expectedReturn"),
                    }
                )
                continue

            if status not in (STATUS_STARTING, STATUS_SUBSTITUTE):
                continue  # coaching staff

            minutes = self._as_number(self._stat(member, "minutes"), int) or 0
            started = status == STATUS_STARTING
            yellows = cards.get(pid, [])
            # -1 is the "no rating given" sentinel, not a score.
            rating = member.get("ranking")
            rating = float(rating) if rating is not None and float(rating) > 0 else None

            players.append(
                {
                    "player_id": pid,
                    "name": name,
                    "jersey": info.get("jerseyNumber"),
                    # Needed to build the headshot URL; the image path is keyed by
                    # athleteId and versioned, not by the lineup member id above.
                    "athlete_id": info.get("athleteId"),
                    "image_version": info.get("imageVersion"),
                    "position": position,
                    "started": started,
                    # A named substitute who never came on has no appearance.
                    "bench_used": (not started) and minutes > 0,
                    "unused_sub": (not started) and minutes == 0,
                    "minutes": minutes,
                    "goals": self._as_number(self._stat(member, "goals"), int) or 0,
                    "assists": self._as_number(self._stat(member, "assists"), int) or 0,
                    "saves": self._as_number(self._stat(member, "saves"), int) or 0,
                    "goals_conceded": self._as_number(self._stat(member, "goals_conceded"), int) or 0,
                    "xg": self._as_number(self._stat(member, "xg"), float) or 0.0,
                    "rating": rating,
                    "yellow": 1 if yellows else 0,
                    # Two yellows in one match is a second-yellow dismissal.
                    "second_yellow": 1 if len(yellows) >= 2 else 0,
                    "red": 1 if pid in reds else 0,
                    "sub_on_minute": sub_on.get(pid),
                    "sub_off_minute": sub_off.get(pid),
                }
            )

        result = None
        if team_score is not None and opp_score is not None:
            result = "W" if team_score > opp_score else "L" if team_score < opp_score else "D"

        # Some ties (the July Toto Cup penalties match) carry lineups but no per-player
        # stats. Rather than leaving them blank, minutes/goals/assists are rebuilt from
        # the event feed; ratings, xG and saves are unrecoverable and stay empty. The
        # match stays flagged so a later run can upgrade it to real stats.
        stats_complete = any(p["minutes"] > 0 for p in players)
        stats_derived = False
        if not stats_complete and players:
            reg_end = self._regulation_end(game, game.get("events") or [])
            players = self.derive_from_events(
                players, game, game.get("events") or [], sub_on, sub_off, reg_end
            )
            stats_derived = any(p["minutes"] > 0 for p in players)

        return {
            "game_id": game.get("id"),
            "source": NAME,
            "stats_complete": stats_complete,
            "stats_derived": stats_derived,
            "competition_id": game.get("competitionId"),
            "competition_name": game.get("competitionDisplayName"),
            "round": game.get("roundNum"),
            "start_time": game.get("startTime"),
            "status_text": game.get("statusText"),
            "final": game.get("statusGroup") == STATUS_GROUP_FINISHED,
            "venue": (game.get("venue") or {}).get("name"),
            "is_home": is_home,
            "opponent": opponent.get("name"),
            "team_score": team_score,
            "opponent_score": opp_score,
            "result": result,
            "team_clean_sheet": opp_score == 0 if opp_score is not None else None,
            "players": players,
            "missing": missing,
            "events": sorted(events_out, key=lambda e: (e["minute"] is None, e["minute"])),
        }
