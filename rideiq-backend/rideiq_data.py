"""
rideiq_data.py — synthetic but realistic data generators for RideIQ.

No external datasets or APIs. Everything is generated with controlled correlations so the
models learn meaningful patterns (e.g. rain + rush hour -> higher surge -> more cancellations).
All generators are seeded for reproducibility.
"""
import numpy as np

RNG = np.random.RandomState(42)


def _rush(hour):
    """1.0 near the 8am and 6pm peaks, ~0 overnight."""
    return np.exp(-((hour - 8) ** 2) / 6) + np.exp(-((hour - 18) ** 2) / 6)


def generate_trips(n=6000, seed=42):
    """Core trip table with features + targets (duration, fare, cancelled, fraud)."""
    rng = np.random.RandomState(seed)
    distance = rng.uniform(0.5, 25.0, n)                     # km
    hour = rng.randint(0, 24, n)
    dow = rng.randint(0, 7, n)
    weather = (rng.rand(n) < 0.25).astype(int)              # 1 = rain
    rush = _rush(hour)

    traffic = np.clip(0.2 + 0.5 * rush + 0.2 * weather + rng.normal(0, 0.08, n), 0, 1)
    surge = np.clip(1.0 + 1.2 * rush + 0.6 * weather + rng.normal(0, 0.15, n), 1.0, 3.5)
    rider_rating = np.clip(rng.normal(4.6, 0.3, n), 3.0, 5.0)

    # duration: base 3 min/km, stretched by traffic and rain
    duration = distance * 3.0 * (1 + 0.8 * traffic + 0.15 * weather) + rng.normal(0, 2, n)
    duration = np.clip(duration, 2, None)

    # fare: base + per-km + per-min, scaled by surge
    fare = (2.5 + 1.1 * distance + 0.25 * duration) * surge + rng.normal(0, 1.5, n)
    fare = np.clip(fare, 3, None)

    # wait time before pickup grows with surge & traffic
    wait_time = np.clip(1.5 + 3.0 * (surge - 1) + 2.0 * traffic + rng.normal(0, 0.7, n), 0, None)

    # cancellation: long waits + high surge + lower rating -> more likely
    z = -3.2 + 0.5 * wait_time + 0.9 * (surge - 1) + 0.6 * weather - 0.4 * (rider_rating - 4.5) * 2
    p_cancel = 1 / (1 + np.exp(-z))
    cancelled = (rng.rand(n) < p_cancel).astype(int)

    # payment + fraud: ~8% of trips have an anomalous payment vs the trip profile
    payment = fare.copy()
    fraud = np.zeros(n, dtype=int)
    idx = rng.choice(n, size=int(0.08 * n), replace=False)
    payment[idx] = fare[idx] * rng.uniform(2.2, 6.0, len(idx))   # inflated charge
    fraud[idx] = 1

    return {
        "distance": distance, "hour": hour, "dow": dow, "weather": weather,
        "traffic": traffic, "surge": surge, "rider_rating": rider_rating,
        "duration": duration, "fare": fare, "wait_time": wait_time,
        "payment": payment, "cancelled": cancelled, "fraud": fraud,
    }


def generate_demand_points(n=800, seed=7):
    """Pickup lat/lon drawn from a few city hotspots — for clustering surge zones."""
    rng = np.random.RandomState(seed)
    centers = np.array([[0.30, 0.30], [0.70, 0.75], [0.50, 0.20],
                        [0.20, 0.80], [0.80, 0.35]])
    weights = np.array([0.30, 0.25, 0.20, 0.15, 0.10])
    counts = rng.multinomial(n, weights)
    pts = []
    for c, k in zip(centers, counts):
        pts.append(rng.normal(c, 0.06, size=(k, 2)))
    X = np.clip(np.vstack(pts), 0, 1)
    rng.shuffle(X)
    return X  # (n, 2) normalized city coordinates


def generate_riders(n=1200, seed=11):
    """Rider profiles with a latent segment label — for PCA + Fisher LDA.
    Segments: 0=commuter, 1=occasional, 2=power user."""
    rng = np.random.RandomState(seed)
    seg = rng.choice([0, 1, 2], size=n, p=[0.4, 0.4, 0.2])
    profiles = {
        0: dict(rides=(18, 4), dist=(9, 2), fare=(16, 3), rating=(4.7, 0.2), cancel=(0.05, 0.03)),
        1: dict(rides=(4, 2),  dist=(6, 3), fare=(12, 4), rating=(4.5, 0.3), cancel=(0.12, 0.05)),
        2: dict(rides=(30, 6), dist=(14, 4), fare=(28, 6), rating=(4.8, 0.15), cancel=(0.03, 0.02)),
    }
    cols = {k: np.zeros(n) for k in ["rides", "dist", "fare", "rating", "cancel"]}
    for i, s in enumerate(seg):
        p = profiles[s]
        for k in cols:
            m, sd = p[k]
            cols[k][i] = rng.normal(m, sd)
    X = np.column_stack([cols["rides"], cols["dist"], cols["fare"], cols["rating"], cols["cancel"]])
    return X, seg, ["rides/week", "avg_distance", "avg_fare", "rating", "cancel_rate"]


def generate_driver_shift(seed=5):
    """One driver's shift as a time series of earnings-per-5-min, driven by hidden states:
    0 = idle (parked), 1 = en-route to rider, 2 = on-trip (earning). For the HMM."""
    rng = np.random.RandomState(seed)
    means = {0: 0.3, 1: 1.5, 2: 4.5}          # $ per 5-min interval
    # a plausible shift: idle, enroute, trip, idle, enroute, trip, ...
    pattern = [0, 1, 2, 0, 1, 2, 2, 0, 1, 2, 0, 1, 2]
    lengths = [6, 2, 5, 3, 2, 7, 4, 4, 2, 6, 3, 2, 5]
    obs, states = [], []
    for s, L in zip(pattern, lengths):
        obs.append(rng.normal(means[s], 0.5, L))
        states += [s] * L
    return np.concatenate(obs).reshape(-1, 1), np.array(states)
