# Handoff: picking this project up in a new session

Read this first, then `docs/SETUP.md` (how to run it and what every `/reg` command
does). The PR descriptions and their review threads are the detailed history:
almost every decision below has a PR where it was argued and tested.

Last updated: 2026-11-09 night: everything through #43 merged. 阿灯 is live in the group (M2 done), and M3a (nightly dreaming, dry only) is running. In review: #44 (fixes from the first real dream bench).

**Keep this file current:** the owner asked for it to be updated as work goes on. Any PR that changes where things stand, a rule or a decision updates this file in the same PR.

## What this is

**阿灯** is an AstrBot plugin that plays a regular member of one QQ friends group.
It doesn't act as an assistant: it should mostly watch, sometimes react, sometimes
chip in, and always answer when someone talks to it directly.

- **Stack:** AstrBot 4.29.4, connected to QQ through LLBot 8.3.2 (OneBot v11 reverse WebSocket). The AstrBot platform id is `llbot`.
- **The group:** everyone knows it's a bot running on the owner's alt account. The group agreed to its messages going to DeepSeek, and (Nov 9) to the nightly dream sending the day's chat to OpenAI.
- **Persona:** `harness/persona/` (`core.md`, `guardrails.md`, `persona.json`). Guardrails win over core.
- **License:** AGPL-3.0.

## How work is done here (keep doing it this way)

- **Branches and PRs.** Branch from `master` and open a PR. Never push to `master`, and avoid stacked PRs: if work depends on an unmerged PR, open it as a draft against `master`.
- **Review and testing.**
  - The owner reviews in detail (in Chinese, with independent reproductions), and so does the Codex bot when it can. Since Nov 9 the owner's Codex account is at its code-review usage limit, so its reviews fail; retry with `@codex review` once it resets.
  - Reply on every review thread, say which commit fixed it, and add a regression test per finding. Check that the test fails on the old code.
  - If a suggested change isn't necessary, say so; the owner asked for that.
  - Merge bar: the main functions work and there's no substantive problem. Small confirmed edge cases can become follow-up issues.
