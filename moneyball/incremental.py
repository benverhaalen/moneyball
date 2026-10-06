"""Exploratory incremental information tests with fixed forward splits.

Historical final-file vintages are unknown. Calendar lags prevent outcome-window
leakage, but do not turn these tests into point-in-time or professional-benchmark
validation. No result from this module changes the projection model.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import io
import math
from pathlib import Path
import statistics
import time

from .features import DEFINITIONS, enrich_features, _write
from .hypotheses import append_event
from .modeling import score_stats, OFFENSE_APPLICABLE, CORE_OFFENSE
from .warehouse import Warehouse, ValidationError, canonical, sha256


BASELINE_METRICS={'targets','carries','pass_attempts'}
BASELINE_BY_POSITION={'QB':('pass_attempts','carries'),'RB':('targets','carries'),'WR':('targets',),'TE':('targets',)}


def _mean(values):return statistics.fmean(values) if values else None


def _solve(matrix,vector):
    """Pivoted elimination for the at-most-three predictor ridge systems."""
    n=len(vector);aug=[list(row)+[vector[i]] for i,row in enumerate(matrix)]
    for col in range(n):
        pivot=max(range(col,n),key=lambda r:abs(aug[r][col]))
        if abs(aug[pivot][col])<1e-12:raise ValidationError('Singular standardized regression system')
        aug[col],aug[pivot]=aug[pivot],aug[col]
        factor=aug[col][col];aug[col]=[x/factor for x in aug[col]]
        for row in range(n):
            if row==col:continue
            factor=aug[row][col]
            aug[row]=[a-factor*b for a,b in zip(aug[row],aug[col])]
    return [row[-1] for row in aug]


def fit_ridge(xs,ys,penalty=1.0):
    if not xs or len(xs)!=len(ys) or penalty<=0:raise ValidationError('Ridge requires paired observations and positive penalty')
    width=len(xs[0]);n=len(xs)
    if any(len(x)!=width for x in xs):raise ValidationError('Inconsistent baseline predictor dimensions')
    means=[_mean([x[j] for x in xs]) for j in range(width)]
    scales=[math.sqrt(_mean([(x[j]-means[j])**2 for x in xs])) or 1.0 for j in range(width)]
    centered=[[(x[j]-means[j])/scales[j] for j in range(width)] for x in xs]
    intercept=_mean(ys)
    gram=[[sum(x[i]*x[j] for x in centered)+(penalty if i==j else 0) for j in range(width)] for i in range(width)]
    rhs=[sum(x[i]*(y-intercept) for x,y in zip(centered,ys)) for i in range(width)]
    return {'intercept':intercept,'means':means,'scales':scales,'coefficients':_solve(gram,rhs),'penalty':penalty,'training_rows':n}


def predict_ridge(model,x):
    return model['intercept']+sum(b*(v-m)/s for b,v,m,s in zip(model['coefficients'],x,model['means'],model['scales']))


def residual_distribution(residuals):
    """Exact empirical bootstrap distribution; no Monte Carlo approximation."""
    if not residuals:raise ValidationError('Empty residual bootstrap')
    values=sorted(residuals);prefix=[0.0]
    for value in values:prefix.append(prefix[-1]+value)
    n=len(values)
    # Half the expected absolute difference of two independent empirical draws.
    dispersion=sum((2*i-n+1)*v for i,v in enumerate(values))/(n*n)
    return {'values':values,'prefix':prefix,'dispersion':dispersion}


def bootstrap_crps(distribution,center,observed):
    values=distribution['values'];prefix=distribution['prefix'];n=len(values)
    shifted=observed-center;left=bisect_right(values,shifted)
    absolute=(shifted*left-prefix[left]+prefix[-1]-prefix[left]-shifted*(n-left))/n
    return max(0.0,absolute-distribution['dispersion'])


def make_lagged_rows(enriched,scoring,*,train_seasons,lag_weeks=3):
    """Lag only exact previous NFL calendar weeks in the same regular season."""
    if lag_weeks!=3:raise ValidationError('This registered study uses exactly three lag weeks')
    rows=enriched['rows'];training=[r for r in rows if r['season'] in train_seasons]
    if not training:raise ValidationError('No training rows for scoring coverage audit')
    audits=[score_stats(r['stats'],scoring,source_schema='nflverse',applicable_keys=OFFENSE_APPLICABLE) for r in training]
    missing=set().union(*(set(a['missing_scoring_keys']) for a in audits))
    core_missing=missing & CORE_OFFENSE
    if core_missing:raise ValidationError('Training scoring missing core components: '+','.join(sorted(core_missing)))
    used={k:v for k,v in scoring.items() if k in OFFENSE_APPLICABLE and k not in missing and v!=0}
    if not used:raise ValidationError('No supported nonzero scoring components')
    scored={};audit=Counter();missing_test=Counter()
    for row in rows:
        key=(row['player_id'],row['season'],row['week'])
        if key in scored:raise ValidationError('Duplicate input player-season-week')
        points=score_stats(row['stats'],used,source_schema='nflverse',applicable_keys=set(used))
        if not points['complete']:
            audit['rows_missing_fixed_supported_score_components']+=1;missing_test.update(points['missing_scoring_keys']);continue
        scored[key]=(row,points['points'])
    output=[]
    for key,(row,points) in scored.items():
        pid,season,week=key
        prior=[scored.get((pid,season,w)) for w in range(week-lag_weeks,week)]
        if not all(prior):audit['target_rows_missing_exact_three_prior_weeks']+=1;continue
        if any(p[0]['position']!=row['position'] for p in prior):audit['position_change_in_lag_window']+=1;continue
        baseline=[_mean([p[1] for p in prior])]
        for metric in BASELINE_BY_POSITION[row['position']]:
            values=[p[0]['metrics'][metric] for p in prior]
            baseline.append(_mean(values) if all(v is not None for v in values) else None)
        if any(v is None for v in baseline):audit['missing_baseline_lag_input']+=1;continue
        candidates={}
        for metric in enriched['definitions']:
            values=[p[0]['metrics'][metric] for p in prior]
            candidates[metric]=_mean(values) if all(v is not None for v in values) else None
        output.append({'player_id':pid,'season':season,'week':week,'position':row['position'],'observed':points,
            'baseline':baseline,'candidate_lags':candidates,'lag_source_weeks':list(range(week-3,week)),
            'lag_available_at':max(p[0]['_provenance']['available_at'] for p in prior)})
    return output,{'input_rows':len(rows),'lagged_rows':len(output),'exclusions':dict(audit),
        'outcome_label':'supported_offensive_league_scoring_components',
        'omitted_nonzero_scoring_keys':{k:scoring[k] for k in sorted(missing)},
        'supported_scoring_coefficients':used,'additional_test_missing_components':dict(missing_test),
        'lag_rule':'Mean of weeks w-3,w-2,w-1 in same season; all must be observed, same position; no bye-gap bridging.',
        'population_warning':'Targets require an observed box-score row; absent/nonparticipating weeks are not forecast outcomes here.'}


def _score_records(records):
    if not records:return None
    return {'rows':len(records),'baseline_mse':_mean([r['baseline_squared_error'] for r in records]),
        'augmented_mse':_mean([r['augmented_squared_error'] for r in records]),
        'mse_improvement':_mean([r['baseline_squared_error']-r['augmented_squared_error'] for r in records]),
        'baseline_crps':_mean([r['baseline_crps'] for r in records]),'augmented_crps':_mean([r['augmented_crps'] for r in records]),
        'crps_improvement':_mean([r['baseline_crps']-r['augmented_crps'] for r in records])}


def evaluate_incremental(enriched,scoring,*,train_seasons=(2018,2019,2020,2021,2022),test_seasons=(2023,2024),ridge=1.0):
    train_seasons=tuple(sorted(set(train_seasons)));test_seasons=tuple(sorted(set(test_seasons)))
    if not train_seasons or not test_seasons or max(train_seasons)>=min(test_seasons) or max(test_seasons)>=2025:
        raise ValidationError('Use forward disjoint splits ending before the already inspected 2025 holdout')
    if any(r['season'] not in train_seasons+test_seasons for r in enriched['rows']):
        raise ValidationError('Input contains seasons outside the registered split, including possibly 2025')
    lagged,lag_audit=make_lagged_rows(enriched,scoring,train_seasons=train_seasons)
    results=[];failures=[]
    for metric,spec in enriched['definitions'].items():
        if metric in BASELINE_METRICS:
            results.append({'metric':metric,'status':'not_tested_already_in_baseline','train_rows':0,'test_rows':0});continue
        position_results=[];records=[];training_total=0;metric_failures=[]
        for position in spec['positions']:
            population=[r for r in lagged if r['position']==position]
            train=[r for r in population if r['season'] in train_seasons and r['candidate_lags'][metric] is not None]
            test=[r for r in population if r['season'] in test_seasons and r['candidate_lags'][metric] is not None]
            if len(train)<20 or not test:
                failure={'metric':metric,'position':position,'reason':'insufficient_common_support','train_rows':len(train),'test_rows':len(test)}
                failures.append(failure);metric_failures.append(failure);continue
            xs=[r['baseline'] for r in train];ys=[r['observed'] for r in train];zs=[r['candidate_lags'][metric] for r in train]
            base=fit_ridge(xs,ys,ridge);candidate_model=fit_ridge(xs,zs,ridge)
            base_fit=[predict_ridge(base,x) for x in xs]
            residual_y=[y-p for y,p in zip(ys,base_fit)]
            residual_z=[z-predict_ridge(candidate_model,x) for z,x in zip(zs,xs)]
            scale=math.sqrt(_mean([z*z for z in residual_z]))
            if scale<1e-10:
                failure={'metric':metric,'position':position,'reason':'candidate_has_no_residual_variation','train_rows':len(train),'test_rows':len(test)}
                failures.append(failure);metric_failures.append(failure);continue
            standardized=[z/scale for z in residual_z]
            coefficient=sum(z*y for z,y in zip(standardized,residual_y))/(sum(z*z for z in standardized)+ridge)
            augmented_residual=[y-coefficient*z for y,z in zip(residual_y,standardized)]
            base_distribution=residual_distribution(residual_y);added_distribution=residual_distribution(augmented_residual)
            predictions=[]
            for row in test:
                center=predict_ridge(base,row['baseline'])
                candidate_residual=(row['candidate_lags'][metric]-predict_ridge(candidate_model,row['baseline']))/scale
                added=center+coefficient*candidate_residual
                predictions.append({'season':row['season'],'player_id':row['player_id'],'week':row['week'],
                    'baseline_squared_error':(row['observed']-center)**2,'augmented_squared_error':(row['observed']-added)**2,
                    'baseline_crps':bootstrap_crps(base_distribution,center,row['observed']),
                    'augmented_crps':bootstrap_crps(added_distribution,added,row['observed'])})
            records.extend(predictions);training_total+=len(train)
            position_results.append({'position':position,'train_rows':len(train),'test_rows':len(test),
                'train_baseline_eligible':sum(r['season'] in train_seasons for r in population),
                'test_baseline_eligible':sum(r['season'] in test_seasons for r in population),
                'baseline_inputs':['lag_supported_league_points']+['lag_'+m for m in BASELINE_BY_POSITION[position]],
                'baseline_fit':base,'candidate_residualizer':candidate_model,'residualized_candidate_scale':scale,
                'incremental_coefficient_per_residual_sd':coefficient,'scores':_score_records(predictions),
                'by_test_season':{str(s):_score_records([r for r in predictions if r['season']==s]) for s in test_seasons}})
        scores=_score_records(records)
        result={'metric':metric,'status':('evaluated_with_partial_position_coverage' if metric_failures else 'evaluated') if records else 'not_evaluated',
            'train_rows':training_total,'test_rows':len(records),'positions':position_results,'failures':metric_failures}
        if scores:
            result.update(scores)
            result['passes_descriptive_sign_rule']=scores['crps_improvement']>0 and scores['mse_improvement']>0
            result['by_test_season']={str(s):_score_records([r for r in records if r['season']==s]) for s in test_seasons}
        results.append(result)
    return {'results':results,'failures':failures,'failure_count':len(failures),'lag_audit':lag_audit,
        'comparison_family_size':len(enriched['definitions']),'nonbaseline_candidates':len(set(enriched['definitions'])-BASELINE_METRICS),
        'evaluated_candidates':sum(r.get('test_rows',0)>0 for r in results),
        'sign_rule_not_met':[r['metric'] for r in results if r.get('passes_descriptive_sign_rule') is False],
        'train_seasons':list(train_seasons),'test_seasons':list(test_seasons),'ridge_penalty':ridge,
        'evidence_status':'EXPLORATORY_UNKNOWN_HISTORICAL_VINTAGES_NOT_PROFESSIONAL_BENCHMARK_VALIDATION',
        'limitations':['All input historical vintages are current final files; calendar lag guards do not establish point-in-time availability.',
            '2025 is excluded entirely. No result promotes a candidate into projection or reselects the points model.',
            'Professional historical forecasts are absent; this tests incremental information over our lagged baseline only.',
            'Each baseline and augmentation use the same candidate-specific train/test rows. Cross-candidate scores use different support.',
            'Repeated players, overlapping lag windows and correlated candidates invalidate naive iid p-values; no inferential significance claimed.',
            'Training-residual bootstrap distributions can underestimate forecast uncertainty; scored exactly, without Monte Carlo noise.',
            'Per-position standardized ridge baseline plus one-dimensional ridge residualization is a diagnostic, not exact joint OLS or a fully optimized model.',
            'Lag windows and observed future box-score outcomes select continuous participants; this is not a full availability forecast.',
            'All 32 candidates are listed; the 3 baseline inputs are not retested as independent additions.'],
        'source_coverage':enriched['coverage'],'input_batches':enriched['input_batches'],'cutoff':enriched['cutoff']}


def run_incremental(warehouse,scoring,*,train_seasons=(2018,2019,2020,2021,2022),test_seasons=(2023,2024),ridge=1.0,output_dir=None):
    train_seasons=tuple(train_seasons);test_seasons=tuple(test_seasons)
    if not train_seasons or not test_seasons or max(train_seasons)>=min(test_seasons) or max(test_seasons)>=2025:
        raise ValidationError('Forward evaluation must exclude 2025 and later')
    specification={'kind':'preregister_incremental_features','candidate_definitions':DEFINITIONS,
        'comparison_family_size':len(DEFINITIONS),'baseline_by_position':BASELINE_BY_POSITION,'lag_weeks':3,
        'train_seasons':train_seasons,'test_seasons':test_seasons,'ridge_penalty':ridge,'minimum_training_rows_per_position':20,
        'scoring_hash':sha256(canonical(scoring).encode()),
        'sign_rule':'Positive baseline-minus-augmentation CRPS AND MSE on identical pooled forward test rows; descriptive flag only.',
        'promotion_rule':'None. Never update projection or claim significance from this screen.',
        'evidence_status':'Exploratory; current historical final files; no professional forecast baseline; 2025 excluded.'}
    registration=append_event(warehouse.root,specification)
    enriched=enrich_features(warehouse,seasons=sorted(set(train_seasons+test_seasons)))
    result=evaluate_incremental(enriched,scoring,train_seasons=train_seasons,test_seasons=test_seasons,ridge=ridge)
    result.update(generated_at=time.time(),registration_event_hash=registration['event_hash'])
    code_hash=sha256(Path(__file__).read_bytes())
    artifact=warehouse.record_artifact('incremental_features',result,input_batches=enriched['input_batches'],
        cutoff=enriched['cutoff'],purpose='exploratory',code_version=code_hash,
        metadata={'registration_event_hash':registration['event_hash'],'scoring_hash':specification['scoring_hash']})
    result['artifact']=artifact
    out=Path(output_dir) if output_dir else warehouse.root/'derived'
    files={'json':_write(out/'incremental-features.json',canonical(result)+'\n')}
    fields=['metric','status','train_rows','test_rows','baseline_mse','augmented_mse','mse_improvement','baseline_crps','augmented_crps','crps_improvement','passes_descriptive_sign_rule']
    buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(result['results'])
    files['csv']=_write(out/'incremental-features.csv',buffer.getvalue())
    append_event(warehouse.root,{'kind':'incremental_features_completed','registration_event_hash':registration['event_hash'],
        'artifact_id':artifact['artifact_id'],'evaluated_candidates':result['evaluated_candidates'],'failure_count':result['failure_count'],
        'sign_rule_not_met':result['sign_rule_not_met'],'evidence_status':result['evidence_status']})
    return {**result,'files':files}
