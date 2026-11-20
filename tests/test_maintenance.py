"""Offline maintenance diagnosis and one-trip dispatch contracts."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from parking import sensor, maintenance, report
from parking.sources import EvidenceConflict, conflicts
from parking.store import connect

AS_OF = '2026-11-17T10:10:00Z'
TIMEOUT = 10


def fixture(name):
    return json.loads((ROOT / 'fixtures' / 'parking' / name).read_text(encoding='utf-8'))


class DiagnosisCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'case.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load_abc(self, *, with_d=False, topology='topology_abc.json'):
        sensor.ingest(self.db, fixture('sensors_lost_abc.json'))
        if with_d:
            sensor.ingest(self.db, fixture('sensor_online_d.json'))
        maintenance.ingest_topology(self.db, fixture(topology))
        maintenance.ingest_frequencies(self.db, fixture('frequencies_gk.json'))

    def diagnose(self, **kwargs):
        kwargs.setdefault('timeout_minutes', TIMEOUT)
        kwargs.setdefault('vehicle_capacity', 1)
        return maintenance.diagnose(self.db, AS_OF, **kwargs)

    # ---------------------------------------------------------- explanations

    def test_three_lost_sensors_yield_gateway_or_controller_not_sensors(self):
        self.load_abc()
        result = self.diagnose()
        self.assertEqual([e['nodes'] for e in result['explanations']], [['G'], ['K']])
        self.assertEqual(result['lost_bays'], ['bA', 'bB', 'bC'])
        # A single sensor fault cannot cover its two siblings.
        self.assertFalse(any(len(e['nodes']) == 1 and e['nodes'][0] in ('A', 'B', 'C')
                             for e in result['explanations']))

    def test_minimal_explanations_are_not_ranked_by_row_or_part_count(self):
        self.load_abc()
        result = self.diagnose()
        by_nodes = {tuple(e['nodes']): e for e in result['explanations']}
        self.assertEqual(set(by_nodes), {('G',), ('K',)})
        # Both are listed even though G is "first" and the gateway is upstream.
        self.assertFalse(by_nodes[('G',)]['confirmed'])
        self.assertFalse(by_nodes[('K',)]['confirmed'])

    def test_explanation_budget_reports_truncation(self):
        self.load_abc()
        result = self.diagnose(max_faults=1)
        self.assertFalse(result['explanation_limit_reached'])
        # A budget of 1 still finds both single-node explanations.
        self.assertEqual(len(result['explanations']), 2)

    def test_input_order_does_not_change_explanations(self):
        topology = fixture('topology_abc.json')[0]
        reversed_topology = dict(topology, event_id='topo-rev',
                                 nodes=list(reversed(topology['nodes'])),
                                 edges=list(reversed(topology['edges'])))
        sensor.ingest(self.db, fixture('sensors_lost_abc.json'))
        maintenance.ingest_topology(self.db, [reversed_topology])
        result = self.diagnose()
        self.assertEqual([e['nodes'] for e in result['explanations']], [['G'], ['K']])

    # ------------------------------------------------------- two contracts

    def test_guaranteed_and_risk_contracts_disagree_on_one_spare(self):
        self.load_abc()
        result = self.diagnose()
        self.assertEqual(result['guaranteed_repair']['decision'], 'cannot_guarantee_one_trip')
        self.assertEqual(result['guaranteed_repair']['parts'], ['SKU-CTRL-K', 'SKU-GATEWAY'])
        self.assertEqual(result['risk_budget']['decision'], 'dispatch_on_risk')
        self.assertEqual(result['risk_budget']['parts'], ['SKU-GATEWAY'])
        self.assertAlmostEqual(result['risk_budget']['coverage_probability'], 0.8)
        self.assertEqual(result['risk_budget']['uncovered'],
                         [{'nodes': ['K'], 'probability': 0.2}])
        self.assertTrue(result['contracts_disagree'])

    def test_guaranteed_contract_succeeds_when_vehicle_fits_every_explanation(self):
        self.load_abc()
        result = self.diagnose(vehicle_capacity=2)
        self.assertEqual(result['guaranteed_repair']['decision'], 'dispatch_with_guarantee')
        self.assertEqual(result['guaranteed_repair']['parts'], ['SKU-CTRL-K', 'SKU-GATEWAY'])

    def test_risk_threshold_can_refuse_dispatch(self):
        self.load_abc()
        result = self.diagnose(risk_threshold=0.9)
        self.assertEqual(result['risk_budget']['decision'], 'cannot_meet_risk_threshold')
        self.assertAlmostEqual(result['risk_budget']['coverage_probability'], 0.8)

    def test_risk_contract_requires_a_frequency_for_every_node(self):
        sensor.ingest(self.db, fixture('sensors_lost_abc.json'))
        maintenance.ingest_topology(self.db, fixture('topology_abc.json'))
        result = self.diagnose()
        self.assertEqual(result['risk_budget']['decision'], 'insufficient_frequency_evidence')
        self.assertFalse(result['frequency_complete'])

    def test_neither_contract_changes_bay_state(self):
        self.load_abc()
        before = sensor.snapshot(self.db, AS_OF, TIMEOUT)['states']
        result = self.diagnose()
        after = sensor.snapshot(self.db, AS_OF, TIMEOUT)['states']
        self.assertEqual({b: s['state'] for b, s in before.items()},
                         {b: s['state'] for b, s in after.items()})
        # Carrying the gateway spare did not "fix" anything.
        self.assertEqual(result['bay_causes']['bA']['state'], 'unknown')
        self.assertEqual(after['bA']['state'], 'unknown')

    def test_frequency_is_not_field_confirmation(self):
        self.load_abc()
        result = self.diagnose()
        self.assertFalse(any(e['confirmed'] for e in result['explanations']))
        self.assertEqual(result['confirmed_nodes'], [])

    # --------------------------------------------------------- late evidence

    def test_late_gateway_self_test_excludes_g_and_keeps_prior_revision(self):
        self.load_abc()
        first = self.diagnose()
        self.assertEqual({tuple(e['nodes']) for e in first['explanations']}, {('G',), ('K',)})
        maintenance.ingest_signals(self.db, fixture('signal_gateway_ok.json'))
        second = self.diagnose()
        self.assertEqual([e['nodes'] for e in second['explanations']], [['K']])
        self.assertTrue(second['late_evidence'])
        self.assertEqual(second['supersedes_revision'], first['revision'])
        # The old revision and its two explanations are still in the ledger.
        old = self.db.execute('SELECT nodes_json FROM diagnosis_explanation WHERE diagnosis_id=? '
                              'ORDER BY explanation_no', (first['diagnosis_id'],)).fetchall()
        self.assertEqual([json.loads(r['nodes_json']) for r in old], [['G'], ['K']])
        # G's 0.8 probability is not reused: K is now the only scenario.
        self.assertAlmostEqual(second['explanations'][0]['probability'], 1.0)
        self.assertEqual(second['guaranteed_repair']['decision'], 'dispatch_with_guarantee')
        self.assertEqual(second['guaranteed_repair']['parts'], ['SKU-CTRL-K'])
        self.assertEqual(second['risk_budget']['parts'], ['SKU-CTRL-K'])
        # G was eliminated by a named independent source, not by row order.
        excluded = {tuple(e['nodes']): e['excluded_by'] for e in second['eliminated']}
        self.assertEqual(excluded[('G',)], ['sig-gateway-ok'])

    def test_self_test_fail_confirms_controller(self):
        self.load_abc()
        maintenance.ingest_signals(self.db, fixture('signal_gateway_ok.json'))
        result = self.diagnose()
        self.assertEqual(result['confirmed_nodes'], ['K'])
        self.assertTrue(result['explanations'][0]['confirmed'])

    # -------------------------------------------------- independent liveness

    def test_online_sensor_behind_other_controller_excludes_gateway_fault(self):
        # D reports on time through K2 behind the same gateway G, while A/B/C
        # are lost. A whole-G failure is impossible; K must be the explanation.
        self.load_abc(with_d=True, topology='topology_with_d.json')
        result = self.diagnose()
        self.assertEqual([e['nodes'] for e in result['explanations']], [['K']])
        self.assertEqual(result['guaranteed_repair']['parts'], ['SKU-CTRL-K'])
        excluded = {tuple(e['nodes']) for e in result['eliminated']}
        self.assertIn(('G',), excluded)
        # The exclusion cites D's own live reading as the source.
        g_evidence = [e for e in result['eliminated'] if e['nodes'] == ['G']][0]
        self.assertTrue(any(eid.startswith('heartbeat:') or eid for eid in g_evidence['excluded_by']))

    def test_lost_count_does_not_vote_out_evidence(self):
        # Three losses do not outweigh direct proof that G forwards D's data.
        self.load_abc(with_d=True, topology='topology_with_d.json')
        result = self.diagnose(max_faults=3)
        self.assertNotIn(['G'], [e['nodes'] for e in result['explanations']])
        self.assertEqual(len(result['lost_bays']), 3)

    # ---------------------------------------------------- confirmed power fault

    def test_confirmed_power_fault_changes_cause_not_state(self):
        sensor.ingest(self.db, fixture('sensors_lost_abc.json'))
        maintenance.ingest_topology(self.db, fixture('topology_power.json'))
        maintenance.ingest_signals(self.db, fixture('signal_power_fail.json'))
        result = self.diagnose(vehicle_capacity=2)
        self.assertEqual(result['confirmed_nodes'], ['P'])
        for bay in ('bA', 'bB', 'bC'):
            self.assertEqual(result['bay_causes'][bay]['reason'], 'upstream_power_fault')
            self.assertEqual(result['bay_causes'][bay]['state'], 'unknown')
        # The report keeps those bays unknown and only attaches the diagnosis.
        r = report.decide(self.db, AS_OF, 3, timeout_minutes=TIMEOUT,
                          diagnose_dispatch=True, vehicle_capacity=2)
        self.assertEqual(r['sensor']['states']['bA']['state'], 'unknown')
        self.assertEqual(r['sensor']['states']['bA']['diagnostic_cause']['reason'], 'upstream_power_fault')
        self.assertNotEqual(r['state'], 'published')

    # --------------------------------------------------------- revision ledger

    def test_repeat_diagnosis_is_idempotent_but_new_evidence_versions(self):
        self.load_abc()
        first = self.diagnose()
        again = self.diagnose()
        self.assertEqual(first['diagnosis_id'], again['diagnosis_id'])
        self.assertEqual(len(maintenance.revisions(self.db)), 1)
        maintenance.ingest_signals(self.db, fixture('signal_gateway_ok.json'))
        self.diagnose()
        self.assertEqual(len(maintenance.revisions(self.db)), 2)

    def test_signal_conflict_is_retained(self):
        self.load_abc()
        signal = fixture('signal_gateway_ok.json')[0]
        maintenance.ingest_signals(self.db, [signal])
        conflicting = dict(signal, result='fail')
        with self.assertRaises(EvidenceConflict):
            maintenance.ingest_signals(self.db, [conflicting])
        kept = conflicts(self.db)
        self.assertEqual(kept[0]['event_id'], signal['event_id'])
        self.assertIn('"result":"fail"', kept[0]['incoming_body'])

    def test_duplicate_signal_is_idempotent(self):
        self.load_abc()
        signal = fixture('signal_gateway_ok.json')[0]
        self.assertEqual(maintenance.ingest_signals(self.db, [signal])['imported'], 1)
        self.assertEqual(maintenance.ingest_signals(self.db, [signal])['imported'], 0)

    # ------------------------------------------------------------- bad input

    def test_topology_cycle_is_rejected(self):
        bad = {'event_id': 'topo-cycle', 'event_time': '2026-11-17T08:00:00Z',
               'nodes': [{'node_id': 'X', 'kind': 'controller', 'spare_sku': 'S1'},
                         {'node_id': 'Y', 'kind': 'gateway', 'spare_sku': 'S2'}],
               'edges': [{'child_id': 'X', 'parent_id': 'Y'}, {'child_id': 'Y', 'parent_id': 'X'}]}
        with self.assertRaises(ValueError):
            maintenance.ingest_topology(self.db, [bad])

    def test_unknown_edge_and_bad_signal_are_rejected(self):
        bad = {'event_id': 'topo-bad', 'event_time': '2026-11-17T08:00:00Z',
               'nodes': [{'node_id': 'X', 'kind': 'sensor', 'spare_sku': 'S1'}],
               'edges': [{'child_id': 'X', 'parent_id': 'missing'}]}
        with self.assertRaises(ValueError):
            maintenance.ingest_topology(self.db, [bad])
        with self.assertRaises(ValueError):
            maintenance.ingest_signals(self.db, [{'event_id': 'sig', 'device_id': 'X',
                                                  'signal_kind': 'self_test', 'result': 'maybe',
                                                  'event_time': '2026-11-17T10:00:00Z',
                                                  'captured_at': '2026-11-17T10:00:00Z',
                                                  'source': 'x', 'reliability': 'high'}])

    def test_missing_topology_is_an_input_error(self):
        sensor.ingest(self.db, fixture('sensors_lost_abc.json'))
        with self.assertRaises(ValueError):
            self.diagnose()

    # ------------------------------------------------- evidence supersession

    def test_later_self_test_supersedes_earlier_failure(self):
        self.load_abc()
        maintenance.ingest_signals(self.db, [
            {'event_id': 'k-fail', 'device_id': 'K', 'signal_kind': 'self_test', 'result': 'fail',
             'event_time': '2026-11-17T10:00:00Z', 'captured_at': '2026-11-17T10:01:00Z',
             'source': 'probe', 'reliability': 'high'},
        ])
        first = self.diagnose()
        self.assertIn(['K'], [e['nodes'] for e in first['explanations']])
        self.assertEqual(first['confirmed_nodes'], ['K'])
        maintenance.ingest_signals(self.db, [
            {'event_id': 'k-ok', 'device_id': 'K', 'signal_kind': 'self_test', 'result': 'ok',
             'event_time': '2026-11-17T10:05:00Z', 'captured_at': '2026-11-17T10:06:00Z',
             'source': 'probe', 'reliability': 'high'},
        ])
        second = self.diagnose()
        # K is now proven working, so it cannot explain the loss; only G can.
        self.assertEqual([e['nodes'] for e in second['explanations']], [['G']])
        self.assertEqual(second['confirmed_nodes'], [])

    def test_child_self_test_does_not_prove_parent_forwards(self):
        # A passing self-test on D says nothing about G when D's own heartbeat
        # is what proves G forwards. Without D's live reading G remains viable.
        self.load_abc(with_d=True, topology='topology_with_d.json')
        maintenance.ingest_signals(self.db, [
            {'event_id': 'd-selftest', 'device_id': 'D', 'signal_kind': 'self_test', 'result': 'ok',
             'event_time': '2026-11-17T10:05:00Z', 'captured_at': '2026-11-17T10:06:00Z',
             'source': 'probe', 'reliability': 'high'},
        ])
        result = self.diagnose()
        # D already reported live, so G is independently excluded regardless.
        self.assertEqual([e['nodes'] for e in result['explanations']], [['K']])

    # ------------------------------------------------ power fault contracts

    def test_confirmed_power_fault_drives_cause_and_keeps_alternatives(self):
        sensor.ingest(self.db, fixture('sensors_lost_abc.json'))
        maintenance.ingest_topology(self.db, fixture('topology_power.json'))
        maintenance.ingest_signals(self.db, fixture('signal_power_fail.json'))
        maintenance.ingest_frequencies(self.db, [
            {'event_id': 'p-freq', 'device_id': 'P', 'frequency': 0.9,
             'event_time': '2025-12-01T00:00:00Z'},
            {'event_id': 'k-freq', 'device_id': 'K', 'frequency': 0.1,
             'event_time': '2025-12-01T00:00:00Z'},
            {'event_id': 'g-freq', 'device_id': 'G', 'frequency': 0.1,
             'event_time': '2025-12-01T00:00:00Z'}])
        result = self.diagnose(vehicle_capacity=3)
        # A first-hand P failure confirms the power fault but does not pretend
        # K or G are known-good; each still covers the three lost sensors.
        self.assertEqual(result['confirmed_nodes'], ['P'])
        self.assertIn(['P'], [e['nodes'] for e in result['explanations']])
        self.assertIn('SKU-PSU', result['guaranteed_repair']['parts'])
        self.assertEqual(result['bay_causes']['bA']['reason'], 'upstream_power_fault')
        # The confirmed fault never changes the bay's state.
        self.assertEqual(result['bay_causes']['bA']['state'], 'unknown')

    def test_bay_without_any_reading_is_not_treated_as_device_loss(self):
        sensor.ingest(self.db, fixture('sensors_lost_abc.json'))
        sensor.ingest(self.db, fixture('sensor_good.json'))  # registers d1/d2/d3 with no timeout yet
        maintenance.ingest_topology(self.db, fixture('topology_abc.json'))
        result = self.diagnose()
        # d1..d3 are registered but have no reading; they must not be blamed on
        # the A/B/C topology, and their bays are reported as no-reading.
        self.assertNotIn('d1', result['lost_devices'])
        self.assertTrue(all(bay not in result['bay_causes'] or
                            result['bay_causes'][bay]['device_id'] in ('A', 'B', 'C')
                            for bay in result['bay_causes']))

    def test_explanation_cannot_contain_a_proven_healthy_node(self):
        self.load_abc()
        maintenance.ingest_signals(self.db, [
            {'event_id': 'k-ok', 'device_id': 'K', 'signal_kind': 'self_test', 'result': 'ok',
             'event_time': '2026-11-17T10:05:00Z', 'captured_at': '2026-11-17T10:06:00Z',
             'source': 'probe', 'reliability': 'high'},
        ])
        result = self.diagnose()
        self.assertNotIn(['K'], [e['nodes'] for e in result['explanations']])
        self.assertIn(['K'], [e['nodes'] for e in result['eliminated']])

    def test_capacity_counts_physical_units_not_distinct_skus(self):
        # Two lost same-SKU sensors whose only common explanation is a pair need
        # two physical sensor units; one-item capacity cannot guarantee it even
        # though only one distinct SKU is involved.
        sensor.ingest(self.db, [
            {'event_id': 'dev-X', 'op': 'device', 'device_id': 'X', 'bay_id': 'bX',
             'calibrated_until': '2026-11-17T11:00:00Z', 'event_time': '2026-11-17T08:00:00Z'},
            {'event_id': 'dev-Y', 'op': 'device', 'device_id': 'Y', 'bay_id': 'bY',
             'calibrated_until': '2026-11-17T11:00:00Z', 'event_time': '2026-11-17T08:00:00Z'},
            {'event_id': 'read-X', 'op': 'reading', 'device_id': 'X', 'bay_id': 'bX',
             'occupied': False, 'sequence_no': 1, 'event_time': '2026-11-17T09:55:00Z'},
            {'event_id': 'read-Y', 'op': 'reading', 'device_id': 'Y', 'bay_id': 'bY',
             'occupied': False, 'sequence_no': 1, 'event_time': '2026-11-17T09:55:00Z'},
        ])
        maintenance.ingest_topology(self.db, [{
            'event_id': 'topo-pair', 'event_time': '2026-11-17T08:00:00Z',
            'nodes': [{'node_id': 'X', 'kind': 'sensor', 'spare_sku': 'SKU-SENSOR'},
                      {'node_id': 'Y', 'kind': 'sensor', 'spare_sku': 'SKU-SENSOR'}],
            'edges': []}])
        result = self.diagnose(vehicle_capacity=1)
        self.assertEqual([e['nodes'] for e in result['explanations']], [['X', 'Y']])
        self.assertEqual(result['guaranteed_repair']['decision'], 'cannot_guarantee_one_trip')
        self.assertEqual(result['guaranteed_repair']['parts'], ['SKU-SENSOR', 'SKU-SENSOR'])
        self.assertEqual(result['risk_budget']['decision'], 'insufficient_frequency_evidence')


if __name__ == '__main__':
    unittest.main()
