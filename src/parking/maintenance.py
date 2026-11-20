"""Offline maintenance diagnosis and two one-trip dispatch contracts.

A lost bay heartbeat is a symptom, not a diagnosis. This module relates lost
sensors through a supplied dependency topology, enumerates every minimal fault
explanation inside a pre-registered fault budget, and turns those explanations
into two independent dispatch decisions:

* guaranteed repair -- carry a spare for every node in the union of all viable
  minimal explanations, or state plainly that one trip cannot guarantee repair;
* risk budget -- use independently saved fault frequencies as scenario priors
  and pick a combination inside vehicle capacity whose covered probability
  reaches a registered threshold, publishing the residual risk.

Saved frequencies are priors, never field confirmation. Neither contract
changes any bay state: lost bays stay unknown until field repair.

Evidence semantics (all events are filtered by `event_time <= as_of`):

* A sensor heartbeat loss is a symptom: the bay is `unknown` because its last
  reading is older than the timeout, or because the device is untrusted. The
  corresponding sensor node is "lost".
* A self-test/voltage `ok` naming device X proves X and every ancestor of X is
  currently working -- an upstream `ok` is independent evidence that rules a
  node out even when its downstream children are lost.
* A self-test/voltage `fail` naming device X positively incriminates X; a
  failure phrased against X does not by itself implicate X's parents.
* A candidate explanation is a non-empty node set S such that every lost device
  is S itself or has a member of S among its ancestors (coverage), and no node
  of S is contradicted by `ok` evidence (consistency). Minimality is by set
  inclusion, never by count or input order.
"""
from __future__ import annotations

import itertools
import json

from . import sensor
from .sources import reject_conflicts, record
from .store import rows

KINDS = ('sensor', 'controller', 'gateway', 'power')
RELIABILITIES = ('high', 'medium', 'low')
DEFAULT_MAX_FAULTS = 2
DEFAULT_VEHICLE_CAPACITY = 3
DEFAULT_RISK_THRESHOLD = 0.75
RELIABILITY_RANK = {'high': 3, 'medium': 2, 'low': 1}
_EPSILON = 1e-9


# ----------------------------------------------------------------- input shapes

