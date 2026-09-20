"""
bandit.py -- the statistical engine for Sleep Bandit.

Pure-Python (no numpy) Bayesian linear regression with Thompson sampling.

Model
-----
For each night we observe HRV (higher = better). Each intervention gets:

  * one shared "does taking it at all help?" effect      alpha_i
  * a small, heavily-shrunk deviation for each dose       delta_{i,d}

so the effect of taking intervention i at dose d, relative to not taking it, is

      effect(i, d) = alpha_i + delta_{i,d}

and a night's HRV is modelled as

      HRV = baseline + sum over taken (i,d) of ( alpha_i + delta_{i,d} ) + noise

Why the split?  It answers the user's two questions cleanly and *without the
maximization bias* that plagues "is any of my 3 doses above zero?" (with 3 noisy
estimates at least one is almost always positive):

  * "Does it help at all / make a difference?"  ->  a SINGLE parameter alpha_i.
    Under a true null, P(alpha_i > 0) is ~50% (well calibrated), not ~88%.
  * "What's the best dose?"                      ->  argmax_d ( alpha_i + delta_{i,d} ).

The delta's are shrunk hard toward zero (doses of the same supplement should
behave similarly), which is partial pooling: it borrows strength across doses
and keeps a single lucky dose from masquerading as a real effect.

Inference is a conjugate Normal-Inverse-Gamma Bayesian linear regression, so the
posterior is closed-form (no MCMC). Thompson sampling = draw one posterior
sample and, per intervention, pick the dose (incl. "off" = 0) with the highest
sampled effect. Good doses get chosen more; uncertainty keeps exploring.
"""

import math
import random

# ---------------------------------------------------------------------------
# Prior hyper-parameters (weakly informative; data overwhelms them quickly).
# Variances are in units of sigma^2 (the noise variance), so they scale with
# however noisy your HRV turns out to be.
# ---------------------------------------------------------------------------
# Baseline HRV level: huge => essentially unconstrained, never shrunk to zero.
INTERCEPT_PRIOR_VAR = 1.0e6
# alpha_i, the "does taking it help at all" effect. ~0.6 => prior SD ~0.8 sigma,
# weakly informative and centered on zero (assume no effect until shown).
ON_PRIOR_VAR = 0.6
# delta_{i,d}, per-dose deviation from the intervention's average effect. Small
# => doses are pulled toward a common effect (partial pooling), so a single
# lucky dose can't fake a signal.
DOSE_DEV_PRIOR_VAR = 0.12
# Inverse-Gamma prior on noise variance sigma^2. Prior mean = b0/(a0-1) = 144
# (~12ms night-to-night SD), held weakly so a couple weeks of data correct it.
SIGMA2_PRIOR_A = 3.0
SIGMA2_PRIOR_B = 288.0

# The smallest HRV change (in your units, e.g. ms) you'd consider worth acting
# on. The "likely helps" verdict is about clearing THIS, not merely clearing 0.
MEANINGFUL_EFFECT = 2.0

READOUT_SAMPLES = 3000     # posterior samples behind the probability read-outs
# The Reports read-outs are Monte-Carlo, so an unseeded run jitters a percent or
# two on every page load. We seed the read-out draws with a fixed value so the
# same data always yields the same reported numbers (planning stays random --
# only analyze() is seeded, and it restores the global RNG state afterwards).
READOUT_SEED = 1234567
DOSE_ROUND = 6             # decimals used to key doses (avoids float-equality bugs)

# Cold-start dose exploration (per-intervention option `delay_explore`, on by
# default). Until an intervention has this many compliant "on" nights, we lean on
# its standard dose instead of exploring alternatives -- so the on/off efficacy
# question is answered at one dose before the dose itself starts moving. With
# delay_explore off, the intervention skips the warm-up and explores from night one.
# The chance of exploring an off-standard dose on an "on" night ramps up
# *quadratically* -- p_explore = (n_on / WARMUP_ON_NIGHTS)**2 -- so it stays low
# for a long time and only accelerates once there's real evidence. With
# WARMUP_ON_NIGHTS = 64 the nightly chance of varying the dose is under 1-in-10
# for the first 20 on-nights (~0.6% at 5 nights, ~2.4% at 10, ~9.8% at 20), then
# climbs to full exploration by 64 on-nights. The on/off decision is untouched;
# this only governs which dose we use once an intervention is on.
WARMUP_ON_NIGHTS = 64

# Dose selection once an intervention is "on". We keep the STANDARD dose most nights
# while data is thin and only optimize the dose as evidence accumulates: the chance of
# deviating from the standard at all ramps up with the warm-up -- QUADRATICALLY for
# supplements (p = (n_on/WARMUP)^2, so under 1-in-10 for the first ~20 on-nights) and
# faster, LINEARLY, for time-based interventions, which are safe to explore harder.
# This single gate covers BOTH exploiting a non-standard best dose AND probing -- so
# nothing moves off the standard dose more than the ramp allows.
#
# When we do deviate, this fraction of those nights are exploratory probes (mapping the
# dose-response curve) and the rest exploit the best (minimum viable) dose so far.
# Supplements probe ONE unit dose at a time, for safety; time-based ones jump several
# rungs (TIME_MAX_JUMP) at once to find a far-off optimum faster.
PROBE_SHARE = 0.4
TIME_MAX_JUMP = 4

# FAST SWEEP: an intervention with delay_explore OFF *and* both a min and a max dose
# has told us two things -- "explore from night one" and "every dose in this range is
# fine to try" -- so the cautious one-rung-at-a-time climb (which also wants MIN_PROBES
# readings per rung before moving) is the wrong tool: on a 30-degree thermostat it
# would take months to leave the standard dose. Instead EVERY on-night is a Thompson
# draw over the WHOLE [min, max] grid, so exploration is steered by what has worked:
#   1. draw one posterior sample of the per-dose effects (the same draw that decided
#      on/off), giving a sampled effect at each dose measured so far;
#   2. the model treats doses as unrelated categories, so blend them across the grid
#      with a Gaussian kernel (bandwidth FAST_KERNEL_STEPS unit doses) -- a rung
#      inherits its neighbours' sampled effects, shrinking toward the plain "on"
#      effect where there is no nearby data (pseudo-weight FAST_PRIOR_WEIGHT);
#   3. each rung's own unknown deviation from that blend is then sampled too: a
#      Gaussian with the model's prior sd for a per-dose deviation
#      (sqrt(DOSE_DEV_PRIOR_VAR) x the sampled noise sd), shrunk by
#      sqrt(1 + effective readings nearby) -- so an untried rung next to a good dose
#      has a real chance of sampling highest, an untried rung next to a bad dose
#      almost none, and the spread collapses as readings accrue;
#   4. take the rung with the highest sampled value.
# It's Thompson sampling over an approximate smooth dose-response, so it explores by
# posterior probability of being best and exploits as the curve firms up. The on/off
# decision is untouched. Everything else (unbounded, or delay on) keeps the slow climb.
FAST_KERNEL_STEPS = 1.0
FAST_PRIOR_WEIGHT = 0.25

# COARSE-FIRST on big grids (a tree search over the dose range). A grid with MORE
# than COARSE_MIN_RUNGS rungs is too big to map rung by rung, so exploration goes in
# two levels. Level 1 is the COARSE set: every rung that is a multiple of
# COARSE_EVERY unit doses (5, 10, 15, 20 ... when the unit dose is 1), plus the min,
# the max, the standard dose and the open-ended arm if there is one. While a region
# is unsurveyed its fine rungs are DEPRIORITIZED, not locked: on COARSE_SHARE of
# probe nights only "open" rungs are eligible, the rest of the time the whole grid
# is. A FINE rung between two coarse rungs opens once both of them have been
# measured and at least one has MIN_PROBES readings -- i.e. the region has evidence
# at both ends and one end is a rung the search is investing in, which Thompson
# sampling only does for promising rungs. So refinement concentrates inside the
# most effective regions while bad regions mostly stay coarse. Already-measured
# rungs are always open (their data stays exploitable). The fast sweep's kernel
# interpolates the coarse readings (its bandwidth widens to half the typical gap
# between measured rungs), so a fine rung between two good coarse rungs draws high
# and one next to a bad coarse rung draws low. Only the SWEEPS do this (the fast
# sweep and the choice+dose sweep); the one-rung slow climb is untouched.
COARSE_MIN_RUNGS = 10
COARSE_EVERY = 5
COARSE_SHARE = 0.8

