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

# Cold-start dose exploration. Until an intervention has this many compliant
# "on" nights, we lean on its standard dose instead of exploring alternatives.
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

# Minimum viable dose: we recommend (and exploit) the SMALLEST dose whose estimated
# effect is at least this fraction of the best measured dose's effect -- so a smaller
# dose that reaches ~95% of the peak is preferred over a bigger one at the peak.
MIN_VIABLE_FRAC = 0.95

# A frontier dose must gather at least this many measurements before the climb judges
# whether to extend past it -- otherwise a single noisy reading can wrongly halt the
# climb (or push it onward). Bigger = more robust but slower to map the curve.
MIN_PROBES = 5


def _d(x):
    return round(float(x), DOSE_ROUND)


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
        levels = set(_d(x) for x in iv["dose_levels"] if x and float(x) > 0)
        for x in history_doses.get(iv["id"], ()):
            if x and float(x) > 0:
                levels.add(_d(x))
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
    X, y = [], []
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
    return X, y


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
def fit(X, y, p, V0_diag, m0=None):
    """Conjugate Normal-Inverse-Gamma posterior. m0 is the prior MEAN vector (default
    zeros -> the uninformative centered-on-zero prior). A non-zero m0 shifts the
    posterior mean toward the prior and adds the m0^T Lambda0 m0 term to bN; it does
    NOT change the precision A (so the covariance and sampling are unaffected)."""
    n = len(X)
    A = [[0.0] * p for _ in range(p)]
    for k in range(p):
        A[k][k] += 1.0 / V0_diag[k]
    XtY = [0.0] * p
    for r in range(n):
        xr = X[r]
        yr = y[r]
        nz = [a for a in range(p) if xr[a] != 0.0]
        for a in nz:
            xa = xr[a]
            XtY[a] += xa * yr
            Aa = A[a]
            for b in nz:
                if b >= a:
                    Aa[b] += xa * xr[b]
    for a in range(p):
        for b in range(a + 1, p):
            A[b][a] = A[a][b]

    L = cholesky(A)
    # posterior mean solves A mN = X^T y + Lambda0 m0  (Lambda0 = diag(1/V0))
    if m0 is None:
        rhs, m0Lam0m0 = XtY, 0.0
    else:
        lam0m0 = [m0[k] / V0_diag[k] for k in range(p)]
        rhs = [XtY[k] + lam0m0[k] for k in range(p)]
        m0Lam0m0 = sum(m0[k] * lam0m0[k] for k in range(p))
    mN = spd_solve(L, rhs)
    yty = sum(yi * yi for yi in y)
    mAm = sum(rhs[i] * mN[i] for i in range(p))   # mN^T A mN = mN . rhs
    aN = SIGMA2_PRIOR_A + n / 2.0
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


def _supp_probe(seen, counts, effs, best, std, step):
    """A supplement's one-rung exploration probe: firm up an edge dose with a few
    readings (MIN_PROBES), then step exactly ONE unit dose past it -- up while the top is
    still rising, down while the bottom is still viable. Never jumps more than one rung."""
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
    targets = [_d(d) for d in targets if d > 0]
    return random.choice(targets) if targets else std


