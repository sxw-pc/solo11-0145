"""Time-windowed capacity orders, set-union bay deductions and on-site handover adjudication."""
from __future__ import annotations

import json
from .sources import record
from .store import rows
from . import sensor

PURPOSES={'construction','fire','accessible'}
HANDOVER_TYPES={'release','takeover','revocation','emergency_override'}
# On-site verdicts rendered per (order, bay) pair under each authorization contract.
CLAIM_STATUSES={'admittable','awaiting_handover','under_investigation','preempted','conflict'}


def ingest(db,events: list[dict]) -> dict:
    made=0
    with db:
        for e in events:
            if e['purpose'] not in PURPOSES:raise ValueError('invalid capacity purpose')
            if e['starts_at']>=e['ends_at']:raise ValueError('empty capacity window')
            if not isinstance(e['bays'],list) or not e['bays']:raise ValueError('capacity order needs bays')
            if record(db,e,'capacity_order',e['order_id']):
                status='approved' if e.get('approved',False) and not e.get('revoked',False) else 'revoked' if e.get('revoked',False) else 'pending'
                db.execute('INSERT INTO capacity_revision VALUES (?,?,?,?,?,?,?,?,?)',
                           (e['event_id'],e['order_id'],e['purpose'],int(e['priority']),status,json.dumps(sorted(set(e['bays']))),e['starts_at'],e['ends_at'],e['event_time']))
                old=db.execute('SELECT source_event FROM capacity_order WHERE order_id=?',(e['order_id'],)).fetchone()
                if old:
                    prior=db.execute('SELECT event_time FROM evidence WHERE event_id=?',(old['source_event'],)).fetchone()
                    if prior['event_time']>e['event_time']:
                        made+=1;continue
                    db.execute('DELETE FROM capacity_window WHERE order_id=?',(e['order_id'],))
                db.execute('INSERT INTO capacity_order(order_id,purpose,priority,status,source_event) VALUES (?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET purpose=excluded.purpose,priority=excluded.priority,status=excluded.status,source_event=excluded.source_event',
                           (e['order_id'],e['purpose'],int(e['priority']),status,e['event_id']))
                for bay in sorted(set(e['bays'])):
                    db.execute('INSERT INTO capacity_window VALUES (?,?,?,?)',(e['order_id'],bay,e['starts_at'],e['ends_at']))
                db.execute('INSERT INTO capacity_decision VALUES (?,?,?) ON CONFLICT(order_id) DO UPDATE SET decision=excluded.decision,reason=excluded.reason',
                           (e['order_id'],status,'signed_order' if status=='approved' else 'not_currently_authorized'))
                made+=1
    return {'imported':made,'orders':len(rows(db,'SELECT order_id FROM capacity_order'))}


def snapshot(db,as_of: str) -> dict:
    revisions=rows(db,'SELECT * FROM capacity_revision WHERE event_time<=? ORDER BY event_time DESC,event_id DESC',(as_of,))
    latest={}
    for r in revisions:latest.setdefault(r['order_id'],r)
    active=[]
    for r in latest.values():
        if r['status']=='approved' and r['starts_at']<=as_of<r['ends_at']:
            active.extend({'order_id':r['order_id'],'purpose':r['purpose'],'priority':r['priority'],'bay_id':bay} for bay in json.loads(r['bays_json']))
    active.sort(key=lambda r:(-r['priority'],r['order_id']))
    bays=sorted({r['bay_id'] for r in active})
    # The highest priority order owns the operational response for a shared bay;
    # the bay is still deducted only once from public capacity.
    owners={}
    for r in active:
        owners.setdefault(r['bay_id'],{'order_id':r['order_id'],'purpose':r['purpose'],'priority':r['priority']})
    return {'reserved_bays':bays,'reserved_count':len(bays),'active_orders':sorted({r['order_id'] for r in active}),
            'owner_by_bay':owners}


# --------------------------------------------------------------------------- #
# On-site handover documents: release, takeover, revocation, emergency override
# --------------------------------------------------------------------------- #

def _require_text(e: dict,key: str):
    v=e.get(key)
    if not isinstance(v,str) or not v.strip():
        raise ValueError(f'handover document {e.get("event_id")} needs non-empty {key}')


