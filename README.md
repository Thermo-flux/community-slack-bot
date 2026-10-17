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

The gateway is the only process with Slack tokens. The voice is standard library only and can reach only the LLM
provider. See `docs/PLAN.md` section 10 for the full design.

## Quick start

```bash
python3.12 -m venv .venv
. .venv/bin/activate                  # Windows: .venv\Scripts\activate
python -m pip install ".[slack,dev]"
cp .env.example .env                  # then fill in the Slack tokens and the shared secret
python -m goose.gateway.app --check-config --config config/goose.yaml
python -m goose.gateway.app --config config/goose.yaml
```

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
switch the voice on. The bot allows up to 200 addressed messages per person per hour, has no per-person cooldown,
and keeps replies short. Provider and gateway deadlines, queue expiry, and circuit-breaker timings are listed in
`docs/RUNBOOK.md`.

## Safety notes

- Secrets live only in `.env` and `voice.env`. Both are git-ignored and never copied into an image.
- Logs hold reason codes and ids, never message text, tokens or keys.
- The voice never sees names or Slack ids. Places and numbers about a person appear only in private replies.
- Rewards are not announced while `rewards.announced` is `false`.
- Distress messages get a plain reply and an alert to the organizers. No joke, no roast.

## Documentation

- `docs/PLAN.md`: behavior, scoring, safety, phases and the audit re-triage
- `docs/RUNBOOK.md`: deploy, backups, restore, Monday checklist, incidents
- `docs/CONTRIBUTING.md`: setup, tests, style, how to change a scoring rule
- `BUILD_SPEC.md`: how the code is organized and built
