# goose-lee runbook

For the people who run the bot. Behavior is in `docs/PLAN.md`. Read the README first.

## Deploy

**Docker (recommended).** On the host, create `.env` from `.env.example` with the Slack tokens and the shared secret.

```bash
docker compose build
docker compose run --rm gateway python -m goose.gateway.app --check-config --config config/goose.yaml
docker compose up -d
docker compose logs -f gateway
```

The gateway publishes no ports. It opens one outbound Slack connection.

**systemd (no Docker).** As root:

```bash
useradd --system --home /opt/goose-lee --shell /usr/sbin/nologin goose
install -d -o goose -g goose /opt/goose-lee/data /opt/goose-lee/backups
# copy the repo to /opt/goose-lee, then create the venv and install
cd /opt/goose-lee && python3.12 -m venv .venv && .venv/bin/pip install ".[slack]"
install -d -m 0750 -o root -g goose /etc/goose-lee
install -m 0640 -o root -g goose .env /etc/goose-lee/goose.env
cp deploy/goose.service /etc/systemd/system/goose.service
systemctl daemon-reload && systemctl enable --now goose
```

## Environment variables

| Name | Where | Meaning |
|---|---|---|
| `SLACK_BOT_TOKEN` | `.env` | Bot token, must start with `xoxb-` |
| `SLACK_APP_TOKEN` | `.env` | App token, must start with `xapp-` |
| `VOICE_SHARED_SECRET` | `.env` and `voice.env` | Same value in both, compared in constant time |
| `VOICE_PORT` | `voice.env` | Voice listen port (default 8081) |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_MODEL_ALLOWLIST` | `voice.env` | Provider settings. The model must be on the allowlist |
| `LLM_API_FORMAT`, `LLM_CACHE` | `voice.env` | `openai` (default; OpenAI and most other providers) or `anthropic`. Caching is on by default and only changes billed input cost |
| `LLM_TIMEOUT_S` | `voice.env` | Provider request deadline, 20 seconds by default |
| `LLM_REASONING_EFFORT`, `LLM_REASONING_MAX_TOKENS` | `voice.env` | Optional OpenAI compatible reasoning effort and reasoning headroom. Headroom defaults to 4096 tokens |

The gateway refuses to start if a token is missing or has the wrong prefix.

The provider deadline is 20 seconds by default. The gateway allows 30 seconds for a voice request and drops queued
jobs after 5 seconds, so provider timeouts normally finish before the gateway deadline. In `config/goose.yaml`,
`voice.messages_per_person_per_hour` defaults to 200 and `voice.person_gap_s` defaults to 0. The failure breaker
opens after 3 failures within 5 minutes and stays open for 15 seconds. The junk breaker disables voice for 30 seconds
when more than 20 percent of a sample of at least 10 responses are junk, using a rolling window of up to 50 responses.
These timers are seconds unless the table says otherwise.

Default generation budgets are 350 tokens for ordinary requests and 600 for elaborate requests. The enforced visible
text caps are 800 and 1200 characters respectively. When an OpenAI compatible reasoning effort is enabled,
`LLM_REASONING_MAX_TOKENS=4096` supplies completion headroom for internal reasoning. Known Grok model ids `grok-4.5`,
`grok-4.6`, `grok-4.7`, and their `x-ai/` forms default to low reasoning effort. `LLM_REASONING_EFFORT` can override
that with a supported effort. For other models, leaving it unset preserves the provider's existing behavior.

## Rotating tokens (manual)

Token rotation is off in Slack, so rotation is a manual step. Rotate when a token may have leaked, and on the
schedule the owner sets.

1. Create the new token in the Slack app settings. Keep the old one active until the new one works.
2. Edit `.env` (or `voice.env` for the shared secret, which must change on both sides at once).
3. Restart: `docker compose up -d gateway` or `systemctl restart goose`.
4. Check the logs for a clean start. Then revoke the old token.

## Reading logs

Logs hold reason codes, ids, kinds, flags, token counts and latency only. They never hold message text, card
contents, raw voice output or keys. Logs are kept 14 days. Useful searches: `reason=`, `breaker`, `junk`,
`heartbeat`. For the organizer trail, use `/goose audit` in Slack.

## Backups

Back up the database and the audit file together, daily, while the gateway runs:

```bash
sqlite3 data/goose.db ".backup backups/goose-$(date +%F).db"
cp data/audit.log backups/audit-$(date +%F).log
```

Keep at least 14 daily copies, and store them off the host. The database is in WAL mode, and `.backup` is safe
while the gateway runs.

**Restore drill (before the shadow weeks, then once a term).** On a spare host or a scratch folder:

1. Stop the gateway. Copy the newest `backups/goose-*.db` to `data/goose.db` and the matching audit file to
   `data/audit.log`.
2. Start the gateway, then check `/goose results week` and `/goose audit 5`.
3. Write down the result and the time it took. Someone other than the operator should run it.

## Kill switches

- `/goose pause`: nothing is scored, posted or reacted to. `/goose resume` turns it back on.
- `/goose voice off`: the core keeps working and the duck uses canned lines. Takes effect on the next request.
- Stop the voice container: `docker compose --profile voice stop voice`. The gateway falls back to canned lines.
- Last resort: `docker compose stop gateway` or `systemctl stop goose`. Nothing is scored while stopped.

## Monday checklist (organizers)

1. Read the provisional post in the organizer channel. Check the top 3 against the stars and the audit trail.
2. If a result looks wrong: `/goose disqualify @person week "reason"`, then read the new ranking.
3. Confirm with `/goose confirm-week`. With `confirmations_required: 2`, a second organizer confirms too.
4. If the week should not count (holiday, outage): `/goose skip-week "reason"`.
5. Write one line in the organizer channel saying what you did. The audit file has the detail.

Tuesday 12:00: the reminder goes out if the week is still unconfirmed. Final results are the owner's call
(`/goose confirm-final`).

## Shadow weeks

1. Volunteers join with `/goose join`. Scoring is silent: no announcements, no rewards.
2. Run two full weekly cycles as rehearsals with the real commands (results, confirm, disqualify, skip).
3. Compare the top 3 with a hand calculation of the same week. Note any difference and its cause.
4. Tune `config/goose.yaml` only with the owner's approval, then freeze the thresholds.
5. Reset shadow data at launch: stop, back up, remove `data/goose.db`, start again.

## Incident response

1. Stop the harm first: `/goose pause`, and `/goose voice off` if the voice is involved.
2. Note the time, the request ids or reason codes from the logs, and the Slack message links. Do not paste message
   text into tickets.
3. Tell the organizer channel in one line. For distress or safeguarding, follow `safety.contact_text` in
   `config/goose.yaml` and the owner's instructions.
4. Fix, run `python -m pytest -q`, deploy, then `/goose resume`.
5. Write a short note: what happened, what changed, who was told.

## Config change procedure

1. Edit `config/goose.yaml` on a branch. A typo stops startup, by design.
2. Run `python -m goose.gateway.app --check-config --config config/goose.yaml`.
3. Run `python tools/level_sim.py` if any scoring value changed.
4. Deploy and restart. Log the change in the organizer channel with the reason. Do not change settings during a
   Monday results window.
