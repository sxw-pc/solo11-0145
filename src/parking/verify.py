"""Controlled field verification of device-to-bay bindings.

Field trials and installation handover certificates are recorded as immutable
evidence. Devices and physical bays form a one-to-one relation; every binding
consistent with all valid trials is enumerated, and a contract may only revise
the mapping when the evidence forces exactly one. Totals are never used to
prove a position.
"""
from __future__ import annotations

import hashlib
import json

from . import sensor
from .sources import canonical, record
from .store import rows

CONTRACTS = ('dual_trial', 'inspection_cosign')
MAX_UNIVERSE = 8  # enumeration is factorial; field groups are small
MAX_LISTED_MAPPINGS = 50


# ---------------------------------------------------------------- ingest

def ingest(db, events: list[dict]) -> dict:
    made = 0
    with db:
        for e in events:
            op = e.get('op')
            if op == 'trial':
                _check_trial(e)
                if record(db, e, 'field_trial', e['trial_id']):
                    db.execute('INSERT INTO field_trial VALUES (?,?,?,?,?,?,?,?,?)',
                               (e['event_id'], e['trial_id'], e['window_start'], e['window_end'],
                                e['executor'], e['witness'], json.dumps(e['confirmed'], sort_keys=True),
                                json.dumps(e['readings'], sort_keys=True), e['event_time']))
                    made += 1
            elif op == 'handover':
                _check_handover(e)
                if record(db, e, 'handover_certificate', e['certificate_id']):
                    db.execute('INSERT INTO handover_certificate VALUES (?,?,?,?,?)',
                               (e['event_id'], e['certificate_id'], e['issuer'],
                                json.dumps(e['bindings'], sort_keys=True), e['event_time']))
                    made += 1
            else:
                raise ValueError(f'unknown verify op {op}')
    return {'imported': made,
            'trials': len(rows(db, 'SELECT event_id FROM field_trial')),
            'certificates': len(rows(db, 'SELECT event_id FROM handover_certificate'))}


def _check_trial(e: dict) -> None:
    for key in ('event_id', 'trial_id', 'window_start', 'window_end', 'executor',
                'witness', 'confirmed', 'readings', 'event_time'):
        if key not in e:
            raise ValueError(f'trial missing {key}')
    if e['window_start'] >= e['window_end']:
        raise ValueError('trial window must be non-empty')
    if not e['executor'] or not e['witness']:
        raise ValueError('trial needs executor and witness')
    if not isinstance(e['confirmed'], dict) or not e['confirmed']:
        raise ValueError('trial needs confirmed bay states')
    for bay, state in e['confirmed'].items():
        if state not in ('occupied', 'free'):
            raise ValueError(f'confirmed state for {bay} must be occupied/free')
    if not isinstance(e['readings'], list) or not e['readings']:
        raise ValueError('trial needs device readings')
    for r in e['readings']:
        for key in ('device_id', 'sequence_no', 'occupied', 'observed_at'):
            if key not in r:
                raise ValueError(f'trial reading missing {key}')


def _check_handover(e: dict) -> None:
    for key in ('event_id', 'certificate_id', 'issuer', 'bindings', 'event_time'):
        if key not in e:
            raise ValueError(f'handover missing {key}')
    if not e['issuer']:
        raise ValueError('handover needs an issuer')
    if not isinstance(e['bindings'], dict) or not e['bindings']:
        raise ValueError('handover needs device-to-bay bindings')


# ------------------------------------------------------------- trial validity

def _reading_problem(db, trial: dict, reading: dict) -> str | None:
    """Why a raw reading cannot serve as binding evidence, or None."""
    if not (trial['window_start'] <= reading['observed_at'] <= trial['window_end']):
        return 'outside_window'
    revision = sensor.revision_at(db, reading['device_id'], reading['observed_at'])
    if revision is None:
        return 'unregistered_device'
    if reading['observed_at'] > revision['calibrated_until']:
        return 'calibration_expired'
    if sensor.fault_at(db, reading['device_id'], reading['observed_at']):
        return 'device_faulty'
    return None