# Early lock-in guard for the on/off decision. Thompson sampling schedules an
# intervention ON with probability P(it helps); a few noisy early nights can push
# that near 1 (or near 0) and the intervention then gets (almost) always the same
# arm -- so the contrast that would correct the estimate stops being collected.
# The clamp is TIERED on the SMALLER arm's compliant-night count: under 15 nights the
# scheduling probability is held within [10%, 90%]; under 30, within [5%, 95%]; from
# 30 up it is unclamped. Between the bounds the decision is the ordinary Thompson
# draw; the clamp only bites at the extremes. The efficacy read-outs are not
# affected -- this is scheduling only. An intervention with no_lockin set opts out
# of the guard entirely (toggled from the Reports page cap indicator).
LOCKIN_TIERS = ((15, 0.90), (30, 0.95))   # (smaller arm has fewer than N nights, cap)
LOCKIN_P_SAMPLES = 400     # posterior draws used to estimate P(helps) for the clamp

# Minimum viable dose: we recommend (and exploit) the SMALLEST dose whose estimated
# effect is at least this fraction of the best measured dose's effect -- so a smaller
# dose that reaches ~95% of the peak is preferred over a bigger one at the peak.
MIN_VIABLE_FRAC = 0.95

# Pairwise interactions (Reports > Interactions). For two interventions A and B we
# fit  y = baseline + a*A + b*B + g*(A and B) + noise  on the nights where BOTH are
# determined (each confirmed-on or assigned-off). g is the interaction: g > 0 means
# the pair does better together than their separate effects add up to (synergy);
# g < 0 means worse than additive -- partial substitutes, or outright interference
# when the pair is worse than the better one alone. The interaction prior is shrunk
# below the main-effect prior: pairs are presumed roughly additive until the data
# says otherwise, which also tempers the many-pairs multiple-comparison problem.
INTERACTION_PRIOR_VAR = 0.3
INTERACTION_SAMPLES = 2000

# A frontier dose must gather at least this many measurements before the climb judges
# whether to extend past it -- otherwise a single noisy reading can wrongly halt the
# climb (or push it onward). Bigger = more robust but slower to map the curve.
MIN_PROBES = 5

# Open-ended top arm. A dose intervention can have ONE more arm ABOVE its max dose
# that isn't a number -- e.g. the spiky mat with no timer, staying on it until you
# fall asleep. It is recorded as this sentinel dose, sits exactly one rung above the
# max on the exploration grid (so the ordinary one-rung-up / jump logic can step into
# it and back out) and gets its own contrast column like any other level. While it
# has fewer than MIN_PROBES readings, this share of probe nights go straight to it so
# it actually gets measured. Mirrored in public/app.js (OPEN_ARM_DOSE).
OPEN_ARM_DOSE = 1e9
OPEN_ARM_PROBE_SHARE = 0.5


def is_open(d):
    """True if a recorded dose is the open-ended arm's sentinel."""
    try:
        return float(d) >= OPEN_ARM_DOSE / 2
    except (TypeError, ValueError):
        return False


def has_open_arm(iv):
    """The intervention tests an open-ended arm above its max (needs a max dose)."""
    return bool(iv.get("open_arm")) and _bounds(iv)[1] is not None


def _d(x):
    return round(float(x), DOSE_ROUND)


# "Multiple choice + dose" (kind 'choice_dose'): each night picks BOTH an option and
# a dose, and every (option, dose) pair is its own arm with its own dose-response
# point -- the model treats levels as unrelated categories, so no structure is
# assumed between options. A pair is stored as ONE level number so the rest of the
# pipeline (assignments, contrasts, read-outs) is untouched:
#     level = option_id * COMBO_BASE + dose
# Doses are far below COMBO_BASE and option ids far below OPEN_ARM_DOSE / COMBO_BASE,
# so the encoding is unambiguous and never collides with the open-arm sentinel.
# Mirrored in public/app.js (COMBO_BASE).
COMBO_BASE = 1e6


def combo_encode(opt_id, dose):
    return _d(int(opt_id) * COMBO_BASE + float(dose))


def combo_decode(level):
    """level -> (option_id, dose)."""
    level = float(level)
    opt = int(level // COMBO_BASE)
    return opt, _d(level - opt * COMBO_BASE)


def live_options(iv):
    """The intervention's options still on offer: retired (removed) options stay in
    iv['options'] so their past nights keep their label, but are never planned."""
    return [o for o in (iv.get("options") or []) if not o.get("retired")]


def candidate_levels(iv):
    """The intervention's candidate arms as level numbers: its dose levels, or for a
    choice+dose every option x dose pair (encoded)."""
    doses = [_d(x) for x in iv.get("dose_levels") or [] if x and float(x) > 0]
    if iv.get("kind") == "choice_dose":
        return [combo_encode(o["id"], d) for o in live_options(iv) for d in doses]
    return doses


# ---------------------------------------------------------------------------
# User prior: converting a PRIOR PROBABILITY that an intervention helps into the
# prior MEAN on its "does it help" effect alpha.
#
# Integrating sigma^2 out of the NIG prior, alpha is marginally Student-t:
#   alpha ~ t_nu(mean, scale),  nu = 2*a0,  scale = sqrt((b0/a0) * ON_PRIOR_VAR).
# So P(alpha > 0) = F_nu(mean / scale); to hit a target probability p we invert:
#   mean = scale * F_nu^{-1}(p).
# Because the posterior with NO data is exactly this prior, an intervention with no
# logged nights schedules at precisely the p the user typed -- then it moves with data.
# ---------------------------------------------------------------------------
_PRIOR_T_DOF = 2.0 * SIGMA2_PRIOR_A
_PRIOR_T_SCALE = math.sqrt((SIGMA2_PRIOR_B / SIGMA2_PRIOR_A) * ON_PRIOR_VAR)
# same idea for a per-option prior: the prior sd of one option's deviation
_DEV_PRIOR_SCALE = math.sqrt((SIGMA2_PRIOR_B / SIGMA2_PRIOR_A) * DOSE_DEV_PRIOR_VAR)


def effective_option_probs(options):
    """Prior probability per option id that it is the best one. Options carry an
    optional 'prior' in (0,1); the ones left blank share the remaining mass equally.
    If everything is assigned (or over-assigned) the numbers are renormalised to sum
    to one. Returns {} unless there are 2+ options."""
    opts = [o for o in (options or []) if o.get("id") is not None]
    if len(opts) < 2:
        return {}
    given = {}
    for o in opts:
        try:
            p = float(o.get("prior")) if o.get("prior") is not None else None
        except (TypeError, ValueError):
            p = None
        if p is not None and p > 0:
            given[int(o["id"])] = p
    rest = [int(o["id"]) for o in opts if int(o["id"]) not in given]
    s = sum(given.values())
    out = dict(given)
    if rest and s < 1.0:
        for oid in rest:
            out[oid] = (1.0 - s) / len(rest)
    else:
        for oid in rest:
            out[oid] = 0.0
    tot = sum(out.values())
    if tot <= 0:
        return {}
    return {oid: p / tot for oid, p in out.items()}


def option_prior_means(iv):
    """Prior MEANS on each option's deviation (from the 'any option' effect) that
    encode the user's prior over which option is best: proportional to the log of
    the option's probability, centred so they sum to zero, on the deviation-prior
    scale. A uniform prior (or none) gives {} -- no prior. Scheduling only."""
    probs = effective_option_probs(live_options(iv))
    if not probs:
        return {}
    floor = 1e-3
    logs = {oid: math.log(max(p, floor)) for oid, p in probs.items()}
    mean = sum(logs.values()) / len(logs)
    out = {oid: _DEV_PRIOR_SCALE * (v - mean) for oid, v in logs.items()}
    if max(abs(v) for v in out.values()) < 1e-9:
        return {}
    return out


def scheduling_priors(iv):
    """Everything the planner's prior-inclusive fit needs for one intervention:
    {'on': prior mean on the on-effect, 'options': {option_id: deviation mean}}, or
    None if the intervention carries no prior at all."""
    on = prior_mean_for_prob(iv.get("prior_helps") or 0)
    opts = option_prior_means(iv) if iv.get("kind") in ("choice", "choice_dose") else {}
    if not on and not opts:
        return None
    return {"on": on, "options": opts}


def _betacf(a, b, x):
    """Continued fraction for the incomplete beta (Lentz's method)."""
    FPMIN, EPS = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a, b, x):
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _student_t_cdf(t, nu):
    """P(T <= t) for a standard Student-t with nu degrees of freedom."""
    x = nu / (nu + t * t)
    ib = _betai(nu / 2.0, 0.5, x)          # I_x(nu/2, 1/2)
    return 1.0 - 0.5 * ib if t >= 0.0 else 0.5 * ib