def _require_text(event: dict, key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{key} is required')
    return value


def _validate_topology(event: dict) -> tuple[list[dict], list[dict]]:
    nodes_raw, edges_raw = event.get('nodes'), event.get('edges')
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ValueError('topology needs nodes')
    if not isinstance(edges_raw, list):
        raise ValueError('topology needs edges')
    nodes, node_ids = [], set()
    for node in nodes_raw:
        node_id = _require_text(node, 'node_id')
        if node.get('kind') not in KINDS:
            raise ValueError('invalid topology node kind')
        spare = node.get('spare_sku')
        if not isinstance(spare, str) or not spare.strip():
            raise ValueError('topology node needs spare_sku')
        if node_id in node_ids:
            raise ValueError(f'duplicate topology node {node_id}')
        node_ids.add(node_id)
        nodes.append({'node_id': node_id, 'kind': node['kind'], 'spare_sku': spare})
    edges, seen_edges = [], set()
    for edge in edges_raw:
        child, parent = _require_text(edge, 'child_id'), _require_text(edge, 'parent_id')
        if child not in node_ids or parent not in node_ids:
            raise ValueError('topology edge references unknown node')
        if child == parent:
            raise ValueError('topology edge cannot be a self-loop')
        if (child, parent) in seen_edges:
            continue
        seen_edges.add((child, parent))
        edges.append({'child_id': child, 'parent_id': parent})
    parents: dict[str, list[str]] = {}
    for edge in edges:
        parents.setdefault(edge['child_id'], []).append(edge['parent_id'])
    # A dependency graph must be acyclic: a cycle would let one fault travel
    # both ways and make ancestor closure meaningless.
    color: dict[str, int] = {}

    def visit(node: str) -> None:
        color[node] = 1
        for nxt in parents.get(node, []):
            if color.get(nxt) == 1:
                raise ValueError('topology contains a dependency cycle')
            if color.get(nxt) is None:
                visit(nxt)
        color[node] = 2

    for node_id in node_ids:
        if color.get(node_id) is None:
            visit(node_id)
    return nodes, edges


def _validate_signal(event: dict) -> dict:
    result = _require_text(event, 'result')
    if result not in ('ok', 'fail', 'inconclusive'):
        raise ValueError('invalid self-test result')
    if _require_text(event, 'signal_kind') not in ('self_test', 'voltage'):
        raise ValueError('invalid signal kind')
    reliability = event.get('reliability')
    if reliability not in RELIABILITIES:
        raise ValueError('invalid signal reliability')
    return {'device_id': _require_text(event, 'device_id'),
            'signal_kind': event['signal_kind'], 'result': result,
            'event_time': _require_text(event, 'event_time'),
            'captured_at': _require_text(event, 'captured_at'),
            'source': _require_text(event, 'source'),
            'reliability': reliability,
            'detail': event.get('detail', {})}


def _validate_frequency(event: dict) -> dict:
    frequency = event.get('frequency')
    if isinstance(frequency, bool) or not isinstance(frequency, (int, float)) or not 0 <= frequency <= 1:
        raise ValueError('frequency must be between 0 and 1')
    return {'device_id': _require_text(event, 'device_id'), 'frequency': float(frequency),
            'event_time': _require_text(event, 'event_time'),
            'source': event.get('source'), 'captured_at': event.get('captured_at')}


# ------------------------------------------------------------------------ ingest

def ingest_topology(db, events: list[dict]) -> dict:
    reject_conflicts(db, events)
    made = 0
    with db:
        for e in events:
            nodes, edges = _validate_topology(e)
            if not record(db, e, 'maintenance_topology', 'lot'):
                continue
            event_id = e['event_id']
            db.execute('INSERT INTO maintenance_topology_snapshot(event_id,event_time) VALUES (?,?)',
                       (event_id, e['event_time']))
            db.executemany('INSERT INTO maintenance_topology_node(event_id,node_id,kind,spare_sku) VALUES (?,?,?,?)',
                           [(event_id, n['node_id'], n['kind'], n['spare_sku']) for n in nodes])
            db.executemany('INSERT INTO maintenance_topology_edge(event_id,child_id,parent_id) VALUES (?,?,?)',
                           [(event_id, x['child_id'], x['parent_id']) for x in edges])
            made += 1
    return {'imported': made, 'topologies': len(rows(db, 'SELECT event_id FROM maintenance_topology_snapshot'))}


def ingest_signals(db, events: list[dict]) -> dict:
    reject_conflicts(db, events)
    made = 0
    with db:
        for e in events:
            signal = _validate_signal(e)
            if not record(db, e, 'maintenance_signal', signal['device_id']):
                continue
            db.execute('INSERT INTO maintenance_signal(event_id,device_id,signal_kind,result,event_time,captured_at,source,reliability,detail_json) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (e['event_id'], signal['device_id'], signal['signal_kind'], signal['result'],
                        signal['event_time'], signal['captured_at'], signal['source'],
                        signal['reliability'], json.dumps(signal['detail'], sort_keys=True)))
            made += 1
    return {'imported': made, 'signals': len(rows(db, 'SELECT event_id FROM maintenance_signal'))}


def ingest_frequencies(db, events: list[dict], source: str | None = None) -> dict:
    reject_conflicts(db, events)
    made = 0
    with db:
        for e in events:
            frequency = _validate_frequency(e)
            if not record(db, e, 'fault_frequency', frequency['device_id']):
                continue
            db.execute('INSERT INTO fault_frequency_revision(event_id,device_id,frequency,event_time,source,captured_at) '
                       'VALUES (?,?,?,?,?,?)',
                       (e['event_id'], frequency['device_id'], frequency['frequency'],
                        frequency['event_time'], frequency['source'] or source, frequency['captured_at']))
            made += 1
    return {'imported': made, 'devices': len(rows(db, 'SELECT DISTINCT device_id FROM fault_frequency_revision'))}


# --------------------------------------------------------------------- topology

