"""Charging-point evidence and independent public charging decisions.

Three local evidence ledgers feed two display contracts that never reuse the
plain bay-availability number:

- equipment: gun -> connectable bays, supported charging types with power,
  hosting circuit and the circuit rating;
- fault/recovery events kept by occurrence time and source identity, so a
  late-arriving older event never overrides a newer state;
- charging sessions naming bay, gun, type and drawn power, checked against
  the gun's bays and circuit; re-reports are idempotent, same id with new
  content conflicts.

Contracts (caller picks explicitly):
- ``single``: which bays each fit one such vehicle right now;
- ``fleet``: the maximum number of such vehicles servable simultaneously,
  solved as a max-flow assignment over bays, guns and circuit slots.
"""
from __future__ import annotations

import json
from collections import deque

from . import capacity, report, sensor
from .sources import record
from .store import rows

CONTRACTS = ('single', 'fleet')
_EPS = 1e-9


def ingest(db, events: list[dict]) -> dict:
    made = 0
    with db:
        for e in events:
            if e.get('op') not in ('equipment', 'fault', 'session'):
                raise ValueError(f"unknown charging op {e.get('op')}")
        # Equipment lands first so sessions in the same batch can be checked.
        for op, apply in (('equipment', _equipment), ('fault', _fault), ('session', _session)):
            for e in events:
                if e.get('op') == op:
                    made += apply(db, e)
    return {'imported': made, 'guns': len(rows(db, 'SELECT gun_id FROM charger_registry'))}


def _equipment(db, e: dict) -> int:
    bays = e.get('bays')
    if not isinstance(bays, list) or not bays or not all(isinstance(b, str) and b for b in bays):
        raise ValueError('equipment needs a non-empty bays list')
    charging = e.get('charging')
    if not isinstance(charging, list) or not charging:
        raise ValueError('equipment needs supported charging entries')
    for c in charging:
        if not isinstance(c, dict) or not c.get('type') or float(c.get('power_kw', 0)) <= 0:
            raise ValueError('charging entry needs a type and positive power_kw')
    if not e.get('circuit_id'):
        raise ValueError('equipment needs circuit_id')
    if float(e.get('circuit_rating_kw', 0)) <= 0:
        raise ValueError('circuit_rating_kw must be positive')
    if not record(db, e, 'charger_equipment', e['gun_id']):
        return 0
    norm = sorted(({'type': c['type'], 'power_kw': float(c['power_kw'])} for c in charging),
                  key=lambda c: (c['type'], c['power_kw']))
    bays_json = json.dumps(sorted(set(bays)))
    charging_json = json.dumps(norm)
    db.execute('INSERT INTO charger_revision VALUES (?,?,?,?,?,?,?)',
               (e['event_id'], e['gun_id'], bays_json, charging_json,
                e['circuit_id'], float(e['circuit_rating_kw']), e['event_time']))
    old = db.execute('SELECT valid_from FROM charger_registry WHERE gun_id=?', (e['gun_id'],)).fetchone()
    if old is None or e['event_time'] >= old['valid_from']:
        db.execute('INSERT INTO charger_registry(gun_id,bays_json,charging_json,circuit_id,circuit_rating_kw,valid_from,source_event)'
                   ' VALUES (?,?,?,?,?,?,?) ON CONFLICT(gun_id) DO UPDATE SET bays_json=excluded.bays_json,'
                   'charging_json=excluded.charging_json,circuit_id=excluded.circuit_id,'
                   'circuit_rating_kw=excluded.circuit_rating_kw,valid_from=excluded.valid_from,source_event=excluded.source_event',
                   (e['gun_id'], bays_json, charging_json, e['circuit_id'],
                    float(e['circuit_rating_kw']), e['event_time'], e['event_id']))
    return 1


def _fault(db, e: dict) -> int:
    if e.get('state') not in ('fault', 'recovery'):
        raise ValueError('fault state must be fault or recovery')
    if not e.get('source'):
        raise ValueError('fault event needs a source identity')
    if not record(db, e, 'charger_fault', e['gun_id']):
        return 0
    db.execute('INSERT INTO charger_fault VALUES (?,?,?,?,?)',
               (e['event_id'], e['gun_id'], e['state'], e['source'], e['event_time']))
    return 1


