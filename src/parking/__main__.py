"""CLI for three independent evidence imports, field verification and reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import sensor,gate,capacity,verify,report
from .store import connect


def main(argv=None):
    parser=argparse.ArgumentParser(prog='parking')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('init','import-sensor','import-gate','import-reserve','import-verify',
                 'verify-status','verify-issue','report','demo'):
        p=sub.add_parser(name);p.add_argument('--db',default='data/demo.sqlite')
        if name.startswith('import-'):p.add_argument('--input',required=True)
        if name=='report':
            p.add_argument('--as-of',required=True);p.add_argument('--total-bays',type=int,required=True)
            p.add_argument('--timeout-minutes',type=int,default=30)
        if name=='verify-status':
            p.add_argument('--as-of',required=True)
        if name=='verify-issue':
            p.add_argument('--contract',required=True,choices=verify.CONTRACTS)
            p.add_argument('--bays',required=True,help='comma separated physical bay ids')
            p.add_argument('--effective-at',required=True)
    args=parser.parse_args(argv)
    db=connect(args.db)
    try:
        if args.command=='init':result={'database':str(Path(args.db).resolve()),'initialized':True}
        elif args.command.startswith('import-'):
            events=json.loads(Path(args.input).read_text(encoding='utf-8'))
            if not isinstance(events,list):raise ValueError('input must be a JSON event list')
            target={'import-sensor':sensor,'import-gate':gate,'import-reserve':capacity,
                    'import-verify':verify}[args.command]
            result=target.ingest(db,events)
        elif args.command=='verify-status':result=verify.status(db,args.as_of)
        elif args.command=='verify-issue':
            bays=[b.strip() for b in args.bays.split(',') if b.strip()]
            result=verify.issue(db,args.contract,bays,args.effective_at)
        elif args.command=='report':result=report.decide(db,args.as_of,args.total_bays,args.timeout_minutes)
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
