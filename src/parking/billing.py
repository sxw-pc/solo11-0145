"""Offline parking-fee adjudication, kept separate from availability releases.

Gate events and issued bills are immutable. New calibration/tariff/contract
evidence appends revisions; narrower calibration later produces a new decision
that supersedes the previous one and only settles the balance difference.

Clock offsets are a property of a *gate* over a calibration validity window.
All readings from the same gate share one offset draw, so entry and exit at
the same gate stay perfectly correlated; offsets of distinct gates vary
independently. Every feasible offset combination is enumerated explicitly -
we never subtract endpoints of two independent per-event time intervals.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from itertools import product

from .sources import canonical, record
from .store import rows

# Exact enumeration is required; refuse silently partial answers beyond this.
MAX_COMBOS = 200_000


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def split_calendar_days(start: datetime, end: datetime):
    """Yield segment duration in seconds split at each UTC midnight."""
    day = timedelta(days=1)
    cursor = start
    while cursor < end:
        next_midnight = (cursor + day).replace(hour=0, minute=0, second=0, microsecond=0)
        seg_end = min(end, next_midnight)
        yield int((seg_end - cursor).total_seconds())
        cursor = seg_end


# --------------------------------------------------------------------------- #
# evidence ingestion (append-only, conflict checked, replay idempotent)
# --------------------------------------------------------------------------- #
def ingest_calibration(db, events: list[dict]) -> dict:
    made = 0
    with db:
        for e in events:
            if e.get('op') != 'gate_calibration':
                raise ValueError('calibration input needs op=gate_calibration')
            if not e.get('gate_id'):
                raise ValueError('calibration needs gate_id')
            if e['valid_from'] >= e['valid_to']:
                raise ValueError('calibration validity window is empty')
            if 'offset_seconds_min' in e:
                lo, hi = int(e['offset_seconds_min']), int(e['offset_seconds_max'])
            else:  # minute-graded evidence is converted once, at ingest time
                lo = int(e['offset_minutes_min']) * 60
                hi = int(e['offset_minutes_max']) * 60
            if lo > hi:
                raise ValueError('calibration offset range is inverted')
            if record(db, e, 'gate_calibration', e['gate_id']):
                db.execute('INSERT INTO gate_calibration VALUES (?,?,?,?,?,?,?)',
                           (e['event_id'], e['gate_id'], e['valid_from'], e['valid_to'],
                            lo, hi, e['event_time']))
                made += 1
    return {'imported': made, 'calibrations': len(rows(db, 'SELECT event_id FROM gate_calibration'))}


def ingest_tariff(db, events: list[dict]) -> dict:
    made = 0
    with db:
        for e in events:
            if e.get('op') != 'tariff':
                raise ValueError('tariff input needs op=tariff')
            if e['valid_from'] >= e['valid_to']:
                raise ValueError('tariff validity window is empty')
            grace = int(e['grace_seconds'])
            cap = int(e['daily_cap_minor'])
            if grace < 0 or cap < 0:
                raise ValueError('grace and daily cap must be non-negative')
            tiers = e['tiers']
            if not isinstance(tiers, list) or not tiers:
                raise ValueError('tariff needs at least one tier')
            norm = []
            last_up = None
            for tier in tiers:
                up_to, amount = tier[0], int(tier[1])
                if amount < 0:
                    raise ValueError('tier amount must be non-negative')
                if up_to is not None:
                    up_to = int(up_to)
                    if up_to <= grace:
                        raise ValueError('tier boundary must exceed the free grace')
                    if last_up is not None and up_to <= last_up:
                        raise ValueError('tier boundaries must be strictly increasing')
                    last_up = up_to
                norm.append([up_to, amount])
            if norm[-1][0] is not None:
                raise ValueError('final tariff tier must be open ended (up_to=null)')
            if record(db, e, 'tariff', e['tariff_id']):
                db.execute('INSERT INTO tariff_revision VALUES (?,?,?,?,?,?,?,?)',
                           (e['event_id'], e['tariff_id'], e['valid_from'], e['valid_to'],
                            grace, cap, json.dumps(norm), e['event_time']))
                made += 1
    return {'imported': made, 'tariffs': len(rows(db, 'SELECT DISTINCT tariff_id FROM tariff_revision'))}


def ingest_contract(db, events: list[dict]) -> dict:
    made = 0
    with db:
        for e in events:
            if e.get('op') != 'contract':
                raise ValueError('contract input needs op=contract')
            if e['contract_type'] not in ('exact', 'guaranteed'):
                raise ValueError('contract_type must be exact or guaranteed')
            if e['valid_from'] >= e['valid_to']:
                raise ValueError('contract validity window is empty')
            if record(db, e, 'contract', e['contract_id']):
                db.execute('INSERT INTO contract_revision VALUES (?,?,?,?,?,?)',
                           (e['event_id'], e['contract_id'], e['contract_type'],
                            e['valid_from'], e['valid_to'], e['event_time']))
                made += 1
    return {'imported': made, 'contracts': len(rows(db, 'SELECT DISTINCT contract_id FROM contract_revision'))}


# --------------------------------------------------------------------------- #
# tariff math (integer minor units; grace, tier start, daily cap, midnight)
# --------------------------------------------------------------------------- #
def tier_charge(tariff: dict, duration_seconds: int) -> int:
    if duration_seconds <= tariff['grace_seconds']:
        return 0
    for up_to, amount in tariff['tiers']:
        if up_to is None or duration_seconds <= up_to:
            return int(amount)
    raise ValueError('tariff has no covering open tier')  # validated at ingest


def charge_for_interval(tariff: dict, start: datetime, end: datetime) -> int:
    """Split at UTC midnight, tier each calendar day independently, cap each day."""
    total = 0
    for duration in split_calendar_days(start, end):
        total += min(tariff['daily_cap_minor'], tier_charge(tariff, duration))
    return total


# --------------------------------------------------------------------------- #
# stay admission and feasible-space construction
# --------------------------------------------------------------------------- #
def stay_events(db, stay_id: str) -> dict:
    items = rows(db, 'SELECT * FROM gate_event WHERE stay_id=? ORDER BY occurred_at,event_id', (stay_id,))
    ins = [e for e in items if e['direction'] == 'in']
    outs = [e for e in items if e['direction'] == 'out']
    return {'all': items, 'ins': ins, 'outs': outs}


def admission_reason(ev: dict) -> str | None:
    ins, outs = ev['ins'], ev['outs']
    if not ins and not outs:
        return 'unknown_stay'
    if len(ins) > 1 or len(outs) > 1:
        return 'duplicate_gate_receipt'
    if not ins:
        return 'orphan_exit'
    if not outs:
        return 'entry_without_exit'
    if outs[0]['occurred_at'] < ins[0]['occurred_at']:
        return 'out_before_in'
    if not ins[0]['gate_id'] or not outs[0]['gate_id']:
        return 'missing_gate_identity'
    return None


def covering_calibrations(db, gate_id: str, device_time: str) -> list[dict]:
    return rows(db, 'SELECT * FROM gate_calibration WHERE gate_id=? AND valid_from<=? AND valid_to>? '
                    'ORDER BY event_id', (gate_id, device_time, device_time))


def build_gate_groups(db, entry: dict, exit_: dict) -> tuple[list[dict], list[str], str | None]:
    """One offset variable per gate (shared by every reading of that gate).

    Overlapping calibration revisions over the same reading intersect into one
    effective interval; an empty intersection means contradictory evidence.
    """
    refs: set[str] = set()
    per_gate: dict[str, list[dict]] = {}
    for event in (entry, exit_):
        for cal in covering_calibrations(db, event['gate_id'], event['occurred_at']):
            refs.add(cal['event_id'])
            per_gate.setdefault(event['gate_id'], []).append(cal)
    groups = []
    for gate_id, cals in per_gate.items():
        lo = max(c['offset_seconds_min'] for c in cals)
        hi = min(c['offset_seconds_max'] for c in cals)
        if lo > hi:
            return [], sorted(refs), 'contradictory_calibration'
        groups.append({'gate_id': gate_id, 'offset_min': lo, 'offset_max': hi})
    groups.sort(key=lambda g: g['gate_id'])
    return groups, sorted(refs), None


# --------------------------------------------------------------------------- #
# feasible durations and amounts
# --------------------------------------------------------------------------- #
def feasible_outcomes(db, stay_id: str) -> dict:
    ev = stay_events(db, stay_id)
    reason = admission_reason(ev)
    if reason:
        return {'stay_id': stay_id, 'admissible': False, 'reason': reason,
                'durations_seconds': [], 'amounts_minor': [], 'combos': 0,
                'calibration_refs': [], 'tariff_refs': []}
    entry, exit_ = ev['ins'][0], ev['outs'][0]
    groups, cal_refs, cal_problem = build_gate_groups(db, entry, exit_)
    if cal_problem:
        return {'stay_id': stay_id, 'admissible': False, 'reason': cal_problem,
                'durations_seconds': [], 'amounts_minor': [], 'combos': 0,
                'calibration_refs': cal_refs, 'tariff_refs': []}
    if not groups:
        return {'stay_id': stay_id, 'admissible': False, 'reason': 'missing_calibration',
                'durations_seconds': [], 'amounts_minor': [], 'combos': 0,
                'calibration_refs': [], 'tariff_refs': []}

    t_in = parse_ts(entry['occurred_at'])
    t_out = parse_ts(exit_['occurred_at'])
    gate_index = {g['gate_id']: i for i, g in enumerate(groups)}
    ranges = [range(g['offset_min'], g['offset_max'] + 1) for g in groups]
    combo_count = 1
    for r in ranges:
        combo_count *= len(r)
    if combo_count > MAX_COMBOS:
        raise ValueError(f'{combo_count} feasible calibration combos for {stay_id} exceed {MAX_COMBOS}')

    durations: set[int] = set()
    amounts: set[int] = set()
    tariff_refs: set[str] = set()
    unrateable = False
    valid_combos = 0
    tariff_cache: dict[str, object] = {}
    tariff_obj_cache: dict[str, dict] = {}
    for offsets in product(*ranges):
        off_in = offsets[gate_index[entry['gate_id']]]
        off_out = offsets[gate_index[exit_['gate_id']]]
        true_in = t_in + timedelta(seconds=off_in)
        true_out = t_out + timedelta(seconds=off_out)
        duration = int((true_out - true_in).total_seconds())
        if duration <= 0:
            continue  # calibration excludes physically impossible readings
        valid_combos += 1
        durations.add(duration)
        cache_key = fmt_ts(true_in)
        if cache_key not in tariff_cache:
            tariff_cache[cache_key] = latest_tariff_at(db, true_in)
        tariff_row = tariff_cache[cache_key]
        if tariff_row is None:
            unrateable = True
            continue
        tariff_refs.add(tariff_row['event_id'])
        if tariff_row['event_id'] not in tariff_obj_cache:
            tariff_obj_cache[tariff_row['event_id']] = {
                'grace_seconds': tariff_row['grace_seconds'],
                'daily_cap_minor': tariff_row['daily_cap_minor'],
                'tiers': json.loads(tariff_row['tiers_json'])}
        tariff = tariff_obj_cache[tariff_row['event_id']]
        amounts.add(charge_for_interval(tariff, true_in, true_out))

    return {'stay_id': stay_id, 'admissible': True,
            'reason': 'no_effective_tariff' if unrateable and not amounts else
                      ('partially_unrateable' if unrateable else None),
            'durations_seconds': sorted(durations), 'amounts_minor': sorted(amounts),
            'combos': valid_combos, 'calibration_refs': cal_refs,
            'tariff_refs': sorted(tariff_refs),
            'entry_event': entry['event_id'], 'exit_event': exit_['event_id']}


def latest_tariff_at(db, at: datetime):
    return db.execute('SELECT * FROM tariff_revision WHERE valid_from<=? AND valid_to>? '
                      'ORDER BY published_at DESC,event_id DESC LIMIT 1',
                      (fmt_ts(at), fmt_ts(at))).fetchone()


def contract_at(db, contract_id: str, at: datetime):
    return db.execute('SELECT * FROM contract_revision WHERE contract_id=? AND valid_from<=? AND valid_to>? '
                      'ORDER BY published_at DESC,event_id DESC LIMIT 1',
                      (contract_id, fmt_ts(at), fmt_ts(at))).fetchone()


# --------------------------------------------------------------------------- #
# decisions, bills and the receivable ledger
# --------------------------------------------------------------------------- #
def _fingerprint(db, outcome: dict, contract_event_id: str | None) -> str:
    parts = [outcome.get('entry_event'), outcome.get('exit_event')]
    digest = {}
    if parts[0]:
        for eid in [p for p in parts if p] + outcome['calibration_refs'] + outcome['tariff_refs']:
            row = db.execute('SELECT sha256 FROM evidence WHERE event_id=?', (eid,)).fetchone()
            digest[eid] = row['sha256'] if row else None
    payload = {'events': digest, 'amounts': outcome['amounts_minor'],
               'durations': outcome['durations_seconds'], 'contract': contract_event_id,
               'reason': outcome['reason']}
    return hashlib.sha256(canonical(payload).encode('utf-8')).hexdigest()


def ledger_balance(db, stay_id: str, contract_id: str) -> int:
    row = db.execute('SELECT COALESCE(SUM(amount_minor),0) AS bal FROM stay_ledger_entry '
                     'WHERE stay_id=? AND contract_id=?', (stay_id, contract_id)).fetchone()
    return int(row['bal'])


def _next_seq(db, stay_id: str, contract_id: str) -> int:
    row = db.execute('SELECT COALESCE(MAX(decision_seq),0)+1 AS seq FROM billing_decision '
                     'WHERE stay_id=? AND contract_id=?', (stay_id, contract_id)).fetchone()
    return int(row['seq'])


def _latest_decision(db, stay_id: str, contract_id: str):
    return db.execute('SELECT * FROM billing_decision WHERE stay_id=? AND contract_id=? '
                      'ORDER BY decision_seq DESC LIMIT 1', (stay_id, contract_id)).fetchone()


def _write_bill_and_entry(db, stay_id, contract_id, seq, bill_type, amount, decision_id, kind):
    bill_id = f'{stay_id}:{contract_id}:d{seq}:{bill_type}'
    exists = db.execute('SELECT 1 FROM bill WHERE bill_id=?', (bill_id,)).fetchone()
    if not exists:
        db.execute('INSERT INTO bill VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)',
                   (bill_id, stay_id, contract_id, bill_type, amount, decision_id, None))
        db.execute('INSERT INTO stay_ledger_entry(stay_id,contract_id,kind,amount_minor,bill_id,decision_id) '
                   'VALUES (?,?,?,?,?,?)', (stay_id, contract_id, kind, amount, bill_id, decision_id))
    return bill_id


def adjudicate(db, stay_id: str, contract_id: str) -> dict:
    """Produce (or replay) one ruling for a stay under one approved contract."""
    with db:
        outcome = feasible_outcomes(db, stay_id)
        # contract version is selected against the device entry time; a missing
        # contract revision blocks any formal financial action.
        ev = stay_events(db, stay_id)
        contract_row = contract_at(db, contract_id, parse_ts(ev['ins'][0]['occurred_at'])) if ev['ins'] else None
        fingerprint = _fingerprint(db, outcome, contract_row['event_id'] if contract_row else None)
        prior = _latest_decision(db, stay_id, contract_id)
        if prior and prior['evidence_fingerprint'] == fingerprint:
            return _decision_payload(prior, note='replayed')

        seq = _next_seq(db, stay_id, contract_id)
        amounts = outcome['amounts_minor']
        admissible = outcome['admissible'] and contract_row is not None and not outcome['reason'] and amounts
        if not admissible:
            reason = outcome['reason'] or ('no_effective_contract' if contract_row is None
                                           else 'no_effective_tariff')
            cur = db.execute('INSERT INTO billing_decision(stay_id,contract_id,decision_seq,status,'
                             'feasible_amounts_json,feasible_durations_json,billed_minor,guaranteed_minor,'
                             'reason,entry_event,exit_event,calibration_refs_json,tariff_refs_json,'
                             'contract_ref,evidence_fingerprint,supersedes_decision) '
                             'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (stay_id, contract_id, seq, 'rejected' if not outcome['admissible'] else 'pending_review',
                              json.dumps(amounts), json.dumps(outcome['durations_seconds']),
                              None, 0, reason, outcome.get('entry_event'), outcome.get('exit_event'),
                              json.dumps(outcome['calibration_refs']),
                              json.dumps(outcome['tariff_refs']),
                              contract_row['event_id'] if contract_row else None,
                              fingerprint, prior['decision_id'] if prior else None))
            return _decision_payload(_latest_decision(db, stay_id, contract_id), note='ruled')

        ctype = contract_row['contract_type']
        certain = len(set(amounts)) == 1
        # If some feasible readings fall outside every tariff window the true
        # amount is not fully knowable: no formal bill, only the common minimum.
        if outcome['reason'] == 'partially_unrateable':
            certain = False
        final_amount = amounts[0] if certain else None
        guaranteed = min(amounts)
        paid = ledger_balance(db, stay_id, contract_id)

        if certain:
            status = 'formal_bill'
            billed = final_amount
            decision_id = _insert_decision(db, stay_id, contract_id, seq, status, amounts, outcome,
                                           final_amount, guaranteed, 'single_feasible_amount',
                                           contract_row, fingerprint, prior)
            delta = final_amount - paid
            if delta > 0:
                kind = 'formal' if paid == 0 else 'supplement'
                bill_type = 'formal' if paid == 0 else 'correction'
                _write_correction_chain(db, stay_id, contract_id, seq, bill_type, delta, decision_id, kind, prior)
            elif delta < 0:
                _write_refund(db, stay_id, contract_id, seq, -delta, decision_id, prior)
        elif ctype == 'exact':
            status = 'pending_review'
            decision_id = _insert_decision(db, stay_id, contract_id, seq, status, amounts, outcome,
                                           None, 0, 'amount_ambiguous_under_calibration',
                                           contract_row, fingerprint, prior)
        else:  # guaranteed contract: collect only the amount every feasible combo guarantees
            status = 'partial_prepayment'
            decision_id = _insert_decision(db, stay_id, contract_id, seq, status, amounts, outcome,
                                           None, guaranteed, 'guaranteed_minimum_collected',
                                           contract_row, fingerprint, prior)
            delta = guaranteed - paid
            if delta > 0:
                _write_bill_and_entry(db, stay_id, contract_id, seq, 'prepayment', delta,
                                      decision_id, 'prepayment')
            elif delta < 0:
                _write_refund(db, stay_id, contract_id, seq, -delta, decision_id, prior)
        return _decision_payload(_latest_decision(db, stay_id, contract_id), note='ruled')


def _insert_decision(db, stay_id, contract_id, seq, status, amounts, outcome, billed,
                     guaranteed, reason, contract_row, fingerprint, prior):
    cur = db.execute('INSERT INTO billing_decision(stay_id,contract_id,decision_seq,status,'
                     'feasible_amounts_json,feasible_durations_json,billed_minor,guaranteed_minor,'
                     'reason,entry_event,exit_event,calibration_refs_json,tariff_refs_json,'
                     'contract_ref,evidence_fingerprint,supersedes_decision) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (stay_id, contract_id, seq, status, json.dumps(amounts),
                      json.dumps(outcome['durations_seconds']), billed, guaranteed, reason,
                      outcome['entry_event'], outcome['exit_event'],
                      json.dumps(outcome['calibration_refs']), json.dumps(outcome['tariff_refs']),
                      contract_row['event_id'], fingerprint,
                      prior['decision_id'] if prior else None))
    return cur.lastrowid


def _prior_formal_bill(db, stay_id, contract_id):
    return db.execute("SELECT * FROM bill WHERE stay_id=? AND contract_id=? AND bill_type IN ('formal','correction') "
                      'ORDER BY rowid DESC LIMIT 1', (stay_id, contract_id)).fetchone()


def _prior_any_bill(db, stay_id, contract_id):
    return db.execute('SELECT * FROM bill WHERE stay_id=? AND contract_id=? '
                      'ORDER BY rowid DESC LIMIT 1', (stay_id, contract_id)).fetchone()


def _write_correction_chain(db, stay_id, contract_id, seq, bill_type, amount, decision_id, kind, prior):
    bill_id = f'{stay_id}:{contract_id}:d{seq}:{bill_type}'
    if db.execute('SELECT 1 FROM bill WHERE bill_id=?', (bill_id,)).fetchone():
        return bill_id
    # Reference the latest instrument: a prior formal/correction bill, or the
    # prepayment voucher this correction settles against.
    old = _prior_formal_bill(db, stay_id, contract_id) or _prior_any_bill(db, stay_id, contract_id)
    db.execute('INSERT INTO bill VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)',
               (bill_id, stay_id, contract_id, bill_type, amount, decision_id,
                old['bill_id'] if old else None))
    db.execute('INSERT INTO stay_ledger_entry(stay_id,contract_id,kind,amount_minor,bill_id,decision_id) '
               'VALUES (?,?,?,?,?,?)', (stay_id, contract_id, kind, amount, bill_id, decision_id))
    return bill_id


def _write_refund(db, stay_id, contract_id, seq, amount, decision_id, prior):
    bill_id = f'{stay_id}:{contract_id}:d{seq}:correction'
    if not db.execute('SELECT 1 FROM bill WHERE bill_id=?', (bill_id,)).fetchone():
        old = _prior_formal_bill(db, stay_id, contract_id) or _prior_any_bill(db, stay_id, contract_id)
        db.execute('INSERT INTO bill VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)',
                   (bill_id, stay_id, contract_id, 'correction', amount, decision_id,
                    old['bill_id'] if old else None))
        db.execute('INSERT INTO stay_ledger_entry(stay_id,contract_id,kind,amount_minor,bill_id,decision_id) '
                   'VALUES (?,?,?,?,?,?)', (stay_id, contract_id, 'refund', -amount, bill_id, decision_id))
    return bill_id


def _decision_payload(row, note: str) -> dict:
    payload = dict(row)
    durations = json.loads(payload.pop('feasible_durations_json'))
    payload['feasible_amounts_minor'] = json.loads(payload.pop('feasible_amounts_json'))
    payload['feasible_duration_summary_seconds'] = {
        'min': durations[0] if durations else None,
        'max': durations[-1] if durations else None,
        'distinct_count': len(durations)}
    payload['calibration_refs'] = json.loads(payload.pop('calibration_refs_json'))
    payload['tariff_refs'] = json.loads(payload.pop('tariff_refs_json'))
    payload['note'] = note
    return payload


def adjudicate_all(db, contract_id: str | None = None) -> dict:
    stay_ids = [r['stay_id'] for r in rows(db, 'SELECT DISTINCT stay_id FROM gate_event ORDER BY stay_id')]
    if contract_id is None:
        contract_ids = [r['contract_id'] for r in
                        rows(db, 'SELECT DISTINCT contract_id FROM contract_revision ORDER BY contract_id')]
    else:
        contract_ids = [contract_id]
    rulings = [adjudicate(db, stay_id, cid) for cid in contract_ids for stay_id in stay_ids]
    return {'rulings': rulings, 'stays': len(stay_ids), 'contracts': len(contract_ids)}


# --------------------------------------------------------------------------- #
# evidence-chain reports
# --------------------------------------------------------------------------- #
def stay_report(db, stay_id: str) -> dict:
    ev = stay_events(db, stay_id)
    chain = []
    for event in ev['all']:
        cals = covering_calibrations(db, event['gate_id'], event['occurred_at']) if event['gate_id'] else []
        chain.append({'event_id': event['event_id'], 'direction': event['direction'],
                      'device_time': event['occurred_at'], 'gate_id': event['gate_id'],
                      'calibration_evidence': [
                          {'event_id': c['event_id'], 'valid_from': c['valid_from'], 'valid_to': c['valid_to'],
                           'offset_seconds': [c['offset_seconds_min'], c['offset_seconds_max']]}
                          for c in cals]})
    decisions = rows(db, 'SELECT * FROM billing_decision WHERE stay_id=? ORDER BY decision_seq', (stay_id,))
    for d in decisions:
        d['feasible_amounts_minor'] = json.loads(d.pop('feasible_amounts_json'))
        d['feasible_durations_seconds'] = json.loads(d.pop('feasible_durations_json'))
        d['calibration_refs'] = json.loads(d.pop('calibration_refs_json'))
        d['tariff_refs'] = json.loads(d.pop('tariff_refs_json'))
        d['bills'] = rows(db, 'SELECT * FROM bill WHERE decision_id=? ORDER BY bill_id', (d['decision_id'],))
        d['ledger_entries'] = rows(db, 'SELECT * FROM stay_ledger_entry WHERE decision_id=? ORDER BY entry_id',
                                   (d['decision_id'],))
    contracts = sorted({d['contract_id'] for d in decisions})
    balances = {cid: ledger_balance(db, stay_id, cid) for cid in contracts}
    return {'stay_id': stay_id, 'gate_events': chain, 'decisions': decisions,
            'ledger_balances_minor': balances}


def billing_report(db) -> dict:
    stay_ids = [r['stay_id'] for r in rows(db, 'SELECT DISTINCT stay_id FROM gate_event ORDER BY stay_id')]
    stays = []
    for stay_id in stay_ids:
        ev = stay_events(db, stay_id)
        decisions = rows(db, 'SELECT contract_id,status,decision_seq,reason,billed_minor,guaranteed_minor '
                             'FROM billing_decision WHERE stay_id=? ORDER BY decision_seq', (stay_id,))
        contracts = {}
        for d in decisions:
            cid = d.pop('contract_id')
            d['balance_minor'] = ledger_balance(db, stay_id, cid)
            contracts.setdefault(cid, []).append(d)
        stays.append({'stay_id': stay_id,
                      'admission': admission_reason(ev) or 'admissible',
                      'contracts': contracts})
    return {'stays': stays,
            'total_collected_minor': int(db.execute('SELECT COALESCE(SUM(amount_minor),0) FROM stay_ledger_entry')
                                         .fetchone()[0])}
