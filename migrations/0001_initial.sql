CREATE TABLE llm_apps (
  id TEXT PRIMARY KEY,
  key_hash TEXT NOT NULL UNIQUE,
  allowed_types TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE llm_jobs (
  id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL,
  type TEXT NOT NULL,
  version INTEGER NOT NULL,
  idempotency_key TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at INTEGER NOT NULL,
  lease_hash TEXT,
  lease_until INTEGER,
  result_json TEXT,
  result_hash TEXT,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(app_id,idempotency_key)
);
CREATE INDEX llm_jobs_ready ON llm_jobs(status,available_at,created_at);
CREATE INDEX llm_jobs_app ON llm_jobs(app_id,created_at);

CREATE TABLE rate_limits(bucket TEXT NOT NULL, window INTEGER NOT NULL, hits INTEGER NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY(bucket,window));
