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
  'session-synthetic-zcode-linear',
  'synthetic ZCode session',
  '/Users/testowner/Documents/demo',
  '3.synthetic',
  1790726400000,
  1790726402000
);

INSERT INTO message VALUES (
  'message-synthetic-user',
  'session-synthetic-zcode-linear',
  '{"role":"user","semantics":{"kind":"user_prompt","origin":"real_user","providerVisibility":"visible","transcriptVisibility":"visible","uiVisibility":"visible"},"time":{"created":1790726401000}}',
  1
);
INSERT INTO part VALUES (
  'part-synthetic-user-text',
  'message-synthetic-user',
  'session-synthetic-zcode-linear',
  '{"type":"text","text":"synthetic ZCode user prompt","time":{"start":1790726401000,"end":1790726401001}}',
  1
);

INSERT INTO message VALUES (
  'message-synthetic-assistant',
  'session-synthetic-zcode-linear',
  '{"role":"assistant","parentID":"message-synthetic-user","semantics":{"kind":"assistant_response","origin":"agent_runtime","providerVisibility":"visible","transcriptVisibility":"visible","uiVisibility":"visible"},"time":{"created":1790726401500,"completed":1790726402000}}',
  2
);
INSERT INTO part VALUES (
  'part-synthetic-assistant-text',
  'message-synthetic-assistant',
  'session-synthetic-zcode-linear',
  '{"type":"text","text":"synthetic ZCode assistant answer","time":{"start":1790726401500,"end":1790726402000}}',
  1
);
