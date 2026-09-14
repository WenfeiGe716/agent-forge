-- Applied once, in the same transaction as the schema version update.
-- Existing stage-one snapshots/receipts decode with Run's compatible defaults.
ALTER TABLE runs ADD COLUMN version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0);
