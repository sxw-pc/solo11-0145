import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import parking
from parking import sensor,gate,verify,report
from parking.sources import EvidenceConflict
from parking.store import connect,rows
from parking.__main__ import main as cli_main

assert Path(parking.__file__).resolve().is_relative_to(ROOT)


def fixture(name):
    return json.loads((ROOT/'fixtures'/'parking'/name).read_text(encoding='utf-8'))


AS_OF_FIRST='2026-11-17T10:10:00Z'
AS_OF_SECOND='2026-11-17T11:10:00Z'
EFFECTIVE='2026-11-17T12:00:00Z'
AS_OF_FIXED='2026-11-17T12:30:00Z'


def post_issuance_readings():
    # Devices keep reporting their stale bay_id after the physical swap;
    # re-projection by device identity must still place them correctly.
    return [
        {'event_id':'read-1210-d1','op':'reading','device_id':'d1','bay_id':'A','occupied':True,'sequence_no':3,'event_time':'2026-11-17T12:10:00Z'},
        {'event_id':'read-1210-d2','op':'reading','device_id':'d2','bay_id':'B','occupied':False,'sequence_no':3,'event_time':'2026-11-17T12:10:00Z'},
    ]


def three_bay_events():
    return [
        {'event_id':'dev-da','op':'device','device_id':'da','bay_id':'A','calibrated_until':'2026-11-18T00:00:00Z','event_time':'2026-11-17T08:00:00Z'},
        {'event_id':'dev-db','op':'device','device_id':'db','bay_id':'B','calibrated_until':'2026-11-18T00:00:00Z','event_time':'2026-11-17T08:00:00Z'},
        {'event_id':'dev-dc','op':'device','device_id':'dc','bay_id':'C','calibrated_until':'2026-11-18T00:00:00Z','event_time':'2026-11-17T08:00:00Z'},
        {'event_id':'read-a','op':'reading','device_id':'da','bay_id':'A','occupied':True,'sequence_no':1,'event_time':'2026-11-17T10:00:00Z'},
        {'event_id':'read-b','op':'reading','device_id':'db','bay_id':'B','occupied':False,'sequence_no':1,'event_time':'2026-11-17T10:00:00Z'},
        {'event_id':'read-c','op':'reading','device_id':'dc','bay_id':'C','occupied':False,'sequence_no':1,'event_time':'2026-11-17T10:00:00Z'},
    ]


def three_bay_trial():
    return [{'event_id':'trial-3bay','op':'trial','trial_id':'t3b',
             'window_start':'2026-11-17T09:55:00Z','window_end':'2026-11-17T10:05:00Z',
             'executor':'alice','witness':'bob',
             'confirmed':{'A':'occupied','B':'free','C':'free'},
             'readings':[{'device_id':'da','sequence_no':1,'occupied':True,'observed_at':'2026-11-17T10:00:00Z'},
                         {'device_id':'db','sequence_no':1,'occupied':False,'observed_at':'2026-11-17T10:00:00Z'},
                         {'device_id':'dc','sequence_no':1,'occupied':False,'observed_at':'2026-11-17T10:00:00Z'}],
             'event_time':'2026-11-17T10:06:00Z'}]


