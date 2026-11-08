# Setup (M0)

M0 watches one group and logs. It never speaks there. The only thing it ever
sends is a reply to your own DM when you type `/reg ...`.

## 1. Versions

- AstrBot **4.29.x** (the plugin pins `>=4.29.4,<4.30`; tested against 4.29.4 source).
- LLBot connected to AstrBot as **OneBot v11 reverse WebSocket** (AstrBot's `aiocqhttp` adapter).
  Message format must be **array** (LLBot's default).

## 2. Install the plugin

Clone into AstrBot's `data/plugins/`:

```
cd <astrbot>/data/plugins
git clone -b m0-skeleton https://github.com/hr599/astrbot_plugin_regular
```

Restart AstrBot (or reload plugins in the dashboard). In the plugin's config page:

- **group_ids**: the friends group's number. Empty = the plugin watches nothing.
- leave `raw_dump` off for now; you'll switch it on from your DM in step 4.

## 3. AstrBot settings

These close the paths by which group members could reach the LLM or commands
without going through the plugin. Dashboard → Config:

| Setting | Value | Why |
|---|---|---|
| **Plugins → available plugins (`plugin_set`)** | **only `astrbot_plugin_regular`** | **required.** AstrBot checks every plugin's command filters *before* any plugin runs and before the group whitelist, so another plugin's command (e.g. a typed-argument error) could reply in the group no matter what this plugin does. Plugins outside `plugin_set` are never checked. |
| `admins_id` | add **your** QQ number | `/reg` only answers admins |
| `disable_builtin_commands` | on | nobody can run `/help`, `/provider`, `/reset`… from the group |
| `no_permission_reply` | off | don't announce that commands exist |
| `empty_mention_waiting` | off | its listener sits above ours and would call the LLM on a bare @ |
| `rate_limit.count` | 0 | the plugin rate-limits per person itself (from M2) |
| `id_whitelist` | the group number (+ your QQ for DMs) | AstrBot drops everything else before plugins see it |
| `wl_ignore_admin_on_group` | off | admins in *other* groups stay ignored too |
| `ignore_bot_self_message` | on | the bot's own messages aren't fed back in |
| speech-to-text (STT) | off | the plugin will do its own voice handling (M2) |
| `group_icl` / `active_reply` / `group_message_history` | off | the plugin keeps its own history |

No model provider is needed for M0. When we get to M2, the DeepSeek key goes in
AstrBot's provider settings only, never in this repo or the plugin config.

## 4. Live-account checks

DM the bot account from your own QQ:

1. `/reg status` → you should get a status block back. If nothing comes back,
   check `admins_id` and that your QQ is in `id_whitelist`.
   Check two lines in it:
   - `isolation: ok`. If it says `NOT isolated: …`, those plugins can still
     answer in the group: fix `plugin_set` (above). The same warning is in
     AstrBot's log at the first message after startup.
   - `cleanup: running hourly`: retention runs on a clock, not on traffic.
2. `/reg dump on` → starts saving raw events (local only, kept 3 days).
3. In the group, over a day or so, have people do each of these at least once:
   - [ ] a normal text message, a reply to someone, an @ of the bot
   - [ ] a photo **and** a sticker (both a QQ "market" sticker and a saved-image sticker)
   - [ ] a voice message
   - [ ] a poke (戳一戳) on the bot
   - [ ] an emoji reaction (表情回应) on a message
   - [ ] someone recalls (撤回) a message
   - [ ] you send something from the bot account by hand, if you can (checks self-message reporting is off)
4. `/reg debug` → the bot replies with a path to a zip in the plugin's data folder.
   Attach that zip in our chat. IDs are salted hashes and all text is replaced by
   its length, so it shows event *shapes*, not anyone's words. Open it first if you like.
5. `/reg dump off`.

Those answer the open questions: what LLBot sends for reactions, pokes and
recalls; how a sticker differs from a photo; whether self-messages are reported.
Two more checks need the speaking path and come with M2: sending reactions/pokes
via `call_action`, and the bot DMing you.

## M1: labels and the bench

The bot still never speaks. What's new: every trace now records what it *would*
have done (`shadow` in traces, policy `m1-baseline`), and you can grade that.

**Labeling (from your DM).** Send `/reg label`. The bot sends a batch of up to 5
logged moments (each marked →, with the few messages that came before it) and a
short guide the first time. Answer the whole batch in one line, in order:
`/reg label 5 5 3 4 5` (or `55345`; `s` skips one). The next batch follows.

| Answer | Meaning |
|---|---|
| `1` | meant for 阿灯, it should reply |
| `2` | meant for 阿灯, a reaction is enough |
| `3` | not meant for it, but it would chime in |
| `4` | not meant for it, a reaction at most |
| `5` | not meant for it, stay quiet |
| `s` | skip (unsure is fine) |

- **Label as 阿灯, not as yourself.** It's a quiet regular, around a tenth of the
  group's messages. "Meant for it" means talking *to* it (its name as an address,
  a question to it, picking up its line); just mentioning it doesn't count.
- **Obvious moments aren't asked.** An @ of the bot or a quote of its message is
  always for it and always answered, by rule.
- **Expect mostly 4s and 5s for now.** Nobody talks to 阿灯 yet because it hasn't
  spoken. Your labels mainly measure false alarms and "would a regular chime in",
  the hardest part of M2. The synthetic fixtures cover being addressed.
- **One label in five is held out** (fixed by message id). Tuning never looks at
  those; the bench reports both, so a rule that memorised your labels shows up as
  a gap instead of a fake win.
- A message recalled or expired while it's on your screen is dropped, not labeled.
- `/reg label help` shows the guide again.

Start with 30–50, spread over days *and* nights. About 200 is the design doc's
target for the addressed-to-bot score; ~10 a day gets there in a few weeks.

**Running the bench.** Send `/reg bench`. It replays three things through the
same decide code the live bot uses, on a fake clock (each moment is decided at
the time it happened):

- everything currently logged, with your labels;
- any recordings you saved with `/reg bench save`;
- the synthetic fixtures that ship in `bench/fixtures/`.

It replies with a short score summary and the path to an HTML trace viewer in
`bench/runs/` (open it locally; it contains real messages, so don't share it).
Tick "only misses and disagreements" to see just what it got wrong.
`/reg debug` now includes the latest bench numbers (numbers only) for me.

You can also run it from a terminal in the plugin folder:
`python -m bench --db <path to regular.db>`; without `--db` it runs the fixtures only.
It reads `bot_aliases` from the plugin's AstrBot config (found from the db path), so
it scores exactly like `/reg bench`; `--aliases 阿灯,灯灯,灯` overrides. The report
header shows which aliases a run used.

**Saving a recording.** `/reg bench save [name]` freezes the last 7 days of logs
plus your labels into `bench/recordings/<name>.json`. Saved recordings are
**not** cleaned up by the 30-day retention (that's the point: a fixed test set),
and they contain real messages. The design doc asks for everyone's OK before
recording a week of the group; that's your call with your friends.

**What the scores mean (baseline).**
- *explicit missed* must be 0: every @, quote of a bot message and poke gets an answer. Hard gate.
- *addressed precision / recall*: of the moments you said were for 阿灯, how many it spotted, and how often it thought something was for it when it wasn't.
- *action agreement*: how often its quiet/react/reply matches your label.
- *bot share*: what fraction of the chat would be 阿灯; target under about 10%.

The baseline follows the design doc's "who's talking to whom": an @, a quote of
its message or a poke always gets a reply; its name counts as *likely* addressed
(the bench scores that as detected) but doesn't force a reply, because people
also talk *about* the bot. It never reacts and never chimes in on its own, so
expect it to miss name addresses that deserve a reply, implied addresses, and
every "a regular would chime in" moment. That's the bar M2 has to beat.

**Command-like messages** (`/`, `!` or `#` followed by letters, from anyone in the
group) are no longer stored at all, per the design doc; the trace just notes one
arrived. The upgrade removes the ones M0 had logged. A message starting with
`...` or `。。。` is ordinary chat.

**Name aliases.** The plugin config has `bot_aliases` (default 阿灯 / 灯灯 / 灯).
Two-character names count anywhere; a single 灯 only counts when it's used as a
name (灯，你觉得呢), not in 开灯 or 灯光.

## Privacy controls (admin DM only)

These work only from your DM, and only for accounts in AstrBot's `admins_id`.
In the group, `/reg ...` is a command and is never stored or run, so nobody
else can trigger them. A friend who wants out asks you.

- `/reg forget <QQ>` shows what's stored about that person. `/reg forget <QQ> confirm`
  erases it: their messages (and search index, labels, traces), memory about
  them and evidence quoting them, conversation summaries they were in, the
  relationship record and their names, plus their events in the local raw dumps
  and their lines in saved bench recordings. Bench HTML reports (they show message
  text) are deleted; `/reg bench` makes a new one. It can't be undone. Memory runs that
  held their words lose their undo snapshot. Add `stop` to also stop recording them.
  `/reg mem forget ...` works the same.
- `/reg optout <QQ>` / `/reg optin <QQ>`: stop or resume recording someone. Opting
  out doesn't erase what's already stored; use forget for that.
- `/reg pause [group]` / `/reg resume [group]`: no group means all watched groups.
  While paused nothing is stored or acted on, except recalls: a message recorded
  before the pause and recalled during it is still blanked. Saved in the plugin
  config as `paused_group_ids`, so a restart keeps it.

If a local file can't be cleaned (say a broken saved recording), forget stops
before touching the database and tells you which file; fix or delete it and send
the same command again. With `stop`, recording stops right away either way.

Memory keeps who said each quote, so forget still finds someone's words after
their messages have aged out (30 days). Undo for a memory run only works while
the messages it cites are still within retention.

Debug bundles you already made are redacted (ids hashed, text masked) and aren't
touched by forget; delete the `debug/` folder yourself if you want them gone.
## Other bots and @ bursts (task G)

List the other bots' QQ numbers in the plugin setting `other_bot_ids`. 阿灯 can
chat with them, within limits (`core/limits.py`):

- once 阿灯 has answered a bot, it doesn't answer any bot again until a person
  speaks (no bot-to-bot chains, never twice in a row to the same bot);
- at most 3 replies a day to each bot (UTC+8 days);
- their messages never feed memory or relationship records.

People: a direct @ / quote / poke always gets a reply, except in a burst. If the
same person has already @'d 阿灯 4 times in the last 2 minutes, it reacts
instead (pokes count too). Normal chat never gets near that.

The limits keep their own state per group, so a busy chat can't push an earlier
@ or a bot exchange out of view, and a restart rebuilds it from today's stored
messages.

While 阿灯 only observes, these show up as the shadow decision's `rule` in
traces (`bot_loop`, `bot_daily_cap`, `rate_limited`) and on the bench. The bot
rules need 阿灯's own messages, which are recorded once sending lands, so live
they start biting then; the bench sees them now.

## Sending (task L)

Off by default. The plugin setting `send_mode` (or `/reg send off|dry|on` from
your DM) switches it:

- `off`: nothing is sent or queued.
- `dry`: everything runs except the actual send: each part is checked, timed
  and logged as a trace (`ended_at=send`, length only), nothing reaches QQ.
- `on`: parts go out through LLBot and are stored as 阿灯's own messages, so
  quotes of them are recognised and the bot-loop and per-bot limits work live.

Before M2 has a reply model, the only thing that sends is the test command
`/reg say <group> <text>` (admin DM only). Your words are checked for safety:
nothing that starts like a command (/ ! # in any width, even after splitting
into parts), no leaked persona text or canaries. Style isn't checked, since
they're your words. Every group also has a hard cap of 20 messages in any hour,
counted from what was really sent (it survives restarts). Switching to `off` or
pausing the group stops a message between parts.

Suggested first check: `/reg send dry`, `/reg say <group> 测试一下～ 马上就好`,
then `/reg debug` (two `send` traces, nothing in the group). Then `/reg send on`
and the same `/reg say`; 阿灯 should post two short messages a few seconds
apart. `/reg send off` afterwards until M2.

## Models and budget (task K)

Nothing calls a model on its own yet (that's M2). This sets up the connection
so it's ready, and lets you test each model once.

1. In AstrBot, add the providers (API keys live only there):
   - DeepSeek, OpenAI-compatible, base URL `https://api.deepseek.com`: one
     provider with model `deepseek-flash`, one with `deepseek-v4-pro`.
   - OpenAI: model `gpt-6.1-terra` (the bench judge; never used in the group).
2. In this plugin's settings, put each provider's AstrBot ID under its role:
   `provider_decide`, `provider_reply`, `provider_caption` -> the flash one;
   `provider_dream` -> the v4-pro one; `provider_judge` -> the OpenAI one.
   A blank role is never called.
3. From your DM: `/reg model test reply` (and the other roles). Each sends one
   tiny prompt and tells you the model, the reply, the time and the cost.
4. `/reg budget` shows today's and this month's spend against the caps.

Budgets: `budget_deepseek_monthly_cny` (50) and `budget_openai_monthly_usd` (30)
become daily caps of 1/30 each, no carry-over, days in UTC+8. A call that could
pass today's cap isn't made (阿灯 stays quiet). Costs are estimated from token
counts and `harness/prices.json` (DeepSeek doubles on weekdays 09-12 and 14-18
Beijing time); `usd_to_cny` converts the USD list prices. The DeepSeek console
is the real bill, so set the cap a little under what you actually want to spend.

## Where data lives

`<astrbot>/data/plugin_data/astrbot_plugin_regular/`

- `regular.db`: transcripts and traces (retention: `transcript_retention_days`, default 30)
- `raw/`: raw event dumps while `raw_dump` is on
- `debug/`: bundles from `/reg debug`
- `bench/runs/`: bench reports (HTML trace viewer + numbers)
- `bench/recordings/`: recordings you saved; not subject to retention

None of this is ever committed; `.gitignore` covers it.

## Running the tests

No AstrBot needed:

```
python -m unittest discover -s tests -t .
```
