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
CREATE TABLE IF NOT EXISTS charger_revision (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), gun_id TEXT NOT NULL,
  bays_json TEXT NOT NULL, charging_json TEXT NOT NULL,
  circuit_id TEXT NOT NULL, circuit_rating_kw REAL NOT NULL,
  valid_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charger_registry (
  gun_id TEXT PRIMARY KEY, bays_json TEXT NOT NULL, charging_json TEXT NOT NULL,
  circuit_id TEXT NOT NULL, circuit_rating_kw REAL NOT NULL,
  valid_from TEXT NOT NULL, source_event TEXT NOT NULL REFERENCES evidence(event_id)
);
CREATE TABLE IF NOT EXISTS charger_fault (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), gun_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('fault','recovery')),
  source TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charge_session (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id), session_id TEXT NOT NULL,
  bay_id TEXT NOT NULL, gun_id TEXT NOT NULL, charging_type TEXT NOT NULL,
  power_kw REAL NOT NULL CHECK(power_kw>0), started_at TEXT NOT NULL, ended_at TEXT,
  event_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charging_release (
  release_id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT NOT NULL,
  contract TEXT NOT NULL, charging_type TEXT NOT NULL, min_power_kw REAL NOT NULL,
  state TEXT NOT NULL, reason TEXT NOT NULL, published TEXT, evidence TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def rows(db: sqlite3.Connection, sql: str, args=()):
    return [dict(row) for row in db.execute(sql, args).fetchall()]
