# Sleep Bandit

A single-user, self-hosted n-of-1 experiment runner for sleep interventions. Each
evening it decides, per intervention, whether tonight is an ON or OFF night and at
what dose or option; each morning it pulls HRV, resting heart rate and sleep metrics
from an Oura ring; and it fits a Bayesian model to tell you which interventions help,
at what dose, and how sure it is.

Pure Python standard library plus SQLite. No dependencies to install.

```
python3 server.py          # then open http://localhost:8777
```

## How a night works

1. **Plan.** When the app is opened (or on a 20-minute timer when hosted) it makes
   sure tonight has a cell for every active intervention. Each missing cell is filled
   by one Thompson-sampling draw from that intervention's own model. Existing cells
   are never re-rolled: what was prescribed is the historical record.
2. **Do it and tick it.** The Tracker grid shows tonight's column. Highlighted cells
   are ON; tap one when you have done it. A cell's completion input is a checkbox by
   default, or a row of buttons or a number if the intervention is set up that way.
3. **Morning.** Oura sync fills in last night's HRV, lowest resting HR, sleep
   efficiency, total sleep and latency. You can also type values by hand.
4. **Reports.** The Effects tab gives each intervention a verdict, its estimated
   effect on HRV and resting HR, a dose-response chart, and the sleep metrics. The
   Interactions tab fits pairs for synergy or interference.

A night belongs to its **evening's date** and rolls over at 4 AM, so a routine done
at 1 AM still files under the previous evening.

## Interventions

Each intervention has a **category** (supplement, behavioral, environmental) and a
**kind**:

| Kind | What the app decides each night |
|---|---|
| Dose | ON or OFF, and if ON, an amount (e.g. 200 mg, 15 minutes) |
| Yes / no | ON or OFF |
| Multiple choice | one of your options, always ON (optionally also testing "skip") |
| Multiple choice + dose | an option AND an amount; every option × dose pair is its own arm |

Dose settings: standard dose, unit dose (the step between doses), optional min and
max, and a "count as" noun so the tracker can say "2 pills" while the reports say
"400 mg". Two switches govern exploration:

- **Explore doses** off means only the standard dose is ever used.
- **Delay exploration** (on by default) holds the standard dose until enough ON
  nights have answered the on/off question, then ramps exploration up slowly.

**Options** on a choice intervention keep a stable id, so renaming keeps history.
Removing an option retires it: it is never planned again, but its past nights still
show under its name, and it can be restored from the "removed options" list in the
editor.

**Open-ended arm.** A dose intervention with a max can have one more arm above it
that is not a number, e.g. the spiky mat with no timer, staying on until you fall
asleep. It is recorded as a sentinel dose, gets its own label in the tracker and
reports, and is treated as a different condition rather than "one more step".

Per-intervention priors (a prior that it helps at all; a prior over which option is
best) steer early scheduling only and fade as nights accumulate. They never enter the
reported estimates.

## How the scheduler decides

The engine is in `bandit.py`: a conjugate Bayesian linear regression with Thompson
sampling, no numpy. Each intervention gets one "does it help at all" effect plus a
heavily shrunk per-dose deviation, so "does it work" is a single well-calibrated
parameter and "which dose" is a separate question.

**ON or OFF** is decided by one posterior draw: ON if the sampled "helps" effect is
positive. So P(ON) tracks P(it helps). An **early lock-in guard** caps that
probability at 90% until the smaller arm has 15 nights, 95% until 30, then lets go,
so a lucky first week cannot stop the contrast from being collected. The guard is
one-sided: there is no floor, so evidence that something hurts tapers it off right
away, even on a small sample.

**Which dose**, once ON, depends on the settings:

- **Slow climb** (the default). The standard dose most nights; the chance of
  deviating ramps with the warm-up. Deviating nights either exploit the minimum
  viable dose (the smallest within 95% of the best measured effect) or probe one
  unit dose at a time (supplements) or in bigger jumps (time-based).
- **Fast sweep** (delay off and both min and max set). Every ON night is a Thompson
  draw over the whole grid, smoothed across neighbouring rungs with an optimism bonus
  where data is thin. On grids of more than ten rungs it goes **coarse-first**:
  multiples of 5 unit doses (plus min, max and standard) are probed on 80% of nights,
  and a fine rung opens only once the coarse rungs bracketing it are both measured
  and one is firm. A fine rung's own old readings do not open it.
- **Open-ended arm** in a fast sweep is scored as an island from its own nights only,
  and is guaranteed a floor share of ON nights: one third with no readings, decaying
  as its own nights accumulate (half that after 5), on top of whatever the draw picks.

**Maximize benefit** is a per-night switch that overrides tonight's unticked cells
with the all-exploit plan (every intervention at its best measured dose, or off).
The randomized plan is parked and restored exactly if you switch it back off. Those
nights are not evidence unless the "count maximize nights" switch is on.

