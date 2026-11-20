import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from parking import sensor,capacity,report
from parking.store import connect


def fixture(name):
    return json.loads((ROOT/'fixtures'/'parking'/name).read_text(encoding='utf-8'))


def claim(snap,order_id,bay_id='A'):
    hits=[c for c in snap['claims'] if c['order_id']==order_id and c['bay_id']==bay_id]
    assert len(hits)==1,(order_id,bay_id,[c['order_id'] for c in snap['claims']])
    return hits[0]


def override_paper(event_id='ho-paper-override-f',override_version='EO-F-0001'):
    p=dict(fixture('handover_override.json')[0])
    p['event_id']=event_id
    p['override_version']=override_version
    return p


class HandoverCases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'handover.sqlite')
        sensor.ingest(self.db,fixture('handover_sensor.json'))
        capacity.ingest(self.db,fixture('handover_orders.json'))
        # C and F are active at 12:05; G only starts at 13:00.
        self.t1205='2026-11-20T12:05:00Z'
        self.t1206='2026-11-20T12:06:00Z'
        self.t1305='2026-11-20T13:05:00Z'
        self.t1405='2026-11-20T14:05:00Z'
        self.t1430='2026-11-20T14:30:00Z'
        # G's window ends 15:00, so only C remains active from 15:30 on.
        self.t1530='2026-11-20T15:30:00Z'

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def base_snapshot(self,as_of,**kw):
        return capacity.handover_snapshot(self.db,as_of,save=kw.get('save',False))

    # --- set-union deduction rule remains unchanged ----------------------- #
    def test_reserved_count_still_deducts_overlap_once(self):
        c=capacity.snapshot(self.db,self.t1205)
        self.assertEqual(c['reserved_count'],1)
        self.assertEqual(c['reserved_bays'],['A'])
        self.assertEqual(c['owner_by_bay']['A']['order_id'],'F')

    # --- emergency override counterexample -------------------------------- #
    def test_standard_waits_but_emergency_admits_with_trusted_free_bay(self):
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        snap=self.base_snapshot(self.t1205)
        f=claim(snap,'F')
        self.assertEqual(f['standard_contract']['status'],'awaiting_handover')
        self.assertEqual(f['emergency_contract']['status'],'admittable')
        self.assertEqual(f['sensor']['clearance'],'clear')
        c=claim(snap,'C')
        self.assertEqual(c['standard_contract']['status'],'preempted')
        self.assertEqual(c['emergency_contract']['status'],'preempted')
        # owner_by_bay ordering must not appear as handover evidence.
        self.assertEqual(f['evidence_event_ids'],['ho-paper-override-f'])
        self.assertEqual(snap['open_emergency_obligations'][0]['displaced_order_id'],'C')

    def test_occupied_bay_blocks_both_contracts(self):
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        snap=self.base_snapshot(self.t1206)  # latest trusted reading is occupied
        f=claim(snap,'F')
        self.assertEqual(f['standard_contract']['status'],'awaiting_handover')
        self.assertEqual(f['emergency_contract']['status'],'awaiting_handover')
        self.assertEqual(f['sensor']['clearance'],'occupied')
        self.assertEqual(snap['statuses']['admittable'],0)

    def test_unknown_sensor_keeps_investigation_not_gate_guess(self):
        # No reading at all for bay B: unknown stays under investigation.
        capacity.ingest(self.db,[{'event_id':'b-order','order_id':'B1','purpose':'construction','priority':1,
                                  'approved':True,'revoked':False,'bays':['B'],
                                  'starts_at':'2026-11-20T09:00:00Z','ends_at':'2026-11-20T18:00:00Z',
                                  'event_time':'2026-11-20T08:30:00Z'}])
        snap=self.base_snapshot(self.t1205)
        b=claim(snap,'B1','B')
        self.assertEqual(b['sensor']['clearance'],'unknown')
        self.assertEqual(b['standard_contract']['status'],'under_investigation')
        # No override paper exists for B1: the emergency contract does not
        # apply to it at all, regardless of the unknown sensor state.
        self.assertEqual(b['emergency_contract']['status'],'not_applicable')

    # --- auto-resume is forbidden ----------------------------------------- #
    def test_ended_fire_order_does_not_restore_construction_automatically(self):
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        snap=self.base_snapshot(self.t1530)  # F ended 14:00, G ended 15:00; free again
        active={c['order_id'] for c in snap['claims']}
        self.assertEqual(active,{'C'})
        c=claim(snap,'C')
        self.assertEqual(c['standard_contract']['status'],'awaiting_handover')
        self.assertIn('no_new_handover_evidence',c['standard_contract']['reason'])

    def test_late_return_papers_restore_entry_after_fire_window(self):
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        before=self.base_snapshot(self.t1530)
        self.assertEqual(claim(before,'C')['standard_contract']['status'],'awaiting_handover')
        capacity.ingest_handover(self.db,fixture('handover_return.json'))
        after=self.base_snapshot(self.t1530)
        c=claim(after,'C')
        self.assertEqual(c['standard_contract']['status'],'admittable')
        self.assertEqual(c['evidence_event_ids'],
                         ['ho-paper-return-release-f','ho-paper-return-takeover-c'])

    # --- equal priority conflict, no lexicographic winner ----------------- #
    def test_equal_priority_orders_conflict_without_papers(self):
        snap=self.base_snapshot(self.t1305)  # F and G both active, priority 10
        f=claim(snap,'F');g=claim(snap,'G')
        self.assertEqual(f['standard_contract']['status'],'conflict')
        self.assertEqual(g['standard_contract']['status'],'conflict')
        self.assertEqual(snap['conflicts'][0]['order_ids'],['F','G'])
        # The lower-priority construction order is simply preempted.
        self.assertEqual(claim(snap,'C')['standard_contract']['status'],'preempted')

    # --- binding: no other bay or stale version signatures ---------------- #
    def test_paper_for_other_bay_is_rejected(self):
        p=override_paper(event_id='ov-wrong-bay',override_version='EO-WRONG-BAY')
        p['bay_id']='B'
        capacity.ingest_handover(self.db,[p])
        snap=self.base_snapshot(self.t1205)
        rejected={d['event_id']:d['reason'] for d in snap['rejected_documents']}
        # Neither named order covers bay B; the first failing binding is C.
        self.assertIn(rejected['ov-wrong-bay'],
                      ('C_does_not_cover_bay','F_does_not_cover_bay'))
        f=claim(snap,'F')
        self.assertEqual(f['emergency_contract']['status'],'not_applicable')

    def test_paper_against_stale_order_version_is_rejected(self):
        # Re-issue F with a new source event (new signed version); the override
        # still cites the old version and must not be reused.
        capacity.ingest(self.db,[dict(fixture('handover_orders.json')[1],
                                      event_id='ho-cap-f-v2',event_time='2026-11-20T11:45:00Z')])
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        snap=self.base_snapshot(self.t1205)
        rejected={d['event_id']:d['reason'] for d in snap['rejected_documents']}
        self.assertEqual(rejected['ho-paper-override-f'],'F_version_mismatch')
        self.assertEqual(claim(snap,'F')['emergency_contract']['status'],'not_applicable')

    def test_override_version_replay_for_other_pair_is_rejected(self):
        capacity.ingest_handover(self.db,[override_paper()])
        replay=override_paper(event_id='ov-replay',override_version='EO-F-0001')
        replay['to_order_id']='G';replay['to_order_version']='ho-cap-g'
        with self.assertRaises(ValueError):
            capacity.ingest_handover(self.db,[replay])

    # --- revocation re-adjudicates by event time -------------------------- #
    def test_revoking_override_reverts_fire_to_waiting(self):
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        rev={'event_id':'ho-revoke-override','doc_type':'revocation','bay_id':'A',
             'from_party':'fire-watch-A','to_party':'construction-crew-A',
             'from_signature':'fire-incident-commander-seal','to_signature':'duty-commander-seal',
             'revokes_event_id':'ho-paper-override-f','event_time':'2026-11-20T12:04:00Z'}
        capacity.ingest_handover(self.db,[rev])
        snap=self.base_snapshot(self.t1205)
        f=claim(snap,'F')
        # Revocation (12:04) precedes the query (12:05): no emergency route
        # remains; under the standard contract F is the sole top-priority
        # holder on a trusted-free bay with no predecessor release.
        self.assertEqual(f['emergency_contract']['status'],'not_applicable')
        self.assertEqual(f['standard_contract']['status'],'admittable')
        self.assertEqual(f['standard_contract']['reason'],'sole_active_order_with_trusted_free_bay')
        # Re-adjudicating as of 12:03:30 still sees the original override as
        # the live paper, but no trusted reading exists yet, so neither
        # contract may admit entry on an unknown bay.
        earlier=self.base_snapshot('2026-11-20T12:03:30Z')
        e=claim(earlier,'F')
        self.assertEqual(e['emergency_contract']['status'],'under_investigation')
        self.assertEqual(e['standard_contract']['status'],'under_investigation')
        # And an as-of snapshot between the 12:05 free reading run later
        # reconstructs the revoked state identically (decisions are replayed
        # from immutable revisions, not from a mutable current row).
        again=self.base_snapshot(self.t1205)
        self.assertEqual(claim(again,'F')['emergency_contract']['status'],'not_applicable')

    # --- late high-priority order re-adjudicates -------------------------- #
    def test_late_high_priority_order_flips_verdict_at_event_time(self):
        # Remove G by only loading C and F initially, then deliver G late.
        with self.db:
            self.db.execute("DELETE FROM capacity_window WHERE order_id='G'")
            self.db.execute("DELETE FROM capacity_decision WHERE order_id='G'")
            self.db.execute("DELETE FROM capacity_order WHERE order_id='G'")
            self.db.execute("DELETE FROM capacity_revision WHERE order_id='G'")
            self.db.execute("DELETE FROM evidence WHERE event_id='ho-cap-g'")
        before=self.base_snapshot('2026-11-20T12:45:00Z')
        # Without G (and without any override paper here), F is the sole
        # top-priority holder on a trusted-free bay and may enter.
        self.assertEqual(claim(before,'F')['standard_contract']['status'],'admittable')
        capacity.ingest(self.db,[fixture('handover_orders.json')[2]])
        after=self.base_snapshot(self.t1305)
        # Once the equally-priority G arrives late, neither can win by
        # lexicographic order_id: both are conflicted without handover papers.
        self.assertEqual(claim(after,'F')['standard_contract']['status'],'conflict')
        self.assertEqual(claim(after,'G')['standard_contract']['status'],'conflict')

    # --- standard contract needs both papers and clear bay ---------------- #
    def test_release_without_takeover_keeps_successor_waiting(self):
        capacity.ingest_handover(self.db,[fixture('handover_return.json')[0]])
        # After both fire windows end, C is sole active order but still carries
        # displacement history and never completed a takeover round.
        snap=self.base_snapshot(self.t1530)
        c=claim(snap,'C')
        self.assertEqual(c['standard_contract']['status'],'awaiting_handover')

    # --- SQLite persistence of papers and decision revisions -------------- #
    def test_papers_and_decision_revisions_are_persisted(self):
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        self.base_snapshot(self.t1205,save=True)
        row=self.db.execute("SELECT doc_type,status,override_version FROM handover_document WHERE event_id='ho-paper-override-f'").fetchone()
        self.assertEqual(tuple(row),('emergency_override','valid','EO-F-0001'))
        revs=self.db.execute("SELECT contract,status,reason FROM handover_decision_revision WHERE bay_id='A' AND order_id='F' ORDER BY contract").fetchall()
        got={r['contract']:(r['status'],r['reason']) for r in revs}
        self.assertEqual(got['standard'][0],'awaiting_handover')
        self.assertEqual(got['emergency'][0],'admittable')
        # Append-only: re-running keeps both adjudication versions queryable.
        self.base_snapshot(self.t1206,save=True)
        count=self.db.execute("SELECT COUNT(*) FROM handover_decision_revision").fetchone()[0]
        self.assertGreaterEqual(count,4)

    def test_emergency_obligation_only_closes_on_predecessor_release(self):
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        # The emergency admission books C's unfinished release duty.
        self.assertEqual(len(self.base_snapshot(self.t1205)['open_emergency_obligations']),1)
        # The fire order's own return papers do not discharge C's duty.
        capacity.ingest_handover(self.db,fixture('handover_return.json'))
        still_open=self.base_snapshot(self.t1530)['open_emergency_obligations']
        self.assertEqual(still_open[0]['displaced_order_id'],'C')
        # C signing its belated release for the same bay/override closes it.
        capacity.ingest_handover(self.db,fixture('handover_discharge.json'))
        self.assertEqual(self.base_snapshot('2026-11-20T15:45:00Z')['open_emergency_obligations'],[])

    def test_override_on_non_fire_order_is_rejected(self):
        # An override paper naming construction order C as the emergency
        # target cannot create an emergency contract for anyone.
        p=override_paper(event_id='ov-not-fire',override_version='EO-NOT-FIRE')
        p['to_order_id']='C';p['to_order_version']='ho-cap-c'
        p['from_order_id']='F';p['from_order_version']='ho-cap-f'
        capacity.ingest_handover(self.db,[p])
        snap=self.base_snapshot(self.t1205)
        rejected={d['event_id']:d['reason'] for d in snap['rejected_documents']}
        self.assertEqual(rejected['ov-not-fire'],'override_target_not_fire_order')

    def test_report_embeds_handover_without_changing_availability(self):
        capacity.ingest_handover(self.db,fixture('handover_override.json'))
        r=report.decide(self.db,self.t1205,3,save=True)
        self.assertEqual(r['capacity']['reserved_count'],1)
        self.assertEqual(r['handover']['claims'][0]['bay_id'],'A')
        self.assertEqual(len(report.history(self.db)),1)


if __name__=='__main__':unittest.main()