def _student_t_ppf(p, nu):
    """Quantile (inverse CDF) of a standard Student-t, by bisection on the monotone CDF."""
    lo, hi = -1.0e6, 1.0e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _student_t_cdf(mid, nu) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def prior_mean_for_prob(p):
    """Prior mean on alpha giving a target prior P(alpha>0)=p. Returns 0 (no prior) for
    an unset (0), neutral (0.5), or out-of-range value; clamps away from 0/1 to stay
    finite. p in (0.5,1) -> positive mean (believed helpful); p in (0,0.5) -> negative."""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0.0 or p >= 1.0 or abs(p - 0.5) < 1e-9:
        return 0.0
    p = min(max(p, 1e-3), 1.0 - 1e-3)
    return _PRIOR_T_SCALE * _student_t_ppf(p, _PRIOR_T_DOF)


# ---------------------------------------------------------------------------
# Small linear-algebra helpers (matrices are tiny: p ~ 1 + 2*number of arms).
# ---------------------------------------------------------------------------
def cholesky(A):
    """Lower-triangular L with A = L L^T, for symmetric positive-definite A."""
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                d = A[i][i] - s
                if d <= 1e-12:
                    d = 1e-12
                L[i][j] = math.sqrt(d)
            else:
                L[i][j] = (A[i][j] - s) / L[j][j]
    return L


def solve_lower(L, b):
    n = len(b)
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    return y


def solve_upper_LT(L, y):
    n = len(y)
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


def spd_solve(L, b):
    return solve_upper_LT(L, solve_lower(L, b))


# ---------------------------------------------------------------------------
# Column layout: col 0 = intercept; then per intervention an "on" column and one
# deviation column per candidate dose.
# ---------------------------------------------------------------------------
def helmert_contrasts(k):
    """A k x (k-1) matrix of *normalized* Helmert contrasts: its columns are an
    orthonormal basis for the sum-to-zero subspace (each column has unit norm,
    sums to zero, and is orthogonal to the others). Because C C^T = I - 11^T/k,
    putting a diagonal prior of variance w on the contrast weights gives EVERY
    dose the same marginal deviation variance w*(1 - 1/k) -- no dose is singled
    out as a reference with inflated uncertainty (the old effect-coding bug that
    let the highest dose win P(best) on variance alone)."""
    C = [[0.0] * (k - 1) for _ in range(k)]
    for j in range(1, k):                  # contrast j uses column j-1
        norm = math.sqrt(j * (j + 1))
        for i in range(j):
            C[i][j - 1] = -1.0 / norm
        C[j][j - 1] = j / norm
    return C


def build_columns(interventions, history_doses):
    """
    Returns (cols, p). Each intervention gets one "on" column plus, if it has 2+
    candidate doses, k-1 orthonormal-contrast deviation columns (see
    helmert_contrasts). The contrasts are orthogonal to the "on" effect, so
    estimating dose-response doesn't inflate the uncertainty of the "does it help
    at all" answer, and they treat every dose symmetrically.

      cols[id] = {"on": idx, "doses": [...], "dev": [idx...],
                  "contrast": {dose: [c_1..c_{k-1}]}}

    We include every candidate dose *and* every dose seen in history, so old data
    always has a home even if you edit an intervention's dose list later.
    """
    cols = {}
    idx = 1  # column 0 is the intercept
    for iv in interventions:
        levels = set(candidate_levels(iv))
        for x in history_doses.get(iv["id"], ()):
            if x and float(x) > 0:
                levels.add(_d(x))
        if has_open_arm(iv):
            levels.add(_d(OPEN_ARM_DOSE))      # the open-ended arm is a level of its own
        doses = sorted(levels)
        entry = {"on": idx, "doses": doses, "dev": [], "contrast": {}}
        idx += 1
        k = len(doses)
        if k >= 2:
            entry["dev"] = list(range(idx, idx + k - 1))
            idx += k - 1
            C = helmert_contrasts(k)
            for i, dose in enumerate(doses):
                entry["contrast"][dose] = C[i]
        cols[iv["id"]] = entry
    return cols, idx


def build_design(completed_nights, cols, p):
    """Design matrix, outcomes, and per-night weights. A night may carry "w" (the
    recency weight, 1 = full strength); it scales that night's contribution to the
    likelihood -- both its pull on the coefficients and its share of the effective
    sample size."""
    X, y, w = [], [], []
    for nt in completed_nights:
        if nt.get("hrv") is None:
            continue
        row = [0.0] * p
        row[0] = 1.0
        for iid, dose in nt.get("taken", {}).items():
            dose = _d(dose)
            if dose <= 0:
                continue
            entry = cols.get(int(iid))
            if not entry:
                continue
            row[entry["on"]] = 1.0                     # took it at all
            crow = entry["contrast"].get(dose)         # its orthonormal contrast
            if crow:
                for c, col in zip(crow, entry["dev"]):
                    row[col] = c
        X.append(row)
        y.append(float(nt["hrv"]))
        w.append(float(nt.get("w", 1.0)))
    return X, y, w


def prior_variances(p, cols):
    v = [DOSE_DEV_PRIOR_VAR] * p
    v[0] = INTERCEPT_PRIOR_VAR
    for entry in cols.values():
        v[entry["on"]] = ON_PRIOR_VAR
        k = len(entry["doses"])
        if k >= 2:
            # Scale the contrast-weight prior so each dose keeps a marginal
            # deviation variance of DOSE_DEV_PRIOR_VAR: marginal = w*(1 - 1/k),
            # so w = DOSE_DEV_PRIOR_VAR * k/(k-1). Same shrinkage for every dose.
            w = DOSE_DEV_PRIOR_VAR * k / (k - 1)
            for col in entry["dev"]:
                v[col] = w
    return v


