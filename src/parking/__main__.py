"""CLI for three independent evidence imports and recomputed reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import sensor,gate,capacity,report,billing
from .store import connect


def main(argv=None):
    parser=argparse.ArgumentParser(prog='parking')
    sub=parser.add_subparsers(dest='command',required=True)
    import_commands=('import-sensor','import-gate','import-reserve',
                     'import-calibration','import-tariff','import-contract')
    for name in ('init',)+import_commands+('report','billing-report','adjudicate','stay-report','demo'):
        p=sub.add_parser(name);p.add_argument('--db',default='data/demo.sqlite')
        if name.startswith('import-'):p.add_argument('--input',required=True)
        if name=='report':
            p.add_argument('--as-of',required=True);p.add_argument('--total-bays',type=int,required=True)
            p.add_argument('--timeout-minutes',type=int,default=30)
        if name=='adjudicate':
            p.add_argument('--stay');p.add_argument('--contract');p.add_argument('--all',action='store_true')
        if name=='stay-report':
            p.add_argument('--stay',required=True)
    args=parser.parse_args(argv)
    db=connect(args.db)
    try:
        if args.command=='init':result={'database':str(Path(args.db).resolve()),'initialized':True}
        elif args.command.startswith('import-'):
            events=json.loads(Path(args.input).read_text(encoding='utf-8'))
            if not isinstance(events,list):raise ValueError('input must be a JSON event list')
            target={'import-sensor':sensor,'import-gate':gate,'import-reserve':capacity,
                    'import-calibration':billing,'import-tariff':billing,'import-contract':billing}[args.command]
            method={'import-calibration':'ingest_calibration','import-tariff':'ingest_tariff',
                    'import-contract':'ingest_contract'}.get(args.command,'ingest')
            result=getattr(target,method)(db,events)
        elif args.command=='report':result=report.decide(db,args.as_of,args.total_bays,args.timeout_minutes)
        elif args.command=='adjudicate':
            if getattr(args,'all',False):
                result=billing.adjudicate_all(db,args.contract)
            else:
                if not args.stay or not args.contract:
                    raise ValueError('adjudicate needs --stay and --contract (or --all)')
                result=billing.adjudicate(db,args.stay,args.contract)
        elif args.command=='stay-report':result=billing.stay_report(db,args.stay)
        elif args.command=='billing-report':result=billing.billing_report(db)
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