class VerificationCases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'case.sqlite')

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def swapped(self):
        sensor.ingest(self.db,fixture('sensor_swapped.json'))
        gate.ingest(self.db,fixture('gate_good.json'))

    def revisions(self,device):
        return rows(self.db,'SELECT bay_id,valid_from FROM sensor_revision WHERE device_id=? ORDER BY valid_from,event_id',(device,))

    def test_swapped_devices_publish_correct_total_with_reversed_states(self):
        # Runnable counterexample: totals reconcile with the gate while the
        # per-bay states are exactly backwards, and nothing proves position.
        self.swapped()
        r=report.decide(self.db,AS_OF_FIRST,2)
        self.assertEqual((r['state'],r['available']),('published',1))
        self.assertEqual((r['sensor']['occupied'],r['gate']['active']),(1,1))
        self.assertEqual(r['sensor']['states']['A']['state'],'free')
        self.assertEqual(r['sensor']['states']['B']['state'],'occupied')
        self.assertEqual(r['binding']['bays']['A']['binding'],'unproven')
        self.assertEqual(r['binding']['bays']['A']['reason'],'no_valid_trials')
        r2=report.decide(self.db,AS_OF_SECOND,2)
        self.assertEqual((r2['state'],r2['sensor']['states']['A']['state'],r2['sensor']['states']['B']['state']),
                         ('published','occupied','free'))

    def test_dual_trial_same_executor_stays_pending(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_same_executor.json'))
        v=verify.issue(self.db,'dual_trial',['A','B'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['reason']),('rejected','insufficient_independent_trials'))
        self.assertEqual(v['feasible_count'],1)
        self.assertEqual(self.revisions('d1'),[{'bay_id':'A','valid_from':'2026-11-17T08:00:00Z'}])
        self.assertEqual(sensor.bay_state(self.db,'A',AS_OF_FIRST)['state'],'free')

    def test_inspection_cosign_corrects_per_bay_states(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_same_executor.json'))
        verify.ingest(self.db,fixture('handover_correct.json'))
        v=verify.issue(self.db,'inspection_cosign',['A','B'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['reason']),('issued','unique_mapping_proven'))
        self.assertEqual(v['mapping'],{'d1':'B','d2':'A'})
        self.assertEqual({a['device_id']:a['bay_id'] for a in v['applied']},{'d1':'B','d2':'A'})
        # Binding versions: old revisions preserved, new ones effective at 12:00.
        self.assertEqual([r['bay_id'] for r in self.revisions('d1')],['A','B'])
        self.assertEqual([r['bay_id'] for r in self.revisions('d2')],['B','A'])
        self.assertEqual(self.revisions('d1')[1]['valid_from'],EFFECTIVE)
        # New readings still carry the stale device-reported bay_id, yet the
        # d2 reading stored against B must surface on A and vice versa.
        sensor.ingest(self.db,post_issuance_readings())
        self.assertEqual(sensor.bay_state(self.db,'A',AS_OF_FIXED)['state'],'free')
        self.assertEqual(sensor.bay_state(self.db,'A',AS_OF_FIXED)['event_id'],'read-1210-d2')
        self.assertEqual(sensor.bay_state(self.db,'B',AS_OF_FIXED)['state'],'occupied')
        self.assertEqual(sensor.bay_state(self.db,'B',AS_OF_FIXED)['event_id'],'read-1210-d1')
        r=report.decide(self.db,AS_OF_FIXED,2)
        self.assertEqual((r['state'],r['available']),('published',1))
        self.assertEqual((r['sensor']['states']['A']['state'],r['sensor']['states']['B']['state']),('free','occupied'))
        self.assertEqual(r['binding']['bays']['A']['binding'],'proven')
        self.assertEqual(r['binding']['bays']['B']['binding'],'proven')
        # Re-issue is idempotent: no extra revisions, adjudication still logged.
        again=verify.issue(self.db,'inspection_cosign',['A','B'],EFFECTIVE)
        self.assertEqual((again['verdict'],again['applied']),('issued',[]))
        self.assertEqual(len(self.revisions('d1')),2)
        self.assertEqual(len(rows(self.db,'SELECT * FROM binding_issuance')),2)

    def test_historical_as_of_keeps_old_binding(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_two_crews.json'))
        verify.issue(self.db,'dual_trial',['A','B'],EFFECTIVE)
        sensor.ingest(self.db,post_issuance_readings())
        # Earlier as_of still resolves through the pre-verification revisions.
        self.assertEqual(sensor.bay_state(self.db,'A',AS_OF_FIRST)['state'],'free')
        self.assertEqual(sensor.bay_state(self.db,'B',AS_OF_FIRST)['state'],'occupied')
        self.assertEqual(sensor.bay_state(self.db,'A',AS_OF_SECOND)['state'],'occupied')
        self.assertEqual(sensor.bay_state(self.db,'B',AS_OF_SECOND)['state'],'free')
        earlier=report.decide(self.db,AS_OF_FIRST,2)
        self.assertEqual(earlier['sensor']['states']['A']['state'],'free')
        self.assertEqual(earlier['binding']['bays']['A']['binding'],'unproven')
        # After the effective time the same raw readings project via the new binding.
        self.assertEqual(sensor.bay_state(self.db,'A',AS_OF_FIXED)['state'],'free')
        self.assertEqual(sensor.bay_state(self.db,'B',AS_OF_FIXED)['state'],'occupied')

    def test_dual_trial_two_crews_issues(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_two_crews.json'))
        v=verify.issue(self.db,'dual_trial',['A','B'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['mapping']),('issued',{'d1':'B','d2':'A'}))
        # The co-sign contract still needs its independent handover certificate.
        c=verify.evaluate(self.db,'inspection_cosign',['A','B'])
        self.assertEqual((c['verdict'],c['reason']),('rejected','handover_missing'))

    def test_handover_contradicting_trial_blocks_cosign(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_two_crews.json'))
        verify.ingest(self.db,fixture('handover_wrong.json'))
        v=verify.issue(self.db,'inspection_cosign',['A','B'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['reason']),('rejected','handover_conflicts_trial'))
        self.assertEqual(len(self.revisions('d1')),1)
        # No majority vote: the certificate cannot force its own mapping either.
        self.assertEqual(sensor.bay_state(self.db,'A',AS_OF_FIXED)['state'],'unknown')

    def test_three_bay_interchangeable_devices_not_proven(self):
        sensor.ingest(self.db,three_bay_events())
        verify.ingest(self.db,three_bay_trial())
        v=verify.issue(self.db,'dual_trial',['A','B','C'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['reason']),('rejected','ambiguous_mappings'))
        self.assertEqual(v['feasible_count'],2)
        s=verify.status(self.db,AS_OF_FIRST)
        self.assertEqual(len(s['candidate_mappings']),2)
        for bay in ('A','B','C'):
            self.assertEqual((s['bays'][bay]['binding'],s['bays'][bay]['reason']),('unproven','ambiguous_mappings'))
        self.assertEqual(len(rows(self.db,'SELECT * FROM sensor_revision')),3)

    def test_late_conflicting_trial_contests_issuance(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_two_crews.json'))
        verify.ingest(self.db,fixture('handover_correct.json'))
        verify.issue(self.db,'inspection_cosign',['A','B'],EFFECTIVE)
        verify.ingest(self.db,fixture('verify_trial_conflicting.json'))
        v=verify.issue(self.db,'inspection_cosign',['A','B'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['reason']),('rejected','contradictory_trials'))
        s=verify.status(self.db,AS_OF_FIXED)
        self.assertEqual(s['bays']['A']['binding'],'contested')
        self.assertEqual(s['bays']['B']['binding'],'contested')
        # All evidence and both adjudications are preserved.
        self.assertEqual(len(rows(self.db,'SELECT * FROM field_trial')),3)
        self.assertEqual([r['verdict'] for r in rows(self.db,'SELECT verdict FROM binding_issuance ORDER BY issuance_id')],
                         ['issued','rejected'])
        self.assertEqual(len(self.revisions('d1')),2)

    def test_device_fault_invalidates_binding_evidence(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_two_crews.json'))
        sensor.ingest(self.db,[{'event_id':'fault-d2','op':'fault','device_id':'d2','faulty':True,
                                'event_time':'2026-11-17T09:45:00Z'}])
        v=verify.issue(self.db,'dual_trial',['A','B'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['reason']),('rejected','device_bay_mismatch'))
        self.assertEqual(sensor.bay_state(self.db,'B',AS_OF_FIRST)['reason'],'device_fault')
        self.assertEqual(sensor.snapshot(self.db,AS_OF_FIRST)['states']['B']['state'],'unknown')

    def test_readings_outside_window_or_expired_calibration_are_not_evidence(self):
        sensor.ingest(self.db,[
            {'event_id':'dev-d1','op':'device','device_id':'d1','bay_id':'A','calibrated_until':'2026-11-17T10:30:00Z','event_time':'2026-11-17T08:00:00Z'},
            {'event_id':'dev-d2','op':'device','device_id':'d2','bay_id':'B','calibrated_until':'2026-11-17T10:30:00Z','event_time':'2026-11-17T08:00:00Z'}])
        verify.ingest(self.db,[
            {'event_id':'trial-late','op':'trial','trial_id':'tl',
             'window_start':'2026-11-17T09:55:00Z','window_end':'2026-11-17T10:05:00Z',
             'executor':'alice','witness':'bob','confirmed':{'A':'occupied','B':'free'},
             'readings':[{'device_id':'d1','sequence_no':1,'occupied':True,'observed_at':'2026-11-17T11:00:00Z'},
                         {'device_id':'d2','sequence_no':1,'occupied':False,'observed_at':'2026-11-17T11:00:00Z'}],
             'event_time':'2026-11-17T10:06:00Z'},
            {'event_id':'trial-expired','op':'trial','trial_id':'te',
             'window_start':'2026-11-17T10:35:00Z','window_end':'2026-11-17T10:45:00Z',
             'executor':'carol','witness':'dave','confirmed':{'A':'occupied','B':'free'},
             'readings':[{'device_id':'d1','sequence_no':2,'occupied':True,'observed_at':'2026-11-17T10:40:00Z'},
                         {'device_id':'d2','sequence_no':2,'occupied':False,'observed_at':'2026-11-17T10:40:00Z'}],
             'event_time':'2026-11-17T10:46:00Z'}])
        v=verify.issue(self.db,'dual_trial',['A','B'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['reason']),('rejected','no_valid_trials'))
        s=verify.status(self.db,AS_OF_FIRST)
        problems={t['event_id']:t['problems'] for t in s['trial_findings']}
        self.assertIn('outside_window:d1',problems['trial-late'])
        self.assertIn('calibration_expired:d1',problems['trial-expired'])

    def test_same_event_id_different_body_rolls_back(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_two_crews.json'))
        changed=dict(fixture('verify_trials_two_crews.json')[0],witness='mallory')
        extra=dict(fixture('verify_trials_two_crews.json')[1],event_id='trial-9',trial_id='t9')
        with self.assertRaises(EvidenceConflict):
            verify.ingest(self.db,[extra,changed])
        self.assertEqual(len(rows(self.db,'SELECT * FROM field_trial')),2)
        self.assertEqual(rows(self.db,"SELECT witness FROM field_trial WHERE event_id='trial-1'")[0]['witness'],'bob')
        v=verify.issue(self.db,'dual_trial',['A','B'],EFFECTIVE)
        self.assertEqual(v['verdict'],'issued')

    def test_handover_written_after_trial_is_not_independent(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_two_crews.json'))
        late=dict(fixture('handover_correct.json')[0],event_id='handover-late',
                  certificate_id='cert-late',event_time='2026-11-17T12:00:00Z')
        verify.ingest(self.db,[late])
        v=verify.issue(self.db,'inspection_cosign',['A','B'],EFFECTIVE)
        self.assertEqual((v['verdict'],v['reason']),('rejected','handover_missing'))

    def test_report_carries_candidate_mappings_and_reasons(self):
        self.swapped()
        verify.ingest(self.db,fixture('verify_trials_two_crews.json'))
        r=report.decide(self.db,AS_OF_FIRST,2)
        self.assertEqual(r['binding']['candidate_mappings'],[{'d1':'B','d2':'A'}])
        self.assertEqual(r['binding']['bays']['A']['reason'],'unique_mapping_not_issued')
        self.assertEqual(r['binding']['valid_trials'],2)
        self.assertIn('d1',r['binding']['binding_history'])


class VerifyCliCases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db_path=str(Path(self.tmp.name)/'cli.sqlite')

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self,*argv):
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            code=cli_main(list(argv))
        return code,json.loads(buf.getvalue())

    def test_cli_verification_flow(self):
        code,out=self.run_cli('init','--db',self.db_path)
        self.assertEqual(code,0)
        code,out=self.run_cli('import-sensor','--db',self.db_path,'--input',str(ROOT/'fixtures'/'parking'/'sensor_swapped.json'))
        self.assertEqual((code,out['imported']),(0,6))
        code,out=self.run_cli('import-verify','--db',self.db_path,'--input',str(ROOT/'fixtures'/'parking'/'verify_trials_same_executor.json'))
        self.assertEqual((code,out['trials']),(0,2))
        code,out=self.run_cli('verify-issue','--db',self.db_path,'--contract','dual_trial',
                              '--bays','A,B','--effective-at',EFFECTIVE)
        self.assertEqual((code,out['verdict'],out['reason']),(0,'rejected','insufficient_independent_trials'))
        code,out=self.run_cli('import-verify','--db',self.db_path,'--input',str(ROOT/'fixtures'/'parking'/'handover_correct.json'))
        self.assertEqual((code,out['certificates']),(0,1))
        code,out=self.run_cli('verify-issue','--db',self.db_path,'--contract','inspection_cosign',
                              '--bays','A,B','--effective-at',EFFECTIVE)
        self.assertEqual((code,out['verdict'],out['mapping']),(0,'issued',{'d1':'B','d2':'A'}))
        code,out=self.run_cli('verify-status','--db',self.db_path,'--as-of',AS_OF_FIXED)
        self.assertEqual(code,0)
        self.assertEqual(out['bays']['A']['binding'],'proven')
        self.assertEqual(out['candidate_mappings'],[{'d1':'B','d2':'A'}])
        code,out=self.run_cli('report','--db',self.db_path,'--as-of',AS_OF_FIXED,'--total-bays','2')
        self.assertEqual(code,0)
        self.assertEqual(out['binding']['bays']['B']['binding'],'proven')


if __name__=='__main__':unittest.main()