# ---------------------------------------------------------------------------
# Posterior fit (closed form) and sampling.
# ---------------------------------------------------------------------------
def fit(X, y, p, V0_diag, m0=None, w=None):
    """Conjugate Normal-Inverse-Gamma posterior. m0 is the prior MEAN vector (default
    zeros -> the uninformative centered-on-zero prior). A non-zero m0 shifts the
    posterior mean toward the prior and adds the m0^T Lambda0 m0 term to bN; it does
    NOT change the precision A (so the covariance and sampling are unaffected).

    w: optional per-night weights (power likelihood -- each night's log-likelihood
    is scaled by its weight, as if it were w nights). Weighted rows enter X'X and
    X'y scaled by w, and the effective sample size Sum(w) replaces n in the noise
    posterior, so decayed evidence both pulls less and certifies less."""
    n = len(X)
    A = [[0.0] * p for _ in range(p)]
    for k in range(p):
        A[k][k] += 1.0 / V0_diag[k]
    XtY = [0.0] * p
    for r in range(n):
        xr = X[r]
        wr = 1.0 if w is None else w[r]
        yr = y[r]
        nz = [a for a in range(p) if xr[a] != 0.0]
        for a in nz:
            xa = xr[a] * wr
            XtY[a] += xa * yr
            Aa = A[a]
            for b in nz:
                if b >= a:
                    Aa[b] += xa * xr[b]
    for a in range(p):
        for b in range(a + 1, p):
            A[b][a] = A[a][b]

    L = cholesky(A)
    # posterior mean solves A mN = X^T W y + Lambda0 m0  (Lambda0 = diag(1/V0))
    if m0 is None:
        rhs, m0Lam0m0 = XtY, 0.0
    else:
        lam0m0 = [m0[k] / V0_diag[k] for k in range(p)]
        rhs = [XtY[k] + lam0m0[k] for k in range(p)]
        m0Lam0m0 = sum(m0[k] * lam0m0[k] for k in range(p))
    mN = spd_solve(L, rhs)
    yty = sum(yi * yi for yi in y) if w is None else sum(w[i] * y[i] * y[i] for i in range(n))
    n_eff = float(n) if w is None else sum(w)
    mAm = sum(rhs[i] * mN[i] for i in range(p))   # mN^T A mN = mN . rhs
    aN = SIGMA2_PRIOR_A + n_eff / 2.0
    bN = SIGMA2_PRIOR_B + 0.5 * (yty + m0Lam0m0 - mAm)
    if bN < 1e-9:
        bN = 1e-9
    return {"L": L, "mN": mN, "aN": aN, "bN": bN, "p": p, "n": n}


def sample_beta(post):
    p = post["p"]
    g = random.gammavariate(post["aN"], 1.0 / post["bN"])  # X~Gamma(aN,1/bN)
    sigma2 = 1.0 / g                                        # sigma^2 ~ InvGamma
    sd = math.sqrt(sigma2)
    z = [random.gauss(0.0, 1.0) * sd for _ in range(p)]
    w = solve_upper_LT(post["L"], z)                        # Cov(w)=sigma2 A^-1
    beta = [post["mN"][i] + w[i] for i in range(p)]
    return beta, sigma2


def lockin_cap(n_on, n_off):
    """The scheduling-probability cap in force for these arm sizes (LOCKIN_TIERS,
    keyed on the smaller arm), or None once both arms are big enough."""
    smaller = min(n_on or 0, n_off or 0)
    for min_nights, cap in LOCKIN_TIERS:
        if smaller < min_nights:
            return cap
    return None


def lockin_active(n_on, n_off):
    """True while the early lock-in guard applies."""
    return lockin_cap(n_on, n_off) is not None


def clamp_p_on(p, n_on, n_off):
    """The scheduling probability actually used: p clamped to [1 - cap, cap] for the
    tier in force, else p itself."""
    cap = lockin_cap(n_on, n_off)
    if p is None or cap is None:
        return p
    return min(max(p, 1.0 - cap), cap)


def p_on_estimate(post, on_col, samples=LOCKIN_P_SAMPLES):
    """Monte-Carlo P(alpha > 0) for one intervention's 'on' column."""
    hits = 0
    for _ in range(samples):
        b, _ = sample_beta(post)
        if b[on_col] > 0.0:
            hits += 1
    return hits / float(samples)


def _decide_on(beta, post, entry, n_on, n_off, no_lockin=False):
    """Stage 1 of planning: is the intervention ON tonight? The ordinary Thompson
    rule is 'on iff the sampled alpha > 0'. While the lock-in guard applies, P(on) is
    estimated and, if it lies outside the tier's [1 - cap, cap], the decision is a
    coin at the clamped probability instead; inside the bounds the sample decides.
    no_lockin (the per-intervention override) skips the guard entirely."""
    on = beta[entry["on"]] > 0.0
    if no_lockin or not lockin_active(n_on, n_off):
        return on
    p = p_on_estimate(post, entry["on"])
    pc = clamp_p_on(p, n_on, n_off)
    if pc != p:                                   # clamp bites: override the sample
        return random.random() < pc
    return on


def _effect(beta, entry, dose):
    """effect(i, d) = alpha_i + delta_{i,d} from a sampled beta vector, where the
    deviation is the orthonormal-contrast reconstruction (0 if the intervention
    has fewer than two doses)."""
    e = beta[entry["on"]]
    crow = entry["contrast"].get(_d(dose))
    if crow:
        e += sum(c * beta[col] for c, col in zip(crow, entry["dev"]))
    return e


# ---------------------------------------------------------------------------
# Planning a night (Thompson) and read-outs (Monte Carlo).
# ---------------------------------------------------------------------------
def _best_dose(beta, entry, dose_levels):
    """Among an intervention's candidate doses, the one with the highest sampled
    effect (alpha + delta). Returns (dose, value) or (None, None) if it has none."""
    best_dose, best_val = None, None
    for x in dose_levels:
        dose = _d(x)
        if dose <= 0:
            continue
        v = _effect(beta, entry, dose)
        if best_val is None or v > best_val:
            best_val, best_dose = v, dose
    return best_dose, best_val


def _dose_step(iv):
    """One rung of the dose grid, used for exploration/hill-climbing: the unit dose
    if defined, else half the standard dose."""
    u = float(iv.get("unit_dose") or 0)
    if u > 0:
        return u
    s = float(iv.get("default_dose") or 0)
    return 0.5 * s if s > 0 else 0.0