def load_trials(db) -> list[dict]:
    """All recorded trials with per-reading validity; evidence is never dropped."""
    trials = []
    for row in rows(db, 'SELECT * FROM field_trial ORDER BY window_start,event_id'):
        t = {'event_id': row['event_id'], 'trial_id': row['trial_id'],
             'window_start': row['window_start'], 'window_end': row['window_end'],
             'executor': row['executor'], 'witness': row['witness'],
             'confirmed': json.loads(row['confirmed_json']),
             'readings': json.loads(row['readings_json']),
             'event_time': row['event_time']}
        problems = []
        valid_readings = []
        if t['executor'] == t['witness']:
            problems.append('witness_not_independent')
        for r in t['readings']:
            problem = _reading_problem(db, t, r)
            if problem is None:
                valid_readings.append(r)
            else:
                problems.append(f"{problem}:{r['device_id']}")
        t['valid_readings'] = valid_readings
        t['problems'] = problems
        t['valid'] = t['executor'] != t['witness'] and bool(valid_readings)
        trials.append(t)
    return trials


def load_certificates(db) -> list[dict]:
    return [{'event_id': r['event_id'], 'certificate_id': r['certificate_id'],
             'issuer': r['issuer'], 'bindings': json.loads(r['bindings_json']),
             'event_time': r['event_time']}
            for r in rows(db, 'SELECT * FROM handover_certificate ORDER BY event_time,event_id')]


# --------------------------------------------------------- feasible mappings

def _analyze(trials: list[dict]) -> dict:
    """Enumerate every one-to-one device/bay binding satisfying all trials."""
    bays = sorted({bay for t in trials for bay in t['confirmed']})
    devices = sorted({r['device_id'] for t in trials for r in t['valid_readings']})
    result = {'devices': devices, 'bays': bays, 'feasible': [], 'reason': 'ok',
              'truncated': False}
    if not devices or not bays:
        result['reason'] = 'no_valid_trials'
        return result
    if len(devices) != len(bays):
        result['reason'] = 'device_bay_mismatch'
        return result
    if len(devices) > MAX_UNIVERSE:
        raise ValueError('verification universe too large to enumerate')
    allowed = {d: set(bays) for d in devices}
    for t in trials:
        for r in t['valid_readings']:
            want = 'occupied' if r['occupied'] else 'free'
            match = {b for b, state in t['confirmed'].items() if state == want}
            allowed[r['device_id']] &= match
    if any(not allowed[d] for d in devices):
        result['reason'] = 'contradictory_trials'
        return result
    order = sorted(devices, key=lambda d: len(allowed[d]))
    assign: dict[str, str] = {}
    used: set[str] = set()

    def walk(i: int) -> None:
        if len(result['feasible']) >= MAX_LISTED_MAPPINGS:
            result['truncated'] = True
            return
        if i == len(order):
            result['feasible'].append(dict(assign))
            return
        d = order[i]
        for b in sorted(allowed[d]):
            if b not in used:
                assign[d] = b
                used.add(b)
                walk(i + 1)
                used.discard(b)
                del assign[d]

    walk(0)
    if not result['feasible']:
        result['reason'] = 'contradictory_trials'
    elif len(result['feasible']) > 1:
        result['reason'] = 'ambiguous_mappings'
    return result


def _crew_independent(trials: list[dict]) -> bool:
    for i, first in enumerate(trials):
        for second in trials[i + 1:]:
            if (first['executor'] != second['executor']
                    and first['witness'] != second['witness']):
                return True
    return False


