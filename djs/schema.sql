-- SQLite. Idempotent. Source of truth for all tables.
CREATE TABLE IF NOT EXISTS tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  provider    TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'keyword',          -- keyword | tenant (copied from provider.kind)
  payload     TEXT NOT NULL,                            -- JSON: {keyword, city?, max_pages?, detail?} | {tenant}
  status      TEXT NOT NULL DEFAULT 'pending',          -- pending|running|waiting_login|done|dead
  attempts    INTEGER NOT NULL DEFAULT 0,
  locked_at   REAL,
  next_run_at REAL NOT NULL DEFAULT 0,
  last_error  TEXT,
  stats       TEXT,                                     -- JSON {pages, jobs_new, jobs_seen, details_ok, details_failed}; live after each page
  created_at  REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS tasks_claim ON tasks(status, next_run_at);

CREATE TABLE IF NOT EXISTS jobs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  provider         TEXT NOT NULL,
  source_job_id    TEXT NOT NULL,
  task_id          INTEGER REFERENCES tasks(id),        -- first discovered by
  title            TEXT, company TEXT, city TEXT, salary_text TEXT,
  description      TEXT,
  source_url       TEXT, published_at TEXT, published_at_kind TEXT,
  payload          TEXT,                                -- raw list item JSON
  detail           TEXT,                                -- raw detail JSON, NULL if none/failed
  detail_error     TEXT,
  first_seen_at    REAL NOT NULL DEFAULT (strftime('%s','now')),
  last_seen_at     REAL NOT NULL DEFAULT (strftime('%s','now')),
  UNIQUE(provider, source_job_id)
);

-- Every job a task saw, new or already known (jobs.task_id only records the first discoverer).
CREATE TABLE IF NOT EXISTS task_jobs (
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  job_id  INTEGER NOT NULL REFERENCES jobs(id),
  PRIMARY KEY (task_id, job_id)
);

CREATE TABLE IF NOT EXISTS egress (
  id        TEXT PRIMARY KEY,                           -- 'direct' or http://user:pass@host:port
  provider  TEXT NOT NULL DEFAULT '*',                  -- '*' shared pool | dedicated provider name
  enabled   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS egress_health (
  egress_id     TEXT NOT NULL,
  provider      TEXT NOT NULL,
  blocked_until REAL NOT NULL DEFAULT 0,
  fail_streak   INTEGER NOT NULL DEFAULT 0,
  last_used     REAL NOT NULL DEFAULT 0,
  total_ok      INTEGER NOT NULL DEFAULT 0,
  total_fail    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (egress_id, provider)
);

-- User overrides of per-platform defaults (defaults live on the Provider class).
CREATE TABLE IF NOT EXISTS provider_settings (
  provider  TEXT PRIMARY KEY,
  interval  REAL,        -- NULL = use class default
  jitter    REAL,
  max_pages INTEGER,
  transport TEXT         -- NULL = the provider's first declared transport
);