def _topology_snapshot(db, as_of: str) -> dict | None:
    snapshot = db.execute(
        'SELECT event_id,event_time FROM maintenance_topology_snapshot WHERE event_time<=? '
        'ORDER BY event_time DESC,event_id DESC LIMIT 1', (as_of,)).fetchone()
    if snapshot is None:
        return None
    nodes = {r['node_id']: {'node_id': r['node_id'], 'kind': r['kind'], 'spare_sku': r['spare_sku']}
             for r in rows(db, 'SELECT node_id,kind,spare_sku FROM maintenance_topology_node WHERE event_id=?',
                           (snapshot['event_id'],))}
    edges = rows(db, 'SELECT child_id,parent_id FROM maintenance_topology_edge WHERE event_id=? '
                     'ORDER BY child_id,parent_id', (snapshot['event_id'],))
    parents: dict[str, list[str]] = {}
    for edge in edges:
        parents.setdefault(edge['child_id'], []).append(edge['parent_id'])
    return {'event_id': snapshot['event_id'], 'event_time': snapshot['event_time'],
            'nodes': nodes, 'edges': edges, 'parents': parents}


def _ancestors(parents: dict, node_id: str) -> set[str]:
    seen, stack = set(), [node_id]
    while stack:
        for parent in parents.get(stack.pop(), []):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


# ---------------------------------------------------------------------- signals

def _signals_as_of(db, as_of: str) -> list[dict]:
    return rows(db, 'SELECT * FROM maintenance_signal WHERE event_time<=? ORDER BY event_time,event_id,result DESC',
                (as_of,))


def _signal_index(signals: list[dict], parents: dict) -> dict:
    """Reduce evidence to the two questions the diagnosis cares about.

    For each (device, signal_kind) the latest signal wins, so a late self-test
    supersedes an earlier one on the same device. `ok_nodes` holds nodes proven
    healthy: a passing self-test or voltage reading proves the named device; a
    live heartbeat additionally proves its ancestors, because live data can only
    have traversed a working dependency chain. A self-test on a child alone does
    not prove its parent forwards. `fail_nodes` holds first-hand failures; an
    `inconclusive` signal removes a prior verdict of the same kind but proves
    nothing.
    """
    latest: dict[tuple[str, str], dict] = {}
    for signal in signals:
        key = (signal['device_id'], signal['signal_kind'])
        current = latest.get(key)
        if current is None or (signal['event_time'], signal['event_id']) > (current['event_time'], current['event_id']):
            latest[key] = signal
    ok_nodes: dict[str, dict] = {}
    fail_nodes: dict[str, dict] = {}
    observed: dict[str, dict] = {}
    for signal in latest.values():
        device = signal['device_id']
        observed[device] = signal
        if signal['result'] == 'ok':
            ok_nodes.setdefault(device, signal)
            if signal['signal_kind'] == 'heartbeat':
                for ancestor in _ancestors(parents, device):
                    ok_nodes.setdefault(ancestor, signal)
        elif signal['result'] == 'fail':
            fail_nodes.setdefault(device, signal)
    return {'ok': ok_nodes, 'fail': fail_nodes, 'observed': observed}


# ---------------------------------------------------------------- explanations

def _covers(nodes: set[str], lost: set[str], parents: dict) -> bool:
    for device in lost:
        if device in nodes:
            continue
        if not (nodes & _ancestors(parents, device)):
            return False
    return True


def _admissible(nodes: set[str], lost: set[str], index: dict, parents: dict) -> bool:
    """A candidate explains every lost device and contradicts no `ok` evidence.

    A node explicitly proven healthy cannot be faulted, even if it is a lost
    device: the loss then has an unexplained cause and is not this node's fault.
    First-hand `fail` evidence does not make a set inadmissible -- it is
    corroboration -- but the set still has to cover all symptoms.
    """
    if not nodes or not lost:
        return False
    if any(node in index['ok'] for node in nodes):
        return False
    return _covers(nodes, lost, parents)


def minimal_explanations(topology: dict, lost: set[str], index: dict, max_faults: int) -> list[dict]:
    """Every inclusion-minimal admissible node set of size <= max_faults.

    The search is exhaustive over the node universe, so results do not depend
    on row order; no explanation is privileged by being first or smallest.
    """
    candidates = sorted(topology['nodes'])
    parents = topology['parents']
    viable: list[set[str]] = []
    for size in range(1, max_faults + 1):
        for combo in itertools.combinations(candidates, size):
            nodes = set(combo)
            if _admissible(nodes, lost, index, parents):
                viable.append(nodes)
    minimal = [nodes for nodes in viable if not any(other < nodes for other in viable)]
    return [{'nodes': sorted(nodes),
             'basis': 'covers_lost' + ('+first_hand_fail' if any(n in index['fail'] for n in nodes) else '')}
            for nodes in sorted(minimal, key=lambda s: (len(s), sorted(s)))]


