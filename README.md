# goose-lee

goose-lee is a community duck bot for a Slack workspace. It scores chat activity and thanks in one official
channel, turns the scores into pond levels, and posts weekly and all-time top 3 results that organizers confirm.
The duck answers with short, canned lines. An optional voice service can answer addressed messages with an LLM,
but it stays off until its safety gates are met.

## Architecture

```text
Slack (one Socket Mode connection)
   |
GATEWAY   goose/gateway   owns the Slack tokens, the database and the scheduler
   |-- CORE   goose/core       scoring, levels, commands, canned lines (never waits for the voice)
   |
   +-- HTTP, JSON, 5 s timeout --> VOICE   goose/voice   separate container
                                            LLM key and a shared secret only. No Slack, no database.
```

The gateway is the only process with Slack tokens, the database and the scheduler, and it owns the single Socket
Mode connection. The core is deterministic and never waits for the voice: if the voice is slow, down, over its
limits or returns junk, the gateway posts a canned line and the scores are unaffected.

The voice is standard library only and can reach only the LLM provider. It has no Slack token and no database. It
sees a small versioned context card (level, a coarse activity band, the last confirmed top 3 as slots) and never
names, Slack ids or other people's numbers. Every voice reply passes the deterministic post-filter before it can be
posted. `docs/PLAN.md` section 10 is the full design.

## Quick start

```bash
python3.12 -m venv .venv
. .venv/bin/activate                  # Windows: .venv\Scripts\activate
python -m pip install ".[slack,dev]"
cp .env.example .env                  # then fill in the Slack tokens and the shared secret
python -m goose.gateway.app --check-config
python -m goose.gateway.app --config config/goose.yaml
```

`pip install` pulls in `tzdata`, which is required for the named timezone (for example
`Europe/Berlin`) in `config/goose.yaml`: on Windows and on slim images the standard library has no
system tz database of its own.

Or with Docker: `docker compose up -d`. The voice needs `voice.env` and `--profile voice`, see the runbook.

## Commands (brief)

Member commands use `/goose`, plus mentions of the duck. Full list: `docs/PLAN.md` sections 7 and 8.

| Who | Examples |
|---|---|
| Members | `/goose join`, `leave`, `show`, `hide`, `rest`, `quiet on`, `me`, `top`, `all`, `explain`, `report` |
| Organizers | `/goose results week`, `winners`, `me @person`, `star`, `bigstar`, `revoke`, `confirm-week`, `disqualify`, `skip-week` |
| Owner only | `/goose confirm-final`, `/goose set-end <date>` |
| Kill switches | `/goose pause`, `/goose resume`, `/goose voice off`, `/goose voice status` |

## Voice gates

The voice (the LLM answers) stays **off** until all of these are done:

- safeguarding sign-off and the distress word list,
- a provider whose terms allow under-18 end users, with retention confirmed,
- a new privacy notice and re-consent from members,
- the owner's approval.

There is **no spend cap** in the bot, by design. Set a spending limit in the LLM provider's console before you
switch the voice on. The bot instead limits how often one person can address it (default 12 messages per hour in
`config/goose.yaml`) and keeps replies short.

## Safety notes

- Secrets live only in `.env` and `voice.env`. Both are git-ignored and never copied into an image.
- Logs hold reason codes and ids, never message text, tokens or keys.
- The voice never sees names or Slack ids. Places and numbers about a person appear only in private replies.
- Rewards are not announced while `rewards.announced` is `false`.
- Distress messages get a plain reply and an alert to the organizers. No joke, no roast.
- Every German phrase comes from the approved table and is written with an inline English gloss; the voice may
  use German only through approved phrase ids.

## Operations

Full steps are in `docs/RUNBOOK.md`. In short:

- **Deploy:** `docker compose up -d`, or the hardened systemd unit in `deploy/goose.service` (users, paths and
  hardening are in the runbook).
- **Backups:** back up the database and the audit file together, daily, while the gateway runs
  (`sqlite3 data/goose.db ".backup ..."`). The database is in WAL mode, so `.backup` is safe live.
- **Restore drill:** copy the newest backup and its audit file back, start the gateway, then check
  `/goose results week` and `/goose audit 5`. Run it before the shadow weeks and once a term.
- **Kill switches:** `/goose pause` stops all scoring, posts and reactions; `/goose voice off` leaves the core
  running on canned lines.

## Documentation

- `docs/PLAN.md`: behavior, scoring, safety, phases and the audit re-triage (the gateway, core and voice split)
- `docs/APPENDIX_VOICE.md`: the approved voice lines and the 36 case eval set that runs in CI
- `docs/RUNBOOK.md`: deploy (Docker and systemd), env vars, backups, the restore drill, Monday checklist and incidents
- `docs/CONTRIBUTING.md`: setup, tests, style, how to change a scoring rule
- `BUILD_SPEC.md`: how the code is organized and built
