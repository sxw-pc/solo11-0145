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
CREATE TABLE IF NOT EXISTS schema_migration (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Migrations are additive and never rewrite historical evidence.  Version 1
# introduces the accessible-occupancy ledger; availability, billing and
# capacity tables above are untouched.
MIGRATIONS = [(1, """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS accessible_permit_revision (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),
  permit_id TEXT NOT NULL, credential_id TEXT NOT NULL, status TEXT NOT NULL,
  bays_json TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
  trusted INTEGER NOT NULL CHECK(trusted IN (0,1)), source TEXT NOT NULL,
  event_time TEXT NOT NULL, duplicate INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS accessible_permit_by_permit
  ON accessible_permit_revision(permit_id, event_time);
CREATE TABLE IF NOT EXISTS accessible_verification (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),
  permit_id TEXT, credential_id TEXT, stay_id TEXT NOT NULL,
  verified_at TEXT NOT NULL, trusted INTEGER NOT NULL CHECK(trusted IN (0,1)),
  source TEXT NOT NULL, duplicate INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bay_scan (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),
  bay_id TEXT NOT NULL, permit_id TEXT, credential_id TEXT, stay_id TEXT,
  scanned_at TEXT NOT NULL, trusted INTEGER NOT NULL CHECK(trusted IN (0,1)),
  source TEXT NOT NULL, duplicate INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS manual_observation (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),
  bay_id TEXT NOT NULL, permit_id TEXT, credential_id TEXT, stay_id TEXT,
  observed_from TEXT NOT NULL, observed_to TEXT NOT NULL,
  trusted INTEGER NOT NULL CHECK(trusted IN (0,1)), source TEXT NOT NULL,
  duplicate INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS accessible_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT NOT NULL,
  scope_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS accessible_incident (
  incident_id INTEGER PRIMARY KEY AUTOINCREMENT, audit_id INTEGER NOT NULL
    REFERENCES accessible_audit(audit_id), as_of TEXT NOT NULL,
  bay_id TEXT NOT NULL, bay_from TEXT NOT NULL, bay_to TEXT NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS accessible_assignment_version (
  version_id INTEGER PRIMARY KEY AUTOINCREMENT, audit_id INTEGER NOT NULL
    REFERENCES accessible_audit(audit_id), as_of TEXT NOT NULL,
  bay_id TEXT NOT NULL, stay_id TEXT NOT NULL, confidence TEXT NOT NULL,
  reason TEXT NOT NULL, permutation_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS accessible_dispatch (
  dispatch_id INTEGER PRIMARY KEY AUTOINCREMENT, audit_id INTEGER NOT NULL
    REFERENCES accessible_audit(audit_id), contract TEXT NOT NULL,
  bay_id TEXT, stay_id TEXT, window_from TEXT, window_to TEXT,
  decision TEXT NOT NULL, reason TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""")]

TABLES = {
    'accessible_permit': 'accessible_permit_revision',
    'accessible_verification': 'accessible_verification',
    'bay_scan': 'bay_scan',
    'manual_observation': 'manual_observation',
}


def migrate(db: sqlite3.Connection) -> None:
    """Apply pending SQL migrations transactionally; never drop old tables."""
    for version, script in MIGRATIONS:
        if db.execute('SELECT 1 FROM schema_migration WHERE version=?', (version,)).fetchone():
            continue
        with db:
            db.executescript(script)
            db.execute('INSERT INTO schema_migration(version) VALUES (?)', (version,))


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    migrate(db)
    return db


def rows(db: sqlite3.Connection, sql: str, args=()):
    return [dict(row) for row in db.execute(sql, args).fetchall()]