def _is_contradicted(nodes: set[str], index: dict) -> bool:
    return any(node in index['ok'] for node in nodes)


def _limit_reached(topology: dict, lost: set[str], index: dict, max_faults: int,
                   found: list[dict]) -> bool:
    """Flag the case where the fault budget was too small to produce any
    explanation while a larger admissible set exists.

    Minimality is by set inclusion, so any admissible superset of a found
    explanation is non-minimal and is not a "missed" explanation. The only
    truncation worth surfacing is therefore an empty result that a larger budget
    would have filled: the caller should raise `max_faults`, not read the empty
    list as proof that no explanation exists."""
    if found or not lost:
        return False
    candidates = sorted(topology['nodes'])
    for size in range(max_faults + 1, len(candidates) + 1):
        if any(_admissible(set(combo), lost, index, topology['parents'])
               for combo in itertools.combinations(candidates, size)):
            return True
    return False


# -------------------------------------------------------------------- diagnose

def _device_bay_map(db) -> dict[str, str]:
    mapping = {r['device_id']: r['bay_id'] for r in rows(db, 'SELECT device_id,bay_id FROM sensor_registry')}
    for r in rows(db, 'SELECT device_id,bay_id FROM sensor_revision ORDER BY valid_from,event_id'):
        mapping.setdefault(r['device_id'], r['bay_id'])
    return mapping


def _frequencies(db, as_of: str) -> dict[str, dict]:
    revisions = rows(db, 'SELECT * FROM fault_frequency_revision WHERE event_time<=? '
                         'ORDER BY event_time,event_id', (as_of,))
    chosen = {}
    for revision in revisions:
        chosen[revision['device_id']] = revision
    return chosen


def _priors(explanations: list[dict], frequencies: dict) -> tuple[list[float | None], list[float | None], bool]:
    """Scenario weights from saved frequencies.

    The absolute weight of an explanation is the product of its nodes' saved
    frequencies. The reported probability renormalises those weights over the
    explanations that remain viable in this revision, so a scenario excluded by
    new evidence does not keep its old probability. Neither number is field
    confirmation; `confirmed` is decided only by first-hand failure evidence.
    """
    weights = []
    for explanation in explanations:
        weight = 1.0
        for node in explanation['nodes']:
            revision = frequencies.get(node)
            if revision is None:
                return [None] * len(explanations), [None] * len(explanations), False
            weight *= revision['frequency']
        weights.append(weight)
    total = sum(weights)
    if total <= 0:
        return [None] * len(explanations), weights, False
    return [w / total for w in weights], weights, True


def _sku_set(nodes, topo_nodes: dict) -> list[str]:
    """Distinct spare SKUs referenced by a node set (informational display)."""
    return sorted({topo_nodes[node]['spare_sku'] for node in nodes})


def _explanation_multiset(explanation: dict, topo_nodes: dict) -> dict[str, int]:
    """Physical spares one explanation needs, keyed by SKU. Two sensors broken
    by the same upstream fault still need two sensor units, not one shared SKU."""
    counts: dict[str, int] = {}
    for node in explanation['nodes']:
        sku = topo_nodes[node]['spare_sku']
        counts[sku] = counts.get(sku, 0) + 1
    return counts


def _required_multiset(explanations: list[dict], topo_nodes: dict) -> dict[str, int]:
    """The smallest carry that dominates every explanation: for each SKU, the
    largest number of units any single explanation needs. A guaranteed repair
    must survive whichever explanation is true, so this is the required carry."""
    required: dict[str, int] = {}
    for explanation in explanations:
        for sku, count in _explanation_multiset(explanation, topo_nodes).items():
            required[sku] = max(required.get(sku, 0), count)
    return required


def _expand(counts: dict[str, int]) -> list[str]:
    return sorted(sku for sku, count in counts.items() for _ in range(count))


# ------------------------------------------------------------------- contracts

