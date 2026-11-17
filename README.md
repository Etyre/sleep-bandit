# Sleep Bandit

A single-user, self-hosted n-of-1 experiment runner for sleep interventions. Each
evening it decides, per intervention, whether tonight is an ON or OFF night and at
what dose or option; each morning it pulls HRV, resting heart rate and sleep metrics
from an Oura ring; and it fits a Bayesian model to tell you which interventions help,
at what dose, and how sure it is. Built from 2026-07-07 (first commit) because
supplements, routines and room settings were being tried one at a time with no
control nights and no record; this randomizes them properly and keeps the record.
Pure Python standard library plus SQLite, no dependencies to install; the UI is
plain HTML/JS in `public/`.

```
python3 server.py          # then open http://localhost:8777
python3 server.py --test   # end-to-end dry run on a throwaway database
```

## How it works

### How a night works

1. **Plan.** When the app is opened (or on a 20-minute timer when hosted) it makes
   sure tonight has a cell for every active intervention. Each missing cell is filled
   by one Thompson-sampling draw from that intervention's own model. Existing cells
   are never re-rolled: what was prescribed is the historical record.
2. **Do it and tick it.** The Tracker grid shows tonight's column. Highlighted cells
   are ON; tap one when you have done it. A cell's completion input is a checkbox by
   default, or a row of buttons or a number if the intervention is set up that way.
3. **Morning.** Oura sync fills in last night's HRV, lowest resting HR, sleep
   efficiency, total sleep and latency. These come from Oura only; the cells are
   read-only.
4. **Reports.** The Effects tab gives each intervention a verdict, its estimated
   effect on HRV and resting HR, a dose-response chart, and the sleep metrics. The
   Interactions tab fits pairs for synergy or interference.

A night belongs to its **evening's date** and rolls over at 4 AM, so a routine done
at 1 AM still files under the previous evening.

The **Tonight** view (what the phone opens on when the site is installed as a
home-screen app) is tonight's column as a checklist, one card per intervention.

### Interventions

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

### How the scheduler decides

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
  unit dose at a time (supplements) or in bigger jumps (time-based). A supplement
  probe steps past an edge of the measured doses, or into a never-tried rung between
  two measured doses while a neighbour is viable.
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

#### Where each scheduling feature is documented

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
two modules. The design rationale for every planning constant is in the comment
block above it in `bandit.py`.

### What counts as evidence

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

### Oura

Oura's API is OAuth2-only. Register a private app at cloud.ouraring.com with redirect
URI `http://localhost:8777/oura/callback`, paste its client id and secret into the
Oura panel, and connect. Tokens live in the settings table and refresh themselves.
A Sleep Bandit night d matches Oura day d+1 (the morning it ends), and only the
session Oura marks as the main sleep is used, never naps. Syncs look back 45 days
for blank nights and are throttled to one per 15 minutes. Every synced night also
keeps Oura's **raw within-night series** (heart rate and HRV every five minutes, the
five-minute hypnogram, 30-second movement, and the summary numbers) in
`night_series`, for the night-shape tagger below.

