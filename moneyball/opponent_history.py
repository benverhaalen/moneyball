"""Audit public historical selections without pretending today's ADP is vintage.

This module does not fit an opponent model. A selection's beneficiary is not
proof that the person clicked it manually; auto-picks and commissioner actions
may be indistinguishable in the public payload.
"""
from collections import Counter


def summarize_history(current, leagues, drafts):
    league_by_id={str(l['league_id']):l for l in leagues}
    user_by_id={str(u['user_id']):u for u in current.get('users',[])}
    opponents={str(r['owner_id']):r for r in current['rosters']
               if r['roster_id']!=current['my_roster_id'] and r.get('owner_id')}
    totals={uid:{'user_id':uid,'current_roster_id':r['roster_id'],
                 'display_name':user_by_id.get(uid,{}).get('display_name',uid),
                 'attributed_nonkeeper_selections':0,'format_matched_selections':0,
                 'sf_selections':0,'drafts':[]} for uid,r in opponents.items()}
    draft_rows=[]; selections=[]; excluded=[]
    seen_drafts=set()
    for draft in drafts:
        info=draft['info'];did=str(info['draft_id'])
        if did in seen_drafts:
            raise ValueError('Duplicate draft in historical audit')
        seen_drafts.add(did)
        if info.get('status')!='complete':
            raise ValueError('Historical audit requires completed drafts')
        picks=sorted(draft['picks'],key=lambda p:int(p['pick_no']))
        if [int(p['pick_no']) for p in picks]!=list(range(1,len(picks)+1)):
            raise ValueError('Historical picks have duplicate numbers or a missing prefix')
        if len({str(p['player_id']) for p in picks})!=len(picks):
            raise ValueError('Historical draft contains duplicate selected player')
        league=league_by_id.get(str(info.get('league_id')), {})
        settings=info.get('settings') or {}; slots=league.get('roster_positions') or []
        flags={'teams_match':settings.get('teams')==12,
               'superflex_match':slots.count('SUPER_FLEX')==1,
               'full_ppr_match':league.get('scoring_settings',{}).get('rec')==1,
               'league_type_code_matches_current':league.get('settings',{}).get('type')==current['league']['settings'].get('type'),
               'round_count_match':settings.get('rounds')==28,
               'starter_slots_match':[p for p in slots if p!='BN']==[p for p in current['league']['roster_positions'] if p!='BN'],
               'scoring_exact_match':league.get('scoring_settings')==current['league']['scoring_settings']}
        coarse=all(flags[k] for k in ('teams_match','superflex_match','full_ppr_match','league_type_code_matches_current'))
        attribution=Counter(); conflicts=0; keepers=0
        order=info.get('draft_order') or {}; reversed_order={}
        for uid,slot in order.items(): reversed_order.setdefault(int(slot),[]).append(str(uid))
        observed_before=[]
        for pick in picks:
            if pick.get('is_keeper'):
                keepers+=1; observed_before.append(str(pick['player_id']));continue
            uid=str(pick.get('picked_by') or '')
            slot_users=reversed_order.get(int(pick['draft_slot']),[])
            method='picked_by'
            if not uid:
                if len(slot_users)==1 and not draft.get('traded_picks'):
                    uid=slot_users[0];method='draft_order_slot_without_trades'
                else:
                    excluded.append({'draft_id':did,'pick_no':pick['pick_no'],'reason':'unresolved beneficiary'})
                    observed_before.append(str(pick['player_id']));continue
            if slot_users and uid not in slot_users and not draft.get('traded_picks'):
                conflicts+=1
                excluded.append({'draft_id':did,'pick_no':pick['pick_no'],'reason':'picked_by disagrees with original slot; no documented draft trades'})
                observed_before.append(str(pick['player_id']));continue
            if uid in opponents:
                attribution[uid]+=1
                position=(pick.get('metadata') or {}).get('position')
                selections.append({'draft_id':did,'league_id':info.get('league_id'),'season':info.get('season'),
                    'user_id':uid,'current_roster_id':opponents[uid]['roster_id'],
                    'pick_no':pick['pick_no'],'round':pick.get('round'),'draft_slot':pick['draft_slot'],
                    'player_id':str(pick['player_id']),'position_in_public_pick_record':position,
                    'attribution_method':method,'manual_human_action_verified':False,
                    'known_removed_before':list(observed_before),'complete_choice_universe_known':False,
                    'coarse_format_match':coarse,'historical_feature_vintage_available':False})
            observed_before.append(str(pick['player_id']))
        row={'draft_id':did,'league_id':info.get('league_id'),'name':(info.get('metadata') or {}).get('name'),
             'season':info.get('season'),'teams':settings.get('teams'),'rounds':settings.get('rounds'),
             'league_type_code':league.get('settings',{}).get('type'),'superflex_slots':slots.count('SUPER_FLEX'),
             'ppr':league.get('scoring_settings',{}).get('rec'),'completed_pick_rows':len(picks),
             'keeper_rows_excluded':keepers,'attribution_conflicts':conflicts,'match_flags':flags,
             'coarse_format_match':coarse,'attributed_to_current_opponents':dict(attribution),
             'autopick_setting':settings.get('cpu_autopick'),
             'autopick_actual_use_observed':any('is_autopick' in p or 'is_auto' in p for p in picks)}
        draft_rows.append(row)
        for uid,n in attribution.items():
            totals[uid]['attributed_nonkeeper_selections']+=n
            totals[uid]['format_matched_selections']+=n if coarse else 0
            totals[uid]['sf_selections']+=n if flags['superflex_match'] else 0
            own=[p for p in selections if p['draft_id']==did and p['user_id']==uid]
            totals[uid]['drafts'].append({'draft_id':did,'n':n,'coarse_format_match':coarse,
                'first_four_positions':[p['position_in_public_pick_record'] for p in own[:4]],
                'first_qb_round':next((p['round'] for p in own if p['position_in_public_pick_record']=='QB'),None)})
    return {'completed_drafts':len(draft_rows),'total_pick_rows':sum(d['completed_pick_rows'] for d in draft_rows),
        'current_opponents':len(opponents),'opponents_with_attributed_history':sum(v['attributed_nonkeeper_selections']>0 for v in totals.values()),
        'attributed_nonkeeper_selections':sum(v['attributed_nonkeeper_selections'] for v in totals.values()),
        'coarse_format_matched_completed_drafts':sum(d['coarse_format_match'] for d in draft_rows),
        'coarse_format_matched_selections':sum(v['format_matched_selections'] for v in totals.values()),
        'prospectively_valid_behavior_training_rows':0,'per_opponent':list(totals.values()),
        'drafts':draft_rows,'selections':selections,'excluded':excluded,
        'warning':'Observed beneficiary counts, not verified manual decisions; different formats and unavailable historical feature vintages prevent automatic model training.'}
