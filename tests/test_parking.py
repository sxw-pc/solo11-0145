import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import parking
from parking import sensor,gate,capacity,report
from parking.sources import EvidenceConflict
from parking.store import connect

print('imported module:',parking.__file__)
assert Path(parking.__file__).resolve().is_relative_to(ROOT)


def fixture(name):
    return json.loads((ROOT/'fixtures'/'parking'/name).read_text(encoding='utf-8'))


class ParkingCases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'case.sqlite')

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def good(self):
        sensor.ingest(self.db,fixture('sensor_good.json'))
        gate.ingest(self.db,fixture('gate_good.json'))
        capacity.ingest(self.db,fixture('capacity_good.json'))

    def test_three_ledgers_publish_one_space(self):
        self.good()
        r=report.decide(self.db,'2026-11-17T10:10:00Z',3)
        self.assertEqual((r['state'],r['available'],r['capacity']['reserved_count']),('published',1,1))
        self.assertEqual(len(report.history(self.db)),1)

    def test_late_reading_does_not_replace_new_occupied(self):
        self.good();sensor.ingest(self.db,fixture('sensor_late.json'))
        r=report.decide(self.db,'2026-11-17T10:10:00Z',3)
        self.assertEqual(r['sensor']['states']['b1']['state'],'occupied')
        self.assertEqual(r['available'],1)

    def test_sensor_timeout_with_complete_gate_uses_bound(self):
        self.good()
        r=report.decide(self.db,'2026-11-17T10:45:00Z',3)
        self.assertEqual((r['state'],r['available']),('conservative',1))

    def test_orphan_exit_does_not_raise_availability(self):
        self.good();gate.ingest(self.db,fixture('gate_orphan.json'))
        r=report.decide(self.db,'2026-11-17T10:10:00Z',3)
        self.assertEqual((r['state'],r['reason'],r['available']),('frozen','orphan_exit',None))
        self.assertEqual(r['gate']['active'],1)

    def test_two_incomplete_ledgers_give_unknown(self):
        self.good();gate.ingest(self.db,fixture('gate_orphan.json'))
        r=report.decide(self.db,'2026-11-17T10:45:00Z',3)
        self.assertEqual((r['state'],r['available']),('unknown',None))

    def test_overlapping_capacity_is_set_union(self):
        self.good();capacity.ingest(self.db,fixture('capacity_overlap.json'))
        r=report.decide(self.db,'2026-11-17T10:10:00Z',3)
        self.assertEqual((r['capacity']['reserved_count'],r['available']),(1,1))
        self.assertEqual(r['capacity']['owner_by_bay']['b3']['order_id'],'o2')

    def test_revoke_capacity_restores_one_slot(self):
        self.good()
        revoke=dict(fixture('capacity_good.json')[0],event_id='capacity-revoke',event_time='2026-11-17T10:05:00Z',revoked=True)
        capacity.ingest(self.db,[revoke])
        r=report.decide(self.db,'2026-11-17T10:10:00Z',3)
        self.assertEqual((r['capacity']['reserved_count'],r['available']),(0,2))
        earlier=report.decide(self.db,'2026-11-17T10:00:00Z',3)
        self.assertEqual(earlier['capacity']['reserved_count'],1)

    def test_revoke_does_not_clear_gate_discrepancy(self):
        self.good()
        revoke=dict(fixture('capacity_good.json')[0],event_id='capacity-revoke',event_time='2026-11-17T10:05:00Z',revoked=True)
        capacity.ingest(self.db,[revoke])
        extra=dict(fixture('gate_good.json')[1],event_id='gate-in-3',stay_id='s3')
        gate.ingest(self.db,[extra])
        r=report.decide(self.db,'2026-11-17T10:10:00Z',3)
        self.assertEqual((r['state'],r['reason']),('frozen','sensor_gate_discrepancy'))

    def test_duplicate_event_same_content_is_idempotent(self):
        self.good();self.assertEqual(gate.ingest(self.db,fixture('gate_good.json'))['imported'],0)
        self.assertEqual(gate.snapshot(self.db,'2026-11-17T10:10:00Z')['active'],1)

    def test_duplicate_id_new_payload_rolls_back_batch(self):
        self.good()
        modified=dict(fixture('gate_good.json')[0],direction='out')
        with self.assertRaises(EvidenceConflict):gate.ingest(self.db,[dict(modified,event_id='new-first'),modified])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM gate_event WHERE event_id='new-first'").fetchone()[0],0)

    def test_billable_cross_midnight_stay(self):
        gate.ingest(self.db,[{'event_id':'a','stay_id':'night','direction':'in','event_time':'2026-11-16T23:55:00Z'},
                             {'event_id':'b','stay_id':'night','direction':'out','event_time':'2026-11-17T00:15:00Z'}])
        r=gate.snapshot(self.db,'2026-11-17T10:10:00Z')
        self.assertEqual((r['active'],r['billable'][0]['to']),(0,'2026-11-17T00:15:00Z'))
        persisted=self.db.execute("SELECT entered_at,exited_at,state FROM vehicle_stay WHERE stay_id='night'").fetchone()
        self.assertEqual((persisted['entered_at'],persisted['exited_at'],persisted['state']),
                         ('2026-11-16T23:55:00Z','2026-11-17T00:15:00Z','closed'))

    def test_uncovered_bays_without_gate_baseline_are_unknown(self):
        sensor.ingest(self.db,[fixture('sensor_good.json')[1],fixture('sensor_good.json')[4]])
        r=report.decide(self.db,'2026-11-17T10:10:00Z',100)
        self.assertEqual((r['state'],r['available'],r['sensor']['uncovered']),('unknown',None,99))

    def test_certified_gate_baseline_can_cover_uninstrumented_bays(self):
        sensor.ingest(self.db,[fixture('sensor_good.json')[1],fixture('sensor_good.json')[4]])
        gate.ingest(self.db,[fixture('gate_good.json')[0]])
        r=report.decide(self.db,'2026-11-17T10:10:00Z',100)
        self.assertEqual((r['state'],r['available']),('conservative',100))

    def test_nonzero_baseline_and_exit_reduce_occupancy(self):
        gate.ingest(self.db,[{'event_id':'base','op':'baseline','count':2,'event_time':'2026-11-17T09:00:00Z'},
                             {'event_id':'in-old','stay_id':'s-old','direction':'in','event_time':'2026-11-17T08:00:00Z'},
                             {'event_id':'out-old','stay_id':'s-old','direction':'out','event_time':'2026-11-17T09:30:00Z'}])
        self.assertEqual(gate.snapshot(self.db,'2026-11-17T10:00:00Z')['active'],1)

    def test_orphan_exit_plus_capacity_reservation_creates_distinct_incident(self):
        gate.ingest(self.db,fixture('gate_capacity_breach.json'))
        before=report.decide(self.db,'2026-11-17T10:10:00Z',3)
        self.assertEqual((before['state'],before['gate']['active']),('unknown',2))
        capacity.ingest(self.db,fixture('capacity_breach.json'))
        after=report.decide(self.db,'2026-11-17T10:10:00Z',3)
        self.assertEqual((after['state'],after['capacity_breach'],after['available']),('capacity_breach',True,None))
        incident=self.db.execute('SELECT active_lower_bound,operable_capacity,decision FROM capacity_incident ORDER BY incident_id DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(incident),(2,1,'operator_investigation'))

    def test_untrusted_sensor_does_not_count_free(self):
        self.good()
        e={'event_id':'read-bad','op':'reading','device_id':'missing','bay_id':'b2','occupied':False,'sequence_no':9,'event_time':'2026-11-17T10:05:00Z'}
        sensor.ingest(self.db,[e])
        self.assertEqual(sensor.snapshot(self.db,'2026-11-17T10:10:00Z')['states']['b2']['state'],'unknown')


if __name__=='__main__':unittest.main()