def _guaranteed_contract(explanations: list[dict], topo_nodes: dict, capacity: int) -> dict:
    if not explanations:
        return {'contract': 'guaranteed_repair', 'decision': 'no_viable_explanation',
                'parts': [], 'coverage_probability': None, 'risk_threshold': None,
                'uncovered': [], 'capacity': capacity,
                'reason': 'no consistent minimal explanation within the fault budget'}
    required = _required_multiset(explanations, topo_nodes)
    parts = _expand(required)
    if len(parts) > capacity:
        return {'contract': 'guaranteed_repair', 'decision': 'cannot_guarantee_one_trip',
                'parts': parts, 'coverage_probability': 1.0, 'risk_threshold': None,
                'uncovered': [], 'capacity': capacity,
                'reason': f'{len(parts)} spares are required to cover every viable explanation, exceeding capacity {capacity}'}
    return {'contract': 'guaranteed_repair', 'decision': 'dispatch_with_guarantee',
            'parts': parts, 'coverage_probability': 1.0, 'risk_threshold': None,
            'uncovered': [], 'capacity': capacity,
            'reason': 'carried spares cover every viable minimal explanation'}


def _covers_explanation(carried: dict[str, int], explanation: dict, topo_nodes: dict) -> bool:
    needed = _explanation_multiset(explanation, topo_nodes)
    return all(carried.get(sku, 0) >= count for sku, count in needed.items())


def _candidate_carries(explanations: list[dict], topo_nodes: dict, capacity: int):
    """Every physical spare combination inside vehicle capacity, as SKU counts.
    Only SKUs that appear in at least one viable explanation can help."""
    skus = sorted({topo_nodes[node]['spare_sku'] for explanation in explanations
                   for node in explanation['nodes']})
    for counts in itertools.product(range(capacity + 1), repeat=len(skus)):
        total = sum(counts)
        if 0 < total <= capacity:
            yield {sku: count for sku, count in zip(skus, counts) if count}


def _risk_contract(explanations: list[dict], probabilities: list[float | None], topo_nodes: dict,
                   capacity: int, threshold: float, complete: bool) -> dict:
    if not explanations:
        return {'contract': 'risk_budget', 'decision': 'no_viable_explanation',
                'parts': [], 'coverage_probability': None, 'risk_threshold': threshold,
                'uncovered': [], 'capacity': capacity,
                'reason': 'no consistent minimal explanation within the fault budget'}
    if not complete:
        return {'contract': 'risk_budget', 'decision': 'insufficient_frequency_evidence',
                'parts': [], 'coverage_probability': None, 'risk_threshold': threshold,
                'uncovered': [], 'capacity': capacity,
                'reason': 'every explanation node needs an independently saved frequency'}
    best = None
    for carried in _candidate_carries(explanations, topo_nodes, capacity):
        covered = sum(p for explanation, p in zip(explanations, probabilities)
                      if _covers_explanation(carried, explanation, topo_nodes))
        missing = [explanation for explanation, p in zip(explanations, probabilities)
                   if not _covers_explanation(carried, explanation, topo_nodes)]
        parts = _expand(carried)
        # Prefer the highest coverage, then the fewest parts, then a stable
        # lexicographic order so the choice never depends on row order.
        rank = (1.0 - covered, len(parts), tuple(parts))
        if best is None or rank < best[0]:
            best = (rank, {'carried': carried, 'parts': parts, 'covered': covered, 'missing': missing})
    if best is None:
        return {'contract': 'risk_budget', 'decision': 'no_capacity_feasible_option',
                'parts': [], 'coverage_probability': 0.0, 'risk_threshold': threshold,
                'uncovered': [{'nodes': e['nodes'], 'probability': p}
                              for e, p in zip(explanations, probabilities)],
                'capacity': capacity, 'reason': 'no spare combination fits vehicle capacity'}
    chosen = best[1]
    coverage = chosen['covered']
    residual = 1.0 - coverage
    uncovered = [{'nodes': e['nodes'], 'probability': p}
                 for e, p in zip(explanations, probabilities) if e in chosen['missing']]
    # The registered threshold is a *minimum coverage probability*. A residual
    # may still be printed, but it is never allowed to stand in for the gate.
    if coverage + _EPSILON < threshold:
        decision = 'cannot_meet_risk_threshold'
        reason = (f'best in-capacity coverage {coverage:.4f} is below required '
                  f'threshold {threshold:.4f}')
    else:
        decision = 'dispatch_on_risk'
        reason = (f'carried spares cover probability {coverage:.4f} at or above '
                  f'threshold {threshold:.4f}; residual {residual:.4f} remains')
    return {'contract': 'risk_budget', 'decision': decision, 'parts': chosen['parts'],
            'coverage_probability': coverage, 'residual_probability': residual,
            'risk_threshold': threshold, 'uncovered': uncovered, 'capacity': capacity,
            'reason': reason, 'carried_by_sku': chosen['carried']}


