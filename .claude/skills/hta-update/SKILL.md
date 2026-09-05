---
name: hta-update
description: Refresh Hapoel Tel Aviv season statistics and report what changed. Runs the full pipeline (fetch new matches, recompute aggregates, rebuild the Hebrew dashboard and Excel workbook, re-arm the scheduled task) then writes a short Hebrew summary. Use when the user asks to update Hapoel Tel Aviv stats, check for new match data, or asks who scored / how a player is doing this season.
---

# Update Hapoel Tel Aviv statistics

## Run the pipeline

```powershell
cd "c:\Users\user\Documents\Claude Agents\HTA_Stats"
.\scripts\run_update.ps1
```

This fetches any newly finished matches, recomputes season aggregates, rebuilds
`output\hta_dashboard.html` and `output\hta_stats.xlsx`, cross-checks against
Transfermarkt, and re-arms the scheduled task for the next match.

If the user only wants to look without touching the schedule, add `-SkipReArm`.

## Then report, in Hebrew, concisely

Read these and summarise:

- `data\last_delta.json` — what changed since the previous run, per player
- `data\schedule_status.json` — next run time and any schedule flag
- `data\season_2026_27.json` — season totals if the user asked about a player
- `data\verify_status.json` — cross-source agreement

Keep it to a short table or a few bullets. Lead with what actually changed; if
nothing changed, say so in one line rather than restating the season.

## Always surface these

- **A schedule flag** (`flag_he` in `schedule_status.json`). 🟡 means the next
  kickoff time is provisional; 🔴 means no kickoff is known at all. Say when the
  next check will happen.
- **A source fallback** — if `fetch_status.json` has `source` other than
  `365scores`, the primary was down and the figures may be partial.
- **Matches missing player stats** (`matches_missing_stats`) — appearances are
  counted, minutes are absent. Do not present those minutes as real.

## Refreshing the phone dashboard

The scheduled task cannot republish, so the artifact only updates when asked. If the
user wants the phone copy refreshed, publish `output\hta_dashboard_artifact.html`
**with this URL** so it updates in place instead of creating a second artifact:

```
https://claude.ai/code/artifact/0951ba53-f684-4f9c-93bf-8570a51e1a77
```

Read it first (`action: "read"` with that url) before publishing to it.

## Notes

- Never invent minutes, ratings or clean sheets for a match the source did not
  provide stats for.
- Player names come from the source already in Hebrew; do not transliterate.
- `apps == starts + bench_apps` is asserted by the aggregator. If it ever raises,
  that is a parsing regression, not a data quirk — see `workflows\update_stats.md`.
- Full operational detail, the fallback ladder and troubleshooting live in
  `workflows\update_stats.md`.
