# Contributing to goose-lee

## Setup

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[slack,dev]"
```

Python 3.9 must keep working (the dev machine runs 3.9, production runs 3.12). Do not use `match`, `X | Y`
annotations at runtime, `zip(strict=)` or `str.removeprefix`. Start each module with `from __future__ import annotations`.

## Tests and checks

```bash
python -m pytest -q          # unit, handler, simulation and hygiene tests
python -m ruff check .       # lint
python tools/level_sim.py    # the scoring simulation and its assertions
python -m goose.gateway.app --check-config --config config/goose.yaml
```

Tests run without network, without Slack and without sleeping. Use `FakeClock` (`clock.advance`) for time and the
`rng` fixture for randomness. Tests must be deterministic.

## Style rules

- No em dashes and no en dashes anywhere: code, comments, strings and docs. Use commas, colons or a plain hyphen.
  `tests/test_hygiene.py` and the CI workflow both check this.
- Log reason codes only. Never log tokens, keys, prompts, replies or message text.
- Time comes from `ctx.clock` or a `now` argument. Core code never calls `time.time()`.
- Randomness comes from the `rng` passed in. Core code never uses the global `random` module.
- The core never imports the voice, and the voice never imports the core, the gateway, `sqlite3` or Slack.
- Add a dependency only with a reason and a pin. The voice service stays standard library only.

## Changing a scoring rule

1. Update the formula and its assertions in `tools/level_sim.py` first. Check that the simulation still prints
   `ALL ASSERTIONS PASSED`.
2. Change the same formula in `goose/core/scoring.py`. Update `tests/test_simulation.py` if the cross-check needs it.
3. Update the scoring section of `docs/PLAN.md` (section 4) so the rule and its reason are written down.
4. Run the full test suite, and note the change in the pull request for the organizers.

## Pull requests

Batch small tweaks into one squashed commit and one pull request. Describe what changed, why, and how it was tested.
