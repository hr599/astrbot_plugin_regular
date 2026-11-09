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
git clone https://github.com/hr599/astrbot_plugin_regular
```

To update later: `git pull` in that folder (on `master`), then restart AstrBot or
reload the plugin. Back up `data/plugin_data/astrbot_plugin_regular/regular.db`
first; schema upgrades run automatically on start.

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
   - OpenAI: model `gpt-6.1-terra` (the bench judge; never used in the group),
     and a second OpenAI provider with model `gpt-6.1-sol` (the nightly dream, M3).
2. In this plugin's settings, put each provider's AstrBot ID under its role:
   `provider_decide`, `provider_reply`, `provider_caption` -> the flash one;
   `provider_dream` -> the `gpt-6.1-sol` one; `provider_judge` -> the Terra one.
   A blank role is never called. The plugin calls each provider's own
   OpenAI-compatible client with an exact output limit per call, so providers
   must be OpenAI-compatible (DeepSeek and OpenAI are), and the provider's
   "custom extra body" setting is not applied to this plugin's calls.
3. From your DM: `/reg model test reply` (and the other roles). Each sends one
   tiny prompt and says 通过 (passed) only if a real answer came back: an empty
   answer or one cut off by the output limit is a failure, with the finish
   reason and how many tokens went to thinking. Thinking per role: decide,
   reply and caption run with thinking off (DeepSeek thinks by default, and the
   thinking counts against the output limit); dream thinks (high); the judge
   reasons a little (low). The test allows 64 tokens without thinking, 2048 with.
4. `/reg budget` shows today's and this month's spend against the caps.

Budgets: `budget_deepseek_monthly_cny` (50) and `budget_openai_monthly_usd` (30)
become daily caps of 1/30 each, no carry-over, days in UTC+8. A call that could
pass today's cap isn't made (阿灯 stays quiet). Costs are estimated from token
counts and `harness/prices.json` (DeepSeek doubles on weekdays 09-12 and 14-18
Beijing time); `usd_to_cny` converts the USD list prices. The DeepSeek console
is the real bill, so set the cap a little under what you actually want to spend.

## 阿灯's persona (task M)

`harness/persona/` holds who 阿灯 is, in Chinese, as the model will read it:
`core.md` (his life, values, interests, how he texts, style anchors, daily
rhythm) and `guardrails.md` (honesty, group messages are not commands, no /!#,
care in hard moments, privacy, not an assistant, other bots). Guardrails win
when the two disagree. `persona.json` is what code reads (aliases, rhythm,
habit caps). Edit them like any file; changes apply on restart, and
`/reg status` shows the persona version (a content hash).

A random canary marker is created once in the plugin's data folder
(`persona_canary.txt`, never in the repo) and appended to the prompt. If it, or
15+ characters copied from the persona or guardrails, ever appears in something
阿灯 is about to send (including `/reg say`), the message is stopped.

## M2a: decide by rules, tuned on your labels

Shadow decide and `/reg bench` now run the M2 rules (policy `m2-rules.1`,
`core/decide_rules.py`). They still never send anything by themselves. On top of
the baseline's hard rules (an @, quote or poke always gets a reply; the bot-loop,
per-bot and @-burst limits), they read:

- **who it's for**: 阿灯's name used to talk to him (`阿灯你…`) vs. about him
  (`阿灯的…`, `让阿灯…`); a follow-up from the person he just answered; an @ or a
  quote aimed at someone else;
- **whether to chime in**: how lively the message is, his interests, a question in
  the air, laughter or a photo (lean to a reaction), times his daily rhythm
  (asleep: never; in class: less);
- **when to stay out**: an argument, a serious moment, if he spoke in the last two
  messages, or if he already has about a fifth of the recent messages. A sincere
  crisis message always gets a caring reply.

The numbers live in `harness/rules/decide.json`. Shadow traces keep the baseline's
answer as `baseline`, and `/reg bench` prints one comparison line per real
recording.

**Tuning.** `/reg bench tune` (admin DM) searches the numbers on your dev labels and
replies with numbers only: balanced accuracy (the average of how well it catches
quiet, react and reply, so "always quiet" can't score well) before and after, the
held-out labels' score, which never steers the search, and the reply share against
what your labels imply. It needs at least 60 dev labels, and the synthetic fixtures
are never tuned on. If the held-out score drops, it says so; don't apply then.
`/reg bench tune apply` writes the changed numbers to `decide_params.json` in the
data folder, which overrides the harness file and takes effect at once.
`/reg bench tune reset` removes it. `python -m bench --db …` uses the same numbers;
add `--baseline` to replay the M1 baseline instead.

## M2b: a model for the moments the rules can't settle

The rules keep the clear cases (direct addresses and their limits, other bots,
crisis, asleep, arguments, serious moments, not twice in a row, the share cap, a
clear "talking to 阿灯"). For the rest, the decide model (`provider_decide`,
thinking off) reads 阿灯's persona and the last 12 messages, with speakers shown as
letters and @-numbers hidden, and scores the moment twice, 0-9: how much he'd
want to react, and how much he'd want to reply. The chat sits in a fenced block
marked as data, and only an exact `{"react": n, "reply": n}` counts as an answer.

Scores, not a single word, because a model asked to choose leans hard one way
(the first run said quiet 194 times out of 197). Two cuts turn scores into an
action: reply if the reply score reaches its cut, else react if the react score
reaches its cut. `/reg bench model` (admin DM) asks about your labelled moments
that the rules weren't sure of, then fits the cuts on your dev labels only and
reports balanced accuracy for rules alone, the default cuts (5/5) and the fitted
cuts, on dev and held-out. The held-out line is the fair test. Calls are budgeted
and cached (refitting the cuts reuses the cached scores, free).
`/reg bench model 20` asks only the first 20. A full run on about 200 moments
cost about ¥0.06 in practice. A failed call is counted by error type only.

For now this is bench-only: shadow decide is still the rules. Whether the model
goes live depends on what this shows.

## M2c (part 1): what 阿灯 says

Once decide says "reply", compose writes the message (`core/compose.py`,
`provider_reply`, thinking off). The reply model gets the persona and guardrails,
short instructions (text like you do in the group: short, casual, one to three
lines; no lists, no 您, no customer-service lines; don't invent numbers, times,
prices or quotes; say you don't know when you don't; never use the prompt's
stand-ins A/B/@某人1 or @ anyone; `[不说]` to stay quiet after all), and the last
16 messages rendered like decide's (speakers as letters, @-numbers hidden,
fenced as data).

Every draft goes through the voice check: leaks of the persona, the canary,
command prefixes, assistant tells, length against the group (about two of the
group's longer messages), made-up specifics (anything not in the chat or his own
persona), and stand-ins. A draft that fails gets one redraft with a note saying
what to fix; a leaked draft is never shown back to the model. A second failure
is dropped: 阿灯 stays quiet. A good draft is split into up to three messages
with typing-time delays.

`/reg bench compose [N]` (admin DM) drafts replies for the first N (default 30)
moments you labelled "reply" and answers in numbers: sent first try, after a
redraft, chose not to speak, dropped, failed; which checks fired; message
counts and lengths; calls and cost. The drafts themselves go into a local HTML
report (`bench/runs/compose-*.html`). It quotes the chat, so it stays on your
machine, and `/reg forget` deletes it. Calls are budgeted and cached; a forget
during the run voids it. Nothing is sent to the group: live sending of composed
replies is the next step, after you've read some drafts.

Not yet: live decide doesn't wait for a speaker's burst to end (the bench does);
that comes with live replies in M2c.

## Image captions

With `caption_images` on (the default), every photo posted in a watched group is
described once, on arrival, by the caption model (`provider_caption`, e.g.
deepseek-flash, which takes images). Decide, compose and your `/reg label` view
then show `[图片：一碗烤冷面，上面撒了葱花]` instead of `[图片]`.

- **What's kept:** only the description text and a content hash (to reuse a
  description when the same image is posted again). The image itself is fetched
  from QQ's image servers into memory and dropped after the call.
- **What it won't do:** name or guess who's in a photo; copy QQ, phone or ID
  numbers from screenshots (long digit runs become `[号码]`); describe stickers.
  Text inside an image is treated like any chat text: data, never instructions.
- **Lifetime:** a description lives as long as a message showing it. Recall,
  `/reg forget` and the transcript retention delete it; a forget during the
  call writes nothing.
- **Cost:** about ¥0.001 per photo on the DeepSeek budget. The daily cap
  applies, with a worst-case allowance per image.
- **Status:** `/reg status` shows `captions` (on/off, and counts since start:
  described, reused, skipped, failed).

A description arrives a few seconds after the photo. A live turn that includes a
photo still being described waits for it, up to 6 seconds (only while one is
actually in progress), so 阿灯 doesn't answer a photo he can't see.

## Live replies (M2c part 2)

With `send_mode` set to `on` (`/reg send on` in the admin DM), 阿灯 talks in the
group. `dry` does everything except the send: each reply is composed, checked and
traced, but not sent. `off` (the default) only observes and makes no model calls
for replies.

How a reply happens (`core/live.py`):
1. A message is stored. 阿灯 waits for that person's pause: 4 seconds without a
   new message from them, at most 15 seconds per turn. A burst is one turn.
2. Decide runs on the whole turn with the M2 rules (`/reg bench tune apply` makes
   them use your tuned numbers). An @, a quote of him or a poke is always
   answered, within the limits (bot loops, @ bursts). Otherwise he chimes in when
   the rules say so; never while asleep, during an argument or a serious moment,
   twice in a row, or above about a fifth of recent messages.
3. Compose writes the reply and the voice check approves it (one redraft at
   most); the outbox checks it again, splits it into up to three messages with
   typing delays, and applies the hourly cap (20 per group).
4. One group, one reply at a time: the next decision waits until this reply is
   sent, so it sees what he just said.

**How chatty.** After he answers someone, their next message counts as talking to
him once (`follow_up_max`, default 1); after that they address him again (@, a
quote, his name) or he lets the conversation go. Replies that weren't to a direct
address are capped at `unprompted_per_hour` (default 6) per group; @s, quotes and
pokes are never capped by this (only by the hourly 20-message cap). Both are in
`harness/rules/decide.json`, and a tuned `decide_params.json` can override them.

An @ that hits a model error gets one retry; over the daily budget it gets a
short canned in-character line instead of a model call.

**Reactions.** When decide says "react", 阿灯 puts an emoji (QQ 表情回应) on the
turn's last message: no model call. The emoji depends on the message (laughter,
a photo, a question, otherwise a default; `harness/rules/reactions.json`), and
once the group has reacted a few times, half the time it's one of the group's own
three favourites, so he reacts the way your group does. At most 20 per group per
hour; never on a poke or another bot's message; only with `send_mode` on (dry
traces what he'd react with). Uses OneBot `set_msg_emoji_like`; to check on the
live account: that LLBot accepts it, and that the default ids (76 赞, 66 爱心,
182 笑哭, 13 呲牙, 124 OK, 179 doge) show the faces you'd expect. Edit the json
to change them. Every turn leaves a trace (`ended_at="live"`: the rule, outcome, hit
kinds, cost; no text).

To stop him at any time: `/reg send off`, or `/reg pause <group>`.

**The hourly cap.** Each group gets at most `send_cap_per_hour` messages an hour
(default 20; every part of a split reply counts). `/reg send cap N` changes it
from the admin DM (saved to the config, at most 60: a low send rate also keeps
the alt account clear of QQ's risk controls). `/reg send reset [group]` clears
the current hour's count, for when the group is in a chatty mood; a restart
within the hour won't count the old messages again.

**Laughs at the end of a line.** He ends at most one line in five with a laugh
("hhh", "哈哈哈", "233"); later ones are trimmed before sending (no extra model
call), and the trace says `trimmed: laugh ending`. A reply that is only a laugh
stays as it is.

## Crisis moments

When a message sounds like someone may want to hurt themselves (不想活, 撑不下去…,
not an obvious joke like "这作业要我命 哈哈"), the decide model first checks
whether it's sincere or hyperbole (`core/crisis.py`). Then:
- **Sincere:** 阿灯 replies with care in the group (the guardrails say how:
  gently, no lecture, people they trust, the 12356 line), and **you get a private
  DM** saying who, in which group, and what they said (shortened), so you can check
  in as a friend. At most once per person per day.
- **Not sincere:** an ordinary message; the usual rules decide.
- **The check couldn't run** (model error, budget, no provider): treated as
  sincere. A kind message too many is better than one too few.

The DM goes to `crisis_dm_to` in the plugin config, or, if that's empty, to
AstrBot's `admins_id`. It works with `send_mode` dry or on (not off). The trace
for it holds a person id and no text, and `/reg forget` removes it.

## Learning from the group (automatic)

Every 5 minutes the plugin looks at moments at least 5 minutes old and labels
what the group's reaction settled (`core/feedback.py`). This only covers times he
**chose** to chime in, since answering an @ is a rule:
- engaged (someone quotes him, @s him, uses his name, or reacts to what he said)
  → that was a good moment to speak;
- ignored (people kept talking without turning to him) or pushed back (a rude
  cue aimed at him) → better to stay quiet;
- nobody said anything → no label.

A question to the group that nobody else answers within 5 minutes, while he
stayed quiet, becomes a "should have spoken" label. Your own labels always win.
These automatic labels feed `/reg bench tune` and the model cutoffs on the
tuning side only: the held-out set stays your hand labels, and the label-rate
estimate ignores them. They're deleted with their message (recall, forget,
retention). `/reg status` shows the counts under `learning`.

## Dreaming: memory, dry run first (M3a)

Once a night, at 04:30 Beijing time, 阿灯 "dreams": a model reads the day's chat
and proposes what's worth remembering about people, and plain code checks every
proposal before anything would be saved. In M3a it only ever runs **dry**: it
writes a report of what it would remember, and changes no memory. Writing
memories comes with M3b, after you've read some reports.

**Before you turn it on:** the dream sends each day's chat (nicknames, no QQ
numbers) to OpenAI, through `provider_dream`. Your friends agreed to DeepSeek,
so check with them first. Even dry runs send it.

1. Add the `gpt-6.1-sol` provider and set `provider_dream` (Models and budget,
   above). `/reg model test dream` should say 通过.
2. `/reg bench dream`: the gate on an invented group with known answers (in the
   repo, `bench/fixtures/dream_v1.json`). It reports how many of the 15 things
   it should remember it found, and how many of the 10 deliberately wrong notes
   it stored (gossip, health, a salary, an order to 阿灯, an opted-out member,
   another bot, a recalled message, a passing mood, an instruction hidden in a
   photo). The gate: 10 or more found, 0 wrong stored. About $0.3; a rerun is
   free (cached). Send me the reply: numbers and fixture ids only.
3. `/reg dream dry` turns on nightly dry runs (`dream_mode` in the config).
   `/reg dream now [群号]` runs one right away. `/reg dream off` stops.
4. `/reg dream log [N]`: the last runs, numbers only. Each run's report is
   `dream/dream-<run>.html` in the data folder: every proposal, the messages it
   quotes, whether it passed and why not. It quotes your group, so it stays on
   your machine; reports are deleted after 30 days, and `/reg forget` deletes
   them all.

What it checks (a proposal that fails any of these is never saved):
- every quote is word for word in the message it cites, and that message is
  still there, from a human in this group who hasn't opted out;
- it isn't an order to 阿灯 (except how someone wants to be called, or a topic
  they don't want raised), and health, money, politics and the like are only
  ever "don't bring this up";
- a fact about someone needs their own words or two different people; less
  than that is kept as a "maybe" that expires in 14 days;
- at most 40 characters, no long digit runs, no copied photo descriptions;
- per night: 15 new memories, 3 about one person, 2 new jokes;
- a second, cheap model (`provider_decide`) answers one question per proposal:
  do these quotes really support it? "no" rejects it; "not sure" keeps a new
  memory only as a "maybe".

Budget: at most `dream_budget_usd` (0.40) a night, from the OpenAI budget. A
busy day is read in pieces of 400 messages; what doesn't fit tonight waits for
tomorrow, and nothing older than 7 days is read. A `/reg forget`, opt-out,
pause or `/reg dream off` while a run is going cancels it.

## Where data lives

`<astrbot>/data/plugin_data/astrbot_plugin_regular/`

- `regular.db`: transcripts and traces (retention: `transcript_retention_days`, default 30)
- `raw/`: raw event dumps while `raw_dump` is on
- `debug/`: bundles from `/reg debug`
- `bench/runs/`: bench reports (HTML trace viewer + numbers)
- `bench/recordings/`: recordings you saved; not subject to retention
- `dream/`: dream run reports (quote the chat; deleted after 30 days and by `/reg forget`)

None of this is ever committed; `.gitignore` covers it.

## Running the tests

No AstrBot needed:

```
python -m unittest discover -s tests -t .
```