def _bounds(iv):
    """Optional (min, max) dose bounds on an intervention; None = unbounded on that side."""
    def f(v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return x if x > 0 else None
    return f(iv.get("min_dose")), f(iv.get("max_dose"))


def _fast_grid(std, step, lo, hi, open_pos=None):
    """Every rung of the [lo, hi] dose grid, laid out from the standard dose in
    unit-dose steps (so the standard dose is always a rung), plus the open-arm rung
    if there is one. Used by the fast sweep."""
    rungs = set()
    k = 0
    while True:
        up, down = _d(std + k * step), _d(std - k * step)
        if up > hi and down < lo:
            break
        if lo <= up <= hi:
            rungs.add(up)
        if lo <= down <= hi and down > 0:
            rungs.add(down)
        k += 1
        if k > 10000:                                    # degenerate step; give up
            break
    if open_pos is not None:
        rungs.add(open_pos)
    return sorted(rungs)


def _coarse_rungs(grid, step, std, open_pos=None):
    """The coarse subset of a dose grid (see COARSE_MIN_RUNGS): multiples of
    COARSE_EVERY unit doses, plus the grid's min and max, the standard dose and the
    open-arm rung. None if the grid is small enough to explore rung by rung."""
    real = [r for r in grid if r != open_pos]
    if len(real) <= COARSE_MIN_RUNGS or step <= 0:
        return None
    span = COARSE_EVERY * step
    coarse = {r for r in real if abs(r / span - round(r / span)) < 1e-6}
    if len(coarse) < 2:               # the grid misses those multiples: every 5th rung from the standard
        coarse = {r for r in real if abs((r - std) / span - round((r - std) / span)) < 1e-6}
    coarse.update((real[0], real[-1]))
    if std in real:
        coarse.add(std)
    if open_pos is not None:
        coarse.add(open_pos)
    return coarse


def _rung_open(r, coarse, counts):
    """Is this rung open for probing? Coarse and already-measured rungs always; a fine
    rung once the coarse rungs bracketing it are both measured and one is firm."""
    if r in coarse or counts.get(r, 0) > 0:
        return True
    below = max((c for c in coarse if c < r), default=None)
    above = min((c for c in coarse if c > r), default=None)
    n = [counts.get(c, 0) for c in (below, above) if c is not None]
    return len(n) == 2 and min(n) >= 1 and max(n) >= MIN_PROBES


def _coarse_night(coarse):
    """Tonight's probe is restricted to open rungs (COARSE_SHARE of the time)."""
    return coarse is not None and random.random() < COARSE_SHARE


def _fast_pick(grid, beta, entry, counts, real, step, noise_sd, center=None):
    """The fast sweep's choice: one Thompson draw over the grid. Each rung's value is
        smoothed sampled effect + N(0, dev_sd / sqrt(1 + n_eff)),
    where the smoothed effect is a Gaussian-kernel blend (bandwidth FAST_KERNEL_STEPS
    unit doses) of the SAMPLED effects at measured rungs, shrunk toward the sampled
    plain "on" effect with pseudo-weight FAST_PRIOR_WEIGHT; dev_sd is the model's
    prior sd for one dose's deviation (sqrt(DOSE_DEV_PRIOR_VAR) x noise sd); n_eff is
    the kernel-weighted count of readings near the rung. Returns (rung, draw) for the
    rung with the highest draw. counts/real: position -> readings / the level to
    evaluate the model at (the open-arm rung's sentinel, or an encoded option+dose)."""
    h = FAST_KERNEL_STEPS * step
    # with sparse (coarse) data the kernel widens to half the typical gap between
    # measured rungs, so untried rungs in between interpolate their neighbours
    ps = sorted(p for p in counts if counts[p] > 0)
    gaps = sorted(b - a for a, b in zip(ps, ps[1:]))
    if gaps:
        h = max(h, 0.5 * gaps[len(gaps) // 2])
    # what a rung with no nearby data shrinks toward: the sampled plain "on" effect,
    # or a caller-supplied centre (e.g. an option's own sampled effect)
    alpha = beta[entry["on"]] if center is None else center
    dev_sd = math.sqrt(DOSE_DEV_PRIOR_VAR) * noise_sd
    measured = [(p, _effect(beta, entry, real[p]), counts[p]) for p in counts if counts[p] > 0]
    best, best_val = None, None
    for r in grid:
        wsum, esum, n_eff = FAST_PRIOR_WEIGHT, FAST_PRIOR_WEIGHT * alpha, 0.0
        for p, eff, n in measured:
            w = math.exp(-0.5 * ((r - p) / h) ** 2) if h > 0 else (1.0 if r == p else 0.0)
            wsum += w * n
            esum += w * n * eff
            n_eff += w * n
        val = esum / wsum + random.gauss(0.0, dev_sd / math.sqrt(1.0 + n_eff))
        if best_val is None or val > best_val:
            best, best_val = r, val
    return best, best_val


def _plan_combo(iv, beta, entry, measured, noise_sd):
    """Choice+dose, once it's ON: one Thompson draw over the whole option x dose grid.
    Each option gets the fast sweep's smoothed draw over its dose grid (its own
    measured pairs inform it; rungs with no nearby data shrink toward the shared
    'any option' effect plus a sampled deviation), and the option whose best rung
    drew highest wins. Untried options and untried doses therefore keep a real chance
    while anything measured bad fades. Returns the encoded level (0 if no options)."""
    std = _d(iv.get("default_dose") or 0)
    lo, hi = _bounds(iv)
    step = _dose_step(iv)
    if not iv.get("explore_doses", True) or step <= 0:
        grid = [std]
    elif lo is not None and hi is not None:
        grid = _fast_grid(std, step, lo, hi)
    else:
        grid = sorted({x for x in (std, _d(std - step), _d(std + step)) if x > 0})
    best_level, best_val = 0.0, None
    for o in live_options(iv):
        oid = int(o["id"])
        counts, real = {}, {}
        for x in measured:
            xo, xd = combo_decode(x)
            if xo != oid or xd <= 0:
                continue
            counts[xd] = counts.get(xd, 0) + 1
            real[xd] = _d(x)
        g = sorted(set(grid) | set(counts))     # measured doses stay exploitable
        coarse = _coarse_rungs(g, step, std)
        if _coarse_night(coarse):
            g = [r for r in g if _rung_open(r, coarse, counts)]
        # this option's own sampled effect (its candidate levels carry its prior and
        # any of its data) is what its untried rungs shrink toward -- so a favoured
        # option stays favoured until its nights say otherwise
        lv = [combo_encode(oid, d) for d in (iv.get("dose_levels") or []) if d and float(d) > 0]
        lv = [x for x in lv if x in entry["contrast"]] or None
        centre = (sum(_effect(beta, entry, x) for x in lv) / len(lv)) if lv else None
        dose, val = _fast_pick(g, beta, entry, counts, real, step if step > 0 else 1.0, noise_sd, center=centre)
        if dose is None:
            continue
        if best_val is None or val > best_val:
            best_level, best_val = combo_encode(oid, dose), val
    return best_level


def _in_bounds(d, lo, hi):
    return (lo is None or d >= lo) and (hi is None or d <= hi)


def _clamp(d, lo, hi):
    if lo is not None and d < lo:
        d = lo
    if hi is not None and d > hi:
        d = hi
    return _d(d)


def _time_based(iv):
    """True for time-dosed interventions (minutes/seconds/hours) -- safe to explore
    aggressively, unlike a supplement measured in mg/g."""
    return (iv.get("unit") or "").strip().lower().startswith(("min", "sec", "hour"))


def _min_viable_dose(effs):
    """The SMALLEST dose whose effect is >= MIN_VIABLE_FRAC of the best dose's effect,
    when the best clears baseline. effs is {dose: effect}. None if nothing helps."""
    if not effs:
        return None
    best = max(effs.values())
    if best <= 0:
        return None
    thr = MIN_VIABLE_FRAC * best
    return min(d for d, e in effs.items() if e >= thr)


def _supp_probe(seen, counts, effs, best, std, step, lo=None, hi=None):
    """A supplement's one-rung exploration probe: firm up an edge dose with a few
    readings (MIN_PROBES), then step exactly ONE unit dose past it -- up while the top is
    still rising, down while the bottom is still viable. Never jumps more than one rung,
    and never outside the optional [lo, hi] bounds."""
    if not seen:
        targets = [std - step, std + step]
    else:
        thr = MIN_VIABLE_FRAC * best if (best is not None and best > 0) else None
        top, bot = seen[-1], seen[0]
        targets = []
        if counts[top] < MIN_PROBES:
            targets.append(top)                       # firm up the top first
        elif len(seen) < 2 or effs[top] > effs[seen[-2]]:
            targets.append(top + step)                # still rising -> climb
        if counts[bot] < MIN_PROBES:
            targets.append(bot)                       # firm up the bottom
        elif thr is None or effs[bot] >= thr:
            targets.append(bot - step)                # still viable -> reach down
    targets = [_d(d) for d in targets if d > 0 and _in_bounds(_d(d), lo, hi)]
    return random.choice(targets) if targets else std


def plan_night(post, cols, active_interventions, n_on=None, measured=None, n_off=None):
    """Draw ONE posterior sample (Thompson sampling) and, per intervention, decide
    tonight's arm in TWO independent stages:

      1. Should we suggest it at all?  For a dose intervention this is decided ONLY
         by the "does it help at all" effect alpha_i: we take it iff a sampled
         alpha_i > 0. This keeps the on/off decision unbiased -- P(on) ~ P(it helps)
         -- and independent of how many candidate doses it has.
      2. If it's on, which dose?  Keep the STANDARD dose most nights while data is thin;
         the chance of deviating ramps with the warm-up (quadratic for supplements ->
         under 1-in-10 for the first ~20 on-nights, faster for time-based) -- unless the
         intervention has delay_explore off, in which case it's fully warm from the
         start and explores every on-night at the normal rate. When it does
         deviate it either exploits the MINIMUM VIABLE dose (smallest within
         MIN_VIABLE_FRAC of the best measured effect) or, PROBE_SHARE of the time, probes
         to map the curve -- supplements one unit dose at a time, time-based in bigger
         jumps -- so the search can reach an optimum far from the standard.
         FAST SWEEP: with delay_explore off AND both min and max bounds set, every
         on-night is instead a Thompson draw over the whole bounded grid -- see the
         FAST_* constants. On a big grid (more than COARSE_MIN_RUNGS rungs) the
         sweep goes COARSE-FIRST: multiples of COARSE_EVERY unit doses (plus min,
         max, standard) are mostly probed before the fine rungs between them,
         which open region by region as the coarse rungs firm up. The slow climb
         is untouched.

    Multiple-choice interventions skip stage 1 -- "off" is NOT an option and we take
    the best option even if all look weakly negative.

    measured: optional {iv_id: iterable of doses actually recorded} -- the points the
    curve is built from; n_on / n_off: optional {iv_id: compliant on-/off-night count}
    for the warm-up ramp and the early lock-in guard (LOCKIN_*)."""
    beta, sigma2 = sample_beta(post)
    n_on = n_on or {}
    n_off = n_off or {}
    measured = measured or {}
    plan = {}
    for iv in active_interventions:
        entry = cols.get(iv["id"])
        if not entry:
            plan[iv["id"]] = 0.0
            continue

        if iv.get("kind") == "choice_dose":
            # Multiple choice + dose: same on/off rule as a choice (always on unless it
            # tests skipping), then a joint search over option x dose.
            if iv.get("test_skip") and not _decide_on(beta, post, entry,
                                                      n_on.get(iv["id"], 0), n_off.get(iv["id"], 0),
                                                      iv.get("no_lockin", False)):
                plan[iv["id"]] = 0.0
                continue
            plan[iv["id"]] = _plan_combo(iv, beta, entry, measured.get(iv["id"], ()), math.sqrt(sigma2))
            continue

        if iv.get("kind") == "choice":
            # By default a choice always lands on some option. If it also tests the
            # "skip" option, run stage 1 first: skip when a sampled alpha <= 0, which
            # sprinkles in control nights so the average effect of doing ANY option vs
            # nothing becomes measurable.
            if iv.get("test_skip") and not _decide_on(beta, post, entry,
                                                      n_on.get(iv["id"], 0), n_off.get(iv["id"], 0),
                                                      iv.get("no_lockin", False)):
                plan[iv["id"]] = 0.0
                continue
            best_dose, _ = _best_dose(beta, entry, iv["dose_levels"])
            plan[iv["id"]] = best_dose if best_dose is not None else 0.0
            continue

        # Stage 1: on/off from the "does it help at all" effect alone (unbiased),
        # with the early lock-in guard clamping the extremes while data is thin.
        if not _decide_on(beta, post, entry, n_on.get(iv["id"], 0), n_off.get(iv["id"], 0),
                          iv.get("no_lockin", False)):
            plan[iv["id"]] = 0.0
            continue

        # Boolean interventions are a plain yes -- there's no dose to choose.
        if iv.get("kind") == "boolean":
            plan[iv["id"]] = 1.0
            continue

        # Stage 2: it's on -- choose the dose. With dose exploration switched off the
        # intervention only ever uses its standard dose -- no probing, no hill-climb
        # (some doses aren't merely un-explored but physically unavailable, e.g. an AC
        # that can't be set below its minimum temperature).
        std = _d(iv.get("default_dose") or 0)
        lo, hi = _bounds(iv)
        if not iv.get("explore_doses", True):
            plan[iv["id"]] = std
            continue
        # Otherwise, if this intervention delays exploration (the default), stay on
        # the STANDARD dose most nights while data is thin; the chance of deviating
        # ramps with the warm-up (quadratic for supplements -> <1-in-10 for the first
        # ~20 on-nights, faster for time-based). With the delay switched off the
        # intervention is treated as fully warmed up from night one. Either way this
        # gate covers BOTH exploiting a non-standard dose AND probing.
        step = _dose_step(iv)
        timed = _time_based(iv)
        if iv.get("delay_explore", True):
            warm = min(1.0, n_on.get(iv["id"], 0) / WARMUP_ON_NIGHTS)
            p_deviate = warm if timed else warm * warm
        else:
            p_deviate = 1.0
        if step <= 0 or random.random() >= p_deviate:
            plan[iv["id"]] = std
            continue

        # Deviating tonight: reconstruct the measured dose-response curve. We work in
        # GRID POSITIONS: if the intervention has an open-ended arm it sits one rung
        # above the max, so the one-rung-up / jump logic below can step into it and
        # back out exactly like any other dose; positions are mapped back to real
        # doses (the sentinel OPEN_ARM_DOSE for that rung) at the end.
        open_ok = has_open_arm(iv)
        open_pos = _d(hi + step) if open_ok else None
        top = open_pos if open_ok else hi                # effective ceiling on the grid

        def pos(d):
            return open_pos if (open_ok and is_open(d)) else d

        def unpos(p):
            if open_ok and hi is not None and p > hi:    # anything past the max = open arm
                return OPEN_ARM_DOSE
            return _clamp(p, lo, hi)                     # bounds cap every deviation

        counts, real = {}, {}                            # position -> times measured / real dose
        for x in measured.get(iv["id"], ()):
            xd = _d(x)
            if xd <= 0 or (is_open(xd) and not open_ok):
                continue                                 # open-arm nights of a now-closed arm
            p = pos(xd)
            counts[p] = counts.get(p, 0) + 1
            real[p] = xd
        seen = sorted(counts)
        effs = {p: _effect(post["mN"], entry, real[p]) for p in seen}   # posterior-mean curve
        best = max(effs.values()) if effs else None

        # exploit target: minimum viable dose once there are >=2 measured doses
        center = std
        if len(seen) >= 2:
            mv = _min_viable_dose(effs)
            if mv is not None:
                center = mv

        n_open = counts.get(open_pos, 0) if open_ok else 0
        fast = (not iv.get("delay_explore", True)) and lo is not None and hi is not None
        if fast:
            # FAST SWEEP (delay off + bounded range): Thompson draw over the whole grid,
            # smoothed across neighbouring rungs, with an optimism bonus where data is
            # thin -- see FAST_* above.
            grid = _fast_grid(std, step, lo, hi, open_pos)
            coarse = _coarse_rungs(grid, step, std, open_pos)
            if _coarse_night(coarse):                    # big grid: coarse-first tree search
                grid = [r for r in grid if _rung_open(r, coarse, counts)]
            dose = _fast_pick(grid, beta, entry, counts, real, step, math.sqrt(sigma2))[0] if grid else std
            dose = unpos(dose) if dose and dose > 0 else 0.0
            plan[iv["id"]] = dose if dose and dose > 0 else 0.0
            continue
        if random.random() < PROBE_SHARE:                # explore: map the curve
            if open_ok and n_open < MIN_PROBES and random.random() < OPEN_ARM_PROBE_SHARE:
                dose = open_pos                          # give the open arm its first readings
            elif timed:
                j = random.randint(1, TIME_MAX_JUMP) * random.choice((-1, 1))
                probe = _d(center + j * step)
                dose = probe if probe > 0 else _d(center + step)
            else:
                dose = _supp_probe(seen, counts, effs, best, std, step, lo, top)
        else:
            dose = center                                # exploit the best dose so far
        dose = unpos(dose) if dose and dose > 0 else 0.0
        plan[iv["id"]] = dose if dose and dose > 0 else 0.0
    return plan


def plan_exploit(post, cols, interventions, measured=None):
    """MAXIMIZE-BENEFIT plan: all exploit, no explore, from the posterior MEAN (the
    shrinkage estimate) rather than a Thompson draw. Per intervention:
      - on iff the mean "does it help" effect alpha > 0 (a choice that doesn't test
        skipping is always on; a boolean is a plain yes);
      - the dose / option / option+dose with the highest mean effect alpha+delta,
        among the levels actually MEASURED so far (a never-tried level hasn't been
        found to do anything) -- the standard dose, or the best-by-prior option,
        when nothing has been measured yet.
    Returns {iv_id: {"dose": level, "effect": mean effect at it, "on_effect": alpha}}
    with dose 0 for "skip"."""
    mN = post["mN"]
    measured = measured or {}
    out = {}
    for iv in interventions:
        entry = cols.get(iv["id"])
        if not entry:
            out[iv["id"]] = {"dose": 0.0, "effect": 0.0, "on_effect": 0.0}
            continue
        alpha = mN[entry["on"]]
        kind = iv.get("kind")
        seen = sorted({_d(x) for x in measured.get(iv["id"], ()) if x and float(x) > 0
                       if not (is_open(x) and not has_open_arm(iv))})
        skip_ok = kind in ("dose", "boolean") or iv.get("test_skip")
        if skip_ok and alpha <= 0:
            out[iv["id"]] = {"dose": 0.0, "effect": alpha, "on_effect": alpha}
            continue
        if kind == "boolean":
            out[iv["id"]] = {"dose": 1.0, "effect": alpha, "on_effect": alpha}
            continue
        if seen:
            levels = seen
        elif kind == "choice_dose":
            std = _d(iv.get("default_dose") or 0)
            levels = [combo_encode(o["id"], std) for o in live_options(iv)]
        elif kind == "choice":
            levels = [_d(x) for x in iv.get("dose_levels") or [] if x and float(x) > 0]
        else:
            levels = [_d(iv.get("default_dose") or 0)]
        dose, val = _best_dose(mN, entry, levels)
        if dose is None:
            dose, val = 0.0, alpha
        out[iv["id"]] = {"dose": dose, "effect": val, "on_effect": alpha}
    return out


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def readouts(interventions, betas, cols, observed=None):
    """
    Per intervention:
      p_helps      = P(alpha_i > 0)                 -- any real benefit
      p_meaningful = P(alpha_i > MEANINGFUL_EFFECT) -- benefit worth acting on
      on_effect    = posterior mean/CI of alpha_i
      per dose     = mean/CI of (alpha+delta) and P(this dose is the best choice)

    observed: optional {iv_id: set of doses actually measured}. When given, per-dose
    rows (and the recommendation) are limited to doses that have real data -- the
    Reports page never shows a datapoint for a dose that hasn't been measured yet.
    """
    S = len(betas)
    out = {}
    for iv in interventions:
        entry = cols.get(iv["id"])
        if not entry:
            out[iv["id"]] = {"p_helps": 0.0, "p_meaningful": 0.0,
                             "on_effect": 0.0, "recommended_dose": 0.0,
                             "recommended_p": 0.0, "p_off_best": 0.0, "doses": []}
            continue
        if observed is not None:
            # only doses actually measured -- and any dose the climb has reached,
            # even if it was never in the original candidate list.
            levels = sorted(_d(x) for x in observed.get(iv["id"], ()) if x and float(x) > 0)
        else:
            levels = sorted(set(candidate_levels(iv)))

        on_samples = [betas[s][entry["on"]] for s in range(S)]
        helps = sum(1 for a in on_samples if a > 0.0)
        meaningful = sum(1 for a in on_samples if a > MEANINGFUL_EFFECT)

        eff = {dose: [_effect(betas[s], entry, dose) for s in range(S)] for dose in levels}
        best_counts = {0.0: 0}
        for dose in levels:
            best_counts[dose] = 0
        for s in range(S):
            bd, bv = 0.0, 0.0
            for dose in levels:
                v = eff[dose][s]
                if v > bv:
                    bv, bd = v, dose
            best_counts[bd] += 1

        dose_rows = []
        for dose in levels:
            samples = sorted(eff[dose])
            dose_rows.append({
                "dose": dose,
                "p_best": best_counts[dose] / S,
                "mean_effect": sum(samples) / S,
                "ci_low": _percentile(samples, 0.05),
                "ci_high": _percentile(samples, 0.95),
            })

        # Recommend the MINIMUM VIABLE dose: the smallest dose whose mean effect is
        # within MIN_VIABLE_FRAC of the best measured dose's -- so a smaller dose that
        # reaches ~95% of the peak wins over a bigger one at the peak. Fall back to
        # "off" (0) if no dose beats skipping on average. recommended_p is that dose's
        # P(best), reported for display.
        if iv.get("kind") == "choice_dose" and dose_rows:
            # choice+dose: pick the option whose best pair has the highest mean effect,
            # then the minimum viable dose WITHIN that option.
            by_opt = {}
            for row in dose_rows:
                o, dd = combo_decode(row["dose"])
                by_opt.setdefault(o, {})[dd] = row["mean_effect"]
            best_opt = max(by_opt, key=lambda o: max(by_opt[o].values()))
            mv = _min_viable_dose(by_opt[best_opt])
            rec_dose = combo_encode(best_opt, mv) if mv is not None else 0.0
        else:
            rec_dose = _min_viable_dose({row["dose"]: row["mean_effect"] for row in dose_rows}) or 0.0
        rec_p = best_counts.get(rec_dose, 0) / S

        on_sorted = sorted(on_samples)
        out[iv["id"]] = {
            "p_helps": helps / S,
            "p_meaningful": meaningful / S,
            "on_effect": sum(on_samples) / S,
            "on_ci_low": _percentile(on_sorted, 0.05),
            "on_ci_high": _percentile(on_sorted, 0.95),
            "p_off_best": best_counts[0.0] / S,
            "recommended_dose": rec_dose,
            "recommended_p": rec_p,
            "doses": dose_rows,
        }
    return out


def group_best(groups, negate=False):
    """P(best) across OBSERVED groups of nights -- e.g. the behavior states in a
    report card's observed section. groups: {key: [(w, y)]}, each night in exactly
    one group. Fits one mean per group (flat prior, shared noise posterior -- the
    same conjugate machinery as the randomized read-outs) and counts, over
    posterior draws, how often each group's mean is the highest. negate=True for a
    lower-is-better metric (resting HR). Returns {key: p_best} for groups with
    nights; groups without nights are omitted. Purely descriptive: the groups are
    not randomized, so this ranks averages, not causal effects."""
    keys = [k for k, v in groups.items() if v]
    if not keys:
        return {}
    if len(keys) == 1:
        return {keys[0]: 1.0}
    idx = {k: i for i, k in enumerate(keys)}
    X, y, w = [], [], []
    for k in keys:
        for wv, yv in groups[k]:
            row = [0.0] * len(keys)
            row[idx[k]] = 1.0
            X.append(row)
            y.append(-float(yv) if negate else float(yv))
            w.append(float(wv))
    post = fit(X, y, len(keys), [INTERCEPT_PRIOR_VAR] * len(keys),
               w=(w if any(x != 1.0 for x in w) else None))
    state = random.getstate()
    random.seed(READOUT_SEED)
    try:
        wins = [0] * len(keys)
        for _ in range(READOUT_SAMPLES):
            beta = sample_beta(post)[0]
            wins[max(range(len(keys)), key=lambda i: beta[i])] += 1
    finally:
        random.setstate(state)
    return {k: wins[idx[k]] / READOUT_SAMPLES for k in keys}


def pair_interaction(nights):
    """Interaction read-out for one pair. nights: [{"y": metric (higher = better),
    "a": 0/1, "b": 0/1}] -- only nights where both A and B are determined.

    Returns cell counts / raw means (neither, a_only, b_only, both) and, from the
    posterior: the main effects a, b; the interaction g (mean, 90% CI); the combined
    effect a+b+g; and the probabilities that drive the verdicts:
      p_synergy   = P(g > 0)                     better together than additive
      p_subadd    = P(g < 0)                     worse together than additive
      p_interfere = P(a+b+g < max(a, b))         the pair is WORSE than the better one alone
      p_stack     = P(a+b+g > max(a, b))         stacking beats the better one alone
    plus the conditional effects each one adds with / without the other (a vs a+g,
    b vs b+g) -- the direct answer to "does A still help on nights I take B?".
    None if there are no nights."""
    if not nights:
        return None
    X, y, w = [], [], []
    cells = {(0, 0): [], (1, 0): [], (0, 1): [], (1, 1): []}   # [(weight, y)]
    for n in nights:
        a, b = (1 if n["a"] else 0), (1 if n["b"] else 0)
        yv, wv = float(n["y"]), float(n.get("w", 1.0))
        X.append([1.0, float(a), float(b), float(a * b)])
        y.append(yv)
        w.append(wv)
        cells[(a, b)].append((wv, yv))
    V0 = [INTERCEPT_PRIOR_VAR, ON_PRIOR_VAR, ON_PRIOR_VAR, INTERACTION_PRIOR_VAR]
    post = fit(X, y, 4, V0, w=(w if any(x != 1.0 for x in w) else None))
    state = random.getstate()
    random.seed(READOUT_SEED)
    try:
        betas = [sample_beta(post)[0] for _ in range(INTERACTION_SAMPLES)]
    finally:
        random.setstate(state)
    S = len(betas)
    ga = [bt[1] for bt in betas]
    gb = [bt[2] for bt in betas]
    gg = [bt[3] for bt in betas]
    comb = [ga[i] + gb[i] + gg[i] for i in range(S)]
    best1 = [max(ga[i], gb[i]) for i in range(S)]
    # conditional effects: what A adds when B is off (a) vs on (a+g), and vice versa.
    a_with_b = [ga[i] + gg[i] for i in range(S)]
    b_with_a = [gb[i] + gg[i] for i in range(S)]

    def summ(v):
        sv = sorted(v)
        return {"mean": sum(sv) / S, "ci_low": _percentile(sv, 0.05), "ci_high": _percentile(sv, 0.95)}

    # weighted cell counts (= plain counts when unweighted) and weighted raw means
    def wcount(v):
        s = sum(wv for wv, _ in v)
        return int(round(s)) if abs(s - round(s)) < 1e-9 else round(s, 1)

    def mean_or_none(v):
        s = sum(wv for wv, _ in v)
        return (sum(wv * yv for wv, yv in v) / s) if s > 0 else None

    return {
        "n": wcount([(n.get("w", 1.0), 0) for n in nights]),
        "n_neither": wcount(cells[(0, 0)]), "n_a_only": wcount(cells[(1, 0)]),
        "n_b_only": wcount(cells[(0, 1)]), "n_both": wcount(cells[(1, 1)]),
        "raw_neither": mean_or_none(cells[(0, 0)]), "raw_a_only": mean_or_none(cells[(1, 0)]),
        "raw_b_only": mean_or_none(cells[(0, 1)]), "raw_both": mean_or_none(cells[(1, 1)]),
        "effect_a": summ(ga), "effect_b": summ(gb),
        "effect_a_with_b": summ(a_with_b), "effect_b_with_a": summ(b_with_a),
        "p_a_helps_without_b": sum(1 for v in ga if v > 0) / S,
        "p_a_helps_with_b": sum(1 for v in a_with_b if v > 0) / S,
        "p_b_helps_without_a": sum(1 for v in gb if v > 0) / S,
        "p_b_helps_with_a": sum(1 for v in b_with_a if v > 0) / S,
        "interaction": summ(gg), "combined": summ(comb),
        "p_synergy": sum(1 for v in gg if v > 0) / S,
        "p_subadd": sum(1 for v in gg if v < 0) / S,
        "p_interfere": sum(1 for i in range(S) if comb[i] < best1[i]) / S,
        "p_stack": sum(1 for i in range(S) if comb[i] > best1[i]) / S,
    }


def fit_model(interventions, completed_nights, priors=None):
    """Fit the posterior only (no read-out sampling) -- enough for planning.

    priors: optional {iv_id: prior_mean} giving a NON-zero prior mean on that
    intervention's "does it help" effect (used for scheduling, so a believed-helpful
    intervention is chosen more often early). Omit for the uninformative default."""
    history = {}
    for nt in completed_nights:
        for iid, dose in nt.get("taken", {}).items():
            if dose and float(dose) > 0:
                history.setdefault(int(iid), set()).add(_d(dose))

    cols, p = build_columns(interventions, history)
    X, y, w = build_design(completed_nights, cols, p)
    V0 = prior_variances(p, cols)
    m0 = None
    if priors:
        m0 = [0.0] * p
        for iv in interventions:
            pm = priors.get(iv["id"])
            if not pm:
                continue
            entry = cols[iv["id"]]
            # a bare number is a prior mean on the "on" effect; a dict may also carry
            # per-option deviation means (see scheduling_priors)
            on = pm.get("on") if isinstance(pm, dict) else pm
            if on:
                m0[entry["on"]] = float(on)
            opt_means = pm.get("options") if isinstance(pm, dict) else None
            if opt_means and entry["dev"]:
                # level deviation means -> contrast-column means (C is orthonormal
                # across levels and the means are centred, so beta_dev = C^T m)
                def level_mean(level):
                    oid = combo_decode(level)[0] if iv.get("kind") == "choice_dose" else int(round(level))
                    return float(opt_means.get(oid, 0.0))
                for j, col in enumerate(entry["dev"]):
                    m0[col] = sum(crow[j] * level_mean(level) for level, crow in entry["contrast"].items())
    post = fit(X, y, p, V0, m0, w=(w if any(x != 1.0 for x in w) else None))
    return {"post": post, "cols": cols, "n": post["n"]}


def analyze(interventions, completed_nights, priors=None):
    """Fit + the Monte-Carlo read-outs used by the Reports tab. priors is passed through
    to fit_model (used for the scheduling probability; efficacy read-outs pass none)."""
    m = fit_model(interventions, completed_nights, priors)
    # Seed the read-out draws so the reported numbers are stable across page loads;
    # save/restore the global RNG so planning (Thompson sampling) stays random.
    state = random.getstate()
    random.seed(READOUT_SEED)
    try:
        betas = [sample_beta(m["post"])[0] for _ in range(READOUT_SAMPLES)]
    finally:
        random.setstate(state)
    # Which doses have actually been measured? (only these get a datapoint.)
    observed = {}
    for nt in completed_nights:
        if nt.get("hrv") is None:
            continue
        for iid, dose in nt.get("taken", {}).items():
            if dose and float(dose) > 0:
                observed.setdefault(int(iid), set()).add(_d(dose))
    reads = readouts(interventions, betas, m["cols"], observed)
    return {"post": m["post"], "cols": m["cols"], "readouts": reads, "n": m["n"]}


# ---------------------------------------------------------------------------
# Self-test: recover a known effect (python3 bandit.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    random.seed(1)
    ivs = [{"id": 1, "dose_levels": [100, 200]}]
    nights = []
    for i in range(120):
        r = random.random()
        if r < 0.34:
            taken, lift = {}, 0.0
        elif r < 0.67:
            taken, lift = {1: 100}, 2.0
        else:
            taken, lift = {1: 200}, 8.0
        nights.append({"hrv": 55.0 + lift + random.gauss(0, 6), "taken": taken})

    res = analyze(ivs, nights)
    r = res["readouts"][1]
    print("nights:", res["n"])
    print("P(helps at all) = %.2f   P(meaningful >%.0f) = %.2f   on-effect = %+.2f"
          % (r["p_helps"], MEANINGFUL_EFFECT, r["p_meaningful"], r["on_effect"]))
    for row in r["doses"]:
        print("  dose %-5g  effect %+6.2f  [%+.2f, %+.2f]  P(best)=%.2f"
              % (row["dose"], row["mean_effect"], row["ci_low"],
                 row["ci_high"], row["p_best"]))
    print("recommended dose:", r["recommended_dose"])
