-- Money is integer cents. Timestamps are UTC ISO-8601 strings. Only Claude edits this file.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS policies (
  version INTEGER PRIMARY KEY,
  full_refund_cutoff_hours INTEGER NOT NULL CHECK (full_refund_cutoff_hours >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  capture_id TEXT NOT NULL UNIQUE,
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  currency TEXT NOT NULL,
  session_name TEXT NOT NULL,
  session_starts_at TEXT NOT NULL
);

-- Conditions are preserved at start, so later edits cannot rewrite past explanations.
CREATE TABLE IF NOT EXISTS delegations (
  id TEXT PRIMARY KEY,
  per_refund_limit_cents INTEGER NOT NULL CHECK (per_refund_limit_cents >= 0),
  total_limit_cents INTEGER NOT NULL CHECK (total_limit_cents >= 0),
  policy_version INTEGER NOT NULL REFERENCES policies(version),
  started_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  stopped_at TEXT
);

-- Editable until confirmed. Model output is stored here, never used to pick the order or amount.
CREATE TABLE IF NOT EXISTS request_drafts (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(id),
  message TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('refund','question','other')),
  ambiguous INTEGER NOT NULL CHECK (ambiguous IN (0,1)),
  summary TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmed_requests (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES request_drafts(id),
  draft_version INTEGER NOT NULL,
  order_id TEXT NOT NULL REFERENCES orders(id),
  kind TEXT NOT NULL,
  ambiguous INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  policy_version INTEGER NOT NULL REFERENCES policies(version),
  delegation_id TEXT REFERENCES delegations(id),
  status TEXT NOT NULL CHECK (status IN
    ('needs_review','rejected_policy','pending_approval','approved','linked')),
  reason TEXT NOT NULL,
  job_id TEXT REFERENCES refund_jobs(id),
  UNIQUE (draft_id, draft_version)
);

-- One row per capture: this constraint is the duplicate-refund barrier.
CREATE TABLE IF NOT EXISTS refund_jobs (
  id TEXT PRIMARY KEY,
  capture_id TEXT NOT NULL UNIQUE,
  request_id TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('agent','seller')),
  delegation_id TEXT REFERENCES delegations(id),
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  state TEXT NOT NULL CHECK (state IN
    ('reserved','executing','paypal_pending','completed','failed','unknown')),
  refund_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (actor = 'seller' OR delegation_id IS NOT NULL)
);

-- History is never deleted. A lost response retries the same key and body;
-- only a definite failure may open a new attempt with a new key.
CREATE TABLE IF NOT EXISTS refund_attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES refund_jobs(id),
  attempt_no INTEGER NOT NULL,
  paypal_request_id TEXT NOT NULL UNIQUE,
  amount_cents INTEGER NOT NULL,
  result TEXT CHECK (result IN ('completed','pending','failed_definite','unknown')),
  refund_id TEXT,
  error_code TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE (job_id, attempt_no)
);

-- auto_execution_paused = '1' after a restore, until reconciliation finishes.
CREATE TABLE IF NOT EXISTS system_flags (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