def evaluate(db, contract: str, bays: list[str]) -> dict:
    """Adjudicate a contract against current evidence; pure, writes nothing."""
    if contract not in CONTRACTS:
        raise ValueError(f'unknown contract {contract}')
    bays = sorted(set(bays))
    if not bays:
        raise ValueError('contract needs at least one bay')
    valid = [t for t in load_trials(db) if t['valid']]
    relevant = [t for t in valid if set(t['confirmed']) & set(bays)]
    verdict = {'contract': contract, 'bays': bays, 'verdict': 'rejected',
               'reason': None, 'mapping': None, 'valid_trials': len(relevant),
               'feasible': [], 'feasible_count': 0, 'details': {}}
    if not relevant:
        verdict['reason'] = 'no_valid_trials'
        return verdict
    analysis = _analyze(relevant)
    verdict['feasible'] = analysis['feasible']
    verdict['feasible_count'] = len(analysis['feasible'])
    missing = [b for b in bays if b not in analysis['bays']]
    if missing:
        verdict['reason'] = 'bays_without_trials'
        verdict['details']['missing'] = missing
        return verdict
    if analysis['reason'] != 'ok':
        verdict['reason'] = analysis['reason']
        return verdict
    mapping = analysis['feasible'][0]
    if contract == 'dual_trial':
        # Every bay in the proven group needs at least two valid trials run by
        # different executors and different witnesses.
        short = []
        for bay in analysis['bays']:
            crews = [t for t in relevant if bay in t['confirmed']]
            if len(crews) < 2 or not _crew_independent(crews):
                short.append(bay)
        if short:
            verdict['reason'] = 'insufficient_independent_trials'
            verdict['details']['bays_short_of_crews'] = short
            return verdict
    else:
        # One controlled trial plus an installation handover certificate that
        # was issued independently (before the trial window, so it cannot be
        # copied from the trial) and binds the same serials to the same bays.
        first_window = min(t['window_start'] for t in relevant)
        independent = [c for c in load_certificates(db) if c['event_time'] < first_window]
        devices = set(mapping)
        conflicting = [c['certificate_id'] for c in independent
                       if any(d in c['bindings'] and c['bindings'][d] != mapping[d] for d in devices)]
        if conflicting:
            verdict['reason'] = 'handover_conflicts_trial'
            verdict['details']['conflicting_certificates'] = conflicting
            return verdict
        confirming = [c['certificate_id'] for c in independent
                      if all(c['bindings'].get(d) == mapping[d] for d in devices)]
        if not confirming:
            verdict['reason'] = 'handover_missing'
            return verdict
        verdict['details']['confirming_certificates'] = confirming
    verdict['verdict'] = 'issued'
    verdict['reason'] = 'unique_mapping_proven'
    verdict['mapping'] = mapping
    return verdict


def issue(db, contract: str, bays: list[str], effective_from: str) -> dict:
    """Adjudicate and, when proven, revise bindings from effective_from.

    Old revisions are never overwritten: a new sensor_revision supersedes the
    binding for queries at or after effective_from. Every adjudication, issued
    or rejected, is appended to binding_issuance so late evidence forces a
    fresh decision instead of silently keeping the old one.
    """
    verdict = evaluate(db, contract, bays)
    applied = []
    with db:
        if verdict['verdict'] == 'issued':
            for device, bay in sorted(verdict['mapping'].items()):
                current = sensor.revision_at(db, device, effective_from)
                if current is None:
                    raise ValueError(f'device {device} has no registered revision')
                if current['bay_id'] == bay:
                    continue
                body = {'event_id': None, 'op': 'binding_revision', 'device_id': device,
                        'bay_id': bay, 'calibrated_until': current['calibrated_until'],
                        'valid_from': effective_from, 'contract': contract,
                        'event_time': effective_from}
                digest = hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()
                body['event_id'] = f'binding:{digest[:16]}'
                if record(db, body, 'binding_revision', device):
                    db.execute('INSERT INTO sensor_revision VALUES (?,?,?,?,?)',
                               (body['event_id'], device, bay, current['calibrated_until'], effective_from))
                    old = db.execute('SELECT valid_from FROM sensor_registry WHERE device_id=?', (device,)).fetchone()
                    if old is None or effective_from >= old['valid_from']:
                        db.execute('INSERT INTO sensor_registry(device_id,bay_id,calibrated_until,valid_from,source_event) VALUES (?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET bay_id=excluded.bay_id,calibrated_until=excluded.calibrated_until,valid_from=excluded.valid_from,source_event=excluded.source_event',
                                   (device, bay, current['calibrated_until'], effective_from, body['event_id']))
                    applied.append({'device_id': device, 'bay_id': bay, 'revision_event': body['event_id']})
        db.execute('INSERT INTO binding_issuance(contract,bays_json,mapping_json,verdict,reason,effective_from) VALUES (?,?,?,?,?,?)',
                   (contract, json.dumps(verdict['bays']), json.dumps(verdict['mapping'] or {}),
                    verdict['verdict'], verdict['reason'],
                    effective_from if verdict['verdict'] == 'issued' else None))
        if applied:
            sensor.refresh_projection(db, sorted({a['bay_id'] for a in applied}))
    verdict['effective_from'] = effective_from if verdict['verdict'] == 'issued' else None
    verdict['applied'] = applied
    return verdict


