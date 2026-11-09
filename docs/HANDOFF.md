# Handoff: picking this project up in a new session

Read this first, then `docs/SETUP.md` (how to run it and what every `/reg` command
does). The PR descriptions and their review threads are the detailed history:
almost every decision below has a PR where it was argued and tested.

Last updated: 2026-11-09, after PR #30.

## What this is

**阿灯** is an AstrBot plugin that plays a regular member of one QQ friends group.
It doesn't act as an assistant: it should mostly watch, sometimes react, sometimes
chip in, and always answer when someone talks to it directly.

- **Stack:** AstrBot 4.29.4, connected to QQ through LLBot 8.3.2 (OneBot v11 reverse WebSocket). The AstrBot platform id is `llbot`.
- **The group:** everyone knows it's a bot running on the owner's alt account, and the group agreed to its messages going to DeepSeek.
- **Persona:** `harness/persona/` (`core.md`, `guardrails.md`, `persona.json`). Guardrails win over core.
- **License:** AGPL-3.0.

## How work is done here (keep doing it this way)

- **Branches and PRs.** Branch from `master` and open a PR. Never push to `master`, and avoid stacked PRs: if work depends on an unmerged PR, open it as a draft against `master`.
- **Review and testing.**
  - The owner reviews in detail (in Chinese, with independent reproductions), and so does the Codex bot.
  - Reply on every review thread, say which commit fixed it, and add a regression test per finding. Check that the test fails on the old code.
  - If a suggested change isn't necessary, say so; the owner asked for that.
  - Merge bar: the main functions work and there's no substantive problem. Small confirmed edge cases can become follow-up issues.