def _session(db, e: dict) -> int:
    if float(e.get('power_kw', 0)) <= 0:
        raise ValueError('session power_kw must be positive')
    if e.get('ended_at') is not None and not e['started_at'] < e['ended_at']:
        raise ValueError('session end must be after its start')
    gun = _gun_at(db, e['gun_id'], e['event_time'])
    if gun is None:
        raise ValueError(f"session references unknown gun {e['gun_id']}")
    if e['bay_id'] not in json.loads(gun['bays_json']):
        raise ValueError(f"bay {e['bay_id']} is not connectable by gun {e['gun_id']}")
    if e.get('circuit_id') is not None and e['circuit_id'] != gun['circuit_id']:
        raise ValueError(f"session circuit {e['circuit_id']} does not match gun circuit {gun['circuit_id']}")
    types = {c['type'] for c in json.loads(gun['charging_json'])}
    if e['charging_type'] not in types:
        raise ValueError(f"gun {e['gun_id']} does not support {e['charging_type']}")
    if not record(db, e, 'charge_session', e['session_id']):
        return 0
    db.execute('INSERT INTO charge_session VALUES (?,?,?,?,?,?,?,?,?)',
               (e['event_id'], e['session_id'], e['bay_id'], e['gun_id'], e['charging_type'],
                float(e['power_kw']), e['started_at'], e.get('ended_at'), e['event_time']))
    return 1


def _gun_at(db, gun_id: str, as_of: str | None = None) -> dict | None:
    if as_of is None:
        row = db.execute('SELECT * FROM charger_revision WHERE gun_id=? '
                         'ORDER BY valid_from DESC,event_id DESC LIMIT 1', (gun_id,)).fetchone()
    else:
        row = db.execute('SELECT * FROM charger_revision WHERE gun_id=? AND valid_from<=? '
                         'ORDER BY valid_from DESC,event_id DESC LIMIT 1', (gun_id, as_of)).fetchone()
    return dict(row) if row else None


def _health_at(db, gun_id: str, as_of: str) -> dict:
    row = db.execute('SELECT * FROM charger_fault WHERE gun_id=? AND occurred_at<=? '
                     'ORDER BY occurred_at DESC,event_id DESC LIMIT 1', (gun_id, as_of)).fetchone()
    if row is None:
        return {'healthy': True, 'state': 'healthy', 'event_id': None, 'source': None, 'occurred_at': None}
    return {'healthy': row['state'] == 'recovery', 'state': row['state'], 'event_id': row['event_id'],
            'source': row['source'], 'occurred_at': row['occurred_at']}


def _active_sessions(db, as_of: str) -> list[dict]:
    latest = {}
    for r in rows(db, 'SELECT * FROM charge_session ORDER BY event_time DESC,event_id DESC'):
        latest.setdefault(r['session_id'], r)
    return [r for r in latest.values()
            if r['started_at'] <= as_of and (r['ended_at'] is None or as_of < r['ended_at'])]


