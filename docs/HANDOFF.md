# Handoff: picking this project up in a new session

Read this first, then `docs/SETUP.md` (how to run it and what every `/reg` command
does). The PR descriptions and their review threads are the detailed history:
almost every decision below has a PR where it was argued and tested.

Last updated: 2026-11-09 afternoon: #33 merged; #34 (captions), #35 (format + fit fixes) and #36 (live replies + automatic labels) in review.

**Keep this file current:** the owner asked for it to be updated as work goes on. Any PR that changes where things stand, a rule or a decision updates this file in the same PR.

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
  - The owner reviews in detail (in Chinese, with independent reproductions), and so does the Codex bot when it can. Since Nov 9 the owner's Codex account is at its code-review usage limit, so its reviews fail; retry with `@codex review` once it resets.
  - Reply on every review thread, say which commit fixed it, and add a regression test per finding. Check that the test fails on the old code.
  - If a suggested change isn't necessary, say so; the owner asked for that.
  - Merge bar: the main functions work and there's no substantive problem. Small confirmed edge cases can become follow-up issues.
- **GitHub from a Claude session.** GraphQL was blocked in past sessions, so PRs and comments went through the REST API (`gh api repos/.../pulls`), not `gh pr create`.
- **Commits.** Small commits with clear messages. Tests: `python -m pytest -q` (361 passing with #33).
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
- **M2b, merged and bench-only (#28, #29, #32).** For moments the rules aren't sure of, deepseek-flash scores react and reply 0–9, and `/reg bench model` fits the two cutoffs on dev labels.
  - It uses dev balanced accuracy only; the 5/5 defaults stay unless beaten by 2 points (#32). Reply share isn't used, because the model only judges labeled moments.
  - Real runs on 31 held-out labels: rules 33% (untuned), the model 42–48% across runs. That's not clearly above the tuned rules' 41%, given about ±9 points of noise. Not live.
- **M2c part 1, merged (#30, #32).** Compose: draft → voice check → one redraft → send or drop. `/reg bench compose` drafts replies for the moments labeled "reply", with the drafts in a local HTML report.
  - First real run: drafts were short and passed the checks, but 24/30 said "在上课 晚点说". The compose prompt said "in class", and the persona gave that line. Fixed in #32: the hour now shapes *how* he replies, never *whether*.
  - The owner's review of the drafts (Nov 9): the problem is following the thread. Drafts missed sarcasm (praising an idea the group was mocking), answered the last line literally, guessed facts, and said "I've lost track" 4/30 times.
- **#33, merged: following the thread, and the labels.**
  - Speakers by nickname (never a QQ number: `display_name`); quotes resolved to whom and what (`回复 小王：“…”`).
  - Compose reads the room first: it answers `{gist, reply}`, and the gist goes to the local report only.
  - Prompts: messages to someone else are usually between them; don't fill in what the chat didn't say; images are unseen.
  - Labeling: 8 lines of context, quotes marked, earlier 3s flagged, `/reg label recheck` for old 3s. The reply labels likely overstate replying (long streaks in one thread, messages meant for others).
  - Voice: a quote counts as made-up only when attributed or 12+ characters.
- **First run after #33 (Nov 9 afternoon, old labels; recheck not done yet).**
  - Compose: the gists show it now reads mockery and jokes correctly (cases 4, 7–9), and there are no brush-offs or repeats. But 19 of 30 first drafts failed the format check: a two-line reply puts a raw line break inside the JSON string. Fixed in #35.
  - Model: held-out 32% (default cuts) and 45.6% (fitted 5/2). The fitted cuts reply to about half of all labeled moments, and react recall is 0. #35 limits fitted cuts to the labeled reply rate +10 points. The model is still not shown to beat the tuned rules (41%).

- **#36, in review: live replies and learning from the group.** The owner's calls (Nov 9): he's doing well enough, go live straight into the group; answer when talked to, plus rule-based chime-ins; learn when to jump in fully automatically (no rating DMs).
  - `core/live.py`: pause (4 s / max 15) → decide the merged turn (M2 rules) → compose → outbox → deliver under a per-group lock. One retry on an @'s model error; a canned line over budget. Nothing happens unless `send_mode` is `on` (or `dry`).
  - `core/feedback.py`, every 5 min: after a chime-in, engaged → reply, ignored or pushed back → quiet; an unanswered group question → reply. Schema v6 `auto_labels`; dev side only, never held out, never in the implied rate; hand labels win.
  - Not yet: emoji reactions, the crisis DM to the owner, waiting for a photo's caption. **After #34 and #36 both merge:** `core/decide.py: merged_msg` must also merge `captions` (left out because #34 adds the field).

## Next steps (in order)

1. **Owner:** review/merge #34 (captions; mark it "Ready for review" on GitHub, since REST can't), #35 and #36; then `/reg bench tune apply` if wanted, and `/reg send on` to go live. Watch `/reg status` (`mode`, `captions`, `learning`). `/reg label recheck` when there's time.
2. **Image captions: #34, in review.** The owner's calls (Nov 9): every photo is described on arrival, and only the caption text plus a content hash are kept, never the image. Captions never name people and mask long digit runs; they're deleted with their message (recall, forget, retention). `caption_images` config, default on. **To check on the live account:** that LLBot's image segment carries a QQ-host `data.url`, and that deepseek-flash accepts the `image_url` part (`/reg status` → `captions`).
3. **Decide whether the decide model goes live.** It needs held-out balanced accuracy clearly above the tuned rules' 41%, on rechecked labels, without over-replying. Reactions are the weak spot for both rules and model (react recall about 0–20%). If the model doesn't clearly win, keep the rules for when to speak and use the model only for compose.
4. **After going live:** a numbers-only check of the first days (replies per day, engaged vs ignored, missed questions, cost). Then retune with the automatic labels, and add emoji reactions, the crisis DM to the owner, and waiting for captions.
5. **M2b leftover:** a model sincerity check for serious and crisis cues. A crisis gets a caring reply plus a DM to the owner, at most once per person per day; hotline 12356.
6. **Open question for the owner:** when the group discusses this bot project itself, should 阿灯 join in (he did, in character) or stay out?
7. **M2d:** a deepseek-flash vs deepseek-v4-pro reply bake-off, then a blind test.
8. **Later:** M3 memory (the dream pass's model half), M4 tone and stickers, M5 learning loop.

## Pointers

- **Pipeline:** `core/pipeline.py` (gate → perceive → decide, in shadow). The live listener is in `main.py`.
- **Decide:** `core/decide.py` (M1 baseline, still used for direct addresses and limits), `core/decide_rules.py` (M2a), `core/decide_model.py` (M2b), `core/limits.py` (room state).
- **Compose and voice:** `core/compose.py`, `core/voice.py`, `harness/rules/tells.json`.
- **Bench:** `bench/` (`runner.py`, `tune.py`, `model_bench.py`, `compose_bench.py`); the CLI is `python -m bench --db <regular.db>`.
- **Admin commands:** `core/admin.py` (help text), `core/people_admin.py`, `core/llm.py` (`ModelAdmin`), `core/outbox.py` (`SendAdmin`).
