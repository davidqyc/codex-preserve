CREATE TABLE session (
  id TEXT PRIMARY KEY,
  title TEXT,
  directory TEXT,
  version TEXT,
  time_created INTEGER,
  time_updated INTEGER
);
CREATE TABLE message (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  data TEXT,
  sequence INTEGER
);
CREATE TABLE part (
  id TEXT PRIMARY KEY,
  message_id TEXT,
  session_id TEXT,
  data TEXT,
  sequence INTEGER
);

INSERT INTO session VALUES (
  'session-synthetic-zcode-unknown',
  'synthetic ZCode unknown session',
  '/Users/testowner/Documents/demo',
  '3.synthetic',
  1790726400000,
  1790726401000
);
INSERT INTO message VALUES (
  'message-synthetic-user',
  'session-synthetic-zcode-unknown',
  '{"role":"user","semantics":{"kind":"user_prompt","origin":"real_user","providerVisibility":"visible","transcriptVisibility":"visible","uiVisibility":"visible"},"time":{"created":1790726401000}}',
  1
);
INSERT INTO part VALUES (
  'part-known-text',
  'message-synthetic-user',
  'session-synthetic-zcode-unknown',
  '{"type":"text","text":"synthetic known text","time":{"start":1,"end":2}}',
  1
);
INSERT INTO part VALUES (
  'part-future',
  'message-synthetic-user',
  'session-synthetic-zcode-unknown',
  '{"type":"future-part","futureValue":"SYNTHETIC_ZCODE_UNKNOWN_PART_CANARY"}',
  2
);
