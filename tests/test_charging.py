import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import parking
from parking import sensor,gate,capacity,report,charging
from parking.__main__ import main as cli_main
from parking.sources import EvidenceConflict
from parking.store import connect

assert Path(parking.__file__).resolve().is_relative_to(ROOT)

AS_OF='2026-11-17T10:10:00Z'


def fixture(name):
    return json.loads((ROOT/'fixtures'/'parking'/name).read_text(encoding='utf-8'))


def fault(event_id,gun,state,occurred,source='scada-1'):
    return {'event_id':event_id,'op':'fault','gun_id':gun,'state':state,'source':source,'event_time':occurred}


class ChargingCases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db_path=Path(self.tmp.name)/'case.sqlite'
        self.db=connect(self.db_path)

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def base(self):
        sensor.ingest(self.db,fixture('charge_sensor.json'))
        gate.ingest(self.db,fixture('charge_gate.json'))
        charging.ingest(self.db,fixture('charge_equipment.json'))

    def single(self,as_of=AS_OF,power=7.0,ctype='dc'):
        return charging.decide(self.db,as_of,3,ctype,power,'single')

    def fleet(self,as_of=AS_OF,power=7.0,ctype='dc'):
        return charging.decide(self.db,as_of,3,ctype,power,'fleet')

    def test_regular_availability_is_two_in_base_scenario(self):
        self.base()
        r=report.decide(self.db,AS_OF,3)
        self.assertEqual((r['state'],r['available']),('published',2))

    def test_single_contract_lists_each_candidate_bay(self):
        self.base()
        r=self.single()
        self.assertEqual((r['state'],r['candidate_count']),('published',2))
        self.assertEqual([c['bay_id'] for c in r['candidates']],['A','B'])
        self.assertNotIn('max_vehicles',r)
        self.assertEqual(r['evidence']['regular_report']['available'],2)

    def test_fleet_contract_shared_circuit_admits_only_one(self):
        self.base()
        r=self.fleet()
        self.assertEqual((r['state'],r['max_vehicles']),('published',1))
        self.assertEqual(len(r['assignment']),1)
        self.assertNotIn('candidate_count',r)
        self.assertEqual(r['evidence']['circuits']['L1']['slots'],1)

    def test_running_session_drains_circuit_then_late_end_recomputes(self):
        self.base()
        before=self.fleet()
        self.assertEqual(before['max_vehicles'],1)
        charging.ingest(self.db,fixture('charge_session_c.json'))
        drained=self.fleet()
        self.assertEqual(drained['max_vehicles'],0)
        self.assertEqual(self.single()['candidate_count'],0)
        self.assertEqual(drained['evidence']['circuits']['L1']['remaining_kw'],0.0)
        end={'event_id':'csess-2','op':'session','session_id':'sess-c','bay_id':'C','gun_id':'gC',
             'charging_type':'dc','power_kw':7,'started_at':'2026-11-17T09:55:00Z',
             'ended_at':'2026-11-17T10:05:00Z','event_time':'2026-11-17T10:06:00Z'}
        charging.ingest(self.db,[end])
        recovered=self.fleet()
        self.assertEqual(recovered['max_vehicles'],1)
        during=self.fleet('2026-11-17T10:00:00Z')
        self.assertEqual(during['max_vehicles'],0)
        same_moment=[h for h in charging.history(self.db) if h['as_of']==AS_OF and h['contract']=='fleet']
        self.assertEqual([json.loads(h['published'])['max_vehicles'] for h in same_moment],[1,0,1])

    def test_fleet_assignment_is_maximum_not_first_gun_greedy(self):
        self.base()
        charging.ingest(self.db,fixture('charge_equipment_independent.json'))
        r=self.fleet()
        self.assertEqual(r['max_vehicles'],2)
        self.assertEqual(r['assignment'],[{'bay_id':'A','gun_id':'gD'},{'bay_id':'B','gun_id':'gB'}])
        s=self.single()
        self.assertEqual(s['candidate_count'],2)
        self.assertEqual({c['bay_id']:c['guns'] for c in s['candidates']},{'A':['gA','gD'],'B':['gB']})

    def test_gun_fault_excludes_candidate_and_replay_is_idempotent(self):
        self.base()
        event=fault('flt-1','gA','fault','2026-11-17T09:30:00Z')
        self.assertEqual(charging.ingest(self.db,[event])['imported'],1)
        r=self.single()
        self.assertEqual([c['bay_id'] for c in r['candidates']],['B'])
        self.assertEqual(r['evidence']['guns']['gA']['health_event'],'flt-1')
        self.assertEqual(self.fleet()['max_vehicles'],1)
        self.assertEqual(charging.ingest(self.db,[event])['imported'],0)
        self.assertEqual([c['bay_id'] for c in self.single()['candidates']],['B'])

    def test_late_recovery_does_not_override_newer_fault(self):
        self.base()
        charging.ingest(self.db,[fault('flt-new','gA','fault','2026-11-17T09:40:00Z')])
        charging.ingest(self.db,[fault('flt-old','gA','recovery','2026-11-17T09:20:00Z')])
        self.assertEqual([c['bay_id'] for c in self.single()['candidates']],['B'])
        charging.ingest(self.db,[fault('flt-rec','gA','recovery','2026-11-17T09:50:00Z')])
        self.assertEqual([c['bay_id'] for c in self.single()['candidates']],['A','B'])

    def test_late_fault_recomputes_same_moment_and_keeps_both_releases(self):
        self.base()
        first=self.fleet()
        self.assertEqual((first['max_vehicles'],first['assignment']), (1,[{'bay_id':'A','gun_id':'gA'}]))
        charging.ingest(self.db,[fault('flt-late','gA','fault','2026-11-17T10:00:00Z')])
        second=self.fleet()
        self.assertEqual(second['assignment'],[{'bay_id':'B','gun_id':'gB'}])
        rows=[h for h in charging.history(self.db) if h['as_of']==AS_OF]
        self.assertEqual(len(rows),2)
        self.assertNotEqual(rows[0]['published'],rows[1]['published'])

    def test_same_event_id_with_new_content_conflicts_and_rolls_back(self):
        self.base()
        event=fault('flt-1','gA','fault','2026-11-17T09:30:00Z')
        charging.ingest(self.db,[event])
        with self.assertRaises(EvidenceConflict):
            charging.ingest(self.db,[dict(event,state='recovery')])
        self.assertEqual([c['bay_id'] for c in self.single()['candidates']],['B'])

    def test_capacity_order_removes_bay_from_candidates(self):
        self.base()
        capacity.ingest(self.db,[{'event_id':'cap-b','order_id':'ob','purpose':'construction','priority':5,
                                  'approved':True,'revoked':False,'bays':['B'],
                                  'starts_at':'2026-11-17T09:30:00Z','ends_at':'2026-11-17T11:00:00Z',
                                  'event_time':'2026-11-17T09:30:00Z'}])
        r=self.single()
        self.assertEqual([c['bay_id'] for c in r['candidates']],['A'])
        self.assertEqual(self.fleet()['max_vehicles'],1)
        self.assertEqual(r['evidence']['capacity']['active_orders'],['ob'])
        self.assertEqual(r['evidence']['regular_report']['available'],1)

    def test_unknown_geomagnetic_is_not_published_as_zero(self):
        gate.ingest(self.db,fixture('charge_gate.json'))
        charging.ingest(self.db,fixture('charge_equipment.json'))
        r=self.single()
        self.assertEqual((r['state'],r['reason']),('unknown','sensor_unknown'))
        self.assertIsNone(r['candidates'])
        f=self.fleet()
        self.assertEqual((f['state'],f['max_vehicles']),('unknown',None))
        row=charging.history(self.db)[0]
        self.assertEqual((row['state'],row['published']),('unknown',None))

    def test_fleet_undetermined_when_a_candidate_bay_sensor_is_missing(self):
        sensor.ingest(self.db,[e for e in fixture('charge_sensor.json') if e.get('bay_id')=='A'])
        gate.ingest(self.db,fixture('charge_gate.json'))
        charging.ingest(self.db,[
            {'event_id':'eq-a','op':'equipment','gun_id':'gA','bays':['A'],'charging':[{'type':'dc','power_kw':7}],
             'circuit_id':'L1','circuit_rating_kw':7,'event_time':'2026-11-17T09:00:00Z'},
            {'event_id':'eq-b','op':'equipment','gun_id':'gB','bays':['B'],'charging':[{'type':'dc','power_kw':7}],
             'circuit_id':'L2','circuit_rating_kw':7,'event_time':'2026-11-17T09:00:00Z'}])
        s=self.single()
        self.assertEqual((s['state'],s['candidate_count']),('published',1))
        self.assertEqual(s['evidence']['undetermined_bays'],['B'])
        f=self.fleet()
        self.assertEqual((f['state'],f['reason']),('unknown','sensor_unknown'))
        self.assertIsNone(f['max_vehicles'])

    def test_shared_circuit_bounds_undetermined_bays(self):
        sensor.ingest(self.db,[e for e in fixture('charge_sensor.json') if e.get('bay_id')=='A'])
        gate.ingest(self.db,fixture('charge_gate.json'))
        charging.ingest(self.db,fixture('charge_equipment.json'))
        f=self.fleet()
        self.assertEqual((f['state'],f['max_vehicles']),('published',1))
        s=self.single()
        self.assertEqual([c['bay_id'] for c in s['candidates']],['A'])
        self.assertEqual(s['evidence']['undetermined_bays'],['B','C'])

    def test_frozen_regular_availability_withholds_charging(self):
        self.base()
        gate.ingest(self.db,[{'event_id':'cgate-orphan','stay_id':'ghost','direction':'out',
                              'event_time':'2026-11-17T10:00:00Z'}])
        r=self.single()
        self.assertEqual(r['state'],'withheld')
        self.assertIsNone(r['candidates'])
        self.assertIn('orphan_exit',r['reason'])
        f=self.fleet()
        self.assertEqual((f['state'],f['max_vehicles']),('withheld',None))
        self.assertIn('frozen',f['reason'])

    def test_capacity_breach_withholds_charging(self):
        self.base()
        capacity.ingest(self.db,[{'event_id':'cap-all','order_id':'oa','purpose':'fire','priority':9,
                                  'approved':True,'revoked':False,'bays':['A','B','C'],
                                  'starts_at':'2026-11-17T09:30:00Z','ends_at':'2026-11-17T11:00:00Z',
                                  'event_time':'2026-11-17T09:30:00Z'}])
        regular=report.decide(self.db,AS_OF,3,save=False)
        self.assertEqual(regular['state'],'capacity_breach')
        f=self.fleet()
        self.assertEqual((f['state'],f['max_vehicles']),('withheld',None))
        self.assertIn('capacity_breach',f['reason'])

    def test_session_validation_checks_bay_gun_and_circuit(self):
        self.base()
        base={'event_id':'s-bad','op':'session','session_id':'x1','bay_id':'B','gun_id':'gA',
              'charging_type':'dc','power_kw':7,'started_at':'2026-11-17T09:00:00Z','ended_at':None,
              'event_time':'2026-11-17T09:05:00Z'}
        with self.assertRaises(ValueError):charging.ingest(self.db,[base])
        with self.assertRaises(ValueError):charging.ingest(self.db,[dict(base,event_id='s-b2',session_id='x2',bay_id='A',gun_id='ghost')])
        with self.assertRaises(ValueError):charging.ingest(self.db,[dict(base,event_id='s-b3',session_id='x3',bay_id='A',circuit_id='L9')])
        with self.assertRaises(ValueError):charging.ingest(self.db,[dict(base,event_id='s-b4',session_id='x4',bay_id='A',charging_type='ac')])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM charge_session').fetchone()[0],0)

    def test_session_replay_idempotent_and_same_id_conflict(self):
        self.base()
        events=fixture('charge_session_c.json')
        self.assertEqual(charging.ingest(self.db,events)['imported'],1)
        self.assertEqual(charging.ingest(self.db,events)['imported'],0)
        with self.assertRaises(EvidenceConflict):
            charging.ingest(self.db,[dict(events[0],power_kw=3)])

    def test_power_thresholds_and_circuit_slot_arithmetic(self):
        self.base()
        self.assertEqual(self.fleet(power=3.0)['max_vehicles'],2)
        weak=self.single(power=8.0)
        self.assertEqual((weak['state'],weak['candidate_count']),('published',0))
        self.assertEqual(self.fleet(power=8.0)['max_vehicles'],0)
        self.assertEqual(self.single(ctype='ac')['candidate_count'],0)

    def test_charging_evidence_does_not_change_regular_report(self):
        self.base()
        charging.ingest(self.db,fixture('charge_session_c.json'))
        charging.ingest(self.db,[fault('flt-1','gA','fault','2026-11-17T09:30:00Z')])
        r=report.decide(self.db,AS_OF,3)
        self.assertEqual((r['state'],r['available']),('published',2))

    def test_cli_import_and_charge_report_json(self):
        buf=io.StringIO()
        with redirect_stdout(buf):
            rc=cli_main(['import-charging','--db',str(self.db_path),
                         '--input',str(ROOT/'fixtures'/'parking'/'charge_equipment.json')])
        self.assertEqual(rc,0)
        self.assertEqual(json.loads(buf.getvalue())['imported'],3)
        sensor.ingest(self.db,fixture('charge_sensor.json'))
        gate.ingest(self.db,fixture('charge_gate.json'))
        buf=io.StringIO()
        with redirect_stdout(buf):
            rc=cli_main(['charge-report','--db',str(self.db_path),'--as-of',AS_OF,'--total-bays','3',
                         '--charging-type','dc','--min-power-kw','7','--contract','fleet'])
        self.assertEqual(rc,0)
        out=json.loads(buf.getvalue())
        self.assertEqual((out['contract'],out['state'],out['max_vehicles']),('fleet','published',1))
        buf=io.StringIO()
        with redirect_stdout(buf):
            rc=cli_main(['charge-report','--db',str(self.db_path),'--as-of',AS_OF,'--total-bays','3',
                         '--charging-type','dc','--min-power-kw','7','--contract','single'])
        self.assertEqual(rc,0)
        out=json.loads(buf.getvalue())
        self.assertEqual((out['contract'],out['candidate_count']),('single',2))


if __name__=='__main__':unittest.main()
