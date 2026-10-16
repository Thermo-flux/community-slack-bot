# BUILD_SPEC: how this project is built

Read this first, then `docs/PLAN.md` (the behavior), `docs/APPENDIX_VOICE.md` (line banks), and the stub files you own
(the signatures and docstrings there are the contract). The lead wrote and owns: `goose/types.py`, `goose/config.py`,
`goose/core/{clock,db,wordlists,context}.py`, `tests/conftest.py`, `pytest.ini`, `config/goose.yaml`. Do not edit those.

## Rules for every agent

1. **Python 3.9 compatible** (the dev machine runs 3.9.13; production is 3.12). Start every module with
   `from __future__ import annotations`. No `match`, no `X | Y` type syntax at runtime, no `zip(strict=)`, no
   `str.removeprefix`.
2. **Dependencies:** standard library plus PyYAML, plus `slack_bolt` only inside `goose/gateway/app.py` (imported lazily).
   The voice service uses the standard library only. Do not add dependencies.
3. **Own only your files** (listed in your assignment). Create your own test files named in your assignment. If you need a
   change in a file you do not own, do not edit it: write it under "Interface requests" in your final report.
4. **Keep the signatures** in the stubs. You may add helper functions, extra optional parameters with defaults, and private
   methods. Do not rename or remove anything public. Replace every `raise NotImplementedError`.
5. **Tests:** pytest, run with `python -m pytest -q <your test files>` from the project root. Use the fixtures in
   `tests/conftest.py` (`clock`, `rng`, `cfg`, `db`, `ledger`, `users`, `levels`, `weekly`, `lines`, `ctx`, `ev`, `join`).
   No network, no real Slack, no sleeping (use `clock.advance`). Tests must be deterministic.
   Other agents are writing their modules at the same time, so a fixture that needs a module that is still a stub may fail
   until that module lands. Test your own module with the pieces that exist and construct anything else by hand.
6. **No em dashes or en dashes** anywhere (code, comments, strings, docs). Use commas, colons or hyphens.
7. **No secrets, no message text in logs.** Log reason codes only. Never log tokens, keys or prompts.
8. **Safety rules are part of the spec:** the voice never sees names or Slack ids; places and numbers about a person appear
   only in private (ephemeral) replies; hidden/quiet/resting people get no public remarks; no reward or prize wording
   while `config.rewards.announced` is false; no spend cap exists (do not add one) but the per-person message limit does.
9. **Final report** (to the lead, at most 300 words): files you created or changed, test command and result (paste the
   last line), anything you could not finish, deviations from the stubs, and "Interface requests". Do not paste code.

## Ownership

| Agent | Files |
|---|---|
| A ledger | `goose/core/{scoring,levels,ledger,users,weekly,card}.py`; tests `tests/test_{scoring,levels,ledger,users,weekly,card}.py` |
| B vibes | `goose/core/{rules,safety,lines,mood,explain,vibes}.py`; `config/lines.yaml`; tests `tests/test_{rules,safety,lines,mood,explain,vibes}.py` |
| C1 safe-output | `goose/gateway/{poster,audit}.py`; tests `tests/test_{poster,audit}.py`, `tests/test_voice_evals.py` (the 36 cases in `docs/APPENDIX_VOICE.md` section C, run through PostFilter with a fake voice) |
| D voice | `goose/voice/{prompt,provider,server}.py`, `goose/gateway/voice_client.py`; tests `tests/test_{voice_prompt,voice_provider,voice_server,voice_client}.py` |
| C2a router | `goose/core/{router,commands_member}.py`; tests `tests/test_{router,commands_member}.py` |
| C2b ops-wiring | `goose/core/commands_organizer.py`, `goose/gateway/{scheduler,slack_io,app}.py`, `slack/manifest.json`; tests `tests/test_{commands_organizer,scheduler,slack_io,app}.py` |
| E ops-docs | `pyproject.toml`, `README.md`, `docs/{RUNBOOK,CONTRIBUTING}.md`, `.gitignore`, `.dockerignore`, `.env.example`, `voice.env.example`, `Dockerfile`, `Dockerfile.voice`, `docker-compose.yml`, `deploy/goose.service`, `.github/workflows/ci.yml`; tests `tests/test_config.py`, `tests/test_simulation.py`, `tests/test_hygiene.py` |

## Key conventions

- Time is always passed in (`now`) or read from `ctx.clock`. Never call `time.time()` in core code.
- Randomness always comes from the `rng` passed in. Never call the global `random`.
- User ids look like `U123`; channels `C123`; message timestamps are strings like `"1760000000.123456"`.
- Week ids are the Monday date, e.g. `"2025-10-06"`. `FakeClock` in tests starts Thursday 2025-10-09 UTC.
- Idempotency keys: `msg:<channel>:<ts>`, `thanks:<channel>:<ts>:<reactor>`, `helpful:<channel>:<ts>:<asker>`,
  `star:<uuid>`; a repeated key is ignored.
- Placeholders: `{de:id}` in the line bank, `{{de:id}}` in voice replies. `{you}`, `{top1}` style slots in voice text are
  written `{{you}}`, `{{top1}}`.
- Private = an `Action(kind="ephemeral", channel, user, text)`. Public = `Action(kind="post", ...)`.
- Organizer posts = `Action(kind="organizer_post", channel=ctx.organizer_channel, text=...)`.
- Moments in `config/lines.yaml` (agent B must provide at least 3 lines each unless noted; German variants and plain
  variants): `join`, `welcome`, `level_up`, `star`, `bigstar`, `level_12`, `ack` (8, from the appendix N1..N8),
  `anger_calm`, `anger_annoyed`, `anger_angry`, `anger_honk`, `anger_timeout`, `anger_cooling` (6 each, from the appendix),
  `rank_remark_active` (R1..R5), `rank_top10` (R6..R9, slot `{rank}`), `rank_below` (R10, R11), `rank_duck` (R12..R14),
  `roast` (5+, templates, slots may be empty), `roast_self`, `limit_reached` ("the duck needs a breather"),
  `program_redirect`, `not_joined`, `unknown_person`, `error`, `safety` (no joke, no German), `busiest_duck`,
  `pond_report_header`, `pond_report_footer`, `pond_report_result`, `chat_fallback` (used when the voice is off or fails;
  short, in character, points to `/goose help`), `unknown_phrase`, `quack`, `pun`.
  Dashes are forbidden. Reward words (wordlists) are forbidden in all moments except those starting with `reward_`.

## Definition of done for the whole project

`python -m pytest -q` passes; `python -m ruff check .` is clean; `python -m goose.gateway.app --check-config` prints OK;
`python tools/level_sim.py` prints ALL ASSERTIONS PASSED; the voice service starts against a fake provider and answers
`/health`; no file contains an em or en dash.
