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

-- Offline maintenance diagnosis.  Topology is stored as immutable snapshots so a
-- later restatement cannot rewrite the graph an earlier diagnosis relied on.
CREATE TABLE IF NOT EXISTS maintenance_topology_snapshot (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),
  event_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance_topology_node (
  event_id TEXT NOT NULL REFERENCES maintenance_topology_snapshot(event_id),
  node_id TEXT NOT NULL, kind TEXT NOT NULL, spare_sku TEXT NOT NULL,
  PRIMARY KEY(event_id,node_id)
);
CREATE TABLE IF NOT EXISTS maintenance_topology_edge (
  event_id TEXT NOT NULL REFERENCES maintenance_topology_snapshot(event_id),
  child_id TEXT NOT NULL, parent_id TEXT NOT NULL,
  PRIMARY KEY(event_id,child_id,parent_id)
);
CREATE TABLE IF NOT EXISTS maintenance_signal (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),
  device_id TEXT NOT NULL, signal_kind TEXT NOT NULL, result TEXT NOT NULL,
  event_time TEXT NOT NULL, captured_at TEXT NOT NULL, source TEXT NOT NULL,
  reliability TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fault_frequency_revision (
  event_id TEXT PRIMARY KEY REFERENCES evidence(event_id),
  device_id TEXT NOT NULL, frequency REAL NOT NULL, event_time TEXT NOT NULL,
  source TEXT, captured_at TEXT
);
CREATE TABLE IF NOT EXISTS diagnosis_run (
  diagnosis_id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision INTEGER NOT NULL, as_of TEXT NOT NULL, timeout_minutes INTEGER NOT NULL,
  max_faults INTEGER NOT NULL, vehicle_capacity INTEGER NOT NULL,
  risk_threshold REAL NOT NULL, topology_event TEXT,
  status TEXT NOT NULL, lost_json TEXT NOT NULL, healthy_json TEXT NOT NULL,
  failed_json TEXT NOT NULL, fingerprint TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS diagnosis_explanation (
  diagnosis_id INTEGER NOT NULL REFERENCES diagnosis_run(diagnosis_id),
  explanation_no INTEGER NOT NULL, nodes_json TEXT NOT NULL,
  basis_json TEXT NOT NULL DEFAULT '[]',
  prior_weight REAL, probability REAL, confirmed INTEGER NOT NULL,
  PRIMARY KEY(diagnosis_id,explanation_no)
);
CREATE TABLE IF NOT EXISTS diagnosis_bay_cause (
  diagnosis_id INTEGER NOT NULL REFERENCES diagnosis_run(diagnosis_id),
  bay_id TEXT NOT NULL, device_id TEXT NOT NULL, reason TEXT NOT NULL,
  nodes_json TEXT NOT NULL,
  PRIMARY KEY(diagnosis_id,bay_id)
);
CREATE TABLE IF NOT EXISTS maintenance_dispatch (
  dispatch_id INTEGER PRIMARY KEY AUTOINCREMENT,
  diagnosis_id INTEGER NOT NULL REFERENCES diagnosis_run(diagnosis_id),
  contract TEXT NOT NULL, decision TEXT NOT NULL, parts_json TEXT NOT NULL,
  coverage_probability REAL, risk_threshold REAL, uncovered_json TEXT NOT NULL,
  capacity INTEGER NOT NULL, reason TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS evidence_conflict (
  conflict_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
  existing_sha256 TEXT NOT NULL, incoming_sha256 TEXT NOT NULL,
  incoming_body TEXT NOT NULL, detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
