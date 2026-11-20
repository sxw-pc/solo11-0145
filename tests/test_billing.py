"""Offline parking-fee adjudication tests.

Coverage:
- same-gate correlated offsets vs. independent per-gate offsets,
- tariff tier boundaries, free grace, daily cap and UTC-midnight split,
- late narrowing calibration and balance-only settlement,
- malformed stays (no exit, orphan exit, duplicate receipts),
- exact vs. guaranteed payment contracts and their divergent decisions,
- evidence conflicts, replay idempotency, correction bills and refunds.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from parking import gate, billing
from parking.sources import EvidenceConflict
from parking.store import connect

TARIFF_STANDARD = {
    'event_id': 'tariff-1', 'op': 'tariff', 'tariff_id': 'T1',
    'valid_from': '2026-01-01T00:00:00Z', 'valid_to': '2027-01-01T00:00:00Z',
    'grace_seconds': 0, 'daily_cap_minor': 500,
    'tiers': [[3600, 200], [7200, 400], [None, 800]],
    'event_time': '2026-01-01T00:00:00Z'}
CONTRACTS = [
    {'event_id': 'k-exact', 'op': 'contract', 'contract_id': 'K-EXACT', 'contract_type': 'exact',
     'valid_from': '2026-01-01T00:00:00Z', 'valid_to': '2027-01-01T00:00:00Z',
     'event_time': '2026-01-01T00:00:00Z'},
    {'event_id': 'k-guar', 'op': 'contract', 'contract_id': 'K-GUAR', 'contract_type': 'guaranteed',
     'valid_from': '2026-01-01T00:00:00Z', 'valid_to': '2027-01-01T00:00:00Z',
     'event_time': '2026-01-01T00:00:00Z'}]
DAY = ('2026-11-20T00:00:00Z', '2026-11-21T00:00:00Z')


def gate_event(event_id, stay_id, direction, clock, gate_id):
    return {'event_id': event_id, 'stay_id': stay_id, 'direction': direction,
            'event_time': clock, 'gate_id': gate_id}


def calibration(event_id, gate_id, lo, hi, published='2026-11-20T09:00:00Z', minutes=False):
    key = 'offset_minutes' if minutes else 'offset_seconds'
    return {'event_id': event_id, 'op': 'gate_calibration', 'gate_id': gate_id,
            'valid_from': DAY[0], 'valid_to': DAY[1],
            f'{key}_min': lo, f'{key}_max': hi, 'event_time': published}


class BillingCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'billing.sqlite')
        billing.ingest_tariff(self.db, [TARIFF_STANDARD])
        billing.ingest_contract(self.db, CONTRACTS)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add_stay(self, stay_id, in_gate='G1', out_gate='G1',
                 in_clock='2026-11-20T10:00:00Z', out_clock='2026-11-20T11:00:00Z'):
        gate.ingest(self.db, [gate_event(f'{stay_id}-in', stay_id, 'in', in_clock, in_gate),
                              gate_event(f'{stay_id}-out', stay_id, 'out', out_clock, out_gate)])

    # ------------------------------------------------------------------ #
    # correlated clock offsets
    # ------------------------------------------------------------------ #
    def test_same_gate_offset_is_shared_not_per_event(self):
        self.add_stay('s1')
        billing.ingest_calibration(self.db, [calibration('c1', 'G1', 0, 5, minutes=True)])
        outcome = billing.feasible_outcomes(self.db, 's1')
        # Shared offset cancels exactly: the stay is 60 minutes under every draw.
        self.assertEqual(outcome['durations_seconds'], [3600])
        self.assertEqual(outcome['amounts_minor'], [200])
        self.assertEqual(outcome['combos'], 301)
        # A per-event independent expansion would have produced 55..65 minutes
        # and crossed the 400-cent tier; that must not happen.
        self.assertNotIn(400, outcome['amounts_minor'])

    def test_distinct_gates_have_independent_offsets(self):
        self.add_stay('s2', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        outcome = billing.feasible_outcomes(self.db, 's2')
        self.assertEqual((outcome['durations_seconds'][0], outcome['durations_seconds'][-1]), (3300, 3900))
        self.assertEqual(outcome['amounts_minor'], [200, 400])
        self.assertEqual(outcome['combos'], 301 * 301)

    def test_contracts_diverge_on_ambiguous_stay(self):
        self.add_stay('s2', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        exact = billing.adjudicate(self.db, 's2', 'K-EXACT')
        guar = billing.adjudicate(self.db, 's2', 'K-GUAR')
        self.assertEqual(exact['status'], 'pending_review')
        self.assertIsNone(exact['billed_minor'])
        self.assertEqual(guar['status'], 'partial_prepayment')
        self.assertEqual(guar['guaranteed_minor'], 200)
        # the exact contract collects nothing while the guaranteed one prepays
        self.assertEqual(billing.ledger_balance(self.db, 's2', 'K-EXACT'), 0)
        self.assertEqual(billing.ledger_balance(self.db, 's2', 'K-GUAR'), 200)
        # a prepayment voucher can never be presented as a formal bill
        row = self.db.execute("SELECT bill_type FROM bill WHERE stay_id='s2'").fetchone()
        self.assertEqual(row['bill_type'], 'prepayment')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bill WHERE bill_type='formal' "
                                         "AND stay_id='s2'").fetchone()[0], 0)

    def test_certain_stay_bills_200_under_both_contracts(self):
        self.add_stay('s1')
        billing.ingest_calibration(self.db, [calibration('c1', 'G1', 0, 5, minutes=True)])
        for contract in ('K-EXACT', 'K-GUAR'):
            d = billing.adjudicate(self.db, 's1', contract)
            self.assertEqual((d['status'], d['billed_minor']), ('formal_bill', 200))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bill WHERE stay_id='s1'").fetchone()[0], 2)

    def test_guaranteed_contract_can_collect_zero_when_grace_is_feasible(self):
        # grace covers stays up to 60 minutes; feasible durations 55..65 min
        # span the grace boundary, so the common guaranteed minimum is zero
        billing.ingest_tariff(self.db, [{
            'event_id': 'tariff-g', 'op': 'tariff', 'tariff_id': 'TG',
            'valid_from': '2026-01-01T00:00:00Z', 'valid_to': '2027-01-01T00:00:00Z',
            'grace_seconds': 3600, 'daily_cap_minor': 500,
            'tiers': [[7200, 400], [None, 800]],
            'event_time': '2026-01-01T00:00:00Z'}])
        self.add_stay('sz', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        exact = billing.adjudicate(self.db, 'sz', 'K-EXACT')
        guar = billing.adjudicate(self.db, 'sz', 'K-GUAR')
        self.assertEqual((exact['status'], exact['billed_minor']), ('pending_review', None))
        # "pending review, zero collected": distinct from a formal zero bill
        self.assertEqual(guar['status'], 'partial_prepayment')
        self.assertEqual(guar['guaranteed_minor'], 0)
        self.assertIsNone(guar['billed_minor'])
        self.assertEqual(billing.ledger_balance(self.db, 'sz', 'K-GUAR'), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bill WHERE stay_id='sz'").fetchone()[0], 0)


    # ------------------------------------------------------------------ #
    # tariff boundaries, grace, cap, midnight
    # ------------------------------------------------------------------ #
    def test_tier_boundary_is_inclusive(self):
        # exactly 60 minutes -> 200 tier; 60 minutes + 1 second -> 400 tier
        self.add_stay('edge', in_clock='2026-11-20T10:00:00Z', out_clock='2026-11-20T11:00:00Z')
        billing.ingest_calibration(self.db, [calibration('ce', 'G1', 0, 0)])
        self.assertEqual(billing.adjudicate(self.db, 'edge', 'K-EXACT')['billed_minor'], 200)
        self.add_stay('edge2', in_clock='2026-11-20T10:00:00Z', out_clock='2026-11-20T11:00:01Z')
        self.assertEqual(billing.adjudicate(self.db, 'edge2', 'K-EXACT')['billed_minor'], 400)

    def test_free_grace_seconds(self):
        billing.ingest_tariff(self.db, [{
            'event_id': 'tariff-grace', 'op': 'tariff', 'tariff_id': 'T2',
            'valid_from': '2026-11-20T00:00:00Z', 'valid_to': '2026-11-21T00:00:00Z',
            'grace_seconds': 900, 'daily_cap_minor': 500,
            'tiers': [[3600, 200], [7200, 400], [None, 800]],
            'event_time': '2026-11-20T08:00:00Z'}])
        self.add_stay('g', in_clock='2026-11-20T08:00:00Z', out_clock='2026-11-20T08:15:00Z')
        billing.ingest_calibration(self.db, [calibration('cg', 'G1', 0, 0)])
        d = billing.adjudicate(self.db, 'g', 'K-EXACT')
        self.assertEqual(d['billed_minor'], 0)
        self.assertEqual(d['tariff_refs'], ['tariff-grace'])

    def test_cross_midnight_splits_days_and_applies_daily_cap(self):
        # 21:00 -> 00:20: day one 180 min would be 800 but caps at 500;
        # day two 20 min is 200. Naive whole-duration pricing gives 800.
        self.add_stay('night', in_clock='2026-11-20T21:00:00Z',
                      out_clock='2026-11-21T00:20:00Z')
        billing.ingest_calibration(self.db, [calibration('cn', 'G1', 0, 0)])
        d = billing.adjudicate(self.db, 'night', 'K-EXACT')
        self.assertEqual(d['billed_minor'], 700)
        self.assertEqual(d['feasible_duration_summary_seconds']['min'], 12000)

    # ------------------------------------------------------------------ #
    # late calibration: narrow, settle difference, never double collect
    # ------------------------------------------------------------------ #
    def test_enumeration_guard_refuses_partial_answers(self):
        self.add_stay('huge', in_gate='G2', out_gate='G3')
        # roughly 250k seconds per gate -> over 60 billion combinations
        billing.ingest_calibration(self.db, [calibration('b', 'G2', 0, 250_000),
                                             calibration('c', 'G3', 0, 250_000)])
        with self.assertRaises(ValueError):
            billing.feasible_outcomes(self.db, 'huge')

    def test_physically_impossible_offset_draws_are_excluded(self):
        # one-minute device stay with broad +/- offsets: draws where the true
        # exit is not after the true entry must be dropped, never priced
        self.add_stay('short', in_gate='G2', out_gate='G3',
                      in_clock='2026-11-20T10:00:00Z',
                      out_clock='2026-11-20T10:01:00Z')
        billing.ingest_calibration(self.db, [calibration('b', 'G2', -200, 200),
                                             calibration('c', 'G3', -200, 200)])
        outcome = billing.feasible_outcomes(self.db, 'short')
        self.assertGreater(outcome['durations_seconds'][0], 0)
        self.assertEqual(outcome['durations_seconds'][-1], 460)
        self.assertLess(outcome['combos'], 401 * 401)

    def test_late_calibration_narrows_and_supplements_balance(self):
        self.add_stay('s2', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        first = billing.adjudicate(self.db, 's2', 'K-GUAR')
        self.assertEqual(first['decision_seq'], 1)
        # late independent evidence: entry offset 0, exit offset +5 min -> 65 min
        billing.ingest_calibration(self.db, [
            calibration('c2b', 'G2', 0, 0, published='2026-11-20T12:00:00Z'),
            calibration('c3b', 'G3', 300, 300, published='2026-11-20T12:00:00Z')])
        outcome = billing.feasible_outcomes(self.db, 's2')
        self.assertEqual((outcome['durations_seconds'], outcome['amounts_minor']), ([3900], [400]))
        second = billing.adjudicate(self.db, 's2', 'K-GUAR')
        self.assertEqual(second['decision_seq'], 2)
        self.assertEqual(second['status'], 'formal_bill')
        self.assertEqual(second['billed_minor'], 400)
        self.assertEqual(second['supersedes_decision'], first['decision_id'])
        # only the 200-cent difference is collected again
        self.assertEqual(billing.ledger_balance(self.db, 's2', 'K-GUAR'), 400)
        bills = [dict(r) for r in self.db.execute(
            "SELECT bill_type,amount_minor,supersedes_bill FROM bill WHERE stay_id='s2' ORDER BY rowid")]
        self.assertEqual([b['bill_type'] for b in bills], ['prepayment', 'correction'])
        self.assertEqual([b['amount_minor'] for b in bills], [200, 200])
        self.assertEqual(bills[1]['supersedes_bill'], 's2:K-GUAR:d1:prepayment')

    def test_exact_contract_pending_then_formal_after_narrowing(self):
        self.add_stay('s2', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        first = billing.adjudicate(self.db, 's2', 'K-EXACT')
        self.assertEqual(first['status'], 'pending_review')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bill WHERE stay_id='s2'").fetchone()[0], 0)
        billing.ingest_calibration(self.db, [
            calibration('c2b', 'G2', 0, 0, published='2026-11-20T12:00:00Z'),
            calibration('c3b', 'G3', 300, 300, published='2026-11-20T12:00:00Z')])
        second = billing.adjudicate(self.db, 's2', 'K-EXACT')
        self.assertEqual(second['status'], 'formal_bill')
        self.assertEqual(second['billed_minor'], 400)
        self.assertEqual(billing.ledger_balance(self.db, 's2', 'K-EXACT'), 400)

    def test_narrowing_that_lowers_amount_issues_refund(self):
        self.add_stay('s2', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        billing.adjudicate(self.db, 's2', 'K-GUAR')  # prepay 200
        # grace revision plus evidence pinning the stay to 55 minutes -> 0 due
        billing.ingest_tariff(self.db, [{
            'event_id': 'tariff-grace', 'op': 'tariff', 'tariff_id': 'T2',
            'valid_from': '2026-11-20T00:00:00Z', 'valid_to': '2026-11-21T00:00:00Z',
            'grace_seconds': 3600, 'daily_cap_minor': 500,
            'tiers': [[7200, 400], [None, 800]],
            'event_time': '2026-11-20T08:00:00Z'}])
        billing.ingest_calibration(self.db, [
            calibration('c2b', 'G2', 300, 300, published='2026-11-20T12:00:00Z'),
            calibration('c3b', 'G3', 0, 0, published='2026-11-20T12:00:00Z')])
        d = billing.adjudicate(self.db, 's2', 'K-GUAR')
        self.assertEqual(d['billed_minor'], 0)
        self.assertEqual(billing.ledger_balance(self.db, 's2', 'K-GUAR'), 0)
        entry = self.db.execute("SELECT kind,amount_minor FROM stay_ledger_entry "
                                "WHERE stay_id='s2' ORDER BY entry_id").fetchall()
        self.assertEqual([(r['kind'], r['amount_minor']) for r in entry],
                         [('prepayment', 200), ('refund', -200)])

    def test_replay_is_idempotent_and_keeps_ledger_stable(self):
        self.add_stay('s2', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        first = billing.adjudicate(self.db, 's2', 'K-GUAR')
        replay = billing.adjudicate(self.db, 's2', 'K-GUAR')
        self.assertEqual((replay['note'], replay['decision_id']), ('replayed', first['decision_id']))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stay_ledger_entry').fetchone()[0], 1)
        self.assertEqual(billing.ledger_balance(self.db, 's2', 'K-GUAR'), 200)

    # ------------------------------------------------------------------ #
    # anomalies and evidence integrity
    # ------------------------------------------------------------------ #
    def test_malformed_stays_are_rejected_and_never_charged(self):
        cases = {
            'open': [('in', '10:00')],
            'orphan': [('out', '09:00')],
            'dup': [('in', '10:00'), ('in', '10:05'), ('out', '11:00')],
        }
        for stay_id, receipts in cases.items():
            gate.ingest(self.db, [gate_event(f'{stay_id}-{i}', stay_id, d,
                                             f'2026-11-20T{t}:00Z', 'G1')
                                  for i, (d, t) in enumerate(receipts)])
            d = billing.adjudicate(self.db, stay_id, 'K-EXACT')
            self.assertEqual(d['status'], 'rejected')
            self.assertEqual(billing.ledger_balance(self.db, stay_id, 'K-EXACT'), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bill').fetchone()[0], 0)

    def test_missing_gate_identity_blocks_billing(self):
        # legacy gate events without a gate_id remain valid for availability,
        # but cannot be adjudicated without clock-identity evidence
        gate.ingest(self.db, [{'event_id': 'legacy-in', 'stay_id': 'legacy', 'direction': 'in',
                               'event_time': '2026-11-20T10:00:00Z'},
                              {'event_id': 'legacy-out', 'stay_id': 'legacy', 'direction': 'out',
                               'event_time': '2026-11-20T11:00:00Z'}])
        d = billing.adjudicate(self.db, 'legacy', 'K-EXACT')
        self.assertEqual((d['status'], d['reason']), ('rejected', 'missing_gate_identity'))

    def test_missing_or_contradictory_calibration(self):
        self.add_stay('s3', in_gate='G4', out_gate='G5')
        d = billing.adjudicate(self.db, 's3', 'K-EXACT')
        self.assertEqual((d['status'], d['reason']), ('rejected', 'missing_calibration'))
        billing.ingest_calibration(self.db, [
            calibration('ca', 'G4', 0, 60), calibration('cb', 'G5', 0, 60)])
        self.assertEqual(billing.feasible_outcomes(self.db, 's3')['reason'], None)
        billing.ingest_calibration(self.db, [
            calibration('ca2', 'G4', 120, 300, published='2026-11-20T09:30:00Z')])
        d2 = billing.adjudicate(self.db, 's3', 'K-EXACT')
        self.assertEqual((d2['status'], d2['reason']), ('rejected', 'contradictory_calibration'))

    def test_same_event_id_with_different_body_conflicts(self):
        self.add_stay('s4')
        billing.ingest_calibration(self.db, [calibration('cx', 'G1', 0, 0)])
        with self.assertRaises(EvidenceConflict):
            billing.ingest_calibration(self.db, [calibration('cx', 'G1', 0, 10)])
        # conflicting batch rolled back: the original range survives unchanged
        row = self.db.execute("SELECT offset_seconds_min,offset_seconds_max FROM gate_calibration "
                              "WHERE event_id='cx'").fetchone()
        self.assertEqual((row['offset_seconds_min'], row['offset_seconds_max']), (0, 0))

    def test_calibration_ingest_is_replay_idempotent(self):
        c = calibration('c1', 'G1', 0, 5, minutes=True)
        first = billing.ingest_calibration(self.db, [c])
        second = billing.ingest_calibration(self.db, [c])
        self.assertEqual((first['imported'], second['imported']), (1, 0))

    # ------------------------------------------------------------------ #
    # evidence chain and versions
    # ------------------------------------------------------------------ #
    def test_each_decision_records_all_evidence_versions(self):
        self.add_stay('s2', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        d = billing.adjudicate(self.db, 's2', 'K-GUAR')
        self.assertEqual(set(d['calibration_refs']), {'c2', 'c3'})
        self.assertEqual(d['tariff_refs'], ['tariff-1'])
        self.assertEqual(d['contract_ref'], 'k-guar')
        report = billing.stay_report(self.db, 's2')
        self.assertEqual({e['event_id'] for e in report['gate_events']}, {'s2-in', 's2-out'})
        self.assertTrue(all(e['calibration_evidence'] for e in report['gate_events']))
        self.assertEqual(report['ledger_balances_minor'], {'K-GUAR': 200})

    def test_original_gate_events_and_bills_are_not_rewritten(self):
        self.add_stay('s2', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('c2', 'G2', 0, 5, minutes=True),
                                             calibration('c3', 'G3', 0, 5, minutes=True)])
        billing.adjudicate(self.db, 's2', 'K-GUAR')
        before = [dict(r) for r in self.db.execute('SELECT * FROM gate_event ORDER BY event_id')]
        bill_count = self.db.execute('SELECT COUNT(*) FROM bill').fetchone()[0]
        billing.ingest_calibration(self.db, [
            calibration('c2b', 'G2', 0, 0, published='2026-11-20T12:00:00Z'),
            calibration('c3b', 'G3', 300, 300, published='2026-11-20T12:00:00Z')])
        billing.adjudicate(self.db, 's2', 'K-GUAR')
        after = [dict(r) for r in self.db.execute('SELECT * FROM gate_event ORDER BY event_id')]
        self.assertEqual(before, after)
        # old prepayment bill row is preserved; only a correction is appended
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bill').fetchone()[0], bill_count + 1)
        old = self.db.execute("SELECT * FROM bill WHERE bill_id='s2:K-GUAR:d1:prepayment'").fetchone()
        self.assertEqual(old['amount_minor'], 200)

    def test_contradictory_new_evidence_preserves_the_former_formal_bill(self):
        self.add_stay('s5')
        billing.ingest_calibration(self.db, [calibration('gen1', 'G1', 0, 0)])
        first = billing.adjudicate(self.db, 's5', 'K-EXACT')
        self.assertEqual(first['billed_minor'], 200)
        # a later revision over the same reading window contradicts gen1 and
        # cannot be intersected: the system rejects rather than picking a side
        billing.ingest_calibration(self.db, [
            {'event_id': 'gen2', 'op': 'gate_calibration', 'gate_id': 'G1',
             'valid_from': DAY[0], 'valid_to': DAY[1],
             'offset_seconds_min': 300, 'offset_seconds_max': 300,
             'event_time': '2026-11-20T13:00:00Z'}])
        again = billing.adjudicate(self.db, 's5', 'K-EXACT')
        self.assertEqual((again['status'], again['reason']),
                         ('rejected', 'contradictory_calibration'))
        self.assertEqual(again['supersedes_decision'], first['decision_id'])
        # no new money moves and the original formal bill is untouched
        self.assertEqual(billing.ledger_balance(self.db, 's5', 'K-EXACT'), 200)
        original = self.db.execute("SELECT * FROM bill WHERE bill_id='s5:K-EXACT:d1:formal'").fetchone()
        self.assertEqual(original['amount_minor'], 200)

    def test_narrowing_to_higher_amount_emits_correction_referencing_formal_bill(self):
        # Independent gates first leave 200 or 400 feasible, so the guaranteed
        # contract collects the common 200 minimum as a prepayment. A later
        # precise revision pins both offsets; the formal 400 bill then arrives
        # as a correction referencing the original voucher.
        self.add_stay('s5', in_gate='G2', out_gate='G3')
        billing.ingest_calibration(self.db, [calibration('b-wide', 'G2', 0, 300),
                                             calibration('c-wide', 'G3', 0, 300)])
        broad = billing.feasible_outcomes(self.db, 's5')
        self.assertEqual(broad['amounts_minor'], [200, 400])
        billing.adjudicate(self.db, 's5', 'K-GUAR')
        billing.ingest_calibration(self.db, [
            {'event_id': 'b-exact', 'op': 'gate_calibration', 'gate_id': 'G2',
             'valid_from': DAY[0], 'valid_to': DAY[1],
             'offset_seconds_min': 0, 'offset_seconds_max': 0,
             'event_time': '2026-11-20T13:00:00Z'},
            {'event_id': 'c-exact', 'op': 'gate_calibration', 'gate_id': 'G3',
             'valid_from': DAY[0], 'valid_to': DAY[1],
             'offset_seconds_min': 300, 'offset_seconds_max': 300,
             'event_time': '2026-11-20T13:01:00Z'}])
        second = billing.adjudicate(self.db, 's5', 'K-GUAR')
        self.assertEqual((second['status'], second['billed_minor']), ('formal_bill', 400))
        correction = self.db.execute(
            "SELECT * FROM bill WHERE bill_id='s5:K-GUAR:d2:correction'").fetchone()
        self.assertEqual((correction['amount_minor'], correction['supersedes_bill']),
                         (200, 's5:K-GUAR:d1:prepayment'))
        self.assertEqual(billing.ledger_balance(self.db, 's5', 'K-GUAR'), 400)

    def test_billing_report_totals_and_per_contract_balances(self):
        self.add_stay('s1')
        billing.ingest_calibration(self.db, [calibration('c1', 'G1', 0, 0)])
        billing.adjudicate(self.db, 's1', 'K-EXACT')
        report = billing.billing_report(self.db)
        self.assertEqual(report['total_collected_minor'], 200)
        row = next(s for s in report['stays'] if s['stay_id'] == 's1')
        self.assertEqual(row['contracts']['K-EXACT'][0]['balance_minor'], 200)


if __name__ == '__main__':
    unittest.main()
