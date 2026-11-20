"""CLI for independent evidence imports and recomputed reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import sensor,gate,capacity,report,maintenance
from .store import connect


def main(argv=None):
    parser=argparse.ArgumentParser(prog='parking')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('init','import-sensor','import-gate','import-reserve','import-topology',
                 'import-signal','import-frequency','report','diagnose','history','demo'):
        p=sub.add_parser(name);p.add_argument('--db',default='data/demo.sqlite')
        if name.startswith('import-'):p.add_argument('--input',required=True)
        if name in ('report','diagnose'):
            p.add_argument('--as-of',required=True)
            p.add_argument('--timeout-minutes',type=int,default=30)
        if name=='report':
            p.add_argument('--total-bays',type=int,required=True)
            p.add_argument('--diagnose',action='store_true',help='attach maintenance diagnosis')
            p.add_argument('--max-faults',type=int,default=maintenance.DEFAULT_MAX_FAULTS)
            p.add_argument('--vehicle-capacity',type=int,default=maintenance.DEFAULT_VEHICLE_CAPACITY)
            p.add_argument('--risk-threshold',type=float,default=maintenance.DEFAULT_RISK_THRESHOLD)
        if name=='diagnose':
            p.add_argument('--max-faults',type=int,default=maintenance.DEFAULT_MAX_FAULTS)
            p.add_argument('--vehicle-capacity',type=int,default=maintenance.DEFAULT_VEHICLE_CAPACITY)
            p.add_argument('--risk-threshold',type=float,default=maintenance.DEFAULT_RISK_THRESHOLD)
    args=parser.parse_args(argv)
    db=connect(args.db)
    try:
        if args.command=='init':result={'database':str(Path(args.db).resolve()),'initialized':True}
        elif args.command.startswith('import-'):
            events=json.loads(Path(args.input).read_text(encoding='utf-8'))
            if not isinstance(events,list):raise ValueError('input must be a JSON event list')
            target={'import-sensor':sensor,'import-gate':gate,'import-reserve':capacity,
                    'import-topology':maintenance,'import-signal':maintenance,
                    'import-frequency':maintenance}[args.command]
            if args.command=='import-topology':result=target.ingest_topology(db,events)
            elif args.command=='import-signal':result=target.ingest_signals(db,events)
            elif args.command=='import-frequency':result=target.ingest_frequencies(db,events)
            else:result=target.ingest(db,events)
        elif args.command=='report':
            result=report.decide(db,args.as_of,args.total_bays,args.timeout_minutes,
                                 diagnose_dispatch=args.diagnose,max_faults=args.max_faults,
                                 vehicle_capacity=args.vehicle_capacity,risk_threshold=args.risk_threshold)
        elif args.command=='diagnose':
            result=maintenance.diagnose(db,args.as_of,timeout_minutes=args.timeout_minutes,
                                        max_faults=args.max_faults,vehicle_capacity=args.vehicle_capacity,
                                        risk_threshold=args.risk_threshold)
        elif args.command=='history':
            result={'releases':report.history(db),'diagnoses':maintenance.revisions(db)}
        else:
            root=Path(__file__).resolve().parents[2]
            for name,mod in [('sensor_good.json',sensor),('gate_good.json',gate),('capacity_good.json',capacity)]:
                mod.ingest(db,json.loads((root/'fixtures'/'parking'/name).read_text(encoding='utf-8')))
            result=report.decide(db,'2026-11-17T10:10:00Z',3)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 0
    except (ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False))
        return 2
    finally:db.close()


if __name__=='__main__':raise SystemExit(main())
