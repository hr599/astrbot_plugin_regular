-- astrbot_plugin_regular store (SQLite, one file per bot, in the data folder)
CREATE TABLE people (
  person_id     INTEGER PRIMARY KEY,
  platform      TEXT NOT NULL,          -- aiocqhttp (QQ via OneBot v11)
  platform_uid  TEXT NOT NULL,
  opted_out     INTEGER NOT NULL DEFAULT 0,
  first_seen    INTEGER NOT NULL,
  UNIQUE (platform, platform_uid)
);
CREATE TABLE aliases (                  -- display-name history
  person_id  INTEGER NOT NULL REFERENCES people,
  name       TEXT NOT NULL,
  first_seen INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL
);
CREATE TABLE person_links (             -- cross-platform, admin only
  a INTEGER NOT NULL REFERENCES people,
  b INTEGER NOT NULL REFERENCES people,
  linked_by TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE TABLE scopes (                   -- one row per group chat or DM
  scope_id TEXT PRIMARY KEY,            -- AstrBot unified_msg_origin
  platform TEXT NOT NULL,
  kind     TEXT NOT NULL CHECK (kind IN ('group', 'dm')),
  title    TEXT,
  paused   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sessions (
  session_id   INTEGER PRIMARY KEY,
  scope_id     TEXT NOT NULL REFERENCES scopes,
  started      INTEGER NOT NULL,
  ended        INTEGER,
  n_msgs       INTEGER NOT NULL DEFAULT 0,
  salience_sum REAL NOT NULL DEFAULT 0,
  dream_run    INTEGER                  -- run that consumed it
);
CREATE TABLE messages (                 -- L0 transcripts
  msg_id          TEXT PRIMARY KEY,     -- "<platform>:<scope>:<native id>"
  scope_id        TEXT NOT NULL REFERENCES scopes,
  session_id      INTEGER REFERENCES sessions,
  person_id       INTEGER REFERENCES people,
  ts              INTEGER NOT NULL,
  reply_to        TEXT,
  mentions        TEXT,                 -- JSON person_ids @-mentioned; "bot" for the bot
  text            TEXT NOT NULL,
  is_bot          INTEGER NOT NULL DEFAULT 0,
  is_command_like INTEGER NOT NULL DEFAULT 0,
  recalled        INTEGER NOT NULL DEFAULT 0,  -- 撤回: text blanked, never used for memory
  salience        REAL NOT NULL DEFAULT 0
);
CREATE INDEX messages_scope_ts ON messages (scope_id, ts);
CREATE VIRTUAL TABLE messages_fts USING fts5 (
  text, content = 'messages', content_rowid = 'rowid',
  tokenize = 'trigram'                  -- default tokenizer can't split Chinese
);
CREATE TABLE claims (                   -- L1 candidates + L2 cards
  claim_id      INTEGER PRIMARY KEY,
  scope_id      TEXT NOT NULL REFERENCES scopes,
  subject_id    INTEGER REFERENCES people,   -- NULL = group-level
  object_id     INTEGER REFERENCES people,   -- second person in a relation
  type          TEXT NOT NULL CHECK (type IN ('identity', 'relation',
                  'self_disclosure', 'joke', 'boundary', 'norm', 'rapport',
                  'bot_self', 'intention', 'reflection')),
  text          TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('candidate', 'validated',
                  'dormant', 'retired')),
  source        TEXT NOT NULL CHECK (source IN ('self', 'corroborated',
                  'admin', 'computed')),
  strength      REAL NOT NULL DEFAULT 1.0,
  pinned        INTEGER NOT NULL DEFAULT 0,  -- promoted to L3
  created       INTEGER NOT NULL,
  last_evidence INTEGER NOT NULL,
  expires       INTEGER,                     -- candidates, intentions
  run_id        INTEGER                      -- dream run that last changed it
);
CREATE INDEX claims_scope_subject ON claims (scope_id, subject_id, status);
CREATE TABLE evidence (
  claim_id INTEGER NOT NULL REFERENCES claims,
  msg_id   TEXT NOT NULL,               -- may outlive the message (TTL)
  quote    TEXT NOT NULL,               -- verbatim substring, checked
  stance   TEXT NOT NULL CHECK (stance IN ('supports', 'contradicts'))
);
CREATE TABLE dream_runs (               -- audit log, revertible
  run_id     INTEGER PRIMARY KEY,
  scope_id   TEXT NOT NULL REFERENCES scopes,
  started    INTEGER NOT NULL,
  span_from  TEXT,
  span_to    TEXT,
  model      TEXT,
  diff_json  TEXT NOT NULL,
  n_applied  INTEGER NOT NULL DEFAULT 0,
  n_rejected INTEGER NOT NULL DEFAULT 0,
  reverted   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE rejected (                 -- input for the persona loop later
  run_id        INTEGER NOT NULL REFERENCES dream_runs,
  proposal_json TEXT NOT NULL,
  reason        TEXT NOT NULL
);
CREATE TABLE episodes (                 -- conversation memory
  episode_id     INTEGER PRIMARY KEY,
  scope_id       TEXT NOT NULL REFERENCES scopes,
  started        INTEGER NOT NULL,
  ended          INTEGER NOT NULL,
  topic          TEXT NOT NULL,
  summary        TEXT NOT NULL,         -- who, what was decided, what the bot said
  participants   TEXT NOT NULL,         -- JSON array of person_ids
  importance     REAL NOT NULL DEFAULT 0.5,
  strength       REAL NOT NULL DEFAULT 1.0,
  last_human_ref INTEGER,               -- refreshed only when a person refers back
  run_id         INTEGER
);
CREATE TABLE episode_evidence (
  episode_id INTEGER NOT NULL REFERENCES episodes,
  msg_id     TEXT NOT NULL,
  quote      TEXT NOT NULL
);
CREATE VIRTUAL TABLE episodes_fts USING fts5 (
  topic, summary, content = 'episodes', content_rowid = 'episode_id',
  tokenize = 'trigram'
);
CREATE TABLE traces (                   -- why each turn ended the way it did
  trace_id        INTEGER PRIMARY KEY,
  scope_id        TEXT NOT NULL,
  msg_id          TEXT,                 -- message that started the turn
  ts              INTEGER NOT NULL,
  ended_at        TEXT NOT NULL,        -- gate | perceive | decide | compose | voice
  decision        TEXT,                 -- quiet | react | reply | delayed_reply
  reasons         TEXT NOT NULL,        -- JSON: scores, rules fired, checks
  harness_version TEXT NOT NULL         -- persona + rules version that produced it
);

-- Social layer: relationships, mood, media and stickers
CREATE TABLE relationships (            -- the bot's attitude to each person, per group
  scope_id      TEXT NOT NULL REFERENCES scopes,
  person_id     INTEGER NOT NULL REFERENCES people,
  familiarity   REAL NOT NULL DEFAULT 0,    -- from real back-and-forth, computed
  warmth        REAL NOT NULL DEFAULT 0.5,  -- floor enforced in code (0.3)
  friction      REAL NOT NULL DEFAULT 0,    -- fades in days; never becomes a claim
  last_exchange INTEGER,
  updated       INTEGER NOT NULL,
  PRIMARY KEY (scope_id, person_id),
  CHECK (familiarity BETWEEN 0 AND 1 AND warmth BETWEEN 0 AND 1
         AND friction BETWEEN 0 AND 1)
);
CREATE TABLE relationship_events (      -- every change with its reason; daily caps sum these
  event_id  INTEGER PRIMARY KEY,
  scope_id  TEXT NOT NULL REFERENCES scopes,
  person_id INTEGER NOT NULL REFERENCES people,
  ts        INTEGER NOT NULL,
  field     TEXT NOT NULL CHECK (field IN ('familiarity', 'warmth', 'friction')),
  delta     REAL NOT NULL,
  reason    TEXT NOT NULL,              -- exchange | dream:friendly | rude_to_bot | decay | admin
  msg_id    TEXT,                       -- evidence, when there is one
  run_id    INTEGER                     -- dream run, for warmth
);
CREATE INDEX relationship_events_day ON relationship_events (scope_id, person_id, ts);
CREATE TABLE bot_state (                -- short-lived, per group: flavor, never policy
  scope_id      TEXT PRIMARY KEY REFERENCES scopes,
  mood          REAL NOT NULL DEFAULT 0,    -- -1..1, back to baseline by half-life
  energy        REAL NOT NULL DEFAULT 0.5,  -- follows the persona's daily rhythm
  unanswered    INTEGER NOT NULL DEFAULT 0, -- its unprompted messages nobody answered
  heat          REAL NOT NULL DEFAULT 0,    -- argument detector
  serious_until INTEGER,                    -- serious-moment mode end
  updated       INTEGER NOT NULL,
  CHECK (mood BETWEEN -1 AND 1 AND energy BETWEEN 0 AND 1 AND heat BETWEEN 0 AND 1)
);
CREATE TABLE media (                    -- captions cached by content
  media_key  TEXT PRIMARY KEY,          -- perceptual hash for images, sha256 otherwise
  kind       TEXT NOT NULL CHECK (kind IN ('image', 'sticker', 'face', 'voice',
               'video', 'file')),
  caption    TEXT,                      -- NULL until captioned; never names people
  ocr_text   TEXT,                      -- untrusted, like any chat text
  meaning    TEXT,                      -- how this group uses a sticker
  n_seen     INTEGER NOT NULL DEFAULT 1,
  first_seen INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL
);
CREATE TABLE message_media (
  msg_id     TEXT NOT NULL REFERENCES messages ON DELETE CASCADE,
  position   INTEGER NOT NULL,          -- order inside the message
  media_key  TEXT NOT NULL REFERENCES media,
  transcript TEXT,                      -- speech-to-text, for voice
  PRIMARY KEY (msg_id, position)
);
CREATE TABLE stickers (                 -- what the bot may send: you approve each one
  sticker_id INTEGER PRIMARY KEY,
  media_key  TEXT NOT NULL UNIQUE REFERENCES media,
  file       TEXT NOT NULL,             -- under the plugin's data folder
  tags       TEXT NOT NULL,             -- JSON: mood and meaning tags
  status     TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate',
               'approved', 'retired')),
  n_sent     INTEGER NOT NULL DEFAULT 0,
  last_sent  INTEGER,
  added      INTEGER NOT NULL
);
