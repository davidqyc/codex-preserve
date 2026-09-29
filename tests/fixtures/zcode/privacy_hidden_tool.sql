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
  'session-synthetic-zcode-privacy',
  'synthetic ZCode privacy session',
  '/Users/testowner/Documents/demo',
  '3.synthetic',
  1790726400000,
  1790726403000
);

INSERT INTO message VALUES (
  'message-visible-user',
  'session-synthetic-zcode-privacy',
  '{"role":"user","semantics":{"kind":"user_prompt","origin":"real_user","providerVisibility":"visible","transcriptVisibility":"visible","uiVisibility":"visible"},"time":{"created":1790726401000}}',
  1
);
INSERT INTO part VALUES (
  'part-visible-user',
  'message-visible-user',
  'session-synthetic-zcode-privacy',
  '{"type":"text","text":"synthetic safe user text","time":{"start":1,"end":2}}',
  1
);

INSERT INTO message VALUES (
  'message-hidden-system',
  'session-synthetic-zcode-privacy',
  '{"role":"user","synthetic":true,"source":"todo_reminder","semantics":{"kind":"todo_reminder","origin":"agent_runtime","source":"todo_reminder","providerVisibility":"visible","transcriptVisibility":"hidden","uiVisibility":"hidden"},"time":{"created":1790726401100}}',
  2
);
INSERT INTO part VALUES (
  'part-hidden-text',
  'message-hidden-system',
  'session-synthetic-zcode-privacy',
  '{"type":"text","text":"SYNTHETIC_ZCODE_HIDDEN_TEXT_CANARY","synthetic":true,"metadata":{"visibility":"model-only","source":"todo_reminder"},"time":{"start":3,"end":4}}',
  1
);

INSERT INTO message VALUES (
  'message-assistant',
  'session-synthetic-zcode-privacy',
  '{"role":"assistant","parentID":"message-visible-user","semantics":{"kind":"assistant_response","origin":"agent_runtime","providerVisibility":"visible","transcriptVisibility":"visible","uiVisibility":"visible"},"time":{"created":1790726402000,"completed":1790726403000}}',
  3
);
INSERT INTO part VALUES (
  'part-reasoning',
  'message-assistant',
  'session-synthetic-zcode-privacy',
  '{"type":"reasoning","text":"SYNTHETIC_ZCODE_REASONING_CANARY","time":{"start":5,"end":6}}',
  1
);
INSERT INTO part VALUES (
  'part-tool',
  'message-assistant',
  'session-synthetic-zcode-privacy',
  '{"type":"tool","callID":"call-synthetic-zcode","tool":"SyntheticTool","state":{"status":"completed","input":{"secret":"SYNTHETIC_ZCODE_TOOL_INPUT_CANARY"},"output":"SYNTHETIC_ZCODE_TOOL_OUTPUT_CANARY","title":"synthetic"}}',
  2
);
INSERT INTO part VALUES (
  'part-visible-assistant',
  'message-assistant',
  'session-synthetic-zcode-privacy',
  '{"type":"text","text":"synthetic safe assistant text","time":{"start":7,"end":8}}',
  3
);
