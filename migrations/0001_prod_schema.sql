-- TYPE
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'receipt_source') THEN
    CREATE TYPE receipt_source AS ENUM ('telegram','email','manual');
  END IF;
END $$;

-- TABLE receipts
CREATE TABLE IF NOT EXISTS receipts (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source receipt_source NOT NULL,
  original_file TEXT,
  raw_text TEXT,
  json_payload JSONB,
  currency VARCHAR(10),
  total_amount NUMERIC(14,2),
  tip_amount NUMERIC(14,2) DEFAULT 0,
  merchant_name TEXT,
  merchant_inn TEXT,
  purchased_at TIMESTAMPTZ,
  status VARCHAR(30) DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TABLE receipt_items
CREATE TABLE IF NOT EXISTS receipt_items (
  id BIGSERIAL PRIMARY KEY,
  receipt_id BIGINT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  line_no INT,
  item_name TEXT,
  qty NUMERIC(12,3),
  unit_price NUMERIC(14,2),
  amount NUMERIC(14,2),
  category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL
);

-- TABLE email_inbox
CREATE TABLE IF NOT EXISTS email_inbox (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  message_id TEXT UNIQUE,
  from_email TEXT,
  subject TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ
);

-- ALTER transactions (не ломаем существующие данные)
ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS receipt_id BIGINT REFERENCES receipts(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS source VARCHAR(16);

-- INDEXES
CREATE INDEX IF NOT EXISTS idx_receipts_user_created ON receipts(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt ON receipt_items(receipt_id);
CREATE INDEX IF NOT EXISTS idx_transactions_receipt ON transactions(receipt_id);