def plan_night(post, cols, active_interventions, n_on=None, measured=None):
    """Draw ONE posterior sample (Thompson sampling) and, per intervention, decide
    tonight's arm in TWO independent stages:

      1. Should we suggest it at all?  For a dose intervention this is decided ONLY
         by the "does it help at all" effect alpha_i: we take it iff a sampled
         alpha_i > 0. This keeps the on/off decision unbiased -- P(on) ~ P(it helps)
         -- and independent of how many candidate doses it has.
      2. If it's on, which dose?  Keep the STANDARD dose most nights while data is thin;
         the chance of deviating ramps with the warm-up (quadratic for supplements ->
         under 1-in-10 for the first ~20 on-nights, faster for time-based). When it does
         deviate it either exploits the MINIMUM VIABLE dose (smallest within
         MIN_VIABLE_FRAC of the best measured effect) or, PROBE_SHARE of the time, probes
         to map the curve -- supplements one unit dose at a time, time-based in bigger
         jumps -- so the search can reach an optimum far from the standard.

    Multiple-choice interventions skip stage 1 -- "off" is NOT an option and we take
    the best option even if all look weakly negative.

    measured: optional {iv_id: iterable of doses actually recorded} -- the points the
    curve is built from; n_on: optional {iv_id: compliant on-night count} for the ramp."""
    beta, _ = sample_beta(post)
    n_on = n_on or {}
    measured = measured or {}
    plan = {}
    for iv in active_interventions:
        entry = cols.get(iv["id"])
        if not entry:
            plan[iv["id"]] = 0.0
            continue

        if iv.get("kind") == "choice":
            # By default a choice always lands on some option. If it also tests the
            # "skip" option, run stage 1 first: skip when a sampled alpha <= 0, which
            # sprinkles in control nights so the average effect of doing ANY option vs
            # nothing becomes measurable.
            if iv.get("test_skip") and beta[entry["on"]] <= 0.0:
                plan[iv["id"]] = 0.0
                continue
            best_dose, _ = _best_dose(beta, entry, iv["dose_levels"])
            plan[iv["id"]] = best_dose if best_dose is not None else 0.0
            continue

        # Stage 1: on/off from the "does it help at all" effect alone (unbiased).
        if beta[entry["on"]] <= 0.0:
            plan[iv["id"]] = 0.0
            continue

        # Boolean interventions are a plain yes -- there's no dose to choose.
        if iv.get("kind") == "boolean":
            plan[iv["id"]] = 1.0
            continue

        # Stage 2: it's on -- choose the dose. Stay on the STANDARD dose most nights while
        # data is thin; the chance of deviating ramps with the warm-up (quadratic for
        # supplements -> <1-in-10 for the first ~20 on-nights, faster for time-based).
        # This gate covers BOTH exploiting a non-standard dose AND probing.
        std = _d(iv.get("default_dose") or 0)
        step = _dose_step(iv)
        timed = _time_based(iv)
        warm = min(1.0, n_on.get(iv["id"], 0) / WARMUP_ON_NIGHTS)
        p_deviate = warm if timed else warm * warm
        if step <= 0 or random.random() >= p_deviate:
            plan[iv["id"]] = std
            continue

        # Deviating tonight: reconstruct the measured dose-response curve.
        counts = {}                                      # dose -> times measured
        for x in measured.get(iv["id"], ()):
            xd = _d(x)
            if xd > 0:
                counts[xd] = counts.get(xd, 0) + 1
        seen = sorted(counts)
        effs = {d: _effect(post["mN"], entry, d) for d in seen}   # posterior-mean curve
        best = max(effs.values()) if effs else None

        # exploit target: minimum viable dose once there are >=2 measured doses
        center = std
        if len(seen) >= 2:
            mv = _min_viable_dose(effs)
            if mv is not None:
                center = mv

        if random.random() < PROBE_SHARE:                # explore: map the curve
            if timed:
                j = random.randint(1, TIME_MAX_JUMP) * random.choice((-1, 1))
                probe = _d(center + j * step)
                dose = probe if probe > 0 else _d(center + step)
            else:
                dose = _supp_probe(seen, counts, effs, best, std, step)
        else:
            dose = center                                # exploit the best dose so far
        plan[iv["id"]] = dose if dose and dose > 0 else 0.0
    return plan


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
            levels = sorted(set(_d(x) for x in iv["dose_levels"] if x and float(x) > 0))

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
    X, y = build_design(completed_nights, cols, p)
    V0 = prior_variances(p, cols)
    m0 = None
    if priors:
        m0 = [0.0] * p
        for iv in interventions:
            pm = priors.get(iv["id"])
            if pm:
                m0[cols[iv["id"]]["on"]] = float(pm)   # prior mean on the "on" effect
    post = fit(X, y, p, V0, m0)
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