### Where each scheduling feature is documented

Every feature below is explained in a comment block or docstring at the named
place; the constants listed are the knobs. Names, not line numbers, so they stay
valid as the files change.

| Feature | Documented at | Knobs |
|---|---|---|
| The model (one "helps" effect + shrunk per-dose deviations, Normal-Inverse-Gamma posterior) | `bandit.py` module docstring; `fit_model` | `ON_PRIOR_VAR`, `DOSE_DEV_PRIOR_VAR`, `SIGMA2_PRIOR_A/B`, `MEANINGFUL_EFFECT` |
| Per-night planning: one Thompson draw, ON/OFF then dose | `plan_night` docstring; `server.ensure_tonight` | — |
| ON/OFF decision from the sampled "helps" effect | `_decide_on` | — |
| Early lock-in guard (tiered cap on P(ON), no floor); per-intervention opt-out | comment above `LOCKIN_TIERS`; `lockin_cap`, `clamp_p_on`, `p_on_estimate` | `LOCKIN_TIERS`, `LOCKIN_P_SAMPLES`, `no_lockin` |
| Cold-start warm-up: hold the standard dose, ramp exploration quadratically (linearly for time-based) | comment above `WARMUP_ON_NIGHTS` and above `PROBE_SHARE` | `WARMUP_ON_NIGHTS`, `delay_explore` |
| Slow climb: probe vs. exploit split, one-unit supplement probes, bigger time-based jumps | comment above `PROBE_SHARE`; `_supp_probe`; the slow-climb branch of `plan_night` | `PROBE_SHARE`, `TIME_MAX_JUMP`, `MIN_PROBES` |
| Minimum viable dose (exploit the smallest dose within 95% of the best) | comment above `MIN_VIABLE_FRAC`; `_min_viable_dose` | `MIN_VIABLE_FRAC` |
| Dose bounds and the explored grid | `_bounds`, `_dose_step`, `store.dose_levels_from` | `min_dose`, `max_dose`, `unit_dose` |
| Fast sweep: kernel-smoothed Thompson draw over the whole bounded grid | comment above `FAST_KERNEL_STEPS`; `_fast_grid`, `_fast_pick` | `FAST_KERNEL_STEPS`, `FAST_PRIOR_WEIGHT` |
| Coarse-first tree search on big grids; when a fine rung opens | comment above `COARSE_MIN_RUNGS`; `_coarse_rungs`, `_rung_open`, `_coarse_night` | `COARSE_MIN_RUNGS`, `COARSE_EVERY`, `COARSE_SHARE`, `MIN_PROBES` |
| Open-ended arm: sentinel dose, island scoring, decaying floor share (fast sweep), early tries (slow climb) | comment above `OPEN_ARM_DOSE`; `open_arm_floor`, `has_open_arm`, `_fast_pick` docstring | `OPEN_ARM_FLOOR`, `OPEN_ARM_FLOOR_HALF`, `OPEN_ARM_PROBE_SHARE` |
| Multiple choice: options as arms, optional "skip" control, retired options never planned | `candidate_levels`, `live_options`; `store._norm_choice_options`, `store.live_options` | `test_skip` |
| Multiple choice + dose: option × dose arms, per-option sweep | `_plan_combo` docstring; `combo_encode` | same as fast sweep |
| Priors: "helps" prior (capped at 80%) and per-option priors, scheduling only | `scheduling_priors`, `prior_mean_for_prob`, `effective_option_probs`, `option_prior_means`; `store._clamp_prior` | `prior_helps`, option `prior` |
| Maximize-benefit nights: all-exploit plan, parked randomized plan, restore | `plan_exploit` docstring; `server.maximize_tonight`; `store.override_assignment`, `store.restore_assignment` | header switch `count_maximize` |
| Assignment freeze: planned cells are never re-rolled; the sanctioned exceptions | `store.set_assignment` docstring; `store.force_assignment`, `store.delete_assignment` | — |
| Tonight's pending cell follows setting changes (new bounds / standard dose; a removed option) | `server.reconcile_tonight_bounds`, `server.reconcile_tonight_options` | — |
| Which nights count: compliance rule, per-cell and per-category exclusions | `server.intervention_nights` docstring | — |
| Recency weighting of evidence and effective counts | comment above `RECENCY_HALF_LIFE_DAYS`; `apply_recency` | `RECENCY_HALF_LIFE_DAYS` |
| Baseline-drift adjustment: each night's ±7-day local baseline as a covariate, in fits, averages and scheduling | comment above `BASE_PRIOR_VAR`; comment above `server.BASELINE_WINDOW_DAYS`; `server.attach_baselines` | `BASE_PRIOR_VAR`, `BASELINE_WINDOW_DAYS`, `BASELINE_MIN_NIGHTS`; header switch `baseline_adjust` |
| Night rollover at 4 AM | `server.today_str` | `NIGHT_ROLLOVER_HOUR` |
| Pairwise interactions (Reports) | comment above `INTERACTION_PRIOR_VAR`; `pair_interaction` | `INTERACTION_PRIOR_VAR`, `INTERACTION_SAMPLES` |
| Deterministic report read-outs (seeded Monte Carlo) | comment above `READOUT_SEED` | `READOUT_SAMPLES`, `READOUT_SEED` |

