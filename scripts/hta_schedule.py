"""Decide when the tracker should next wake up, and flag an unreliable kickoff time.

Runs at the end of every update, so the schedule is re-read and the trigger rewritten
every single time - there is no static timetable.

365scores publishes no "provisional" marker, so a placeholder kickoff is inferred from
two signals seen in the live data:

  * Tail run  - the fixture list ends in a block of consecutive matches sharing one
                identical local kick-off time. On 2026-09-05 rounds 15-25 were all
                listed at exactly 19:00 while rounds 3-14 carried varied real times
                (20:00, 20:30, 15:45, 19:15, 14:00 ...). That uniform tail is the
                league's default slot, not a schedule.
  * Instability - a kickoff that moved between runs, tracked in fixture_history.json.

A flag never stands alone: an unconfirmed kickoff always falls back to a safe cadence,
so the tracker degrades to checking daily rather than going silent.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.common import DATA, MATCHES, load_config, read_json, setup_logging, write_json

LOG = setup_logging("schedule")

OK = "ok"
WARN = "warning"
ALERT = "alert"


def parse_time(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def provisional_ids(fixtures, tz: ZoneInfo, min_run: int, now, confirm_days: int) -> set[int]:
    """Fixture ids sitting in the uniform placeholder block at the tail of the list.

    Two conditions must both hold, and the second is what makes this survive the season:

      1. the fixture is inside a run of consecutive matches sharing one identical
         local kick-off time at the end of the list, and
      2. it is still further away than the confirmation window.

    Condition 2 replaced an earlier "only trust the tail run if earlier fixtures vary"
    rule, which had a nasty failure mode: once the varied near-term fixtures have been
    played and only the uniform block is left, that rule disabled detection entirely
    and called an obvious placeholder confirmed. Lead time does not decay that way -
    Israeli kick-off times are fixed by TV roughly two weeks out, so a match inside the
    window is real even when its time happens to match the block.
    """
    timed = [(f, parse_time(f.get("start_time"))) for f in fixtures]
    timed = [(f, t) for f, t in timed if t]
    if len(timed) < min_run:
        return set()

    timed.sort(key=lambda pair: pair[1])
    last_slot = timed[-1][1].astimezone(tz).strftime("%H:%M")

    run = []
    for fixture, moment in reversed(timed):
        if moment.astimezone(tz).strftime("%H:%M") == last_slot:
            run.append((fixture["game_id"], moment))
        else:
            break

    if len(run) < min_run:
        return set()

    horizon = now + timedelta(days=confirm_days)
    return {gid for gid, moment in run if moment > horizon}


def unstable_ids(history: dict) -> set[int]:
    """Fixtures whose kickoff has moved at least once since we started watching."""
    return {int(gid) for gid, seen in history.items() if len(seen) > 1}


def decide(cfg: dict) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    sched = cfg["schedule"]
    now = datetime.now(timezone.utc)

    fixtures_doc = read_json(DATA / "fixtures.json", default={}) or {}
    fixtures = fixtures_doc.get("fixtures") or []
    history = read_json(DATA / "fixture_history.json", default={}) or {}
    fetch_status = read_json(DATA / "fetch_status.json", default={}) or {}
    previous = read_json(DATA / "schedule_status.json", default={}) or {}
    cached = {p.stem for p in MATCHES.glob("*.json")}

    def result(next_run, state, flag=None, flag_he=None, **extra):
        payload = {
            "computed_at": now.isoformat(),
            "next_run_utc": next_run.isoformat(),
            "next_run_local": next_run.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "state": state,
            "severity": OK if not flag else (ALERT if flag_he and flag == ALERT else flag),
            "flag_he": flag_he,
        }
        payload.update(extra)
        return payload

    # 1. The fixture feed itself failed - retry sooner than a normal cycle.
    if fetch_status and fetch_status.get("fixtures_ok") is False:
        return result(
            now + timedelta(hours=sched["fetch_failed_check_hours"]),
            "fixture_fetch_failed",
            flag=ALERT,
            flag_he="כשל בעדכון לוח המשחקים",
        )

    # 2. A match that should have finished by now is not in the cache yet.
    buffer = timedelta(hours=sched["post_match_buffer_hours"])
    awaiting = None
    for fixture in fixtures:
        kickoff = parse_time(fixture.get("start_time"))
        if kickoff and kickoff + buffer <= now and str(fixture["game_id"]) not in cached:
            awaiting = fixture
            break

    if awaiting:
        gid = awaiting["game_id"]
        tries = previous.get("retry_count", 0) + 1 if previous.get("awaiting_game_id") == gid else 1
        if tries <= sched["not_finished_max_retries"]:
            return result(
                now + timedelta(minutes=sched["not_finished_retry_minutes"]),
                "awaiting_result",
                awaiting_game_id=gid,
                retry_count=tries,
                awaiting_opponent=f"{awaiting.get('home')} - {awaiting.get('away')}",
            )
        return result(
            now + timedelta(hours=sched["unconfirmed_check_hours"]),
            "awaiting_result_gave_up",
            flag=ALERT,
            flag_he="תוצאת המשחק טרם התפרסמה",
            awaiting_game_id=gid,
            retry_count=tries,
        )

    # 3. No fixtures at all - cup undrawn, or the playoff split not yet published.
    upcoming = sorted(
        [(f, parse_time(f.get("start_time"))) for f in fixtures],
        key=lambda pair: (pair[1] is None, pair[1]),
    )
    upcoming = [(f, t) for f, t in upcoming if t and t > now]

    if not upcoming:
        return result(
            now + timedelta(hours=sched["no_fixtures_check_hours"]),
            "no_fixtures",
            flag=ALERT,
            flag_he="אין מועד ידוע למשחק הבא",
        )

    fixture, kickoff = upcoming[0]
    provisional = provisional_ids(
        fixtures, tz, sched["provisional_tail_run_min"], now, sched["confirm_window_days"]
    )
    unstable = unstable_ids(history)
    is_provisional = fixture["game_id"] in provisional
    is_unstable = fixture["game_id"] in unstable

    next_match = {
        "game_id": fixture["game_id"],
        "competition": fixture.get("competition_name"),
        "round": fixture.get("round"),
        "home": fixture.get("home"),
        "away": fixture.get("away"),
        "kickoff_utc": fixture.get("start_time"),
        "kickoff_local": kickoff.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "provisional_block": is_provisional,
        "recently_moved": is_unstable,
    }

    counts = {
        "fixtures_total": len(fixtures),
        "fixtures_provisional": len(provisional),
        "fixtures_recently_moved": len(unstable),
    }

    if is_provisional or is_unstable:
        why = "שעה זמנית בלוח" if is_provisional else "מועד המשחק שונה לאחרונה"
        return result(
            min(kickoff + buffer, now + timedelta(hours=sched["unconfirmed_check_hours"])),
            "next_match_unconfirmed",
            flag=WARN,
            flag_he=f"שעת המשחק הבא טרם אושרה ({why})",
            next_match=next_match,
            **counts,
        )

    return result(kickoff + buffer, "next_match_confirmed", next_match=next_match, **counts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute the tracker's next wake-up")
    parser.add_argument("--dry-run", action="store_true", help="print the decision, write nothing")
    args = parser.parse_args()

    cfg = load_config()
    decision = decide(cfg)

    if not args.dry_run:
        write_json(DATA / "schedule_status.json", decision)

    level = LOG.info if decision["severity"] == OK else LOG.warning
    level(
        "next run %s (local) | state=%s%s",
        decision["next_run_local"],
        decision["state"],
        f" | FLAG: {decision['flag_he']}" if decision.get("flag_he") else "",
    )
    if decision.get("next_match"):
        nm = decision["next_match"]
        LOG.info("next match: %s vs %s | %s | kickoff %s",
                 nm["home"], nm["away"], nm["competition"], nm["kickoff_local"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
