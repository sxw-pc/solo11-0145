"""Accessible-bay occupancy audit over independent evidence ledgers.

Three questions stay deliberately separate:

* which trustworthy occupied periods exist -- only calibrated, fresh
  geomagnetic readings create them; a gate count or a scan never does;
* which anonymous gate stay could occupy each period -- a one-to-one matching
  constrained by gate in/out intervals, credential verifications, bay scans
  and manual observations; a stay is never in two bays at the same time;
* whether *every* feasible complete attribution proves that no valid
  accessible permit covered the bay.

A gate baseline or mere contemporaneous presence never invents a stay identity.
Late evidence is appended, never edited; every audit is a new stored version
and the two approved contracts (patrol notice, targeted notice) are derived
from the audit without overstating what was proven.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import gate, sensor
from .sources import record
from .store import TABLES, rows

KINDS = {
    "permit": "accessible_permit",
    "accessible_permit": "accessible_permit",
    "verification": "accessible_verification",
    "credential_verification": "accessible_verification",
    "bay_scan": "bay_scan",
    "scan": "bay_scan",
    "manual_observation": "manual_observation",
    "observation": "manual_observation",
}
PERMIT_STATUSES = {"approved", "revoked"}
ASSIGNMENT_LIMIT = 5000
TIME_KEYS = ("event_time", "verified_at", "scanned_at", "observed_to", "observed_from")


class AccessibleEvidenceError(ValueError):
    pass


def _dt(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AccessibleEvidenceError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise AccessibleEvidenceError(f"timestamp {value!r} needs a timezone")
    return parsed


def _overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return _dt(a_start) < _dt(b_end) and _dt(b_start) < _dt(a_end)


def _contains(start: str, end: str, moment: str) -> bool:
    return _dt(start) <= _dt(moment) < _dt(end)


def _covered(outer_from: str, outer_to: str, inner_from: str, inner_to: str) -> bool:
    return _dt(outer_from) <= _dt(inner_from) and _dt(inner_to) <= _dt(outer_to)


def _event_time(event: dict) -> str:
    for key in TIME_KEYS:
        if event.get(key):
            return str(event[key])
    raise AccessibleEvidenceError("accessible evidence needs an event time")


def _trusted(event: dict) -> int:
    if event.get("trusted") is False:
        return 0
    source = str(event.get("source") or "")
    return 1 if source and not source.startswith("untrusted") else 0


def _kind(event: dict) -> str:
    raw = event.get("kind") or event.get("op") or ""
    if raw not in KINDS:
        raise AccessibleEvidenceError(f"unknown accessible evidence kind {raw!r}")
    return KINDS[raw]


def _object_id(event: dict, kind: str) -> str:
    if kind == "accessible_permit":
        return str(event["permit_id"])
    if kind == "accessible_verification":
        return str(event["stay_id"])
    return str(event["bay_id"])


def _shape(event: dict, kind: str) -> tuple:
    if kind == "accessible_permit":
        return (kind, str(event["permit_id"]), tuple(sorted(set(event["bays"]))),
                str(event["valid_from"]), str(event["valid_to"]),
                str(event["status"]), str(event.get("credential_id") or event["permit_id"]),
                str(event.get("event_time") or event["valid_from"]))
    if kind == "accessible_verification":
        return (kind, event.get("permit_id"), event.get("credential_id"),
                str(event["stay_id"]), str(event["verified_at"]))
    if kind == "bay_scan":
        return (kind, str(event["bay_id"]), event.get("permit_id"),
                event.get("credential_id"), event.get("stay_id"), str(event["scanned_at"]))
    return (kind, str(event["bay_id"]), event.get("permit_id"),
            event.get("credential_id"), event.get("stay_id"),
            str(event["observed_from"]), str(event["observed_to"]))


def _stored_shape(row: dict, kind: str) -> tuple:
    if kind == "accessible_permit":
        return (kind, row["permit_id"], tuple(json.loads(row["bays_json"])),
                row["valid_from"], row["valid_to"], row["status"], row["credential_id"],
                row["event_time"])
    if kind == "accessible_verification":
        return (kind, row["permit_id"], row["credential_id"], row["stay_id"], row["verified_at"])
    if kind == "bay_scan":
        return (kind, row["bay_id"], row["permit_id"], row["credential_id"], row["stay_id"],
                row["scanned_at"])
    return (kind, row["bay_id"], row["permit_id"], row["credential_id"], row["stay_id"],
            row["observed_from"], row["observed_to"])


def _validate(event: dict, kind: str) -> None:
    if kind == "accessible_permit":
        if not event.get("permit_id"):
            raise AccessibleEvidenceError("permit needs permit_id")
        if event.get("status") not in PERMIT_STATUSES:
            raise AccessibleEvidenceError("permit status must be approved or revoked")
        if not isinstance(event.get("bays"), list) or not event["bays"]:
            raise AccessibleEvidenceError("permit needs bays")
        if str(event["valid_from"]) >= str(event["valid_to"]):
            raise AccessibleEvidenceError("empty permit window")
    elif kind == "accessible_verification":
        if not event.get("stay_id") or not event.get("verified_at"):
            raise AccessibleEvidenceError("verification needs stay_id and verified_at")
        if not event.get("permit_id") and not event.get("credential_id"):
            raise AccessibleEvidenceError("verification needs a credential")
    elif kind == "bay_scan":
        if not event.get("bay_id") or not event.get("scanned_at"):
            raise AccessibleEvidenceError("bay scan needs bay_id and scanned_at")
        if not event.get("permit_id") and not event.get("credential_id"):
            raise AccessibleEvidenceError("bay scan needs a credential")
    else:
        if not event.get("bay_id") or not event.get("observed_from") or not event.get("observed_to"):
            raise AccessibleEvidenceError("observation needs bay and window")
        if str(event["observed_from"]) >= str(event["observed_to"]):
            raise AccessibleEvidenceError("empty observation window")
        if not event.get("permit_id") and not event.get("credential_id"):
            raise AccessibleEvidenceError("observation needs a credential")


def _insert(db, event: dict, kind: str, *, duplicate: bool = False) -> None:
    trusted = int(_trusted(event))
    source = str(event.get("source") or "")
    flag = int(duplicate)
    if kind == "accessible_permit":
        db.execute(
            "INSERT INTO accessible_permit_revision(event_id,permit_id,credential_id,status,"
            "bays_json,valid_from,valid_to,trusted,source,event_time,duplicate) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(event["event_id"]), str(event["permit_id"]),
             str(event.get("credential_id") or event["permit_id"]), str(event["status"]),
             json.dumps(sorted(set(event["bays"]))), str(event["valid_from"]),
             str(event["valid_to"]), trusted, source,
             str(event.get("event_time") or event["valid_from"]), flag))
    elif kind == "accessible_verification":
        db.execute(
            "INSERT INTO accessible_verification(event_id,permit_id,credential_id,stay_id,"
            "verified_at,trusted,source,duplicate) VALUES (?,?,?,?,?,?,?,?)",
            (str(event["event_id"]), event.get("permit_id"), event.get("credential_id"),
             str(event["stay_id"]), str(event["verified_at"]), trusted, source, flag))
    elif kind == "bay_scan":
        db.execute(
            "INSERT INTO bay_scan(event_id,bay_id,permit_id,credential_id,stay_id,scanned_at,"
            "trusted,source,duplicate) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(event["event_id"]), str(event["bay_id"]), event.get("permit_id"),
             event.get("credential_id"), event.get("stay_id"), str(event["scanned_at"]),
             trusted, source, flag))
    else:
        db.execute(
            "INSERT INTO manual_observation(event_id,bay_id,permit_id,credential_id,stay_id,"
            "observed_from,observed_to,trusted,source,duplicate) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(event["event_id"]), str(event["bay_id"]), event.get("permit_id"),
             event.get("credential_id"), event.get("stay_id"),
             str(event["observed_from"]), str(event["observed_to"]), trusted, source, flag))


def ingest(db, events: list[dict]) -> dict:
    """Append accessible evidence; identical bodies are idempotent.

    A semantically repeated source (same kind and facts under a new event_id)
    is stored once with ``duplicate=1`` so the next audit reports the overlap
    instead of silently counting it twice.  A recycled event_id with different
    content raises ``EvidenceConflict`` and rolls back the whole batch.
    """
    made = 0
    duplicates = 0
    with db:
        for event in events:
            kind = _kind(event)
            _validate(event, kind)
            table = TABLES[kind]
            shape = _shape(event, kind)
            stored = dict(event, kind=kind, event_time=event.get("event_time") or _event_time(event))
            prior = rows(db, f"SELECT * FROM {table}")
            if any(_stored_shape(row, kind) == shape for row in prior):
                if not record(db, stored, table, _object_id(event, kind)):
                    continue
                _insert(db, event, kind, duplicate=True)
                duplicates += 1
                continue
            if not record(db, stored, table, _object_id(event, kind)):
                continue
            _insert(db, event, kind)
            made += 1
    return {"imported": made, "duplicates": duplicates}


# --------------------------------------------------------------------------
# Trustworthy occupied periods


def occupancy_periods(db, as_of: str, timeout_minutes: int = 30) -> dict:
    """Build occupied periods only from trustworthy geomagnetic readings.

    Every reading is re-evaluated with the calibration, heartbeat and
    late-arrival rules of :mod:`parking.sensor`.  A trusted occupied reading
    opens a period; the next trusted free reading closes it; otherwise it stays
    open at ``as_of``.  A reading the sensor itself cannot trust contributes no
    occupancy and is reported with its reason.
    """
    events = rows(db, "SELECT * FROM occupancy_event WHERE observed_at<=? "
                      "ORDER BY bay_id,observed_at,sequence_no,event_id", (as_of,))
    by_bay: dict[str, list[dict]] = {}
    for event in events:
        by_bay.setdefault(event["bay_id"], []).append(event)
    periods: list[dict] = []
    unknown: list[dict] = []
    for bay_id, items in sorted(by_bay.items()):
        open_period: dict | None = None
        for event in items:
            state = sensor.bay_state(db, bay_id, event["observed_at"], timeout_minutes)
            if state["event_id"] != event["event_id"]:
                continue  # shadowed by a later reading at the same moment
            if state["state"] == "unknown":
                unknown.append({"bay_id": bay_id, "at": event["observed_at"],
                                "event_id": event["event_id"], "reason": state["reason"]})
                continue
            if event["occupied"]:
                if open_period is None:
                    open_period = {"bay_id": bay_id, "from": event["observed_at"],
                                   "opening_event": event["event_id"], "readings": 1}
                else:
                    open_period["readings"] += 1
            elif open_period is not None:
                open_period["to"] = event["observed_at"]
                open_period["closing_event"] = event["event_id"]
                periods.append(open_period)
                open_period = None
        if open_period is not None:
            periods.append(open_period)
    latest_by_bay: dict[str, str] = {}
    for event in events:
        latest_by_bay[event["bay_id"]] = event["observed_at"]
    out = []
    for period in periods:
        if "to" in period:
            end, closed = period["to"], True
            state, reason = "occupied", "closed_by_free_reading"
        else:
            end, closed = as_of, False
            last = latest_by_bay[period["bay_id"]]
            current = sensor.bay_state(db, period["bay_id"], as_of, timeout_minutes)
            if current["state"] == "unknown":
                # The latest view of this bay is untrusted or stale; occupancy
                # cannot be asserted for the open tail of the period.
                state, reason = "unknown", current["reason"]
            else:
                state, reason = "occupied", "open_at_as_of"
            if _dt(as_of) - _dt(last) > timedelta(minutes=timeout_minutes):
                state, reason = "unknown", "latest_reading_stale"
        out.append({"bay_id": period["bay_id"], "from": period["from"], "to": end,
                    "closed": closed, "state": state, "reason": reason,
                    "opening_event": period["opening_event"],
                    "closing_event": period.get("closing_event")})
    return {"periods": out, "unknown_readings": unknown}


def gate_stays(db, as_of: str) -> list[dict]:
    """Anonymous stay intervals from gate receipts only.

    A baseline has no stay identity and therefore is never emitted here.
    Orphan receipts and stays with duplicated in/out receipts do not produce a
    usable interval: their true entry or exit is uncertain, so they may not be
    used to attribute a bay.
    """
    snap = gate.snapshot(db, as_of)
    stays = [{"stay_id": item["stay_id"], "from": item["from"], "to": item["to"],
              "open": False}
             for item in snap["billable"]]
    excluded: dict[str, str] = {}
    events = rows(db, "SELECT * FROM gate_event WHERE occurred_at<=? "
                      "ORDER BY occurred_at,event_id", (as_of,))
    by_stay: dict[str, list[dict]] = {}
    for event in events:
        by_stay.setdefault(event["stay_id"], []).append(event)
    for stay_id, items in sorted(by_stay.items()):
        ins = [e for e in items if e["direction"] == "in"]
        outs = [e for e in items if e["direction"] == "out"]
        if len(ins) > 1 or len(outs) > 1:
            excluded[stay_id] = "duplicate_gate_receipt"
    stays = [s for s in stays if s["stay_id"] not in excluded]
    known = {s["stay_id"] for s in stays}
    for stay_id, items in sorted(by_stay.items()):
        if stay_id in known or stay_id in excluded:
            continue
        ins = [e for e in items if e["direction"] == "in"]
        outs = [e for e in items if e["direction"] == "out"]
        if not ins or (outs and outs[0]["occurred_at"] < ins[0]["occurred_at"]):
            continue
        if len(ins) > 1 or len(outs) > 1:
            continue
        if not outs:
            stays.append({"stay_id": stay_id, "from": ins[0]["occurred_at"], "to": as_of,
                          "open": True})
    # A scan may fall exactly at as_of; an open stay observed to that instant
    # covers it just like an open occupancy period.  Extend the upper edge by
    # one microsecond for containment tests without changing reported instants.
    return sorted(stays, key=lambda s: (s["from"], s["stay_id"]))


def excluded_stays(db, as_of: str) -> list[dict]:
    """Anonymous stays that exist but cannot be attributed to a bay."""
    events = rows(db, "SELECT * FROM gate_event WHERE occurred_at<=?", (as_of,))
    by_stay: dict[str, list[dict]] = {}
    for event in events:
        by_stay.setdefault(event["stay_id"], []).append(event)
    out = []
    for stay_id, items in sorted(by_stay.items()):
        ins = [e for e in items if e["direction"] == "in"]
        outs = [e for e in items if e["direction"] == "out"]
        if len(ins) > 1 or len(outs) > 1:
            out.append({"stay_id": stay_id, "reason": "duplicate_gate_receipt"})
        elif not ins or (outs and outs[0]["occurred_at"] < ins[0]["occurred_at"]):
            out.append({"stay_id": stay_id, "reason": "orphan_gate_receipt"})
    return out


# --------------------------------------------------------------------------
# Permit revisions


def _permit_state(db, permit_id: str, as_of: str) -> dict | None:
    """Latest revision as of ``as_of``; a late revocation wins.

    The newest revision is returned even when its source is untrusted, so an
    untrusted update cannot be hidden behind an older trusted one; callers
    then treat the state as unknown rather than authorized or absent.
    """
    rev = db.execute(
        "SELECT * FROM accessible_permit_revision WHERE permit_id=? AND event_time<=? "
        "AND duplicate=0 ORDER BY event_time DESC,event_id DESC LIMIT 1",
        (permit_id, as_of)).fetchone()
    if rev is None:
        return None
    row = dict(rev)
    row["bays"] = json.loads(row["bays_json"])
    return row


def permit_valid_for_bay(db, permit_id: str, bay_id: str, period: dict, as_of: str) -> dict:
    """Decide whether ``permit_id`` authorizes ``bay_id`` over ``period``.

    A revoked, expired, untrusted or out-of-window permit is not an
    authorization.  A permit that covers only part of the period, or whose
    current revision is unknown, cannot prove either authorization or its
    absence and is reported as unknown.
    """
    state = _permit_state(db, permit_id, as_of)
    base = {"permit_id": permit_id, "valid": False, "known": True, "revision": None}
    if state is None:
        return dict(base, known=False, reason="permit_unknown")
    base["revision"] = state["event_id"]
    if not state["trusted"]:
        return dict(base, known=False, reason="permit_untrusted_source")
    if state["status"] != "approved":
        return dict(base, reason="permit_revoked")
    if bay_id not in state["bays"]:
        return dict(base, reason="permit_does_not_cover_bay")
    if not _overlap(state["valid_from"], state["valid_to"], period["from"], period["to"]):
        reason = "permit_expired" if period["to"] > state["valid_to"] else "permit_not_yet_valid"
        return dict(base, reason=reason)
    if not _covered(state["valid_from"], state["valid_to"], period["from"], period["to"]):
        return dict(base, known=False, reason="permit_partial_coverage")
    return dict(base, valid=True, reason="permit_covers_period")


def _permit_ids_for(db, permit_id, credential_id, as_of: str) -> list[str]:
    """Permit IDs whose current credential matches the supplied identity."""
    found: set[str] = set()
    if permit_id:
        found.add(str(permit_id))
    if credential_id:
        for row in rows(db, "SELECT DISTINCT permit_id FROM accessible_permit_revision "
                            "WHERE event_time<=? AND duplicate=0", (as_of,)):
            state = _permit_state(db, row["permit_id"], as_of)
            if state and state["credential_id"] == credential_id:
                found.add(row["permit_id"])
    return sorted(found)


def _credential_for(db, permit_id: str, as_of: str) -> str | None:
    state = _permit_state(db, permit_id, as_of)
    return state["credential_id"] if state else None


def _credentials_of(db, permit_id, credential_id, as_of: str) -> list[str]:
    creds = set()
    if credential_id:
        creds.add(str(credential_id))
    for pid in _permit_ids_for(db, permit_id, credential_id, as_of):
        cred = _credential_for(db, pid, as_of)
        if cred:
            creds.add(cred)
    return sorted(creds)


# --------------------------------------------------------------------------
# Evidence model: possible stay <-> period relations


def _period_key(period: dict) -> str:
    return f"{period['bay_id']}|{period['from']}|{period['to']}"


def _stay_covers(stay: dict, moment: str) -> bool:
    """An open stay is observed through as_of; a closed stay ends at exit."""
    if _contains(stay["from"], stay["to"], moment):
        return True
    return bool(stay.get("open")) and _dt(stay["from"]) <= _dt(moment) <= _dt(stay["to"])


def _period_contains(period: dict, moment: str) -> bool:
    """An open period is observed up to ``as_of`` and includes that instant."""
    if not period.get("closed", True):
        return _dt(period["from"]) <= _dt(moment) <= _dt(period["to"])
    return _contains(period["from"], period["to"], moment)


class Model:
    def __init__(self, periods: list[dict], stays: list[dict]):
        self.periods = periods
        self.stays = stays
        self.allowed: dict[str, dict[str, bool]] = {}
        self.excluded: dict[str, dict[str, str]] = {}
        self.pins: dict[str, set[str]] = {_period_key(p): set() for p in periods}
        self.notes: list[dict] = []
        self.conflicts: list[dict] = []
        self.doubts: dict[str, list[dict]] = {_period_key(p): [] for p in periods}
        self.stay_credentials: dict[str, set[str]] = {s["stay_id"]: set() for s in stays}
        self.stay_permits: dict[str, set[str]] = {s["stay_id"]: set() for s in stays}
        # A credential use proves the linked stay was at that bay at that time;
        # only those moment/bay pairs may license an attribution.
        self.stay_presence: dict[str, list[dict]] = {s["stay_id"]: [] for s in stays}
        for period in periods:
            key = _period_key(period)
            self.allowed[key] = {}
            self.excluded[key] = {}
            for stay in stays:
                if _covered(stay["from"], stay["to"], period["from"], period["to"]) or (
                    stay.get("open") and _dt(stay["from"]) <= _dt(period["from"])
                    and _dt(period["to"]) <= _dt(stay["to"])):
                    self.allowed[key][stay["stay_id"]] = True
                else:
                    self.allowed[key][stay["stay_id"]] = False
                    self.excluded[key][stay["stay_id"]] = "gate_interval_does_not_cover_period"

    def doubt(self, key: str, reason: str, **detail) -> None:
        self.doubts[key].append(dict(detail, reason=reason))


def _collect_uses(db, as_of: str, model: Model) -> list[dict]:
    """Trusted credential-use records, normalized across evidence tables."""
    uses: list[dict] = []
    stay_by_id = {s["stay_id"]: s for s in model.stays}
    verifications = rows(db, "SELECT * FROM accessible_verification WHERE verified_at<=? "
                             "ORDER BY verified_at,event_id", (as_of,))
    for row in verifications:
        if row["duplicate"]:
            continue
        stay = stay_by_id.get(row["stay_id"])
        if stay is None:
            continue
        use = {"event_id": row["event_id"], "kind": "verification",
               "stay_id": row["stay_id"], "moment": row["verified_at"],
               "bay_id": None, "permit_id": row["permit_id"],
               "credential_id": row["credential_id"], "trusted": bool(row["trusted"])}
        if not row["trusted"]:
            use["reason"] = "permit_untrusted_source"
        elif not _stay_covers(stay, row["verified_at"]):
            use["reason"] = "verification_outside_stay"
        uses.append(use)
        if use.get("reason"):
            continue
        for cred in _credentials_of(db, row["permit_id"], row["credential_id"], as_of):
            model.stay_credentials[stay["stay_id"]].add(cred)
        for pid in _permit_ids_for(db, row["permit_id"], row["credential_id"], as_of):
            model.stay_permits[stay["stay_id"]].add(pid)

    for table, kind, moment_key, end_key in (
            ("bay_scan", "bay_scan", "scanned_at", None),
            ("manual_observation", "manual_observation", "observed_from", "observed_to")):
        for row in rows(db, f"SELECT * FROM {table} WHERE {end_key or moment_key}<=? "
                            f"ORDER BY {moment_key},event_id", (as_of,)):
            if row["duplicate"]:
                continue
            moment = row[moment_key]
            end = row[end_key] if end_key else moment
            use = {"event_id": row["event_id"], "kind": kind, "bay_id": row["bay_id"],
                   "moment": moment, "end": end, "stay_id": row["stay_id"],
                   "permit_id": row["permit_id"], "credential_id": row["credential_id"],
                   "trusted": bool(row["trusted"])}
            if not row["trusted"]:
                use["reason"] = "scan_untrusted_source" if kind == "bay_scan" else "observation_untrusted_source"
            uses.append(use)
    return uses


def _credential_links(uses: list[dict]) -> dict[str, set[str]]:
    """Credential -> stays from trusted verification records."""
    links: dict[str, set[str]] = {}
    for use in uses:
        if use["kind"] != "verification" or use.get("reason") or not use.get("stay_id"):
            continue
        for cred in _credentials_from_use(use):
            links.setdefault(cred, set()).add(use["stay_id"])
    return links


def _credentials_from_use(use: dict) -> list[str]:
    creds = set()
    if use.get("credential_id"):
        creds.add(str(use["credential_id"]))
    for cred in use.get("credentials", []):
        creds.add(str(cred))
    return sorted(creds)


def build_model(db, as_of: str, periods: list[dict], stays: list[dict]) -> Model:
    model = Model(periods, stays)
    uses = _collect_uses(db, as_of, model)
    # Resolve the credentials and permit IDs each trusted use refers to.
    for use in uses:
        if use.get("reason"):
            continue
        use["credentials"] = _credentials_of(db, use.get("permit_id"),
                                             use.get("credential_id"), as_of)
        use["permits"] = _permit_ids_for(db, use.get("permit_id"),
                                         use.get("credential_id"), as_of)
    links = _credential_links(uses)
    _shared_credential_conflicts(db, as_of, model, uses, links)
    for use in uses:
        if use.get("reason"):
            continue
        _apply_presence(db, as_of, model, links, use)
    _drop_shared_credentials(model, uses)
    _apply_pins(model)
    _record_duplicates(db, as_of, model)
    return model


def _drop_shared_credentials(model: Model, uses: list[dict]) -> None:
    """A credential used by two stays at once must not license either of them.

    The conflict is recorded by :func:`_shared_credential_conflicts`; here the
    shared permit is removed from each stay's evidence so no attribution can
    be judged authorized through a credential that two cars used simultaneously.
    """
    for conflict in model.conflicts:
        if conflict["reason"] != "credential_shared_by_stays":
            continue
        window_from = conflict.get("from")
        window_to = conflict.get("to")
        for stay_id in conflict["stays"]:
            for presence in model.stay_presence.get(stay_id, []):
                if window_from is None or window_to is None or (
                        presence["from"] <= window_to and window_from <= presence["to"]):
                    presence["conflicted"] = True
                    presence["conflict_reason"] = "credential_shared_by_stays"
            for use in uses:
                if use.get("stay_id") != stay_id:
                    continue
                for permit_id in list(use.get("permits", [])):
                    model.stay_permits[stay_id].discard(permit_id)
                    for presence in model.stay_presence.get(stay_id, []):
                        if permit_id in presence["permits"]:
                            presence["permits"].remove(permit_id)


def _shared_credential_conflicts(db, as_of: str, model: Model, uses: list[dict],
                                 links: dict[str, set[str]]) -> None:
    by_credential: dict[str, list[dict]] = {}
    for use in uses:
        if use.get("reason") or not use.get("stay_id"):
            continue
        for cred in _credentials_from_use(use):
            by_credential.setdefault(cred, []).append(use)
    stay_by_id = {s["stay_id"]: s for s in model.stays}
    for credential, items in sorted(by_credential.items()):
        permits = sorted({p for u in items for p in u.get("permits", [])})
        licensed_bays: set[str] | None = set()
        for permit_id in permits:
            state = _permit_state(db, permit_id, as_of)
            if state is None:
                licensed_bays = None
                break
            licensed_bays |= set(state["bays"])
        stay_ids = sorted({u["stay_id"] for u in items})
        for left_index, left in enumerate(stay_ids):
            for right in stay_ids[left_index + 1:]:
                a, b = stay_by_id[left], stay_by_id[right]
                # A credential belongs to one car: if both stays are in the lot
                # over the same interval and both present it, the evidence is
                # contradictory even though the two presentations differ in
                # time.  Mere sequential use after one car left is not.
                if not _stay_overlap(a, b):
                    continue
                start = max(a["from"], b["from"])
                end = min(a["to"], b["to"])
                model.conflicts.append({
                    "reason": "credential_shared_by_stays", "credential_id": credential,
                    "stays": [left, right], "from": start, "to": end})
                for period in model.periods:
                    # The doubt only touches bays the shared credential could
                    # license; elsewhere the overlap proves nothing about it.
                    if licensed_bays is not None and period["bay_id"] not in licensed_bays:
                        continue
                    if _overlap(period["from"], period["to"], start, end):
                        model.doubt(_period_key(period), "credential_shared_by_stays",
                                    credential_id=credential, stays=[left, right])


def _stay_overlap(a: dict, b: dict) -> bool:
    if a["to"] <= b["from"] or b["to"] <= a["from"]:
        return False
    return _overlap(a["from"], a["to"], b["from"], b["to"]) or (
        bool(a.get("open")) or bool(b.get("open"))) and a["from"] <= b["to"] and b["from"] <= a["to"]


def _apply_presence(db, as_of: str, model: Model, links: dict[str, set[str]],
                    use: dict) -> None:
    bay_id, moment = use["bay_id"], use["moment"]
    end = use.get("end") or moment
    if use["kind"] == "verification":
        # A verification names no bay, so it never establishes a period
        # identity or pins a stay; it only links the stay to its permit.
        stay = next((s for s in model.stays if s["stay_id"] == use.get("stay_id")), None)
        if stay is None or not _stay_covers(stay, moment):
            model.conflicts.append({"reason": "verification_outside_stay",
                                    "event_id": use["event_id"],
                                    "stay_id": use.get("stay_id")})
            return
        model.stay_presence[stay["stay_id"]].append({
            "event_id": use["event_id"], "kind": use["kind"], "bay_id": None,
            "from": moment, "to": moment,
            "permits": list(use.get("permits", [])),
            "credentials": _credentials_from_use(use)})
        for cred in _credentials_from_use(use):
            model.stay_credentials[stay["stay_id"]].add(cred)
        for permit_id in use.get("permits", []):
            model.stay_permits[stay["stay_id"]].add(permit_id)
        return
    hits = [p for p in model.periods if p["bay_id"] == bay_id
            and (_period_contains(p, moment)
                 or (end != moment and _overlap(p["from"], p["to"], moment, end)))]
    if not hits:
        model.notes.append({"event_id": use["event_id"], "kind": use["kind"],
                            "reason": "scan_outside_occupancy" if use["kind"] == "bay_scan"
                            else "observation_outside_occupancy"})
        # A credential presented outside every occupied period cannot license
        # the occupancy, but it also cannot prove the absence of a permit.
        for period in model.periods:
            if period["bay_id"] == bay_id:
                model.doubt(_period_key(period), "credential_use_outside_occupancy",
                            event_id=use["event_id"])
        return
    stay_ids: set[str] = set()
    if use.get("stay_id"):
        stay_ids.add(use["stay_id"])
    else:
        for cred in _credentials_from_use(use):
            stay_ids |= links.get(cred, set())
    stay_ids &= {s["stay_id"] for s in model.stays}
    # Only a stay that could be on site at the moment can have used the
    # credential then.  A credential reused sequentially by two different cars
    # (one already left before the other arrived) is not a simultaneous share.
    stay_by_id = {s["stay_id"]: s for s in model.stays}
    stay_ids = {sid for sid in stay_ids if _stay_covers(stay_by_id[sid], moment)}
    if not stay_ids:
        model.notes.append({"event_id": use["event_id"], "kind": use["kind"],
                            "reason": "credential_unlinked_to_stay"})
        for period in hits:
            for permit_id in use.get("permits", []):
                check = permit_valid_for_bay(db, permit_id, period["bay_id"], period, as_of)
                if check["valid"]:
                    model.doubt(_period_key(period), "unlinked_valid_credential_present",
                                event_id=use["event_id"], permit_id=permit_id)
        return
    if len(stay_ids) > 1:
        model.conflicts.append({"reason": "credential_shared_by_stays",
                                "event_id": use["event_id"], "stays": sorted(stay_ids)})
        for period in hits:
            model.doubt(_period_key(period), "credential_shared_by_stays",
                        event_id=use["event_id"], stays=sorted(stay_ids))
        return
    stay_id = next(iter(stay_ids))
    stay = next(s for s in model.stays if s["stay_id"] == stay_id)
    if not _stay_covers(stay, moment):
        model.conflicts.append({"reason": "scan_outside_stay_interval",
                                "event_id": use["event_id"], "stay_id": stay_id})
        for period in hits:
            model.doubt(_period_key(period), "scan_outside_stay_interval",
                        event_id=use["event_id"], stay_id=stay_id)
        return
    for cred in _credentials_from_use(use):
        model.stay_credentials[stay_id].add(cred)
    for permit_id in use.get("permits", []):
        model.stay_permits[stay_id].add(permit_id)
    model.stay_presence[stay_id].append({
        "event_id": use["event_id"], "kind": use["kind"], "bay_id": bay_id,
        "from": moment, "to": use.get("end") or moment,
        "permits": list(use.get("permits", [])),
        "credentials": _credentials_from_use(use)})
    for period in hits:
        key = _period_key(period)
        if not model.allowed[key].get(stay_id):
            model.conflicts.append({"reason": "scan_stay_interval_conflict",
                                    "event_id": use["event_id"], "stay_id": stay_id,
                                    "bay_id": period["bay_id"]})
            model.doubt(key, "scan_stay_interval_conflict", event_id=use["event_id"],
                        stay_id=stay_id)
            continue
        model.pins[key].add(stay_id)


def _apply_pins(model: Model) -> None:
    periods_by_key = {_period_key(p): p for p in model.periods}
    # A single stay cannot be scanned at two bays over overlapping periods.
    # Such scans are contradictory; keep both periods ambiguous instead of
    # letting the pin logic empty both candidate sets.
    pinned_stays: dict[str, list[dict]] = {}
    for key, pinned in model.pins.items():
        for stay_id in pinned:
            pinned_stays.setdefault(stay_id, []).append(periods_by_key[key])
    contradictory: set[str] = set()
    for stay_id, stays_periods in pinned_stays.items():
        for index, left in enumerate(stays_periods):
            for right in stays_periods[index + 1:]:
                if left["bay_id"] != right["bay_id"] and _overlap(
                        left["from"], left["to"], right["from"], right["to"]):
                    contradictory.add(_period_key(left))
                    contradictory.add(_period_key(right))
                    model.conflicts.append({"reason": "conflicting_scans_for_stay",
                                            "stay_id": stay_id,
                                            "bays": sorted([left["bay_id"], right["bay_id"]]),
                                            "from": max(left["from"], right["from"]),
                                            "to": min(left["to"], right["to"])})
                    model.doubt(_period_key(left), "conflicting_scans_for_stay",
                                stay_id=stay_id)
                    model.doubt(_period_key(right), "conflicting_scans_for_stay",
                                stay_id=stay_id)
    for key in contradictory:
        model.pins[key].clear()
    for key, pinned in sorted(model.pins.items()):
        if not pinned:
            continue
        period = periods_by_key[key]
        if len(pinned) > 1:
            model.conflicts.append({"reason": "conflicting_scans_for_bay",
                                    "bay_id": period["bay_id"], "stays": sorted(pinned),
                                    "from": period["from"], "to": period["to"]})
            model.doubt(key, "conflicting_scans_for_bay", stays=sorted(pinned))
            continue
        stay_id = next(iter(pinned))
        for other_stay in list(model.allowed[key]):
            if other_stay != stay_id:
                model.allowed[key][other_stay] = False
                model.excluded[key][other_stay] = "bay_pinned_to_scanned_stay"
        for other in model.periods:
            other_key = _period_key(other)
            if other_key == key or other["bay_id"] == period["bay_id"]:
                continue
            if model.allowed[other_key].get(stay_id) and _overlap(
                    other["from"], other["to"], period["from"], period["to"]):
                model.allowed[other_key][stay_id] = False
                model.excluded[other_key][stay_id] = "stay_pinned_elsewhere_simultaneously"


def _record_duplicates(db, as_of: str, model: Model) -> None:
    for table, flag_column, time_column in (
            ("accessible_permit_revision", "event_time", "event_time"),
            ("accessible_verification", "verified_at", "verified_at"),
            ("bay_scan", "scanned_at", "scanned_at"),
            ("manual_observation", "observed_to", "observed_to")):
        for row in rows(db, f"SELECT event_id FROM {table} WHERE duplicate=1 AND {time_column}<=?",
                        (as_of,)):
            model.conflicts.append({"reason": "duplicate_source_ignored",
                                    "table": table, "event_id": row["event_id"]})


# --------------------------------------------------------------------------
# Feasible complete attributions


def enumerate_assignments(model: Model, limit: int = ASSIGNMENT_LIMIT) -> dict:
    """Enumerate complete one-to-one attributions compatible with evidence.

    Every period that has a compatible stay receives one; a period with no
    compatible stay is ``unmatched`` and never receives an invented identity.
    A stay may serve several non-overlapping periods (a car can change bay) but
    never two bays at the same time.  Periods marked by a doubt are still
    enumerated so the ambiguity remains visible.
    """
    keys = [_period_key(p) for p in model.periods]
    unmatched = [k for k in keys if not any(model.allowed[k].values())]
    variables = [k for k in keys if k not in unmatched]
    order = sorted(variables, key=lambda k: (sum(model.allowed[k].values()), k))
    periods_by_key = {_period_key(p): p for p in model.periods}
    assignments: list[dict] = []
    truncated = False

    def conflicts_with(schedule: list[tuple[str, str]], stay_id: str, key: str) -> bool:
        for other_key, other_stay in schedule:
            if other_stay == stay_id and _overlap(
                    periods_by_key[other_key]["from"], periods_by_key[other_key]["to"],
                    periods_by_key[key]["from"], periods_by_key[key]["to"]):
                return True
        return False

    def walk(index: int, schedule: list[tuple[str, str]]) -> None:
        nonlocal truncated
        if len(assignments) >= limit:
            truncated = True
            return
        if index == len(order):
            assignments.append(dict(schedule))
            return
        key = order[index]
        options = [s for s in sorted(model.allowed[key]) if model.allowed[key][s]]
        for stay_id in options:
            if conflicts_with(schedule, stay_id, key):
                continue
            schedule.append((key, stay_id))
            walk(index + 1, schedule)
            schedule.pop()

    walk(0, [])
    return {"assignments": assignments, "unmatched": unmatched, "truncated": truncated,
            "variables": variables}


# --------------------------------------------------------------------------
# Authorization per attribution


def bay_permit_summary(db, bay_id: str, period: dict, as_of: str) -> dict:
    """Classify every permit revision that could cover this bay and period.

    ``valid`` means the latest revision is trusted, approved, names this bay
    and covers the whole period.  ``unknown`` means trust or coverage cannot
    be established (partial or untrusted), so the absence of a permit is not
    proven.  Anything else is a known non-authorization.
    """
    valid, unknown, invalid = [], [], []
    for row in rows(db, "SELECT DISTINCT permit_id FROM accessible_permit_revision "
                        "WHERE event_time<=?", (as_of,)):
        state = _permit_state(db, row["permit_id"], as_of)
        if state is None:
            continue
        check = permit_valid_for_bay(db, row["permit_id"], bay_id, period, as_of)
        if check["valid"]:
            valid.append(check)
        elif not check["known"]:
            unknown.append(check)
        elif check["reason"] in ("permit_expired", "permit_not_yet_valid",
                                 "permit_partial_coverage", "permit_revoked",
                                 "permit_does_not_cover_bay"):
            # These revisions genuinely touch the bay in question (covering,
            # revoked, or expired), so their non-authorization is informative.
            invalid.append(check)
    return {"valid": valid, "unknown": unknown, "invalid": invalid,
            "has_valid": bool(valid), "has_unknown": bool(unknown)}


def license_for_stay(db, stay: dict, bay_id: str, period: dict, as_of: str,
                     model: Model | None = None, bay_summary: dict | None = None) -> dict:
    """Judge one attribution ``stay occupies bay/period``.

    Authorization can come from either:
    * a trusted credential/verification record that binds the stay to a
      permit (the permit itself names the bay and window), or
    * a trusted bay scan or manual observation at *this* bay that binds the
      stay while the period was running (a scan at another bay does not).

    Revoked or expired permits do not authorize; an unknown or partially
    covering permit cannot prove absence, so the result stays unknown.
    """
    presence = [] if model is None else model.stay_presence.get(stay["stay_id"], [])
    if model is not None:
        # A conflicting credential use cannot license anything: keep the
        # verdict unknown rather than letting either stay benefit from it.
        shared = [p for p in presence if p.get("conflicted")]
        if shared:
            return {"stay_id": stay["stay_id"], "licensed": False, "known": False,
                    "reason": shared[0].get("conflict_reason", "credential_shared_by_stays"),
                    "permit_id": None, "checks": []}
    # Bay-agnostic credential verifications (no bay in the record).
    unbound = [p for p in presence if p["bay_id"] is None
               and (_overlap(p["from"], p["to"], period["from"], period["to"])
                    or _period_contains(period, p["from"]))]
    # Credential use proven at this bay while the period was running.
    at_bay = [p for p in presence if p["bay_id"] == bay_id
              and (_overlap(p["from"], p["to"], period["from"], period["to"])
                   or _period_contains(period, p["from"])
                   or _period_contains(period, p["to"]))]
    permit_ids = {pid for p in unbound + at_bay for pid in p["permits"]}
    if model is None:
        # Fallback for callers without a model: consult verification records
        # directly, still requiring the permit to cover bay and period.
        for row in rows(db, "SELECT permit_id,credential_id FROM accessible_verification "
                            "WHERE stay_id=? AND verified_at<=? AND trusted=1 AND duplicate=0",
                        (stay["stay_id"], as_of)):
            permit_ids.update(_permit_ids_for(db, row["permit_id"], row["credential_id"], as_of))
    if not permit_ids:
        # No credential is bound to this stay for this bay/period.  A valid
        # permit for the bay blocks an unauthorized verdict unless it is
        # demonstrably bound to a *different* stay: then it cannot belong to
        # this attribution, so the absence is proven for this car.
        summary = bay_summary or bay_permit_summary(db, bay_id, period, as_of)
        candidates = list(summary["valid"]) + list(summary["unknown"])
        if candidates:
            unresolved = candidates
            if model is not None:
                unresolved = [p for p in candidates
                              if not any(p["permit_id"] in permits
                                         for other, permits in model.stay_permits.items()
                                         if other != stay["stay_id"])]
            if unresolved:
                return {"stay_id": stay["stay_id"], "licensed": False, "known": False,
                        "reason": "valid_permit_for_bay_unbound",
                        "permit_id": unresolved[0]["permit_id"], "checks": []}
        if presence and not unbound and not at_bay:
            return {"stay_id": stay["stay_id"], "licensed": False, "known": True,
                    "reason": "permit_not_bound_to_this_bay",
                    "permit_id": None, "checks": []}
        return {"stay_id": stay["stay_id"], "licensed": False, "known": True,
                "reason": "no_permit_evidence", "permit_id": None, "checks": []}
    # A permit can authorize this attribution only when the credential was
    # actually presented by this stay at this bay (or verified for it) and the
    # permit itself covers the bay and period.  A permit bound to a stay but
    # only used elsewhere does not authorize this occupancy.
    if permit_ids:
        covered = [pid for pid in permit_ids
                   if permit_valid_for_bay(db, pid, bay_id, period, as_of)["known"]]
        if not covered:
            return {"stay_id": stay["stay_id"], "licensed": False, "known": False,
                    "reason": "permit_state_unknown", "permit_id": sorted(permit_ids)[0],
                    "checks": []}
    checks = [permit_valid_for_bay(db, permit_id, bay_id, period, as_of)
              for permit_id in sorted(permit_ids)]
    for check in checks:
        if check["valid"]:
            return {"stay_id": stay["stay_id"], "licensed": True, "known": True,
                    "reason": check["reason"], "permit_id": check["permit_id"],
                    "checks": checks}
    known = all(check["known"] for check in checks)
    reasons = sorted({check["reason"] for check in checks})
    if not any(check["valid"] for check in checks) and known:
        # The stay's own credentials do not license this bay.  A valid permit
        # for the bay that is not demonstrably bound elsewhere still leaves
        # open the possibility that this car holds it; absence is not proven.
        summary = bay_summary or bay_permit_summary(db, bay_id, period, as_of)
        if summary["has_valid"] or summary["has_unknown"]:
            return {"stay_id": stay["stay_id"], "licensed": False, "known": False,
                    "reason": "valid_permit_for_bay_unbound",
                    "permit_id": (summary["valid"] or summary["unknown"])[0]["permit_id"],
                    "checks": checks}
    return {"stay_id": stay["stay_id"], "licensed": False, "known": known,
            "reason": reasons[0], "reasons": reasons,
            "permit_id": checks[0]["permit_id"], "checks": checks}


def _no_identity_verdict(db, period: dict, as_of: str, reason: str) -> dict:
    """Bay-level verdict when no stay identity can be attributed.

    The absence of a stay identity is not the absence of a violation: if the
    permit ledger proves that no valid permit covers this bay and period, the
    bay is definitely unauthorized even though the car is unknown.  If instead
    a valid (or unknown) permit exists, the occupancy may belong to that
    permit holder, so the verdict stays unknown.
    """
    summary = bay_permit_summary(db, period["bay_id"], period, as_of)
    if summary["has_valid"] or summary["has_unknown"]:
        return {"verdict": "unknown", "reason": reason, "always_unlicensed_stay": None,
                "attributions": [{"stay_ids": [], "licensed": False, "known": False,
                                  "reason": reason, "occurrences": 0, "total": 0}]}
    return {"verdict": "unauthorized", "reason": reason, "always_unlicensed_stay": None,
            "attributions": [{"stay_ids": [], "licensed": False, "known": True,
                              "reason": "bay_lacks_valid_permit", "occurrences": 0,
                              "total": 0}]}


def _judge_period(model: Model, enumeration: dict, period: dict, db, as_of: str,
                  stay_by_id: dict[str, dict]) -> dict:
    key = _period_key(period)
    result = {"bay_id": period["bay_id"], "from": period["from"], "to": period["to"],
              "state": period["state"], "verdict": "unknown", "reason": "",
              "always_unlicensed_stay": None, "candidates": [],
              "attributions": [], "excluded": model.excluded.get(key, {}),
              "pins": sorted(model.pins.get(key, set())), "notes": []}
    if period["state"] != "occupied":
        result["reason"] = "sensor_unknown"
        result["attributions"] = [{"stay_ids": [], "licensed": False, "known": False,
                                   "reason": "sensor_unknown"}]
        result["must_patrol"] = False
        result["targetable_stay"] = None
        return result
    if enumeration["truncated"]:
        result["reason"] = "assignment_search_truncated"
        result["must_patrol"] = False
        result["targetable_stay"] = None
        return result
    if model.doubts.get(key):
        result["notes"].extend(model.doubts[key])
    projections = [a for a in enumeration["assignments"] if key in a]
    if key in enumeration["unmatched"] or not projections:
        reason = ("no_stay_identity_for_occupied_bay"
                  if key in enumeration["unmatched"] else "no_feasible_attribution")
        verdict = _no_identity_verdict(db, period, as_of, reason)
        if model.doubts.get(key):
            # Contradictory evidence leaves the attribution itself unresolved;
            # absence of a permit cannot then be asserted for the bay.
            verdict = {"verdict": "unknown", "reason": model.doubts[key][0]["reason"],
                       "always_unlicensed_stay": None,
                       "attributions": [{"stay_ids": [], "licensed": False,
                                         "known": False,
                                         "reason": model.doubts[key][0]["reason"],
                                         "occurrences": 0, "total": 0}]}
        result.update(verdict)
        result["must_patrol"] = verdict["verdict"] == "unauthorized"
        result["targetable_stay"] = None
        return result
    occupants = sorted({a[key] for a in projections})
    result["candidates"] = occupants
    checks = {stay_id: license_for_stay(db, stay_by_id[stay_id], period["bay_id"],
                                        period, as_of, model)
              for stay_id in occupants}
    for stay_id in occupants:
        check = checks[stay_id]
        result["attributions"].append({
            "stay_ids": [stay_id], "licensed": check["licensed"], "known": check["known"],
            "reason": check["reason"], "reasons": check.get("reasons", []),
            "permit_id": check.get("permit_id"),
            "occurrences": sum(1 for a in projections if a[key] == stay_id),
            "total": len(projections)})
    if model.doubts.get(key):
        result["notes"].extend(model.doubts[key])
    licensed = [c for c in checks.values() if c["licensed"]]
    unknown = [c for c in checks.values() if not c["licensed"] and not c["known"]]
    doubts = model.doubts.get(key, [])
    if doubts:
        result["verdict"] = "unknown"
        result["reason"] = doubts[0]["reason"]
    elif len(licensed) == len(checks):
        result["verdict"] = "authorized"
        result["reason"] = "every_attribution_has_valid_permit"
    elif licensed:
        result["verdict"] = "unknown"
        result["reason"] = "licensed_under_some_attributions_only"
    elif unknown:
        result["verdict"] = "unknown"
        result["reason"] = unknown[0]["reason"]
    else:
        result["verdict"] = "unauthorized"
        result["reason"] = ("attribution_lacks_valid_permit" if len(checks) == 1
                            else "every_attribution_lacks_valid_permit")
        # Targeted notice requires one stay occupying this bay under *every*
        # feasible attribution.  With interchangeable stays there is no such
        # identity, so the patrol notice still dispatches while the targeted
        # notice stays pending.
        if len(occupants) == 1:
            result["always_unlicensed_stay"] = occupants[0]
    result["must_patrol"] = result["verdict"] == "unauthorized"
    result["targetable_stay"] = result["always_unlicensed_stay"]
    return result


# --------------------------------------------------------------------------
# Scope, contracts and persistence


def _scope(db, as_of: str, explicit: dict | None) -> dict:
    """Accessible bays from active accessible capacity orders plus explicit scope."""
    revisions = rows(db, "SELECT * FROM capacity_revision WHERE event_time<=? "
                         "ORDER BY event_time DESC,event_id DESC", (as_of,))
    latest: dict[str, dict] = {}
    for revision in revisions:
        latest.setdefault(revision["order_id"], revision)
    bays: set[str] = set()
    orders, inactive = [], []
    for revision in latest.values():
        if revision["purpose"] != "accessible":
            continue
        order_bays = set(json.loads(revision["bays_json"]))
        if (revision["status"] == "approved"
                and revision["starts_at"] <= as_of < revision["ends_at"]):
            bays |= order_bays
            orders.append({"order_id": revision["order_id"], "bays": sorted(order_bays),
                           "revision": revision["event_id"]})
        else:
            inactive.append({"order_id": revision["order_id"],
                             "status": revision["status"],
                             "revision": revision["event_id"]})
    explicit_bays = set((explicit or {}).get("bays") or [])
    bays |= explicit_bays
    if orders and explicit_bays:
        reason = "capacity_accessible_order_and_explicit_scope"
    elif orders:
        reason = "capacity_accessible_order"
    elif inactive and explicit_bays:
        reason = "accessible_order_not_active_with_explicit_scope"
    elif inactive:
        reason = "accessible_order_not_active"
    elif explicit_bays:
        reason = "explicit_scope"
    else:
        reason = "no_accessible_scope"
    return {"bays": sorted(bays), "active_orders": sorted(orders, key=lambda o: o["order_id"]),
            "inactive_orders": sorted(inactive, key=lambda o: o["order_id"]),
            "explicit_bays": sorted(explicit_bays), "reason": reason}


def _contracts(occupancy: list[dict]) -> dict:
    """Derive the patrol and targeted contracts without upgrading suspicion."""
    patrol, targeted = [], []
    for period in occupancy:
        base = {"bay_id": period["bay_id"], "from": period["from"], "to": period["to"]}
        if period["verdict"] == "unauthorized":
            patrol.append(dict(base, decision="dispatch",
                               reason="unauthorized_occupancy_proven"))
            stay = period["always_unlicensed_stay"]
            if stay:
                targeted.append(dict(base, stay_id=stay, decision="notify",
                                     reason="same_stay_occupies_bay_in_every_attribution"))
            elif period["candidates"]:
                targeted.append(dict(base, stay_id=None, decision="pending_identity",
                                     reason="stay_identity_differs_across_attributions"))
            else:
                # The bay is proven unauthorized but no stay identity is known;
                # a targeted notice must wait for identity, never guess one.
                targeted.append(dict(base, stay_id=None, decision="pending_identity",
                                     reason="no_stay_identity_for_occupied_bay"))
        elif period["verdict"] == "authorized":
            patrol.append(dict(base, decision="no_dispatch", reason="occupancy_authorized"))
            targeted.append(dict(base, stay_id=None, decision="no_notice",
                                 reason="occupancy_authorized"))
        else:
            patrol.append(dict(base, decision="no_dispatch",
                               reason="occupancy_not_proven_unauthorized"))
            targeted.append(dict(base, stay_id=None, decision="pending_verification",
                                 reason=period["reason"]))
    return {"patrol": patrol, "targeted": targeted}


def _inactive_notices(db, scope_info: dict) -> list[dict]:
    """Report lapsed accessible orders instead of keeping a stale incident open."""
    notices = []
    for order in scope_info["inactive_orders"]:
        state = db.execute("SELECT status FROM capacity_revision WHERE event_id=?",
                           (order["revision"],)).fetchone()
        if state and state["status"] == "revoked":
            notices.append({"order_id": order["order_id"],
                            "reason": "accessible_order_revoked",
                            "revision": order["revision"]})
    return notices


def _permit_view(db, bays: set[str], as_of: str) -> list[dict]:
    out = []
    for row in rows(db, "SELECT DISTINCT permit_id FROM accessible_permit_revision "
                        "WHERE event_time<=?", (as_of,)):
        state = _permit_state(db, row["permit_id"], as_of)
        if state and set(state["bays"]) & bays:
            out.append({"permit_id": row["permit_id"],
                        "credential_id": state["credential_id"],
                        "status": state["status"], "bays": state["bays"],
                        "valid_from": state["valid_from"], "valid_to": state["valid_to"],
                        "revision": state["event_id"], "trusted": bool(state["trusted"])})
    return sorted(out, key=lambda item: item["permit_id"])


def _evidence_refs(db, as_of: str, model: Model) -> dict:
    return {
        "permits": [r["event_id"] for r in rows(
            db, "SELECT event_id FROM accessible_permit_revision WHERE event_time<=? "
                "ORDER BY event_time,event_id", (as_of,))],
        "verifications": [r["event_id"] for r in rows(
            db, "SELECT event_id FROM accessible_verification WHERE verified_at<=? "
                "ORDER BY verified_at,event_id", (as_of,))],
        "bay_scans": [r["event_id"] for r in rows(
            db, "SELECT event_id FROM bay_scan WHERE scanned_at<=? ORDER BY scanned_at,event_id",
            (as_of,))],
        "observations": [r["event_id"] for r in rows(
            db, "SELECT event_id FROM manual_observation WHERE observed_to<=? "
                "ORDER BY observed_from,event_id", (as_of,))],
        "duplicates": [r["event_id"] for table, time_column in (
            ("accessible_permit_revision", "event_time"),
            ("accessible_verification", "verified_at"),
            ("bay_scan", "scanned_at"),
            ("manual_observation", "observed_to"))
            for r in rows(db, f"SELECT event_id FROM {table} WHERE duplicate=1 AND {time_column}<=?",
                          (as_of,))],
        "conflicts": model.conflicts,
        "notes": model.notes,
    }


def audit(db, as_of: str, timeout_minutes: int = 30, save: bool = True,
          scope: dict | None = None) -> dict:
    """Recompute the accessible-occupancy verdict from raw evidence.

    Every call re-reads the raw ledgers, so a permit revision, capacity
    revocation, late scan or manual observation yields a new audit version.
    Ordinary occupied bays are considered only where they overlap accessible
    occupancy in time; unrelated ordinary occupancy cannot create a notice.
    """
    _dt(as_of)
    if timeout_minutes <= 0:
        raise AccessibleEvidenceError("timeout_minutes must be positive")
    scope_info = _scope(db, as_of, scope)
    accessible_bays = set(scope_info["bays"])
    occupancy = occupancy_periods(db, as_of, timeout_minutes)
    # A device stays calibrated only until its declared instant: a period that
    # extends past calibration is reported, never silently trusted.
    in_scope_periods = [p for p in occupancy["periods"] if p["bay_id"] in accessible_bays]
    untrusted_periods = [p for p in in_scope_periods
                         if p["state"] != "occupied" or p["reason"] == "latest_reading_stale"]
    # Ordinary bays matter only when a stay could be in either an accessible
    # bay and that ordinary bay over the same interval: the one-to-one
    # attribution then genuinely crosses bays.  Unrelated ordinary occupancy
    # is excluded from the model and cannot influence a notice.
    accessible_periods = [p for p in occupancy["periods"] if p["bay_id"] in accessible_bays]
    relevant = []
    for period in occupancy["periods"]:
        if period["bay_id"] in accessible_bays:
            relevant.append(period)
        elif period["state"] != "occupied":
            # An untrusted or stale ordinary period is not proven occupancy, so
            # it may not consume a stay and cannot influence the attribution.
            continue
        elif any(_overlap(period["from"], period["to"], q["from"], q["to"])
                 for q in accessible_periods):
            relevant.append(period)
    stays = gate_stays(db, as_of)
    model = build_model(db, as_of, relevant, stays)
    enumeration = enumerate_assignments(model)
    stay_by_id = {s["stay_id"]: s for s in stays}
    judged = [_judge_period(model, enumeration, p, db, as_of, stay_by_id)
              for p in relevant if p["bay_id"] in accessible_bays]
    result = {
        "as_of": as_of,
        "scope": scope_info,
        "accessible_bays": sorted(accessible_bays),
        "stays": stays,
        "excluded_stays": excluded_stays(db, as_of),
        "unknown_readings": occupancy["unknown_readings"],
        "untrusted_periods": [{"bay_id": p["bay_id"], "from": p["from"], "to": p["to"],
                               "reason": p["reason"]} for p in untrusted_periods],
        "occupancy": judged,
        "permits": _permit_view(db, accessible_bays, as_of),
        "assignment_count": len(enumeration["assignments"]),
        "assignment_truncated": enumeration["truncated"],
        "unmatched_periods": sorted(enumeration["unmatched"]),
        "evidence_refs": _evidence_refs(db, as_of, model),
        "scope_notices": _inactive_notices(db, scope_info),
        "reasons": sorted({p["reason"] for p in judged}
                          | {u["reason"] for u in occupancy["unknown_readings"]}
                          | {c["reason"] for c in model.conflicts}),
    }
    result["contracts"] = _contracts(judged)
    result["audit_version"] = _persist(db, result) if save else None
    return result


def _persist(db, result: dict) -> int:
    with db:
        cursor = db.execute(
            "INSERT INTO accessible_audit(as_of,scope_json,evidence_json) VALUES (?,?,?)",
            (result["as_of"], json.dumps(result["scope"], sort_keys=True),
             json.dumps(result, sort_keys=True, default=str)))
        audit_id = cursor.lastrowid
        for period in result["occupancy"]:
            if period["verdict"] == "unauthorized":
                db.execute(
                    "INSERT INTO accessible_incident(audit_id,as_of,bay_id,bay_from,bay_to,"
                    "decision,reason) VALUES (?,?,?,?,?,?,?)",
                    (audit_id, result["as_of"], period["bay_id"], period["from"],
                     period["to"], "unauthorized", period["reason"]))
            for attribution in period["attributions"]:
                if not attribution["stay_ids"]:
                    continue
                stay_id = attribution["stay_ids"][0]
                confidence = ("proven" if len(period["candidates"]) == 1
                              else "ambiguous")
                db.execute(
                    "INSERT INTO accessible_assignment_version(audit_id,as_of,bay_id,stay_id,"
                    "confidence,reason,permutation_json) VALUES (?,?,?,?,?,?,?)",
                    (audit_id, result["as_of"], period["bay_id"], stay_id, confidence,
                     attribution.get("reason", period["reason"]),
                     json.dumps({"attributions": period["attributions"],
                                 "excluded": period["excluded"],
                                 "pins": period["pins"],
                                 "assignment_count": result["assignment_count"],
                                 "unmatched_periods": result["unmatched_periods"]},
                                sort_keys=True)))
        for contract in ("patrol", "targeted"):
            for item in result["contracts"][contract]:
                db.execute(
                    "INSERT INTO accessible_dispatch(audit_id,contract,bay_id,stay_id,"
                    "window_from,window_to,decision,reason) VALUES (?,?,?,?,?,?,?,?)",
                    (audit_id, contract, item["bay_id"], item.get("stay_id"),
                     item["from"], item["to"], item["decision"], item["reason"]))
    return audit_id


def latest(db) -> dict | None:
    row = db.execute("SELECT audit_id,evidence_json FROM accessible_audit "
                     "ORDER BY audit_id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return dict(json.loads(row["evidence_json"]), audit_version=row["audit_id"])


def history(db) -> list[dict]:
    return rows(db, "SELECT audit_id,as_of,scope_json,created_at FROM accessible_audit "
                    "ORDER BY audit_id")


def dispatches(db, audit_id: int | None = None) -> list[dict]:
    if audit_id is None:
        return rows(db, "SELECT * FROM accessible_dispatch ORDER BY dispatch_id")
    return rows(db, "SELECT * FROM accessible_dispatch WHERE audit_id=? "
                    "ORDER BY dispatch_id", (audit_id,))


def incidents(db, audit_id: int | None = None) -> list[dict]:
    if audit_id is None:
        return rows(db, "SELECT * FROM accessible_incident ORDER BY incident_id")
    return rows(db, "SELECT * FROM accessible_incident WHERE audit_id=? "
                    "ORDER BY incident_id", (audit_id,))


def assignments(db, audit_id: int | None = None) -> list[dict]:
    if audit_id is None:
        return rows(db, "SELECT * FROM accessible_assignment_version ORDER BY version_id")
    return rows(db, "SELECT * FROM accessible_assignment_version WHERE audit_id=? "
                    "ORDER BY version_id", (audit_id,))
