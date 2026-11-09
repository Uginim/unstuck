CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  unit_cents INTEGER NOT NULL CHECK (unit_cents > 0),
  stock INTEGER NOT NULL CHECK (stock >= 0)
);
-- Only substitutions the seller approved can ever be offered.
CREATE TABLE IF NOT EXISTS substitutes (
  product_id TEXT NOT NULL REFERENCES products(id),
  substitute_id TEXT NOT NULL REFERENCES products(id),
  PRIMARY KEY (product_id, substitute_id)
);
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL CHECK (role IN ('customer','agent')), body TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
  kind TEXT NOT NULL CHECK (kind IN ('exact','substitute','partial')),
  lines TEXT NOT NULL, total_cents INTEGER NOT NULL CHECK (total_cents > 0),
  expires_at TEXT NOT NULL
);
-- One checkout per offer. Holds live here until paid, released, or expired.
CREATE TABLE IF NOT EXISTS checkouts (
  id TEXT PRIMARY KEY, offer_id TEXT NOT NULL UNIQUE REFERENCES offers(id),
  state TEXT NOT NULL CHECK (state IN ('held','awaiting_payment','capturing','paid','unknown','failed','expired')),
  total_cents INTEGER NOT NULL, hold_expires_at TEXT NOT NULL,
  create_request_id TEXT NOT NULL, capture_request_id TEXT NOT NULL,
  paypal_order_id TEXT, approve_url TEXT, capture_id TEXT, error_code TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, checkout_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_micro_usd INTEGER NOT NULL, at TEXT NOT NULL
);
