# astrbot_plugin_regular · 群里的老熟人

An [AstrBot](https://github.com/AstrBotDevs/AstrBot) plugin that aims to be a
humanlike regular in one QQ friends group: it reads along, remembers what
matters, and speaks when a regular would.

**Status: M0, observe-only.** It logs the configured group and never speaks there.

## Milestones

| | What | State |
|---|---|---|
| M0 | Skeleton: gate, perceive, store, traces, admin DM commands | this branch |
| M1 | Bench: replay transcripts, score decisions | next |
| M2 | Speaking: decide → compose → voice | |
| M3 | Memory: claims, episodes, dream pass | |
| M4 | Social: relationships, mood, stickers | |
| M5 | Learning loop (evidence-gated) | |

## Layout

```
main.py            AstrBot entry: one listener that stops every event
adapter/           the only code that imports astrbot
core/              framework-free: onebot parsing, gate, perceive, pipeline, admin
services/          SQLite store (schema.sql), raw dumps, redacted debug bundles
tests/             stdlib unittest, no AstrBot needed
docs/SETUP.md      AstrBot settings, LLBot, live-account checklist
```

## Safety defaults

- Every event is stopped after the plugin sees it, so nothing reaches AstrBot's
  default LLM or other plugins' commands.
- `/reg` admin commands answer only admins, only in a DM; they're not
  registered in AstrBot's command system at all.
- Group text is treated as data. Likely prompt-injection attempts are flagged
  in the trace; they are never followed.
- Unwatched groups and stranger DMs leave no record.
- Chat data stays in AstrBot's data folder and is never committed.

See [docs/SETUP.md](docs/SETUP.md) to install.

## License

AGPL-3.0
