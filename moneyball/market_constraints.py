"""Separate observed threshold restrictions from assumptions about missing tails.

Quote bands are sensitivity restrictions, not frequentist confidence intervals.
Entropy projection follows the discrete minimum-relative-entropy construction
in Meucci (2010), arXiv:1012.2848, equations25–28; a prior is always explicit.
"""
import math
from collections import defaultdict
from .market_distribution import devig, implied_probability


def envelope(points):
    """Tight monotonic CDF bounds at integer boundaries; no silent repair."""
    by_x=defaultdict(list)
    for p in points:
        x=p['x'];lo=p['cdf_low'];hi=p['cdf_high']
        if isinstance(x,bool) or not isinstance(x,int):raise ValueError('Integer CDF boundary required')
        if any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in (lo,hi)):
            raise ValueError('Finite probability bounds required')
        if not 0<=lo<=hi<=1:raise ValueError('Invalid CDF interval')
        by_x[x].append(p)
    rows=[{'x':x,'cdf_low':max(p['cdf_low'] for p in ps),
           'cdf_high':min(p['cdf_high'] for p in ps),'observations':ps}
          for x,ps in sorted(by_x.items())]
    for i in range(1,len(rows)):rows[i]['cdf_low']=max(rows[i]['cdf_low'],rows[i-1]['cdf_low'])
    for i in range(len(rows)-2,-1,-1):rows[i]['cdf_high']=min(rows[i]['cdf_high'],rows[i+1]['cdf_high'])
    violations=[{'x':p['x'],'gap':p['cdf_low']-p['cdf_high']} for p in rows if p['cdf_low']>p['cdf_high']+1e-10]
    return {'points':rows,'feasible':not violations,'violations':violations,
            'confidence_interval':None,'independent_sample_size':None,
            'interpretation':'CDF restrictions conditional on interpreting the supplied price/margin bands as probability bounds; not empirical calibration.'}


def quantile_bounds(curve,q):
    if not 0<q<1 or not curve['feasible']:raise ValueError('Feasible CDF and interior quantile required')
    lower=upper=None
    for p in curve['points']:
        if p['cdf_high']<q-1e-12:lower=p['x']+1
        if p['cdf_low']>=q-1e-12:
            upper=p['x'];break
    return {'probability':q,'lower':lower,'upper':upper,
            'tail_note':'Null endpoint means the acquired thresholds do not bound that tail.'}


def finite_support_mean_bounds(curve,lower,upper):
    if not curve['feasible'] or not isinstance(lower,int) or not isinstance(upper,int) or lower>=upper:
        raise ValueError('Feasible CDF and explicit finite integer support required')
    if any(not lower<=p['x']<upper for p in curve['points']):raise ValueError('Threshold outside support')
    # For integer X, E[X]=L+sum(k=L..U-1) P(X>k). Missing intervals use only
    # monotonic restrictions; no interpolation or invented terminal probability.
    mean_low=mean_high=float(lower)
    for k in range(lower,upper):
        lows=[p['cdf_low'] for p in curve['points'] if p['x']<=k]
        highs=[p['cdf_high'] for p in curve['points'] if p['x']>=k]
        mean_low+=1-min(highs,default=1)
        mean_high+=1-max(lows,default=0)
    return {'lower':mean_low,'upper':mean_high,'support_assumption':[lower,upper],
            'interpretation':'Identification bounds conditional on the supplied finite support, not a confidence interval.'}


def anchored_alternate_probability(yes_odds,over_odds,under_odds,method):
    """Explicit sensitivity: transfer the player's main-line margin to an alt.

    There is no paired alt price here. The hypothesis that its margin follows
    the same transform is NOT established by the paired main line or literature.
    """
    q=implied_probability(yes_odds,'american')
    qo=implied_probability(over_odds,'american');qu=implied_probability(under_odds,'american')
    total=qo+qu
    if method=='multiplicative':p=q/total
    elif method=='additive':p=q-(total-1)/2
    elif method=='power':
        lo,hi=0.,1.
        while qo**hi+qu**hi>1:hi*=2
        for _ in range(80):
            mid=(lo+hi)/2
            if qo**mid+qu**mid>1:lo=mid
            else:hi=mid
        p=q**((lo+hi)/2)
    else:raise ValueError('Unknown margin method')
    if not 0<p<1:raise ValueError('Transferred margin yields invalid alternate probability')
    return {'over':p,'method':method,'main_line_overround':total-1,
            'conditional_on':'Same margin transform as this player/stat main line; action conditions match; neither assumption validated.',
            'paired_alternate':False,'empirically_calibrated':False}


def entropy_projection(curve,values,prior,*,tolerance=1e-8):
    """Reweight caller-supplied scenarios; never manufacture missing support.

    SciPy's convex optimizer provides materially safer numerical behavior than
    hand-writing a general constrained solver. It is an optional dependency.
    Effective scenario count measures weight concentration, not data sample N.
    """
    import numpy as np
    from scipy.optimize import minimize
    from scipy.special import logsumexp
    if not curve['feasible']:raise ValueError('Inconsistent threshold constraints')
    x=np.asarray(values,dtype=float);p=np.asarray(prior,dtype=float)
    if x.ndim!=1 or p.shape!=x.shape or len(x)==0 or not np.all(np.isfinite(x)) or not np.all(np.isfinite(p)) or np.any(p<=0):
        raise ValueError('Finite scenarios and positive prior weights required')
    p=p/p.sum();logp=np.log(p);a=[];b=[]
    for point in curve['points']:
        indicator=(x<=point['x']).astype(float)
        a.extend([indicator,-indicator]);b.extend([point['cdf_high'],-point['cdf_low']])
    if not a:return {'weights':p.tolist(),'kl_divergence':0,'max_constraint_violation':0,'effective_scenario_count':float(1/(p@p))}
    A=np.asarray(a);B=np.asarray(b)
    def objective(lam):
        logw=logp-lam@A;z=logsumexp(logw);weights=np.exp(logw-z)
        return float(z+lam@B),B-A@weights
    fit=minimize(objective,np.zeros(len(b)),jac=True,method='L-BFGS-B',
                 bounds=[(0,None)]*len(b),options={'ftol':1e-14,'gtol':tolerance/10,'maxiter':10000})
    logw=logp-fit.x@A;q=np.exp(logw-logsumexp(logw))
    violation=float(max(0,np.max(A@q-B)))
    if not fit.success or violation>tolerance:
        raise ValueError('Prior support cannot satisfy constraints or numerical optimization failed: '+str(fit.message)+'; violation='+str(violation))
    return {'weights':q.tolist(),'kl_divergence':float(np.sum(q*(np.log(np.maximum(q,1e-300))-logp))),
            'max_constraint_violation':violation,'effective_scenario_count':float(1/(q@q)),
            'mean':float(q@x),'variance':float(q@((x-q@x)**2)),
            'prior_mean':float(p@x),'prior_support':[float(x.min()),float(x.max())],
            'method':'Minimum relative entropy subject to explicit threshold bounds',
            'reference':'https://arxiv.org/abs/1012.2848',
            'confidence_interval':None,'empirically_calibrated':False,
            'interpretation':'Model-conditional reweighting. Preserves supplied scenarios, not necessarily their prior correlations. Cannot create an unseen failure or tail state.'}
