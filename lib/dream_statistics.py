"""Bounded, paired, fixed-sample evidence; no claims about adaptive stopping."""
import math


def paired_evidence(pairs, alpha=.05, minimum_gain=.02, utility_bounds=(-1., 1.)):
    if type(alpha) not in (int,float) or not math.isfinite(alpha) or not 0 < alpha < 1:
        raise ValueError('alpha must be between zero and one')
    if type(minimum_gain) not in (int,float) or not math.isfinite(minimum_gain) or minimum_gain < 0:
        raise ValueError('minimum_gain must be finite and nonnegative')
    if len(utility_bounds)!=2 or any(type(x) not in (int,float) or not math.isfinite(x) for x in utility_bounds):
        raise ValueError('utility_bounds must contain two finite numbers')
    low,high=utility_bounds
    if low>=high:raise ValueError('utility_bounds must be increasing')
    clipped=lambda x:min(high,max(low,x))
    if any(not math.isfinite(v) for pair in pairs for v in pair):raise ValueError('nonfinite paired utility')
    differences=[clipped(new)-clipped(old) for new,old in pairs]
    n=len(differences);width=high-low
    mean=sum(differences)/n if n else None
    # Each independent task-level difference is in [-width,+width].
    radius=2*width*math.sqrt(math.log(1/alpha)/(2*n)) if n else None
    lower=max(-width,mean-radius) if n else None
    breached=any(v<low or v>high for pair in pairs for v in pair)
    return dict(method='paired-hoeffding-fixed-sample-v1',sample_size=n,alpha=alpha,
                minimum_gain=minimum_gain,utility_bounds=list(utility_bounds),
                mean_clipped_utility_difference=mean,lower_bound=lower,
                bounds_breached=breached,supported=bool(n and not breached and lower>minimum_gain),
                assumptions=['Independent held-out task instances from the target distribution.',
                             'Candidate, sample size, bounds and alpha fixed before inspecting held-out outcomes.',
                             'Bound concerns clipped utility; any observed clipping blocks recommendation.',
                             'Not an anytime-valid bound or a guarantee under task/model distribution shift.'])
