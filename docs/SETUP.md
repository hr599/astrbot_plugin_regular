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

## Where data lives

`<astrbot>/data/plugin_data/astrbot_plugin_regular/`

- `regular.db`: transcripts and traces (retention: `transcript_retention_days`, default 30)
- `raw/`: raw event dumps while `raw_dump` is on
- `debug/`: bundles from `/reg debug`

None of this is ever committed; `.gitignore` covers it.

## Running the tests

No AstrBot needed:

```
python -m unittest discover -s tests -t .
```