Two read-only endpoints serve other local tools (the executive-assistant agent):
`/api/agent/summary?days=30` (recent nights with metrics, cells, tags, tonight's
checklist and each intervention's verdict, with no planning side effect) and
`/api/oura/collection?name=daily_readiness&days=14` (a passthrough to any Oura v2
collection in `oura.READ_COLLECTIONS`, using this app's token so there is a single
owner of the rotating refresh token).

### Night-shape tags

Nights differ in shape, not only in their HRV number. For every recorded night,
Claude reads the raw series next to a per-feature summary of your typical nights
(p10 / median / p90 over every recorded night at or above the low-night line,
default 42, set in the **🏷 Night tags** panel) and assigns zero to four
**shape tags** -- none when the night looks typical. (Until 2026-11-16 only nights
below that line were read; those earlier reads are kept.) It works from a shared
vocabulary (`tag_vocab`) and adds a tag when
none fits; the vocabulary was seeded from a read of the first 713 recorded nights,
where low nights differed from typical ones mainly in heart rate that starts high
and settles late and in a missing early-night HRV rise, with restlessness, late
onset, long wake bouts and short nights as rarer kinds (`tags.SEED_TAGS` lists them
with the measured cut-offs). Tags only describe what the series shows, never
causes.

**Valence.** Whether a tag is good or bad news is read off the data rather than its
name (`tags.valences`): over the nights that have been read, mean HRV of the
nights carrying the tag minus mean HRV of those that don't, Welch's t-test. A tag
is **positive** or **negative** when p < 0.05 with at least five nights on each
side, otherwise **neutral** (no clear gap, or too few nights). Chips are green,
red or grey accordingly, and the panel's vocabulary list gives the numbers. It is
a raw comparison: no correction for the number of tags, the HRV trend, or which
interventions were on. While the older good nights are still being read, the
comparison leans on low nights and will shift as the backlog clears.

Tags appear on the Tracker in a **Night shape** row between resting HR and total
sleep (hover for the tagger's one-line reading). On the Reports page each
intervention is tested against every tag: the share of its confirmed-on nights
carrying the tag versus its assigned-off nights, same nights as its HRV read-out,
two-sided Fisher exact test on raw counts. Anything at p < 0.05 with at least three
tagged nights is flagged, in either direction, and coloured by the tag's valence
(more of a negative tag is bad, more of a positive one good). Only nights that
have been read count. A tag that goes with low HRV follows any intervention that
lowers HRV outright; the flag's tooltip therefore also gives the split among low
nights alone.

Setup: paste a Claude API key into the panel (stored in the settings table, or read
from `ANTHROPIC_API_KEY`), tick the switch. Pending nights are then read newest
first, in the background, a few per Oura sync (`tags.PER_PASS`) or all at once from
the panel. The model is `claude-opus-5-5` at medium effort with the system prompt
cached; one night is roughly 1.6k cached + 1.8k fresh input tokens, a few cents.
Every read is audited (`tag_night`, `tag_usage`); a new vocabulary entry is audited
as `tag_vocab_add`. The call goes over plain HTTPS from `tags.py` -- no SDK, since
the app stays standard-library only on the system Python.

**Shared LLM standard** (`~/Documents/agent-coding/CLAUDE.md`, `shared/README.md`):
the system prompt starts, word for word, with `shared/llm-system-prompt-preamble.md`;
every call offers the `contact_eli` tool (alongside the JSON-schema output format,
which the API accepts together); and every response is checked for a `contact_eli`
call or an `@ELI:` line. A contact is recorded in the LLM inbox
(`~/Library/Application Support/LLMInbox/messages.jsonl`, read with `llm-inbox`),
the tagger stops, and the settings key `tags_hold` keeps it stopped, so the
20-minute autosync cannot re-send the same night. The panel shows "Paused: …";
saving the panel lifts the hold. `tags.py` imports `shared/llm_contact.py` when it
can and otherwise reads the preamble and tool files directly and pipes the check to
`llm_contact.py` under a newer interpreter (see Gotchas: the LaunchAgent's Python
is 3.9).

### Setting vs. outdoor temperature

A temperature-dosed intervention (unit contains `°`: Chili Pad, air conditioning)
gets a section at the foot of its report asking whether its best setting depends on
the season. Each night has a **city** (dropdown under the date on the Tracker; the
list starts as Berkeley, Washington DC, Phoenix and New York City, and "Other…" adds
a place through Open-Meteo's geocoder). A night with no choice of its own inherits
the previous night's, so a trip only needs marking on its first and last nights.
`weather.py` keeps each night's outdoor temperature at that city from Open-Meteo:
the mean and minimum over the hours actually asleep (Oura's bedtime start to bedtime
end, padded half an hour each side; 9 pm–6 am until a night's sleep record arrives,
then redone), and the day's maximum, fetched with each sync and again whenever a
city changes. The section plots every
counted night as outdoor night temperature against HRV, colored by setting, bins the
nights into outdoor-temperature terciles with each setting's average per bin (★ the
best with at least two nights), and fits HRV ~ setting × outdoor temperature on the
on-nights, reporting the interaction with a 90% interval. Same nights and the same
baseline adjustment as the read-outs. `server.season_payload`.

### Drafts are never lost

Every text field in the UI saves what you type to `localStorage` on each keystroke
(`sb.draft.<field>.<record>`: per-night notes and metric values by date, quantity
cells by date and intervention, the whole intervention editor by intervention id or
"new", the Oura client id, the tag threshold) and restores it before the field is
shown, so a grid re-render, a tab switch or a reload brings the text back. A draft
is cleared only when its write succeeds; in the editor, **Cancel** or **Delete**
discards it while ✕ and clicking outside keep it (the title then reads "unsaved
draft restored"). Password fields (Claude key, Oura secret, admin login) are never
saved. Code: `draftGet/draftSet/draftClear/withDraft` in `app.js`.

### Hosted mode

`deploy/README.md` covers running it on a VPS behind Caddy at a public domain: a
read-only public view at `/`, an unlinked password-protected admin path with the
full app, background planning and Oura sync on a 20-minute timer, and daily backups.
Hosted mode is off by default; locally everything is read/write with no login.

Relevant environment variables: `SB_PUBLIC`, `SB_ADMIN_PATH`, `SB_PASSWORD_HASH`
(from `python3 auth.py`), `SB_BASE_URL`, `SB_AUTOSYNC`, `SB_DB_PATH`.

### Data and backups

Everything is in `data/sleep_bandit.db`. Tables: `interventions`, `nights`,
`assignments` (one row per intervention per night: assigned dose, taken dose,
exclusion, completion input, parked randomized dose), `settings` (Oura credentials
and tokens, the Claude key, switches, sync bookkeeping), `night_series`, `tag_vocab`,
night tags, cities and weather, and an append-only `audit_log` recording every write
with a millisecond timestamp.

Each planned cell's audit row (`assign`; `maximize_assign` for a maximize-benefit
override) also records its **planning provenance**: the code version (git commit,
`+dirty` if the source was modified), the analysis switches in force (recency,
count maximize, baseline), the scheduling probability before and after the lock-in
cap, the cap and effective on/off counts, the prior used, and the fitted baseline
coefficient. So every night says which scheduler, in which mode, on what evidence,
prescribed it (`server._plan_context`). Rows before 2026-10-22 carry the dose only.

`backup.sh` takes a WAL-safe snapshot with `sqlite3 .backup` into `backups/` and
keeps the newest 30. The database was committed to git alongside the code until
2026-11-07; it is now untracked (`data/` is gitignored) because it holds the Oura
tokens, the Oura client secret and the Claude key as well as personal data. Commits
before that date still carry old copies with Oura tokens; the GitHub remote never
received the file. `backups/` is not in git either. Once the site is hosted,
`pull.sh` mirrors the server's DB back to this Mac every 6 hours (see
`deploy/README.md` §6).

## Files

| File | Role |
|---|---|
| `server.py` | HTTP server, planning, analysis payloads, hosted mode, `--test` self-test, optional `config.env` loading |
| `bandit.py` | the model, Thompson sampling, dose exploration, interactions |
| `store.py` | SQLite schema, migrations, every write (audited) |
| `oura.py` | OAuth and sync, including the raw within-night series; read-only collection passthrough |
| `tags.py` | night-shape tags: features, reference, the Claude call (shared preamble, `contact_eli`, inbox check, hold), background tagger |
| `weather.py` | outdoor temperature per night (Open-Meteo) for the season sections |
| `auth.py` | password hashing and session cookies for hosted mode; `python3 auth.py` prints `SB_PASSWORD_HASH` |
| `applog.py` | timestamped, flushed log lines (`log`, `log_exc`) for everything that runs under launchd |
| `icons.py` | generates the home-screen icons in `public/` |
| `seed_demo.py` | fills an empty database with simulated history whose ground truth is known |
| `backup.sh` | daily WAL-safe snapshot into `backups/`, newest 30 kept |
| `pull.sh` | Mac-side mirror of the hosted DB over ssh (needs `deploy/server.conf`) |
| `install.sh` | copies the LaunchAgent plists from `deploy/mac/` and reloads them by label (idempotent) |
| `public/` | the single-page UI: `index.html`, `app.js`, `styles.css`, `sw.js` (service worker, network-first), icons |
| `deploy/mac/` | this Mac's LaunchAgents (`com.elityre.sleep-bandit`, `-backup`, `-pull`) and `config.env.example` |
| `deploy/` | VPS setup: `push.sh`, `setup.sh`, systemd units, Caddyfile, backup timer, `README.md` |
| `data/` | `sleep_bandit.db` (gitignored) |
| `backups/` | snapshots from `backup.sh` and `pull.sh` (gitignored) |

## Runtime locations

- Installed copy and working directory: `~/sleep-bandit`; `~/Documents/agent-coding/sleep-bandit`
  is a symlink to it, so the LaunchAgent runs this very checkout (edits to `public/`
  are live on the next page load; Python changes need a restart).
- Database: `~/sleep-bandit/data/sleep_bandit.db` (`SB_DB_PATH` overrides). Credentials
  live in its `settings` table, not in files.
- Config (optional): `~/Library/Application Support/Sleep Bandit/config.env`, KEY=VALUE
  overrides read at startup (`SB_PORT`, `SB_HOST`, `SB_DB_PATH`, `ANTHROPIC_API_KEY`);
  template in `deploy/mac/config.env.example`. Real environment variables win.
- Log: `~/Library/Logs/sleep-bandit.log` (every line timestamped by `applog.py`).
  The older plist split stdout and stderr: `sleep-bandit.err.log` holds the
  pre-2026-11-07 tracebacks and can be deleted.
- Backup and pull logs: `~/Library/Logs/sleep-bandit-backup.log`, `sleep-bandit-pull.log`
  (the currently loaded backup agent still writes `backups/backup.log` until reinstalled).
- LaunchAgents: `~/Library/LaunchAgents/com.elityre.sleep-bandit.plist` (server,
  `RunAtLoad` + `KeepAlive`), `com.elityre.sleep-bandit-backup.plist` (daily 15:00),
  `com.elityre.sleep-bandit-pull.plist` (every 6 h, hosted setup only). Sources in `deploy/mac/`.
- Backups: `~/sleep-bandit/backups/`.
- LLM inbox (contacts from the tagger): `~/Library/Application Support/LLMInbox/messages.jsonl`.
- Hosted copy: `/opt/sleep-bandit` on the VPS, env in `/etc/sleep-bandit.env`; see `deploy/README.md`.

## Common tasks

```sh
cd ~/sleep-bandit
python3 server.py                      # run by hand on :8777 (SB_PORT, SB_HOST override)
python3 server.py --test               # self-test: seeds a temp DB, serves it, checks state/reports/
                                       # interactions/tags payloads and the Claude prompt preamble
SB_PORT=8811 SB_DB_PATH=/tmp/x.db python3 server.py   # a second copy for development (see Gotchas)
python3 seed_demo.py                   # demo history into the configured DB (--wipe to start fresh)

./install.sh                           # copy plists from deploy/mac/ and reload the agents (restarts the server)
./install.sh --no-restart              # copy the plists only
launchctl kickstart -k gui/$UID/com.elityre.sleep-bandit      # restart the server after a code change
launchctl bootout gui/$UID/com.elityre.sleep-bandit           # stop it (e.g. when the hosted site takes over)
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.elityre.sleep-bandit.plist   # start it again
launchctl print gui/$UID/com.elityre.sleep-bandit | grep -E 'state|pid'
lsof -nP -iTCP:8777 -sTCP:LISTEN       # who holds the port (kill by that PID, never pkill by name)
tail -f ~/Library/Logs/sleep-bandit.log

./backup.sh                            # snapshot now
./pull.sh                              # mirror the hosted DB down (needs deploy/server.conf)
open http://localhost:8777/api/export  # full JSON export
python3 auth.py                        # hosted mode: prompts for the admin password, prints SB_PASSWORD_HASH=
deploy/push.sh SERVER_IP               # deploy / update the hosted copy
llm-inbox list                         # messages the tagging model sent to Eli
```

## Settings that matter

- **Oura developer portal** (cloud.ouraring.com → Developer → OAuth applications): the
  private app's redirect URI must be `http://localhost:8777/oura/callback` locally, or
  `https://<site>/oura/callback` when hosted. Changing it means **Oura → Save & reconnect**.
- **Claude API key**: pasted in the 🏷 Night tags panel (settings table) or `ANTHROPIC_API_KEY`
  in the environment / `config.env`. Nothing is sent to Anthropic unless tagging is on.
- **Login**: the LaunchAgent starts at login and is kept alive; no Login Item is needed.
- **nightblock** closes `api.anthropic.com` from midnight to 6 am on this Mac, so a tagging
  pass that runs in that window fails with "Could not reach the Claude API" and is retried
  on the next Oura sync.
- **Hosted**: DNS A records, `/etc/sleep-bandit.env`, Caddy; all in `deploy/README.md`.

## Gotchas learned

- The LaunchAgent runs `/opt/homebrew/bin/python3`, not `/usr/bin/python3`: macOS denies the system Python access to `~/Documents` when launchd starts it, so the shared LLM preamble, tool and inbox check under `agent-coding/shared` could not be read (seen 2026-11-07 as `ModuleNotFoundError: llm_contact` in the log). Homebrew's Python has that access.

- If you start a second copy by hand while the agent's copy holds the port, the agent's
  copy exits with "address already in use" until the port is free (KeepAlive restarts it
  every 10 s). Use another port and `SB_DB_PATH` for development.
- **Never point a second running copy at a database that holds live Oura tokens** (a
  plain copy of `data/sleep_bandit.db` included): Oura rotates the refresh token on
  every refresh, so the copy that refreshes first invalidates the other's token. Strip
  the `oura_*` rows from a development copy, or use `seed_demo.py`.
- `/usr/bin/python3` on this Mac is Xcode's **Python 3.9**, and that is what the
  LaunchAgent runs. Code here must stay 3.9-compatible (no `X | None` annotations, no
  `match`). `shared/llm_contact.py` is not (it fails to import on 3.9), which is why
  `tags.py` falls back to reading the preamble/tool files and piping the inbox check to
  `/opt/homebrew/bin/python3`. Fix at the source when convenient: add
  `from __future__ import annotations` to `shared/llm_contact.py`, or move the plist to
  `/opt/homebrew/bin/python3`.
- Under launchd, `print()` to a file is block-buffered: `sleep-bandit.log` stayed empty
  for four months while 27 MB of untimestamped tracebacks went to `.err.log`. `applog.py`
  writes timestamped lines to stderr and flushes; the plist sets `PYTHONUNBUFFERED=1`
  and sends both streams to one file.
- Restart by PID or `launchctl kickstart -k` / `bootout`, never `pkill`: a pkill that
  matches nothing exits quietly and the old code keeps running.
- `ConnectionResetError: [Errno 54]` tracebacks in the log are a browser closing a
  connection mid-response (tab closed, reload); harmless.
- The DB was tracked in git until 2026-11-07 (the `.gitignore` even said "the .db
  itself is kept"), so Oura tokens were in every commit. `git rm --cached` fixed the
  future; history was not rewritten. Rotate the Oura client secret if the repo is ever
  pushed somewhere public.
- The hosted and Mac copies must not both plan: stop the Mac agent while the site is
  live, or the two planners diverge and `pull.sh` cannot refresh the standby DB (it
  skips while a local server holds :8777).
- Structured output plus a tool: the tagger asks for a JSON schema **and**
  offers `contact_eli`; the Messages API accepts both at once (verified 2026-11-07).
  If the model calls the tool there is no text block, so the inbox check runs before
  the "returned no text" error.
- A night belongs to the evening's date (4 AM rollover) but Oura's day is the morning:
  night d = Oura day d+1. Naps are never used.