Unqualified names are in `bandit.py`; `server.` and `store.` prefixes name the other
two modules.

## What counts as evidence

- An ON night counts only if you ticked it. An unticked ON night is dropped, not
  treated as a control.
- An OFF night is always a control.
- A cell can be excluded individually (e.g. assigned OFF but you did it anyway), and
  a whole night can be excluded per category (e.g. "ignore tonight's supplements, I
  was ill").
- **Recency weighting** (header switch) weights every night by 0.5^(age / 60 days)
  in both the model and the effective counts, and disables the lock-in guard.
- **Adjust for baseline drift** (header switch) gives every night a local baseline,
  the average of the same metric over the 7 days before and after (the night itself
  excluded), and conditions every fit on it. An on/off comparison is only fair when
  the on- and off-nights come from similar weeks; because scheduling is adaptive, an
  intervention that locks in early collects its few off nights in a cluster, and a
  bad month at the start then reads as "it helped". With the switch on, the model can
  read that month as drift instead. Report averages become baseline-adjusted (same
  units, flagged), the interaction tables too, and the planner schedules from the
  same fit. Nights with fewer than 4 neighbours get no baseline and sit at the centre.

Reports show effects on HRV (higher is better) and lowest resting HR (lower is
better), each with a model 90% interval and a probability of a real benefit. A
choice intervention shows a horse race of its options. Where a choice's options pin a
behaviour (e.g. fasting vs. a full meal), an **observed** table groups nights by
what actually happened, flagged as correlational.

## Oura

Oura's API is OAuth2-only. Register a private app at cloud.ouraring.com with redirect
URI `http://localhost:8777/oura/callback`, paste its client id and secret into the
Oura panel, and connect. Tokens live in the settings table and refresh themselves.
A Sleep Bandit night d matches Oura day d+1 (the morning it ends), and only the
session Oura marks as the main sleep is used, never naps. Syncs look back 45 days
for blank nights and are throttled to one per 15 minutes.

## Running locally

`python3 server.py` starts it on port 8777 (`SB_PORT`, `SB_HOST` override). Opening
the app plans tonight and syncs Oura; nothing runs in the background.

On this Mac a LaunchAgent (`com.elityre.sleep-bandit`) runs `server.py` from
`~/sleep-bandit` and keeps it alive, with logs in `~/Library/Logs/sleep-bandit.log`.
If you start a second copy by hand while the agent's copy holds the port, the agent's
copy exits with "address already in use" until the port is free. A second agent
(`com.elityre.sleep-bandit-backup`) runs `backup.sh` daily.

`python3 seed_demo.py` fills an empty database with simulated history whose ground
truth is known, so you can see the reports work before you have real data.

## Data and backups

Everything is in `data/sleep_bandit.db`. Tables: `interventions`, `nights`,
`assignments` (one row per intervention per night: assigned dose, taken dose,
exclusion, completion input, parked randomized dose), `settings`, and an
append-only `audit_log` recording every write with a millisecond timestamp.

`backup.sh` takes a WAL-safe snapshot with `sqlite3 .backup` into `backups/` and
keeps the newest 30. The database is committed to git alongside the code in this
repo; `backups/` is not.

## Hosting

`deploy/README.md` covers running it on a VPS behind Caddy at a public domain: a
read-only public view at `/`, an unlinked password-protected admin path with the
full app, background planning and Oura sync on a 20-minute timer, and daily backups.
Hosted mode is off by default; locally everything is read/write with no login.

Relevant environment variables: `SB_PUBLIC`, `SB_ADMIN_PATH`, `SB_PASSWORD_HASH`
(from `python3 auth.py`), `SB_BASE_URL`, `SB_AUTOSYNC`, `SB_DB_PATH`.

## Files

| File | Role |
|---|---|
| `server.py` | HTTP server, planning, analysis payloads, hosted mode |
| `bandit.py` | the model, Thompson sampling, dose exploration, interactions |
| `store.py` | SQLite schema, migrations, every write (audited) |
| `oura.py` | OAuth and sync |
| `auth.py` | password hashing and session cookies for hosted mode |
| `public/` | the single-page UI: `index.html`, `app.js`, `styles.css` |
| `deploy/` | VPS setup: systemd units, Caddyfile, backup timer |

The design rationale for every planning constant is in the comment block above it in
`bandit.py`; the table under "How the scheduler decides" maps each feature to it.
