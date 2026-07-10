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
DOSE_ROUND = 6             # decimals used to key doses (avoids float-equality bugs)

# Cold-start dose exploration. Until an intervention has this many compliant
# "on" nights, we lean on its standard dose instead of exploring alternatives.
# The chance of exploring an off-standard dose on an "on" night ramps up
# *quadratically* -- p_explore = (n_on / WARMUP_ON_NIGHTS)**2 -- so it stays low
# for a long time and only accelerates once there's real evidence. With
# WARMUP_ON_NIGHTS = 64 the nightly chance of varying the dose is under 1-in-10
# for the first 20 on-nights (~0.1% at 5 nights, ~2% at 10, ~9.8% at 20), then
# climbs to full exploration by 64 on-nights. The on/off decision is untouched;
# this only governs which dose we use once an intervention is on.
WARMUP_ON_NIGHTS = 64


def _d(x):
    return round(float(x), DOSE_ROUND)


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
def fit(X, y, p, V0_diag):
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
    mN = spd_solve(L, XtY)  # prior mean is 0
    yty = sum(yi * yi for yi in y)
    mAm = sum(XtY[i] * mN[i] for i in range(p))
    aN = SIGMA2_PRIOR_A + n / 2.0
    bN = SIGMA2_PRIOR_B + 0.5 * (yty - mAm)
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
def plan_night(post, cols, active_interventions, n_on=None):
    """Draw ONE posterior sample (Thompson sampling); per intervention pick the
    arm with the highest sampled effect.

    Dose interventions include "off" (effect 0) as an option, so a dose that looks
    unhelpful gets skipped. Multiple-choice interventions must always land on one
    option, so "off" is NOT an option — we take the best option even if all look
    weakly negative (their effects are relative to each other anyway).

    n_on: optional {iv_id: compliant "on"-night count}. While an intervention is
    below WARMUP_ON_NIGHTS we're in cold start: whenever the sampler decides to
    take it, we snap the dose to its standard dose with probability p_default =
    1 - (n_on / WARMUP_ON_NIGHTS)**2. The quadratic ramp keeps the off-standard
    chance under 1-in-10 for the first 20 on-nights, then accelerates toward full
    exploration by WARMUP_ON_NIGHTS. The on/off decision itself is untouched —
    only which dose we use once it's on."""
    beta, _ = sample_beta(post)
    n_on = n_on or {}
    plan = {}
    for iv in active_interventions:
        entry = cols.get(iv["id"])
        is_choice = iv.get("kind") == "choice"
        best_dose = None if is_choice else 0.0   # choice must pick; dose may go off
        best_val = None if is_choice else 0.0
        if entry:
            for x in iv["dose_levels"]:
                dose = _d(x)
                if dose <= 0:
                    continue
                v = _effect(beta, entry, dose)
                if best_val is None or v > best_val:
                    best_val, best_dose = v, dose

        # Cold start: once we've decided to take a dose intervention, prefer its
        # standard dose until it has enough on-nights to earn dose exploration.
        if not is_choice and best_dose:
            std = _d(iv.get("default_dose") or 0)
            levels = {_d(x) for x in iv["dose_levels"] if x and float(x) > 0}
            if std in levels:
                warm = min(1.0, n_on.get(iv["id"], 0) / WARMUP_ON_NIGHTS)
                p_default = 1.0 - warm * warm     # quadratic ramp; see WARMUP_ON_NIGHTS
                if p_default > 0.0 and random.random() < p_default:
                    best_dose = std

        plan[iv["id"]] = best_dose if best_dose is not None else 0.0
    return plan


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def readouts(interventions, betas, cols):
    """
    Per intervention:
      p_helps      = P(alpha_i > 0)                 -- any real benefit
      p_meaningful = P(alpha_i > MEANINGFUL_EFFECT) -- benefit worth acting on
      on_effect    = posterior mean/CI of alpha_i
      per dose     = mean/CI of (alpha+delta) and P(this dose is the best choice)
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

        # Recommend the dose with the best *mean* effect, not the highest P(best):
        # P(best) rewards a wide posterior, so a barely-tried dose could win on
        # uncertainty alone. Fall back to "off" (0) if no dose beats skipping on
        # average. recommended_p is reported as that dose's P(best) for display.
        rec_dose, rec_mean = 0.0, 0.0
        for row in dose_rows:
            if row["mean_effect"] > rec_mean:
                rec_mean, rec_dose = row["mean_effect"], row["dose"]
        rec_p = best_counts[rec_dose] / S

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


def fit_model(interventions, completed_nights):
    """Fit the posterior only (no read-out sampling) -- enough for planning."""
    history = {}
    for nt in completed_nights:
        for iid, dose in nt.get("taken", {}).items():
            if dose and float(dose) > 0:
                history.setdefault(int(iid), set()).add(_d(dose))

    cols, p = build_columns(interventions, history)
    X, y = build_design(completed_nights, cols, p)
    V0 = prior_variances(p, cols)
    post = fit(X, y, p, V0)
    return {"post": post, "cols": cols, "n": post["n"]}


def analyze(interventions, completed_nights):
    """Fit + the Monte-Carlo read-outs used by the Reports tab."""
    m = fit_model(interventions, completed_nights)
    betas = [sample_beta(m["post"])[0] for _ in range(READOUT_SAMPLES)]
    reads = readouts(interventions, betas, m["cols"])
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