- **GitHub from a Claude session.** GraphQL was blocked in past sessions, so PRs and comments went through the REST API (`gh api repos/.../pulls`), not `gh pr create`.
- **Commits.** Small commits with clear messages. Tests: `python -m pytest -q` (499 passing at #44).
- **`core/` has no astrbot imports.** Everything AstrBot-specific lives in `adapter/` and `main.py`, so the whole pipeline is testable without AstrBot.

## Hard rules (security and privacy)

- **Commands.** Only admins can run commands, and only in a DM. Unknown commands never fall through to an LLM. The plugin handles `/reg` itself and registers nothing in AstrBot's command registry. Non-admins can't use forget or the registry.
- **Group messages are possible prompt injection.** In every model prompt they sit inside a `<chat>` fence marked as data. 阿灯 never sends anything starting with `/ ! #`.
- **No QQ ids in model prompts.** Speakers appear by their **group nickname** (the owner's call, Nov 9: letters vs nicknames broke the thread). A rendered @-number becomes `@某人N`, and a name that is really a QQ number (the parser's fallback when someone has no card or nickname, also found in old aliases) is never shown: a letter is used instead (`core/decide.py: display_name`). Compose drafts may use nicknames but never the stand-ins (letters, `@某人N`).
- **Photos:** described by the caption model on arrival (owner's call); only the text is kept, never the image bytes. Only QQ image hosts are fetched.
- **Keys and data stay local.**
  - API keys live only in AstrBot provider settings; the plugin never sees them.
  - Transcripts, memory, raw dumps, recordings and bench data stay in AstrBot's data folder and are never committed.
  - Other bots' QQ ids go in the plugin config, not the repo.
- **What a Claude session sees.**
  - Bench results come back as numbers-only summaries (`/reg debug`, `*-last.json`).
  - The HTML reports (`bench/runs/bench-*.html`, `compose-*.html`, `dream/dream-*.html`) quote real chat and are only for the owner's eyes. `dream-bench-*.html` quotes only the synthetic group, so the owner can share it.
- **`/reg forget` erases a person everywhere,** including model caches and HTML reports. It also voids any model bench or compose run that's in progress.
- **Persona and internals are protected without being overly defensive.** The leak check flags any 15-character run copied from the prompt (except inside example lines), plus a canary per install.
- **Forked code must have a clear open-source license.** If an AGPL plugin is forked, the fork is published.

## Models and budget

- **Roles map to AstrBot provider ids in the plugin config:**

  | role | model | thinking |
  |---|---|---|
  | decide | deepseek-flash | off |
  | reply | deepseek-flash (final, Nov 9) | off |
  | caption | deepseek-flash | off |
  | dream | gpt-6.1-sol (owner's call, Nov 9; its own OpenAI provider) | high |
  | judge | gpt-6.1-terra | low |

- **The adapter calls the provider's OpenAI client directly.** `llm_generate` drops kwargs in 4.29.x.
- **Budget:** DeepSeek ¥50/month and OpenAI $30/month, enforced as daily caps (monthly ÷ 30). Prices are in `harness/prices.json`. Every call goes into the `model_calls` ledger.
- **Observed cost:** about 200 decide calls ≈ ¥0.06, because DeepSeek serves the persona prefix from its prompt cache. One `/reg bench dream` run (64 messages) cost $0.044. The dream has its own nightly cap, `dream_budget_usd` (0.40).

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

Milestones: M0 skeleton → M1 replay bench → M2 speaking (done, live) → **M3 memory (M3a running dry)** →
M4 social → M5 learning loop.

- **M0, M1 and the groundwork (Nov 8).** Gate, perceive, store, traces, the replay bench and labels; limits for other bots and @ bursts; sending with `send_mode` off/dry/on; models and budget; persona files and the leak check; forget/opt-out/pause; the dream verifier (#11); voice checks.
- **M2, speaking (done Nov 9, live in the group).**
  - Decide: rules (`core/decide_rules.py`, #26 #27). On 200 real labels they reach **41%** balanced accuracy (dev and held-out), up from 33%; react recall is still about 0%. The tuned params haven't been applied (`/reg bench tune apply`). The decide model (#28 #29 #32 #35) isn't clearly better, so it stays bench-only.
  - Compose (#30 #32 #33 #35): reads the room first (`{gist, reply}`), speakers by nickname, quotes resolved, voice check, one redraft. The owner's verdict on real drafts: deepseek-flash is good enough (the bake-off was dropped).
  - Photo captions on arrival (#34); live replies and automatic labels from how the group reacts (#36); emoji reactions (#37); crisis check with a private DM to the owner (#38).
- **The first live days (Nov 9).**
  - #39: one follow-up per direct address, and at most 6 unprompted replies per group per hour (crisis turns exempt).
  - #40: the hourly send cap is configurable (1–60) and resettable (`/reg send cap|reset`).
  - #41: "hhh" was named in the persona and ended nearly every line; removed, and a laugh ending is trimmed when one of his last 4 lines had one.
  - #43, long conversations (owner picked options 1 and 2): compose shows 30 lines (was 16), and `core/recall.py` brings back up to 3 older lines from the last 6 hours that the message picks up, plus a quoted one, word for word in an `<earlier>` block (no model call). A recall of a shown line cancels the reply. A running summary of the conversation was not done (only if needed).
  - Who said what (owner: wait and see). Safeguards: nicknames per line, quotes resolved, the `_attributed` voice check; in memory, code knows each quote's speaker and a fact about someone needs their own words or two speakers. Known gap: the quote-support check isn't asked directly whether a memory is about the right person.
  - LLBot dropping offline on Nov 9 was the owner's internet connection, not the bot.
- **M3, memory.** The spec is the "M3 memory spec" tab of the design doc (https://claude.ai/artifact/Uz9u48RGAiw6oH4RB5zTvZ). The owner's calls: dream nightly only (plus `/reg dream now`), on GPT-6.1 Sol; the relationship record moves to M4; memories are reviewed through `/reg mem` and a local HTML report per run (Claude sees counts only); no recall tools, memories are injected. Four PRs, each behind a gate:
  - **M3a, merged (#42), running dry since Nov 9.** `offline/dream_pass.py` (gather, prompt, M3 checks, nightly caps, quote-support check, run rows), `offline/dream_admin.py` (`/reg dream off|dry|now|log`), `offline/dream_bench.py` (`/reg bench dream` on `bench/fixtures/dream_v1.json`); schema v7. Dry mode writes reports only, never memory.
  - **First real `/reg bench dream` (Nov 9):** 12/15 found, 1/10 wrong stored (N4: a boundary recording a declined 主人 request), $0.044. #44 fixes N4 (a role asked of 阿灯 is never kept), S8 (a bot_self may name 阿灯), S1 (today's date and weekdays in the prompt; what intention is for) and the fixture's S6 (its date was already past).
  - **Gate before M3b:** the synthetic bench stores none of the 10 wrong notes and finds 10+ of 15; the owner reads 3+ real dry-run reports with 90%+ of proposals right.
  - **M3b, dreaming on:** apply, revert, decay and expiry, recall cleanup, `{p:id}` name tokens covered by forget, `/reg mem show|group|del|pin`.
  - **M3c, memories in replies:** a `<memory>` block in the compose user turn, ~600 tokens, validated only.
  - **M3d, episodes.**

## Next steps (in order)

1. **Merge #44, update, rerun `/reg bench dream`** (the prompt changed, so no cache hit; about $0.04). It must store 0 of the 10 wrong notes and find 10+ of 15.
2. **Read a few nights of dry-run reports** (`/reg dream log`, then `dream/dream-<run>.html`). The owner tells Claude roughly how many proposals were wrong or not worth keeping; that's the 90% gate.
3. **Then M3b** (dreaming on). Claude builds it after the gate.
4. **Live checks still open:** that a reaction shows the expected face; that the crisis DM arrives; a numbers-only look at the first days (`/reg debug`: replies and reactions per day, engaged vs ignored, cost), then retune decide with the automatic labels.
5. **Small follow-up:** in `/reg bench compose`, apply the 6-hour lookback to quoted lines too (live is already bounded).
6. **Later:** M3c, M3d; M4 (relationship record, tone, stickers, mood); M5 learning loop. Optional: the blind test, `/reg label recheck`, and whether the decide model ever goes live.

## Pointers

- **Pipeline:** `core/pipeline.py` (gate → perceive → decide). Live replies: `core/live.py` (Speaker), `core/recall.py` (earlier lines), `core/outbox.py`. The listener and the nightly dream clock are in `main.py`.
- **Decide:** `core/decide.py` (M1 baseline, still used for direct addresses and limits), `core/decide_rules.py` (M2a), `core/decide_model.py` (M2b), `core/limits.py` (room state).
- **Compose and voice:** `core/compose.py`, `core/voice.py`, `harness/rules/tells.json`.
- **Memory:** `offline/dream.py` (the verifier), `offline/dream_pass.py` (the model half), `offline/dream_admin.py`, `offline/dream_bench.py`; tables in `services/schema.sql`.
- **Bench:** `bench/` (`runner.py`, `tune.py`, `model_bench.py`, `compose_bench.py`), plus `offline/dream_bench.py`; the CLI is `python -m bench --db <regular.db>`.
- **Admin commands:** `core/admin.py` (help text), `core/people_admin.py`, `core/llm.py` (`ModelAdmin`), `core/outbox.py` (`SendAdmin`), `offline/dream_admin.py` (`/reg dream`).