def _online_health(db, lost: set[str], as_of: str, timeout_minutes: int) -> list[dict]:
    """Devices whose latest reading is within the deadline are alive now. That is
    direct, topical proof the device and its ancestors were forwarding at the
    outage -- the evidence that rules out a whole-gateway fault when another
    controller behind the same gateway is still reporting."""
    seen, signals = set(), []
    for row in rows(db, 'SELECT device_id,observed_at FROM occupancy_event WHERE observed_at<=? '
                        'ORDER BY observed_at DESC,event_id DESC', (as_of,)):
        device = row['device_id']
        if device in seen:
            continue
        seen.add(device)
        if device in lost:
            continue
        elapsed = (_parse(as_of) - _parse(row['observed_at'])).total_seconds() / 60
        if elapsed > timeout_minutes:
            continue
        signals.append({'event_id': f'heartbeat:{device}:{row["observed_at"]}',
                        'device_id': device, 'signal_kind': 'heartbeat', 'result': 'ok',
                        'event_time': row['observed_at'], 'captured_at': as_of,
                        'source': 'sensor_heartbeat', 'reliability': 'high', 'detail': {}})
    return signals


def _parse(value: str):
    from datetime import datetime
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def diagnose(db, as_of: str, *, timeout_minutes: int = 30, max_faults: int = DEFAULT_MAX_FAULTS,
             vehicle_capacity: int = DEFAULT_VEHICLE_CAPACITY,
             risk_threshold: float = DEFAULT_RISK_THRESHOLD, save: bool = True) -> dict:
    if max_faults < 1:
        raise ValueError('max_faults must be at least 1')
    if vehicle_capacity < 0:
        raise ValueError('vehicle_capacity cannot be negative')
    if not 0 <= risk_threshold <= 1:
        raise ValueError('risk_threshold must be between 0 and 1')
    topology = _topology_snapshot(db, as_of)
    if topology is None:
        raise ValueError('no maintenance topology available at as_of')
    topo_nodes = topology['nodes']
    parents = topology['parents']
    states = sensor.snapshot(db, as_of, timeout_minutes)['states']
    losses = sensor.heartbeat_losses(db, as_of, timeout_minutes)
    lost_bays = sorted(loss['bay_id'] for loss in losses)
    # The device is the one that actually produced the last reading for the bay;
    # the sensor registry still owns the bay binding and is never rewritten here.
    lost = {loss['device_id'] for loss in losses if loss['device_id']} & set(topo_nodes)
    unrelated_lost = sorted({loss['device_id'] for loss in losses
                             if loss['device_id'] and loss['device_id'] not in topo_nodes})
    no_reading_bays = sorted(bay for bay, state in states.items() if state['reason'] == 'no_reading')

    signals = _signals_as_of(db, as_of)
    index = _signal_index(signals, parents)
    # A device that is currently online is direct proof that it and its
    # ancestors work. This is what rules out a whole-gateway fault when another
    # controller behind the same gateway is still reporting.
    online = _signal_index(_online_health(db, lost, as_of, timeout_minutes), parents)
    for node, signal in online['ok'].items():
        index['ok'].setdefault(node, signal)
    explanations = minimal_explanations(topology, lost, index, max_faults)
    # Explanations that cover every symptom but are ruled out by independent
    # evidence are still reported, with the evidence that excluded them, so the
    # audit trail shows why e.g. a whole-gateway fault was dropped.
    eliminated = []
    for size in range(1, max_faults + 1):
        for combo in itertools.combinations(sorted(topo_nodes), size):
            nodes = set(combo)
            if not _covers(nodes, lost, parents):
                continue
            if any(set(e['nodes']) == nodes for e in explanations):
                continue
            if any(set(other) < nodes for other in (set(e['nodes']) for e in explanations)):
                continue
            blockers = sorted(node for node in nodes if node in index['ok'])
            if blockers:
                eliminated.append({'nodes': sorted(nodes),
                                   'excluded_by': sorted({index['ok'][n]['event_id'] for n in blockers})})
    frequencies = _frequencies(db, as_of)
    probabilities, weights, complete = _priors(explanations, frequencies)
    confirmed_sets = [e['nodes'] for e in explanations
                      if all(node in index['fail'] for node in e['nodes'])]
    guaranteed = _guaranteed_contract(explanations, topo_nodes, vehicle_capacity)
    risk = _risk_contract(explanations, probabilities, topo_nodes, vehicle_capacity, risk_threshold, complete)

    explanation_rows = []
    for number, explanation in enumerate(explanations, start=1):
        explanation_rows.append({'number': number, 'nodes': explanation['nodes'],
                                 'spare_skus': _sku_set(explanation['nodes'], topo_nodes),
                                 'prior_weight': weights[number - 1],
                                 'probability': probabilities[number - 1],
                                 'confirmed': explanation['nodes'] in confirmed_sets,
                                 'basis': explanation['basis']})

    # Which explanations keep each lost bay unknown. A confirmed upstream power
    # fault changes the *cause* text but never the bay state itself.
    bay_causes = {}
    # The history-preserving reverse map: a device may be re-bound to a new bay
    # later, but the cause of *this* bay's silence is the device that last read it.
    reverse = {}
    for device, bay in sorted(_device_bay_map(db).items()):
        reverse.setdefault(bay, device)
    for loss in losses:
        bay = loss['bay_id']
        device = loss['device_id'] or reverse.get(bay)
        if device is None or device not in topo_nodes:
            bay_causes[bay] = {'bay_id': bay, 'device_id': device, 'reason': states[bay]['reason'],
                               'state': states[bay]['state'], 'nodes': []}
            continue
        explaining = [e['nodes'] for e in explanations
                      if device in e['nodes'] or set(e['nodes']) & _ancestors(parents, device)]
        nodes = sorted({node for combo in explaining for node in combo})
        power = [n for n in nodes if topo_nodes[n]['kind'] == 'power' and n in index['fail']]
        reason = 'upstream_power_fault' if power else states[bay]['reason']
        bay_causes[bay] = {'bay_id': bay, 'device_id': device, 'reason': reason,
                           'bay_state_reason': states[bay]['reason'],
                           'state': states[bay]['state'], 'nodes': nodes}

    confirmed_nodes = sorted({node for combo in confirmed_sets for node in combo})
    result = {
        'as_of': as_of,
        'topology_event': topology['event_id'],
        'max_faults': max_faults,
        'vehicle_capacity': vehicle_capacity,
        'lost_devices': sorted(lost),
        'unrelated_lost_devices': unrelated_lost,
        'healthy_devices': sorted(set(index['ok']) & set(topo_nodes)),
        'failed_devices': sorted(set(index['fail']) & set(topo_nodes)),
        'lost_bays': lost_bays,
        'no_reading_bays': no_reading_bays,
        'explanation_count': len(explanation_rows),
        'explanations': explanation_rows,
        'explanation_limit_reached': _limit_reached(topology, lost, index, max_faults, explanations),
        'eliminated': eliminated,
        'confirmed_nodes': confirmed_nodes,
        'bay_causes': bay_causes,
        'guaranteed_repair': guaranteed,
        'risk_budget': risk,
        'frequency_complete': complete,
        'contracts_disagree': guaranteed['decision'] != risk['decision'],
    }
    if save:
        result.update(_persist(db, as_of, timeout_minutes, max_faults, vehicle_capacity,
                               risk_threshold, topology['event_id'], lost_bays, sorted(lost),
                               result['healthy_devices'], result['failed_devices'],
                               explanation_rows, bay_causes, guaranteed, risk))
    return result