def _evaluate(db, as_of: str, charging_type: str, min_power_kw: float, timeout_minutes: int) -> dict:
    guns = {}
    for row in rows(db, 'SELECT DISTINCT gun_id FROM charger_revision'):
        rev = _gun_at(db, row['gun_id'], as_of)
        if rev is not None:
            guns[row['gun_id']] = rev
    sessions = _active_sessions(db, as_of)
    busy_guns = {s['gun_id'] for s in sessions}
    busy_bays = {s['bay_id'] for s in sessions}
    cap = capacity.snapshot(db, as_of)
    reserved = set(cap['reserved_bays'])
    # Circuit rating: the most conservative rating reported for the circuit.
    rating, used = {}, {}
    for rev in guns.values():
        cid = rev['circuit_id']
        rating[cid] = min(rating.get(cid, float('inf')), float(rev['circuit_rating_kw']))
    session_view = []
    for s in sorted(sessions, key=lambda r: (r['session_id'], r['event_id'])):
        rev = _gun_at(db, s['gun_id'], as_of) or _gun_at(db, s['gun_id'])
        cid = rev['circuit_id'] if rev else None
        if cid is not None:
            rating.setdefault(cid, float(rev['circuit_rating_kw']))
            used[cid] = used.get(cid, 0.0) + float(s['power_kw'])
        session_view.append({'session_id': s['session_id'], 'event_id': s['event_id'],
                             'bay_id': s['bay_id'], 'gun_id': s['gun_id'], 'circuit_id': cid,
                             'charging_type': s['charging_type'], 'power_kw': s['power_kw'],
                             'started_at': s['started_at'], 'ended_at': s['ended_at']})
    circuits = {}
    for cid in sorted(rating):
        remaining = rating[cid] - used.get(cid, 0.0)
        circuits[cid] = {'rating_kw': rating[cid], 'used_kw': round(used.get(cid, 0.0), 9),
                         'remaining_kw': round(remaining, 9),
                         'slots': max(0, int((remaining + _EPS) // min_power_kw))}
    gun_view = {}
    bay_guns = {}
    for gid in sorted(guns):
        rev = guns[gid]
        health = _health_at(db, gid, as_of)
        supports = any(c['type'] == charging_type and float(c['power_kw']) >= min_power_kw
                       for c in json.loads(rev['charging_json']))
        idle = gid not in busy_guns
        circuit_ok = circuits[rev['circuit_id']]['slots'] >= 1
        usable = supports and health['healthy'] and idle and circuit_ok
        gun_view[gid] = {'revision_event': rev['event_id'], 'circuit_id': rev['circuit_id'],
                         'healthy': health['healthy'], 'health_event': health['event_id'],
                         'health_source': health['source'], 'idle': idle,
                         'supports_request': supports, 'circuit_has_power': circuit_ok,
                         'usable': usable}
        if usable:
            for bay in json.loads(rev['bays_json']):
                bay_guns.setdefault(bay, []).append(gid)
    sensor_states = {bay: sensor.bay_state(db, bay, as_of, timeout_minutes) for bay in sorted(bay_guns)}
    candidates, pairs, undetermined, undetermined_pairs = [], [], [], []
    for bay in sorted(bay_guns):
        if bay in busy_bays or bay in reserved:
            continue
        state = sensor_states[bay]['state']
        if state == 'free':
            candidates.append({'bay_id': bay, 'guns': bay_guns[bay]})
            pairs.extend((bay, g) for g in bay_guns[bay])
        elif state == 'unknown':
            undetermined.append(bay)
            undetermined_pairs.extend((bay, g) for g in bay_guns[bay])
    evidence = {'sensor': sensor_states,
                'capacity': {'reserved_bays': cap['reserved_bays'], 'active_orders': cap['active_orders']},
                'guns': gun_view, 'sessions': session_view, 'circuits': circuits,
                'undetermined_bays': undetermined}
    return {'candidates': candidates, 'pairs': pairs,
            'undetermined_bays': undetermined, 'undetermined_pairs': undetermined_pairs,
            'circuit_slots': {cid: c['slots'] for cid, c in circuits.items()},
            'gun_circuit': {gid: guns[gid]['circuit_id'] for gid in guns},
            'evidence': evidence}


def _max_assignment(pairs: list[tuple[str, str]], circuit_slots: dict, gun_circuit: dict):
    """Largest bay<->gun matching with per-circuit vehicle slots (max-flow)."""
    edge_set = set(pairs)
    bays = sorted({b for b, _ in edge_set})
    guns = sorted({g for _, g in edge_set})
    cap = {}

    def add(u, v, c):
        cap.setdefault(u, {})
        cap.setdefault(v, {})
        cap[u][v] = cap[u].get(v, 0) + c
        cap[v].setdefault(u, 0)

    src, snk = '@src', '@sink'
    for b in bays:
        add(src, 'b:' + b, 1)
    for b, g in edge_set:
        add('b:' + b, 'g:' + g, 1)
    for g in guns:
        add('g:' + g, 'l:' + gun_circuit[g], 1)
    for cid, n in circuit_slots.items():
        if n > 0:
            add('l:' + cid, snk, n)
    total = 0
    while True:
        prev = {src: None}
        queue = deque([src])
        while queue:
            u = queue.popleft()
            if u == snk:
                break
            for v in sorted(cap.get(u, {})):
                if cap[u][v] > 0 and v not in prev:
                    prev[v] = u
                    queue.append(v)
        if snk not in prev:
            break
        v = snk
        while v != src:
            u = prev[v]
            cap[u][v] -= 1
            cap[v][u] += 1
            v = u
        total += 1
    assignment = [{'bay_id': b, 'gun_id': g} for b, g in sorted(edge_set)
                  if cap.get('b:' + b, {}).get('g:' + g, 0) == 0
                  and cap.get('g:' + g, {}).get('b:' + b, 0) > 0]
    return total, assignment


def decide(db, as_of: str, total_bays: int, charging_type: str, min_power_kw: float,
           contract: str, timeout_minutes: int = 30, save: bool = True) -> dict:
    if contract not in CONTRACTS:
        raise ValueError(f'contract must be one of {CONTRACTS}')
    if not charging_type:
        raise ValueError('charging_type is required')
    min_power_kw = float(min_power_kw)
    if min_power_kw <= 0:
        raise ValueError('min_power_kw must be positive')
    regular = report.decide(db, as_of, total_bays, timeout_minutes, save=False)
    found = _evaluate(db, as_of, charging_type, min_power_kw, timeout_minutes)
    evidence = {'regular_report': regular,
                'gate': {'active': regular['gate']['active'], 'complete': regular['gate']['complete'],
                         'orphan': regular['gate']['orphan'], 'duplicate': regular['gate']['duplicate']},
                **found['evidence']}
    if regular['state'] in ('frozen', 'capacity_breach'):
        # A frozen plain-availability number muzzles the charging number too.
        state = 'withheld'
        reason = f"regular_availability_{regular['state']}:{regular['reason']}"
        published = None
    elif contract == 'single':
        if not found['candidates'] and found['undetermined_bays']:
            state, reason, published = 'unknown', 'sensor_unknown', None
        else:
            state, reason = 'published', 'bay_level_evidence'
            published = {'candidates': found['candidates'], 'candidate_count': len(found['candidates'])}
    else:
        lower, assignment = _max_assignment(found['pairs'], found['circuit_slots'], found['gun_circuit'])
        if found['undetermined_bays']:
            upper, _ = _max_assignment(found['pairs'] + found['undetermined_pairs'],
                                       found['circuit_slots'], found['gun_circuit'])
        else:
            upper = lower
        if upper != lower:
            state, reason, published = 'unknown', 'sensor_unknown', None
        else:
            state, reason = 'published', 'max_flow_assignment'
            published = {'max_vehicles': lower, 'assignment': assignment}
    result = {'as_of': as_of, 'contract': contract, 'charging_type': charging_type,
              'min_power_kw': min_power_kw, 'state': state, 'reason': reason,
              'published': published, 'evidence': evidence}
    if contract == 'single':
        result['candidates'] = published['candidates'] if published else None
        result['candidate_count'] = published['candidate_count'] if published else None
    else:
        result['max_vehicles'] = published['max_vehicles'] if published else None
        result['assignment'] = published['assignment'] if published else None
    if save:
        with db:
            cur = db.execute('INSERT INTO charging_release(as_of,contract,charging_type,min_power_kw,state,reason,published,evidence)'
                             ' VALUES (?,?,?,?,?,?,?,?)',
                             (as_of, contract, charging_type, min_power_kw, state, reason,
                              json.dumps(published, ensure_ascii=False, sort_keys=True) if published is not None else None,
                              json.dumps(result, ensure_ascii=False, sort_keys=True)))
        result['release_id'] = cur.lastrowid
    return result


def history(db) -> list[dict]:
    return rows(db, 'SELECT release_id,as_of,contract,charging_type,min_power_kw,state,reason,published,evidence'
                    ' FROM charging_release ORDER BY release_id')