# ------------------------------------------------------------------- status

def _issued_for(db, bay: str, as_of: str) -> dict | None:
    issued = None
    for r in rows(db, "SELECT * FROM binding_issuance WHERE verdict='issued' ORDER BY issuance_id"):
        mapping = json.loads(r['mapping_json'])
        if bay in mapping.values() and r['effective_from'] and r['effective_from'] <= as_of:
            issued = {'issuance_id': r['issuance_id'], 'contract': r['contract'],
                      'bays': json.loads(r['bays_json']), 'mapping': mapping,
                      'effective_from': r['effective_from']}
    return issued


def status(db, as_of: str) -> dict:
    """Per-bay binding proof, candidate mappings and reasons at as_of."""
    trials = load_trials(db)
    valid = [t for t in trials if t['valid']]
    analysis = _analyze(valid)
    bindings = sensor.bindings_at(db, as_of)
    bay_names = {r['bay_id'] for r in bindings.values()}
    bay_names |= {r['bay_id'] for r in rows(db, 'SELECT DISTINCT bay_id FROM occupancy_event')}
    bay_names |= {b for t in valid for b in t['confirmed']}
    per_bay = {}
    for bay in sorted(bay_names):
        device = next((d for d, r in sorted(bindings.items()) if r['bay_id'] == bay), None)
        entry = {'device_id': device, 'binding': 'unproven', 'reason': None}
        issued = _issued_for(db, bay, as_of)
        if issued:
            group = set(issued['bays'])
            relevant = [t for t in valid if set(t['confirmed']) & group]
            recheck = _analyze(relevant)
            supported = any(all(m.get(d) == b for d, b in issued['mapping'].items())
                            for m in recheck['feasible'])
            if supported:
                entry['binding'] = 'proven'
                entry['reason'] = f"{issued['contract']}#issuance-{issued['issuance_id']}"
            else:
                entry['binding'] = 'contested'
                entry['reason'] = 'evidence_no_longer_supports_issued_mapping'
        else:
            touching = [t for t in valid if bay in t['confirmed']]
            if not touching:
                entry['reason'] = 'no_valid_trials'
            elif analysis['reason'] != 'ok':
                entry['reason'] = analysis['reason']
            else:
                entry['reason'] = 'unique_mapping_not_issued'
        per_bay[bay] = entry
    history = {}
    for r in rows(db, 'SELECT * FROM sensor_revision ORDER BY device_id,valid_from,event_id'):
        history.setdefault(r['device_id'], []).append(
            {'bay_id': r['bay_id'], 'valid_from': r['valid_from'],
             'calibrated_until': r['calibrated_until'], 'source_event': r['event_id']})
    return {'as_of': as_of,
            'candidate_mappings': analysis['feasible'],
            'candidate_reason': analysis['reason'],
            'universe': {'devices': analysis['devices'], 'bays': analysis['bays']},
            'valid_trials': len(valid),
            'trial_findings': [{'event_id': t['event_id'], 'valid': t['valid'], 'problems': t['problems']}
                               for t in trials if t['problems'] or not t['valid']],
            'bays': per_bay,
            'binding_history': history,
            'issuances': rows(db, 'SELECT * FROM binding_issuance ORDER BY issuance_id')}