def ingest_handover(db,events: list[dict]) -> dict:
    """Import signed on-site handover papers into the evidence ledger.

    Every paper is bound to one bay, the predecessor and successor order IDs,
    the order/override source versions it was signed against, a signing time,
    both parties and both signatures. Replay of the same event_id is idempotent;
    an override version reused for another (bay, fire order) pair is rejected.
    """
    made=0
    with db:
        for e in events:
            if e.get('doc_type') not in HANDOVER_TYPES:
                raise ValueError('handover doc_type must be release/takeover/revocation/emergency_override')
            _require_text(e,'bay_id');_require_text(e,'from_party');_require_text(e,'to_party')
            _require_text(e,'from_signature');_require_text(e,'to_signature')
            t=e['doc_type']
            if t in ('release','takeover'):
                _require_text(e,'from_order_id');_require_text(e,'to_order_id')
                _require_text(e,'from_order_version');_require_text(e,'to_order_version')
            elif t=='emergency_override':
                _require_text(e,'from_order_id');_require_text(e,'to_order_id')
                _require_text(e,'to_order_version')
                _require_text(e,'override_version')
                dup=db.execute('SELECT event_id FROM handover_document WHERE doc_type=? AND override_version=? AND (bay_id<>? OR to_order_id<>?)',
                               (t,e['override_version'],e['bay_id'],e['to_order_id'])).fetchone()
                if dup:
                    raise ValueError(f'override version {e["override_version"]} already bound to another bay/order')
            else:  # revocation
                _require_text(e,'revokes_event_id')
            if record(db,e,'handover_document',e['bay_id']):
                revoked=bool(e.get('revoked',False))
                row=(e['event_id'],t,e['bay_id'],e.get('from_order_id'),e.get('to_order_id'),
                     e['from_party'],e['to_party'],e['from_signature'],e['to_signature'],
                     e.get('from_order_version'),e.get('to_order_version'),e.get('override_version'),
                     e.get('revokes_event_id'),e['event_time'],'revoked' if revoked else 'valid')
                db.execute('INSERT INTO handover_revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
                if t=='revocation' and not revoked:
                    db.execute('UPDATE handover_document SET status=? WHERE event_id=?',('revoked',e['revokes_event_id']))
                db.execute('INSERT INTO handover_document VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                           'ON CONFLICT(event_id) DO UPDATE SET status=excluded.status',row)
                made+=1
    return {'imported':made,'documents':len(rows(db,'SELECT event_id FROM handover_document'))}


def _latest_revisions(db,as_of: str) -> dict:
    latest={}
    for r in rows(db,'SELECT * FROM capacity_revision WHERE event_time<=? ORDER BY event_time DESC,event_id DESC',(as_of,)):
        latest.setdefault(r['order_id'],r)
    return latest


def _prior_displacement(db,bay: str,order_id: str,as_of: str,revs: dict) -> dict|None:
    """Return the most recent higher-priority order that pushed this order
    aside on this bay before the query time, if any."""
    own=revs.get(order_id)
    if own is None:
        return None
    hits=[]
    for r in revs.values():
        if r['order_id']==order_id or r['priority']<=own['priority']:
            continue
        if bay not in json.loads(r['bays_json']):
            continue
        if r['starts_at']<as_of and r['starts_at']<own['ends_at'] and own['starts_at']<r['ends_at']:
            hits.append(r)
    if not hits:
        return None
    return max(hits,key=lambda r:r['starts_at'])


def _doc_binding_ok(db,doc: dict,revs: dict,as_of: str) -> tuple[bool,str|None]:
    """Verify a paper against the exact order revisions and bay it names.

    A signature carried for another bay or an older order version cannot be
    reused: every referenced order must exist at the query time, carry the
    version named on the paper, and actually cover the paper's bay.
    """
    t=doc['doc_type'];bay=doc['bay_id']
    refs=[('from_order_id','from_order_version'),('to_order_id','to_order_version')]
    if t=='emergency_override':
        # The override names the displaced predecessor and its signed order version.
        refs=[('from_order_id','from_order_version'),('to_order_id','to_order_version')]
    if t=='revocation':
        target=db.execute('SELECT * FROM handover_revision WHERE event_id=?',(doc['revokes_event_id'],)).fetchone()
        if target is None:
            return False,'revokes_unknown_document'
        if target['bay_id']!=bay:
            return False,'revocation_bay_mismatch'
        return True,None
    for id_key,ver_key in refs:
        oid=doc[id_key]
        if oid is None:
            return False,'missing_order_reference'
        rev=revs.get(oid)
        if rev is None:
            return False,f'{oid}_unknown_at_as_of'
        if doc[ver_key]!=rev['event_id']:
            return False,f'{oid}_version_mismatch'
        if bay not in json.loads(rev['bays_json']):
            return False,f'{oid}_does_not_cover_bay'
    if t=='emergency_override':
        if revs[doc['to_order_id']]['status']!='approved':
            return False,'override_target_not_approved'
        if revs[doc['to_order_id']]['purpose']!='fire':
            return False,'override_target_not_fire_order'
        if revs[doc['to_order_id']]['priority']<=revs[doc['from_order_id']]['priority']:
            return False,'override_target_not_higher_priority'
    return True,None


def handover_snapshot(db,as_of: str,timeout_minutes: int=30,save: bool=True) -> dict:
    """Re-adjudicate every (active order, bay) claim under both contracts.

    The set-union public-capacity deduction in snapshot() is untouched. This
    projection answers, per order and bay, whether the holder may physically
    enter now, must wait for a handover, is preempted by a higher priority
    order, conflicts with an equal-priority order, or stays under investigation
    because the magnetic sensor cannot prove the bay is clear.
    """
    revs=_latest_revisions(db,as_of)
    active=[]
    for r in revs.values():
        if r['status']=='approved' and r['starts_at']<=as_of<r['ends_at']:
            for bay in json.loads(r['bays_json']):
                active.append({'order_id':r['order_id'],'purpose':r['purpose'],
                               'priority':r['priority'],'bay_id':bay,
                               'starts_at':r['starts_at'],'ends_at':r['ends_at']})
    # Replay the append-only paper revisions as of the query time: a revocation
    # only invalidates its target from the revocation event onward, so earlier
    # adjudications still see the original paper as effective.
    docs=[dict(d) for d in rows(db,'SELECT * FROM handover_revision WHERE doc_time<=? ORDER BY doc_time,event_id',(as_of,))]
    revoked_ids={d['revokes_event_id'] for d in docs
                 if d['doc_type']=='revocation' and d['status']=='valid'}
    valid_docs=[];invalid_docs=[]
    for d in docs:
        if d['event_id'] in revoked_ids or d['status']=='revoked':
            continue
        ok,why=_doc_binding_ok(db,d,revs,as_of)
        if ok:
            valid_docs.append(d)
        else:
            invalid_docs.append({'event_id':d['event_id'],'doc_type':d['doc_type'],
                                 'bay_id':d['bay_id'],'reason':why})

    by_bay={}
    for c in active:
        by_bay.setdefault(c['bay_id'],[]).append(c)

    clearances={bay:sensor.bay_clearance(db,bay,as_of,timeout_minutes) for bay in sorted(by_bay)}
    claims=[];conflicts=[];obligations=[]
    # An emergency override paper itself books the displaced predecessor's
    # unfinished release duty on the ledger, independent of whether the bay is
    # clear at this exact snapshot and independent of snapshot persistence.
    for d in valid_docs:
        if d['doc_type']=='emergency_override':
            obligations.append({'as_of':as_of,'bay_id':d['bay_id'],
                                'displaced_order_id':d['from_order_id'],
                                'override_event_id':d['event_id'],
                                'fire_order_id':d['to_order_id']})

    for bay,claimants in sorted(by_bay.items()):
        releases=[d for d in valid_docs if d['doc_type']=='release' and d['bay_id']==bay]
        takeovers=[d for d in valid_docs if d['doc_type']=='takeover' and d['bay_id']==bay]
        overrides=[d for d in valid_docs if d['doc_type']=='emergency_override' and d['bay_id']==bay]
        # Completed standard handover chains: predecessor release plus successor
        # takeover on the same bay, takeover never earlier than the release.
        chains=[]
        for rel in releases:
            tk=max((t for t in takeovers
                    if t['to_order_id']==rel['to_order_id'] and t['from_order_id']==rel['from_order_id']
                    and t['doc_time']>=rel['doc_time']),
                   key=lambda t:t['doc_time'],default=None)
            if tk is not None:
                chains.append({'predecessor':rel['from_order_id'],'successor':rel['to_order_id'],
                               'release_event':rel['event_id'],'takeover_event':tk['event_id'],
                               'release_time':rel['doc_time'],'takeover_time':tk['doc_time']})
        chain_successors={ch['successor']:ch for ch in chains}
        chain_predecessors={ch['predecessor']:ch for ch in chains}
        override_orders={d['to_order_id']:d for d in overrides
                         if any(c['order_id']==d['to_order_id'] and c['purpose']=='fire' for c in claimants)}

        max_prio=max(c['priority'] for c in claimants)
        max_group={c['order_id'] for c in claimants if c['priority']==max_prio}
        # Only an actual release/takeover chain settles who holds the bay at
        # top priority; an emergency override never removes an equal-priority
        # conflict between two live orders.
        resolved=set(chain_successors)|set(chain_predecessors)
        unresolved_max=max_group-resolved
        # A single top-priority holder is not a conflict; at least two unresolved
        # equal-priority orders must compete before lexicographic ordering can
        # be (correctly) refused as a tie-breaker.
        conflicted=unresolved_max if len(unresolved_max)>=2 else set()
        if len(unresolved_max)>=2:
            conflicts.append({'bay_id':bay,'order_ids':sorted(unresolved_max),
                              'reason':'equal_priority_orders_without_handover_evidence'})
        clearance=clearances[bay]

        for c in sorted(claimants,key=lambda x:(-x['priority'],x['order_id'])):
            oid=c['order_id']
            base_evidence=[]
            # Priority layer: lower priority orders are pushed aside while any
            # higher priority order remains active; equal-priority orders with
            # no handover chain conflict, and lexicographic order_id never wins.
            if c['priority']<max_prio:
                std=eme=('preempted','higher_priority_order_active')
            elif oid in conflicted:
                std=eme=('conflict','equal_priority_orders_without_handover_evidence')
            else:
                std=None;eme=None

            if std is None and oid in chain_successors:
                ch=chain_successors[oid]
                base_evidence=[ch['release_event'],ch['takeover_event']]
                if clearance['clearance']=='clear':
                    std=('admittable','release_and_takeover_with_trusted_free_bay')
                elif clearance['clearance']=='occupied':
                    std=('awaiting_handover','paper_complete_but_sensor_still_occupied')
                else:
                    std=('under_investigation',f"sensor_{clearance['reason']}")

            if std is None and oid in chain_predecessors:
                # The predecessor signed the bay away; it needs a fresh return
                # chain; the old order never restores entry on its own.
                std=('awaiting_handover','released_bay_needs_new_takeover_evidence')

            if std is None and oid in override_orders:
                ov=override_orders[oid]
                base_evidence=[ov['event_id']]
                if clearance['clearance']=='clear':
                    eme=('admittable','emergency_override_with_trusted_free_bay')
                elif clearance['clearance']=='occupied':
                    eme=('awaiting_handover','override_signed_but_sensor_still_occupied')
                else:
                    eme=('under_investigation',f"sensor_{clearance['reason']}")
                # The standard contract still demands the predecessor's release.
                if std is None:
                    if clearance['clearance']=='clear':
                        std=('awaiting_handover','missing_predecessor_release_for_standard_contract')
                    elif clearance['clearance']=='occupied':
                        std=('awaiting_handover','missing_release_and_bay_occupied')
                    else:
                        std=('under_investigation',f"sensor_{clearance['reason']}")

            if std is None:
                # Sole or already-settled top-priority holder without papers:
                # entry still requires a physically clear, trusted bay. If a
                # higher priority order ever pushed this order aside, the old
                # order does not auto-resume: a fresh release/takeover round
                # after the displacement is mandatory. These fallbacks only
                # rule on the standard contract; without an independent
                # override paper the emergency contract stays not_applicable.
                displacement=_prior_displacement(db,bay,oid,as_of,revs)
                if displacement is not None:
                    fresh=any(ch['successor']==oid and ch['takeover_time']>=displacement['ends_at']
                              for ch in chains)
                    if not fresh:
                        std=('awaiting_handover','higher_priority_order_ended_but_no_new_handover_evidence')
                if std is None:
                    if clearance['clearance']=='clear':
                        std=('admittable','sole_active_order_with_trusted_free_bay')
                    elif clearance['clearance']=='occupied':
                        std=('awaiting_handover','no_handover_paper_and_bay_occupied')
                    else:
                        std=('under_investigation',f"sensor_{clearance['reason']}")
            if eme is None:
                # Emergency contract only exists for fire orders holding an
                # independent override paper; everyone else is not eligible.
                eme=('not_applicable','no_emergency_override_for_this_order')

            claims.append({'bay_id':bay,'order_id':oid,'priority':c['priority'],
                           'purpose':c['purpose'],'window':{'starts_at':c['starts_at'],'ends_at':c['ends_at']},
                           'sensor':clearance,
                           'standard_contract':{'status':std[0],'reason':std[1]},
                           'emergency_contract':{'status':eme[0],'reason':eme[1]},
                           'entry_status':std[0],
                           'evidence_event_ids':sorted(set(base_evidence))})

    result={'as_of':as_of,'claims':claims,
            'statuses':{s:sum(1 for cl in claims if cl['entry_status']==s) for s in sorted(CLAIM_STATUSES)},
            'conflicts':conflicts,'open_emergency_obligations':_open_obligations(db,obligations),
            'rejected_documents':invalid_docs,
            'contracts':{'standard':'release + takeover + trusted free bay',
                         'emergency':'approved fire order + independent override paper + trusted free bay; release obligation stays on the ledger'}}
    if save:
        with db:
            for cl in claims:
                for contract,verdict in (('standard',cl['standard_contract']),('emergency',cl['emergency_contract'])):
                    db.execute('INSERT INTO handover_decision_revision(as_of,bay_id,order_id,contract,status,reason,evidence_json) VALUES (?,?,?,?,?,?,?)',
                               (as_of,cl['bay_id'],cl['order_id'],contract,verdict['status'],verdict['reason'],
                                json.dumps(cl['evidence_event_ids'],sort_keys=True)))
            for ob in obligations:
                db.execute('INSERT OR IGNORE INTO emergency_obligation(as_of,bay_id,displaced_order_id,override_event_id,fire_order_id) VALUES (?,?,?,?,?)',
                           (ob['as_of'],ob['bay_id'],ob['displaced_order_id'],ob['override_event_id'],ob['fire_order_id']))
    return result


def _open_obligations(db,fresh: list[dict]) -> list[dict]:
    """Emergency takeovers leave the displaced predecessor's release duty on
    the ledger.

    The duty belongs to the displaced order itself: a fire order handing the
    bay back later does not discharge the predecessor's missing release. The
    obligation closes only when that predecessor signs a release paper for the
    same bay and override after the emergency takeover; the fire order simply
    ending never clears it.
    """
    open_now=[]
    seen=rows(db,'SELECT DISTINCT bay_id,displaced_order_id,override_event_id,fire_order_id FROM emergency_obligation')
    # Include obligations produced by this very run so a save=False snapshot
    # reaches the same verdict as a persisted one.
    keys={(o['bay_id'],o['displaced_order_id'],o['override_event_id']) for o in fresh}
    fire_by_key={}
    for o in fresh:
        fire_by_key[(o['bay_id'],o['displaced_order_id'],o['override_event_id'])]=o['fire_order_id']
    for o in seen:
        k=(o['bay_id'],o['displaced_order_id'],o['override_event_id'])
        keys.add(k);fire_by_key[k]=o['fire_order_id']
    for bay,displaced,override_event in sorted(keys):
        closed=db.execute("""SELECT 1 FROM handover_revision
                             WHERE status='valid' AND doc_type='release' AND bay_id=?
                               AND from_order_id=? AND doc_time>(SELECT doc_time FROM handover_revision WHERE event_id=?)
                               AND event_id NOT IN (
                                 SELECT revokes_event_id FROM handover_revision
                                 WHERE doc_type='revocation' AND status='valid'
                                   AND revokes_event_id IS NOT NULL)
                             LIMIT 1""",
                          (bay,displaced,override_event)).fetchone()
        if closed is None:
            open_now.append({'bay_id':bay,'displaced_order_id':displaced,
                             'override_event_id':override_event,
                             'fire_order_id':fire_by_key.get((bay,displaced,override_event))})
    return open_now
