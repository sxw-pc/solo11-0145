"""SQLite schema and transactional evidence access."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence (
  event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, object_id TEXT NOT NULL,
  event_time TEXT NOT NULL, captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  sha256 TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sensor_registry (
  device_id TEXT PRIMARY KEY, bay_id TEXT NOT NULL, calibrated_until TEXT NOT NULL,
  valid_from TEXT NOT NULL, source_event TEXT NOT NULL REFERENCES evidence(event_id)
);
CREATE TABLE IF NOT EXISTS sensor_revision (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), device_id TEXT NOT NULL,
  bay_id TEXT NOT NULL, calibrated_until TEXT NOT NULL, valid_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occupancy_event (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), device_id TEXT NOT NULL,
  bay_id TEXT NOT NULL, observed_at TEXT NOT NULL, sequence_no INTEGER NOT NULL,
  occupied INTEGER NOT NULL CHECK(occupied IN (0,1))
);
CREATE TABLE IF NOT EXISTS bay_projection (
  bay_id TEXT PRIMARY KEY, state TEXT NOT NULL, selected_event TEXT,
  observed_at TEXT, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_event (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), stay_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('in','out')),
  occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_baseline (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),
  occurred_at TEXT NOT NULL, count INTEGER NOT NULL CHECK(count>=0)
);
CREATE TABLE IF NOT EXISTS vehicle_stay (
  stay_id TEXT PRIMARY KEY, entered_at TEXT, exited_at TEXT, state TEXT NOT NULL,
  reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_balance (
  id INTEGER PRIMARY KEY CHECK(id=1), active_count INTEGER NOT NULL,
  orphan_count INTEGER NOT NULL, duplicate_count INTEGER NOT NULL,
  computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capacity_order (
  order_id TEXT PRIMARY KEY, purpose TEXT NOT NULL, priority INTEGER NOT NULL,
  status TEXT NOT NULL, source_event TEXT NOT NULL REFERENCES evidence(event_id)
);
CREATE TABLE IF NOT EXISTS capacity_window (
  order_id TEXT NOT NULL REFERENCES capacity_order(order_id), bay_id TEXT NOT NULL,
  starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
  PRIMARY KEY(order_id,bay_id,starts_at)
);
CREATE TABLE IF NOT EXISTS capacity_decision (
  order_id TEXT PRIMARY KEY REFERENCES capacity_order(order_id),
  decision TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capacity_revision (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), order_id TEXT NOT NULL,
  purpose TEXT NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL,
  bays_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
  event_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS availability_release (
  release_id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT NOT NULL,
  available INTEGER, state TEXT NOT NULL, evidence TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS capacity_incident (
  incident_id INTEGER PRIMARY KEY AUTOINCREMENT,as_of TEXT NOT NULL,
  active_lower_bound INTEGER NOT NULL,operable_capacity INTEGER NOT NULL,
  decision TEXT NOT NULL,reason TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Append-only offline parking fee adjudication ledger. Gate events and bills
# are never rewritten: new calibration/tariff/contract evidence adds revisions
# and later decisions supersede earlier ones through version links.
BILLING_SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_calibration (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), gate_id TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
  offset_seconds_min INTEGER NOT NULL, offset_seconds_max INTEGER NOT NULL,
  published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tariff_revision (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), tariff_id TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
  grace_seconds INTEGER NOT NULL CHECK(grace_seconds>=0),
  daily_cap_minor INTEGER NOT NULL CHECK(daily_cap_minor>=0),
  tiers_json TEXT NOT NULL, published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_revision (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), contract_id TEXT NOT NULL,
  contract_type TEXT NOT NULL CHECK(contract_type IN ('exact','guaranteed')),
  valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_decision (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  stay_id TEXT NOT NULL, contract_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL, status TEXT NOT NULL,
  feasible_amounts_json TEXT NOT NULL, feasible_durations_json TEXT NOT NULL,
  billed_minor INTEGER, guaranteed_minor INTEGER, reason TEXT NOT NULL,
  entry_event TEXT, exit_event TEXT,
  calibration_refs_json TEXT NOT NULL, tariff_refs_json TEXT NOT NULL,
  contract_ref TEXT, evidence_fingerprint TEXT NOT NULL,
  supersedes_decision INTEGER REFERENCES billing_decision(decision_id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(stay_id,contract_id,decision_seq)
);
CREATE TABLE IF NOT EXISTS bill (
  bill_id TEXT PRIMARY KEY, stay_id TEXT NOT NULL, contract_id TEXT NOT NULL,
  bill_type TEXT NOT NULL CHECK(bill_type IN ('formal','prepayment','correction')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor>=0),
  decision_id INTEGER NOT NULL REFERENCES billing_decision(decision_id),
  supersedes_bill TEXT REFERENCES bill(bill_id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS stay_ledger_entry (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  stay_id TEXT NOT NULL, contract_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('prepayment','formal','supplement','refund')),
  amount_minor INTEGER NOT NULL,
  bill_id TEXT REFERENCES bill(bill_id),
  decision_id INTEGER NOT NULL REFERENCES billing_decision(decision_id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ledger_stay ON stay_ledger_entry(stay_id,contract_id);
CREATE INDEX IF NOT EXISTS idx_bill_stay ON bill(stay_id,contract_id);
"""

# Columns added to the original gate ledger without rewriting any row.
MIGRATIONS = [
    "ALTER TABLE gate_event ADD COLUMN gate_id TEXT",
    "ALTER TABLE vehicle_stay ADD COLUMN entry_gate TEXT",
    "ALTER TABLE vehicle_stay ADD COLUMN exit_gate TEXT",
]


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    user_version = db.execute('PRAGMA user_version').fetchone()[0]
    for index, statement in enumerate(MIGRATIONS):
        if user_version <= index:
            try:
                db.execute(statement)
            except sqlite3.OperationalError as exc:  # column already exists
                if 'duplicate column name' not in str(exc):
                    raise
            db.execute(f'PRAGMA user_version={index + 1}')
    db.executescript(BILLING_SCHEMA)
    return db


def rows(db: sqlite3.Connection, sql: str, args=()):
    return [dict(row) for row in db.execute(sql, args).fetchall()]