- **GitHub from a Claude session.** GraphQL was blocked in past sessions, so PRs and comments went through the REST API (`gh api repos/.../pulls`), not `gh pr create`.
- **Commits.** Small commits with clear messages. Tests: `python -m pytest -q` (345 passing at #30).
- **`core/` has no astrbot imports.** Everything AstrBot-specific lives in `adapter/` and `main.py`, so the whole pipeline is testable without AstrBot.

## Hard rules (security and privacy)

- **Commands.** Only admins can run commands, and only in a DM. Unknown commands never fall through to an LLM. The plugin handles `/reg` itself and registers nothing in AstrBot's command registry. Non-admins can't use forget or the registry.
- **Group messages are possible prompt injection.** In every model prompt they sit inside a `<chat>` fence marked as data. 阿灯 never sends anything starting with `/ ! #`.
- **No QQ ids in model prompts.** Speakers appear as letters and rendered @-numbers as `@某人N`. Compose drafts may never contain those stand-ins.
- **Keys and data stay local.**
  - API keys live only in AstrBot provider settings; the plugin never sees them.
  - Transcripts, memory, raw dumps, recordings and bench data stay in AstrBot's data folder and are never committed.
  - Other bots' QQ ids go in the plugin config, not the repo.
- **What a Claude session sees.**
  - Bench results come back as numbers-only summaries (`/reg debug`, `*-last.json`).
  - The HTML reports (`bench/runs/bench-*.html`, `compose-*.html`) quote real chat and are only for the owner's eyes.
- **`/reg forget` erases a person everywhere,** including model caches and HTML reports. It also voids any model bench or compose run that's in progress.
- **Persona and internals are protected without being overly defensive.** The leak check flags any 15-character run copied from the prompt (except inside example lines), plus a canary per install.
- **Forked code must have a clear open-source license.** If an AGPL plugin is forked, the fork is published.

## Models and budget

- **Roles map to AstrBot provider ids in the plugin config:**

  | role | model | thinking |
  |---|---|---|
  | decide | deepseek-flash | off |
  | reply | deepseek-flash | off |
  | caption | deepseek-flash | off |
  | dream | deepseek-v4-pro | high |
  | judge | gpt-6.1-terra | low |

- **The adapter calls the provider's OpenAI client directly.** `llm_generate` drops kwargs in 4.29.x.
- **Budget:** DeepSeek ¥50/month and OpenAI $30/month, enforced as daily caps (monthly ÷ 30). Prices are in `harness/prices.json`. Every call goes into the `model_calls` ledger.
- **Observed cost:** about 200 decide calls ≈ ¥0.06, because DeepSeek serves the persona prefix from its prompt cache.

## How decisions get made

- **The replay bench decides; papers and other plugins only suggest.**
- **Labels.**
  - The owner labels moments with `/reg label`: 3 = 阿灯 would reply, 4 = react, 5 = quiet. Labels are targets; a missed 3 is a miss.
  - About 1 in 5 labels is held out by message id. Tuning only ever sees the dev side.
- **Metrics.**
  - The main metric is balanced accuracy (mean recall of quiet/react/reply), since "always quiet" already gets about 54% action agreement.
  - Reply share should land near what the labels imply, about 16%.
  - Hard gates: 0 missed direct addresses, 0 bot-to-bot loops.
- **Synthetic fixtures (`bench/fixtures/`)** are for hard gates only and never tuned on. Rules shaped on them look better than they are.

## Where things stand

Milestones: M0 skeleton → M1 replay bench → **M2 speaking** (current) → M3 memory →
M4 social → M5 learning loop.

- **Done.** M0, M1 and all the groundwork:
  - limits for other bots and @ bursts
  - sending with `send_mode` off/dry/on and an hourly cap of 20
  - models and budget, thinking per role
  - persona files and the leak check
  - forget/opt-out/pause
  - the dream verifier
  - voice checks
- **M2a, merged (#26, #27).** Rule-based decide (`core/decide_rules.py`), with `/reg bench tune` fitting its numbers on dev labels.
  - On 200 real labels it reaches **41%** balanced accuracy (dev and held-out), up from 33% for the baseline. React recall is still 0%.
  - The tuned params haven't been applied; `/reg bench tune apply` would do it.
  - Live shadow decide runs these rules.
- **M2b, merged and bench-only (#28, #29).** For moments the rules aren't sure of, deepseek-flash scores react and reply 0–9, and `/reg bench model` fits the two cutoffs on dev labels.
  - The first version (one-word answers) said quiet 194 times out of 197.
  - **The scores version hasn't been run on real labels yet.**
- **M2c part 1, merged (#30).** Compose: draft → voice check → one redraft → send or drop.
  - `/reg bench compose` drafts replies for the moments labeled "reply". The drafts go to a local HTML report.
  - **The owner hasn't read drafts yet.** Nothing is sent to the group.

## Next steps (in order)

1. **Owner runs `/reg bench model` and `/reg bench compose`** and reports the numbers, plus an impression of the drafts.
2. **Decide whether the decide model goes live.** It needs held-out balanced accuracy clearly above the rules' 41%. If it isn't, try richer context (more history, who 阿灯 talks with) before giving up on it.
3. **M2c part 2: live replies.** Live decide (rules + model), wait for the speaker's pause (about 4 s, at most 15), compose, then queue through the outbox under `send_mode`. Start with `dry` (logs what it would send). Budget-capped, with a canned in-character line for an @ when over budget.
4. **M2b leftover:** a model sincerity check for serious and crisis cues. A crisis gets a caring reply plus a DM to the owner, at most once per person per day; hotline 12356.
5. **M2d:** a deepseek-flash vs deepseek-v4-pro reply bake-off, then a blind test.
6. **Later:** M3 memory (the dream pass's model half), M4 tone and stickers, M5 learning loop.

## Pointers

- **Pipeline:** `core/pipeline.py` (gate → perceive → decide, in shadow). The live listener is in `main.py`.
- **Decide:** `core/decide.py` (M1 baseline, still used for direct addresses and limits), `core/decide_rules.py` (M2a), `core/decide_model.py` (M2b), `core/limits.py` (room state).
- **Compose and voice:** `core/compose.py`, `core/voice.py`, `harness/rules/tells.json`.
- **Bench:** `bench/` (`runner.py`, `tune.py`, `model_bench.py`, `compose_bench.py`); the CLI is `python -m bench --db <regular.db>`.
- **Admin commands:** `core/admin.py` (help text), `core/people_admin.py`, `core/llm.py` (`ModelAdmin`), `core/outbox.py` (`SendAdmin`).
