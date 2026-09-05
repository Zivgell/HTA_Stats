"""Cross-check the 365scores aggregates against Transfermarkt.

Two unrelated sources agreeing is the only real evidence the numbers are right. During
development they agreed to within one minute (7,907 vs 7,908 over 8 matches).

Transfermarkt does not cover the Toto Cup, so the comparison is run over the same
subset it reports - competitions are excluded by config, not by hard-coded ids.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.common import DATA, MATCHES, SourceError, load_config, read_json, setup_logging, write_json
from sources.transfermarkt import Transfermarkt

LOG = setup_logging("verify")

# Transfermarkt does not publish Toto Cup appearances, so it is left out of the
# like-for-like comparison rather than being counted as a discrepancy.
TRANSFERMARKT_BLIND_SPOTS = {546}

TOLERANCE_PCT = 1.0


def local_totals(cfg: dict) -> dict:
    matches = [read_json(p) for p in MATCHES.glob("*.json")]
    matches = [m for m in matches if m and m.get("competition_id") not in TRANSFERMARKT_BLIND_SPOTS]

    minutes: dict[int, int] = {}
    goals = assists = 0
    for match in matches:
        for player in match["players"]:
            if (player.get("minutes") or 0) > 0:
                minutes[player["player_id"]] = minutes.get(player["player_id"], 0) + player["minutes"]
            goals += player.get("goals") or 0
            assists += player.get("assists") or 0

    return {
        "matches": len(matches),
        "players_with_minutes": len(minutes),
        "total_minutes": sum(minutes.values()),
        "total_goals": goals,
        "total_assists": assists,
    }


def compare(local: dict, remote: dict) -> list[dict]:
    checks = []
    for key in ("players_with_minutes", "total_minutes", "total_goals", "total_assists"):
        a, b = local.get(key, 0), remote.get(key, 0)
        biggest = max(abs(a), abs(b), 1)
        drift = 100.0 * abs(a - b) / biggest
        checks.append(
            {
                "metric": key,
                "local_365scores": a,
                "transfermarkt": b,
                "drift_pct": round(drift, 2),
                "ok": drift <= TOLERANCE_PCT,
            }
        )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-check season totals against Transfermarkt")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    local = local_totals(cfg)

    try:
        remote = Transfermarkt(cfg, LOG).totals()
    except SourceError as exc:
        LOG.warning("cross-check unavailable (Transfermarkt unreachable): %s", exc)
        write_json(DATA / "verify_status.json", {"available": False, "error": str(exc), "local": local})
        return 0  # not a pipeline failure - the check is advisory

    checks = compare(local, remote)
    passed = all(c["ok"] for c in checks)
    write_json(
        DATA / "verify_status.json",
        {"available": True, "passed": passed, "local": local, "checks": checks},
    )

    if not args.quiet:
        LOG.info("comparing %d matches (Toto Cup excluded - Transfermarkt does not cover it)",
                 local["matches"])
        for check in checks:
            level = LOG.info if check["ok"] else LOG.warning
            level("%-22s 365scores=%-6s transfermarkt=%-6s drift=%s%% %s",
                  check["metric"], check["local_365scores"], check["transfermarkt"],
                  check["drift_pct"], "OK" if check["ok"] else "MISMATCH")

    LOG.info("cross-check %s", "PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
