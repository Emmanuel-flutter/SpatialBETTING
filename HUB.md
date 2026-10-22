# HUB -- Multi-session coordination

Last checked: 2026-10-22. No work is in flight. `main` is level with `origin/main`, and every worktree is clean.

## Active work

| Branch | Task | Owner | Status | Files touched |
|---|---|---|---|---|
| `feat/p2p-ball-roulette` | Dan's ball-position P2P roulette (settles on where the ball is when a window ends, with a perimeter zone) | Emmanuel | Not started. The branch has no commits. | none |

## Finished work

| Branch | Task | Owner | Status | Files touched |
|---|---|---|---|---|
| `main` | Initial setup -- consumer, config, docs | Emmanuel | Done 2026-10-12 (`fd6ea9b`) | all |
| `feat/live-fixture-view` | Live fixture view | Emmanuel | Done on `main` in `0c9095b` (2026-10-15). The branch has no commits. | `services/fixtures.py`, `services/feed.py`, `static/` |
| `feat/roulette-wallet` | Roulette wallet | Emmanuel | Done on `main` in `0c9095b` (2026-10-15). The branch has no commits. | `services/wallet.py`, `services/p2proulette.py` |
| `feat/skybet-ui` | Sky Bet styling for the demo | Emmanuel | Done on `main` in `0c9095b` (2026-10-15). The branch has no commits. | `static/` |
| `fix/clock-feed-stopped` | Show "Feed stopped" when the feed stops, not a running clock | Emmanuel | Done on `main` in `0c9095b` (2026-10-15). The branch has no commits. | `services/match_state.py` |
| `main` | Demo app: FastAPI server, P2P roulette, Who Where When, replay data | Emmanuel | Done 2026-10-15 (`0c9095b`) | `server.py`, `services/`, `static/`, `tools/`, `data/`, `config/`, `ARCHITECTURE.md`, `requirements.txt` |
| `main` | Live odds that move with the ball in the demo roulette | Emmanuel | Done 2026-10-15 (`4568eec`) | `server.py`, `services/`, `static/`, `config/` |
| `feat/odds-mode` | Fixed-odds system beside P2P, with live odds from Opta events | Emmanuel | Merged to `main` 2026-10-16 (`7dd8fa2`). The worktree `../SpatialBETTING-odds-mode` still exists and is clean. | `services/`, `static/`, `tools/`, `data/`, `config/` |

## Conventions

- Check this file before editing shared files.
- Update the table when starting or finishing work.
- One branch per task. Do not commit directly to main from a subagent.