def _persist(db, as_of, timeout_minutes, max_faults, vehicle_capacity, risk_threshold,
             topology_event, lost_bays, lost_devices, healthy_nodes, failed_nodes,
             explanation_rows, bay_causes, guaranteed, risk) -> dict:
    # Identity of a diagnosis is its complete evidence state, not just its
    # query. A late self-test arriving at the same as_of changes the evidence
    # and therefore produces a new revision; the earlier revision (and its
    # explanations) stays in the ledger. Replaying the identical inputs is
    # idempotent. `late_evidence` lists what changed since the previous run.
    signal_events = [r['event_id'] for r in
                     rows(db, 'SELECT event_id FROM maintenance_signal WHERE event_time<=? ORDER BY event_id', (as_of,))]
    frequency_events = [r['event_id'] for r in
                        rows(db, 'SELECT event_id FROM fault_frequency_revision WHERE event_time<=? ORDER BY event_id', (as_of,))]
    fingerprint = json.dumps({
        'as_of': as_of, 'timeout': timeout_minutes, 'max_faults': max_faults,
        'capacity': vehicle_capacity, 'threshold': risk_threshold, 'topology': topology_event,
        'lost': lost_bays, 'signals': signal_events, 'frequencies': frequency_events,
    }, sort_keys=True)
    previous = db.execute('SELECT diagnosis_id,revision,failed_json FROM diagnosis_run WHERE as_of=? '
                          'ORDER BY revision DESC,diagnosis_id DESC LIMIT 1', (as_of,)).fetchone()
    with db:
        existing = db.execute('SELECT diagnosis_id FROM diagnosis_run WHERE as_of=? AND fingerprint=? '
                              'ORDER BY diagnosis_id DESC LIMIT 1', (as_of, fingerprint)).fetchone()
        if existing:
            diagnosis_id = existing['diagnosis_id']
            db.execute('DELETE FROM maintenance_dispatch WHERE diagnosis_id=?', (diagnosis_id,))
            db.execute('DELETE FROM diagnosis_explanation WHERE diagnosis_id=?', (diagnosis_id,))
            db.execute('DELETE FROM diagnosis_bay_cause WHERE diagnosis_id=?', (diagnosis_id,))
            db.execute('UPDATE diagnosis_run SET created_at=CURRENT_TIMESTAMP WHERE diagnosis_id=?', (diagnosis_id,))
            revision = db.execute('SELECT revision FROM diagnosis_run WHERE diagnosis_id=?',
                                  (diagnosis_id,)).fetchone()['revision']
        else:
            revision = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM diagnosis_run WHERE as_of=?',
                                  (as_of,)).fetchone()[0]
            diagnosis_id = db.execute(
                'INSERT INTO diagnosis_run(revision,as_of,timeout_minutes,max_faults,vehicle_capacity,risk_threshold,topology_event,status,lost_json,healthy_json,failed_json,fingerprint) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (revision, as_of, timeout_minutes, max_faults, vehicle_capacity, risk_threshold,
                 topology_event, 'consistent' if explanation_rows else 'no_explanation',
                 json.dumps(lost_devices), json.dumps(sorted(healthy_nodes)),
                 json.dumps(sorted(failed_nodes)), fingerprint)).lastrowid
        for row in explanation_rows:
            db.execute('INSERT INTO diagnosis_explanation(diagnosis_id,explanation_no,nodes_json,basis_json,prior_weight,probability,confirmed) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (diagnosis_id, row['number'], json.dumps(row['nodes']), json.dumps(row['basis']),
                        row['prior_weight'], row['probability'], int(row['confirmed'])))
        for bay, cause in bay_causes.items():
            db.execute('INSERT INTO diagnosis_bay_cause(diagnosis_id,bay_id,device_id,reason,nodes_json) VALUES (?,?,?,?,?)',
                       (diagnosis_id, bay, cause['device_id'], cause['reason'], json.dumps(cause['nodes'])))
        for contract in (guaranteed, risk):
            db.execute('INSERT INTO maintenance_dispatch(diagnosis_id,contract,decision,parts_json,coverage_probability,risk_threshold,uncovered_json,capacity,reason) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (diagnosis_id, contract['contract'], contract['decision'],
                        json.dumps(contract.get('parts', [])), contract.get('coverage_probability'),
                        contract.get('risk_threshold'), json.dumps(contract.get('uncovered', []), sort_keys=True),
                        contract['capacity'], contract['reason']))
    return {'diagnosis_id': diagnosis_id, 'revision': revision,
            'supersedes_revision': previous['revision'] if previous and previous['revision'] != revision else None,
            'late_evidence': previous is not None and previous['revision'] != revision}


def revisions(db, as_of: str | None = None) -> list[dict]:
    sql = ('SELECT diagnosis_id,revision,as_of,max_faults,vehicle_capacity,risk_threshold,'
           'status,created_at FROM diagnosis_run')
    args: tuple = ()
    if as_of is not None:
        sql += ' WHERE as_of=?'
        args = (as_of,)
    return rows(db, sql + ' ORDER BY diagnosis_id', args)
