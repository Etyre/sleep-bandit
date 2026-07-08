"""
seed_demo.py -- optional: fill the database with simulated history so you can
see Sleep Bandit in action before you have real data.

    python3 seed_demo.py         # add demo data
    python3 seed_demo.py --wipe  # start fresh, then add demo data

Ground truth baked into the simulation:
  - Magnesium: 200mg gives +7ms HRV, 300mg +8ms, 100mg +2ms (mild dose-response)
  - "Late screens off": a real +4ms behaviour (on/off)
  - Vitamin C: a genuine placebo, 0 effect at every dose
The app should discover this from the noisy data alone.
"""

import random
import sys
from datetime import date, timedelta

import store

TRUTH = {
    "Magnesium": lambda dose: {100: 2.0, 200: 7.0, 300: 8.0}.get(dose, 0.0),
    "Late screens off": lambda dose: 4.0 if dose > 0 else 0.0,
    "Vitamin C": lambda dose: 0.0,  # placebo
}
BASELINE = 58.0
NOISE_SD = 6.0
N_NIGHTS = 90
SEED = 61  # a representative run (the placebo correctly reads as "no effect")


def main():
    conn = store.connect()
    if "--wipe" in sys.argv:
        for t in ("assignments", "nights", "interventions"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()

    ivs = {}
    ivs["Magnesium"] = store.create_intervention(conn, "Magnesium", "mg", 200)
    ivs["Late screens off"] = store.create_intervention(conn, "Late screens off", "", 1)
    ivs["Vitamin C"] = store.create_intervention(conn, "Vitamin C", "mg", 500)

    random.seed(SEED)
    start = date.today() - timedelta(days=N_NIGHTS)
    for i in range(N_NIGHTS):
        d = (start + timedelta(days=i)).isoformat()
        store.ensure_night(conn, d)
        hrv = BASELINE + random.gauss(0, NOISE_SD)
        taken = {}
        for name, iv in ivs.items():
            # simulate the randomized assignment (roughly uniform over off+doses)
            choices = [0.0] + [float(x) for x in iv["dose_levels"]]
            dose = random.choice(choices)
            store.set_assignment(conn, d, iv["id"], dose)
            taken[iv["id"]] = dose
            hrv += TRUTH[name](dose)
        store.record_night(conn, d, round(hrv, 1), taken)

    print(f"Seeded {N_NIGHTS} nights across {len(ivs)} interventions.")
    print("Open http://localhost:8777 to explore.")


if __name__ == "__main__":
    main()
