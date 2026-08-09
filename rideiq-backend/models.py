"""
models.py — RideIQ's ML brain. Trains all 15 algorithms once at import and exposes
one function per product feature. Every algorithm powers a real part of the product:

  ETA prediction        : Linear Regression, Random Forest, Neural Network
  Fare estimation       : Random Forest, KNN (comparable trips)
  Cancellation risk     : Logistic Regression, Decision Tree, AdaBoost
  Payment-fraud check   : SVM, Naive Bayes
  Surge / demand zones  : K-Means, Gaussian Mixture (EM), Hierarchical
  Rider segmentation    : PCA, Fisher Discriminant (LDA)
  Driver shift analysis : Hidden Markov Model
  Cancellation causes   : Bayesian Network
"""
import io
import base64

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.ensemble import RandomForestRegressor, AdaBoostClassifier
from sklearn.neural_network import MLPRegressor
from sklearn.neighbors import KNeighborsRegressor
from sklearn.tree import DecisionTreeClassifier
from sklearn.svm import SVC
from sklearn.naive_bayes import GaussianNB
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans, AgglomerativeClustering
from sklearn.mixture import GaussianMixture
from sklearn.metrics import r2_score, accuracy_score, silhouette_score
from scipy.cluster.hierarchy import dendrogram, linkage

import rideiq_data as data

RNG = 42


def _b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ════════════════════════ train everything once ════════════════════════
_T = data.generate_trips()

# ---- ETA (regression) : distance, traffic, weather, hour -> duration ----
_X_eta = np.column_stack([_T["distance"], _T["traffic"], _T["weather"], _T["hour"]])
_y_eta = _T["duration"]
_eta_scaler = StandardScaler().fit(_X_eta)
_eta_linear = LinearRegression().fit(_X_eta, _y_eta)
_eta_rf = RandomForestRegressor(n_estimators=120, random_state=RNG).fit(_X_eta, _y_eta)
_eta_nn = MLPRegressor(hidden_layer_sizes=(64, 32), max_iter=600, random_state=RNG)\
    .fit(_eta_scaler.transform(_X_eta), _y_eta)
_ETA_SCORE = {
    "linear_r2": round(float(r2_score(_y_eta, _eta_linear.predict(_X_eta))), 3),
    "random_forest_r2": round(float(r2_score(_y_eta, _eta_rf.predict(_X_eta))), 3),
    "neural_net_r2": round(float(r2_score(_y_eta, _eta_nn.predict(_eta_scaler.transform(_X_eta)))), 3),
}

# ---- Fare : distance, duration, surge -> fare ----
_X_fare = np.column_stack([_T["distance"], _T["duration"], _T["surge"]])
_y_fare = _T["fare"]
_fare_rf = RandomForestRegressor(n_estimators=120, random_state=RNG).fit(_X_fare, _y_fare)
_fare_knn = KNeighborsRegressor(n_neighbors=7).fit(_X_fare, _y_fare)

# ---- Cancellation : wait_time, surge, traffic, rider_rating -> cancelled ----
_X_c = np.column_stack([_T["wait_time"], _T["surge"], _T["traffic"], _T["rider_rating"]])
_y_c = _T["cancelled"]
_c_scaler = StandardScaler().fit(_X_c)
_c_logistic = LogisticRegression(max_iter=1000).fit(_c_scaler.transform(_X_c), _y_c)
_c_tree = DecisionTreeClassifier(max_depth=5, random_state=RNG).fit(_X_c, _y_c)
_c_ada = AdaBoostClassifier(n_estimators=120, random_state=RNG).fit(_X_c, _y_c)

# ---- Fraud : payment, distance, duration, surge -> fraud ----
_X_f = np.column_stack([_T["payment"], _T["distance"], _T["duration"], _T["surge"]])
_y_f = _T["fraud"]
_f_scaler = StandardScaler().fit(_X_f)
_f_svm = SVC(probability=True, random_state=RNG).fit(_f_scaler.transform(_X_f), _y_f)
_f_nb = GaussianNB().fit(_X_f, _y_f)


# ════════════════════════ per-feature predictions ════════════════════════
def predict_eta(distance, hour, weather, traffic):
    x = np.array([[distance, traffic, weather, hour]], dtype=float)
    lin = float(_eta_linear.predict(x)[0])
    rf = float(_eta_rf.predict(x)[0])
    nn = float(_eta_nn.predict(_eta_scaler.transform(x))[0])
    ens = (lin + rf + nn) / 3
    return {"linear_regression": round(lin, 1), "random_forest": round(rf, 1),
            "neural_network": round(nn, 1), "ensemble_min": round(ens, 1),
            "unit": "minutes", "model_r2": _ETA_SCORE}


def estimate_fare(distance, duration, surge):
    x = np.array([[distance, duration, surge]], dtype=float)
    rf = float(_fare_rf.predict(x)[0])
    knn = float(_fare_knn.predict(x)[0])
    _, idx = _fare_knn.kneighbors(x)
    comps = [round(float(_y_fare[i]), 2) for i in idx[0]]
    return {"random_forest": round(rf, 2), "knn_comparable": round(knn, 2),
            "comparable_trip_fares": comps, "currency": "USD"}


def cancellation_risk(wait_time, surge, traffic, rider_rating):
    x = np.array([[wait_time, surge, traffic, rider_rating]], dtype=float)
    log = float(_c_logistic.predict_proba(_c_scaler.transform(x))[0][1])
    tree = float(_c_tree.predict_proba(x)[0][1])
    ada = float(_c_ada.predict_proba(x)[0][1])
    avg = (log + tree + ada) / 3
    return {"logistic_regression": round(log, 3), "decision_tree": round(tree, 3),
            "adaboost": round(ada, 3), "consensus": round(avg, 3),
            "flag": "high" if avg >= 0.5 else "low"}


def fraud_check(payment, distance, duration, surge):
    x = np.array([[payment, distance, duration, surge]], dtype=float)
    svm = float(_f_svm.predict_proba(_f_scaler.transform(x))[0][1])
    nb = float(_f_nb.predict_proba(x)[0][1])
    avg = (svm + nb) / 2
    return {"svm": round(svm, 3), "naive_bayes": round(nb, 3),
            "consensus": round(avg, 3), "flag": "review" if avg >= 0.5 else "ok"}


# ════════════════════════ analytics (with plots) ════════════════════════
def surge_zones(k=5):
    X = data.generate_demand_points()
    km = KMeans(n_clusters=k, n_init=10, random_state=RNG).fit(X)
    labels = km.labels_
    gmm = GaussianMixture(n_components=k, random_state=RNG).fit(X)
    sil = float(silhouette_score(X, labels))

    zones = []
    for i, c in enumerate(km.cluster_centers_):
        zones.append({"zone": i, "lat": round(float(c[1]), 3), "lon": round(float(c[0]), 3),
                      "demand": int((labels == i).sum())})
    zones.sort(key=lambda z: z["demand"], reverse=True)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.6))
    a1.scatter(X[:, 0], X[:, 1], c=labels, cmap="tab10", s=14, alpha=0.7)
    a1.scatter(km.cluster_centers_[:, 0], km.cluster_centers_[:, 1],
               c="black", marker="X", s=160, label="hotspot")
    a1.set_title(f"Demand hotspots (K-Means, silhouette={sil:.2f})")
    a1.set_xlabel("lon"); a1.set_ylabel("lat"); a1.legend()
    Z = linkage(X[:60], method="ward")
    dendrogram(Z, ax=a2, no_labels=True, color_threshold=0.7 * max(Z[:, 2]))
    a2.set_title("Zone taxonomy (Hierarchical)")
    a2.set_ylabel("merge distance")
    return {"algorithms": ["K-Means", "Gaussian Mixture (EM)", "Hierarchical"],
            "silhouette": round(sil, 3), "zones": zones, "plot_png_base64": _b64(fig)}


def rider_segments():
    X, seg, names = data.generate_riders()
    Xs = StandardScaler().fit_transform(X)
    pca = PCA(n_components=2, random_state=RNG).fit(Xs)
    proj = pca.transform(Xs)
    lda = LinearDiscriminantAnalysis().fit(Xs, seg)
    acc = float(accuracy_score(seg, lda.predict(Xs)))
    seg_names = {0: "commuter", 1: "occasional", 2: "power user"}

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.6))
    for s in np.unique(seg):
        m = seg == s
        a1.scatter(proj[m, 0], proj[m, 1], s=16, alpha=0.7, label=seg_names[s])
    a1.set_title(f"Rider segments — PCA 2D ({pca.explained_variance_ratio_[:2].sum()*100:.0f}% var)")
    a1.set_xlabel("PC1"); a1.set_ylabel("PC2"); a1.legend()
    ld = lda.transform(Xs)
    for s in np.unique(seg):
        a2.hist(ld[seg == s, 0], bins=25, alpha=0.6, label=seg_names[s])
    a2.set_title(f"Fisher LDA separation (acc={acc:.2f})")
    a2.set_xlabel("LD1"); a2.legend()
    return {"algorithms": ["PCA", "Fisher Discriminant (LDA)"],
            "lda_accuracy": round(acc, 3),
            "pca_var_first2": round(float(pca.explained_variance_ratio_[:2].sum()), 3),
            "segments": list(seg_names.values()), "plot_png_base64": _b64(fig)}


def driver_shift(n_states=3):
    from hmmlearn import hmm
    X, true = data.generate_driver_shift()
    model = hmm.GaussianHMM(n_components=n_states, covariance_type="diag",
                            n_iter=100, random_state=RNG).fit(X)
    states = model.predict(X)
    # label states by their mean earnings so output is human-readable
    order = np.argsort([model.means_[s][0] for s in range(n_states)])
    names = {order[0]: "idle", order[1]: "en-route", order[2]: "on-trip"}

    fig, ax = plt.subplots(figsize=(10, 4))
    t = np.arange(len(X))
    ax.scatter(t, X[:, 0], c=states, cmap="tab10", s=18)
    ax.plot(t, X[:, 0], color="gray", lw=0.5, alpha=0.5)
    ax.set_title(f"Driver shift — HMM decoded states (log-lik={model.score(X):.0f})")
    ax.set_xlabel("5-min interval"); ax.set_ylabel("$ earned")
    frac = {names[s]: round(float((states == s).mean()), 2) for s in range(n_states)}
    return {"algorithm": "Hidden Markov Model", "n_states": n_states,
            "log_likelihood": round(float(model.score(X)), 1),
            "time_fraction": frac, "plot_png_base64": _b64(fig)}


def cancellation_causes():
    """Bayesian Network: Rain → Demand → Surge → Cancel. Shows how rain propagates."""
    from pgmpy.models import DiscreteBayesianNetwork
    from pgmpy.factors.discrete import TabularCPD
    from pgmpy.inference import VariableElimination
    import networkx as nx

    m = DiscreteBayesianNetwork([("Rain", "Demand"), ("Demand", "Surge"), ("Surge", "Cancel")])
    m.add_cpds(
        TabularCPD("Rain", 2, [[0.75], [0.25]]),
        TabularCPD("Demand", 2, [[0.7, 0.3], [0.3, 0.7]], evidence=["Rain"], evidence_card=[2]),
        TabularCPD("Surge", 2, [[0.8, 0.35], [0.2, 0.65]], evidence=["Demand"], evidence_card=[2]),
        TabularCPD("Cancel", 2, [[0.95, 0.6], [0.05, 0.4]], evidence=["Surge"], evidence_card=[2]),
    )
    infer = VariableElimination(m)
    p_dry = float(infer.query(["Cancel"], evidence={"Rain": 0}, show_progress=False).values[1])
    p_rain = float(infer.query(["Cancel"], evidence={"Rain": 1}, show_progress=False).values[1])

    fig, ax = plt.subplots(figsize=(7, 4.5))
    g = nx.DiGraph(list(m.edges()))
    pos = {"Rain": (0, 1), "Demand": (1, 1), "Surge": (2, 1), "Cancel": (3, 1)}
    nx.draw(g, pos, ax=ax, with_labels=True, node_color="#93C5FD",
            node_size=2400, font_size=10, arrowsize=22, edgecolors="k")
    ax.set_title("Cancellation causal chain (Bayesian Network)")
    return {"algorithm": "Bayesian Network",
            "P_cancel_given_dry": round(p_dry, 3),
            "P_cancel_given_rain": round(p_rain, 3),
            "insight": f"Rain raises cancellation probability from {p_dry:.0%} to {p_rain:.0%}.",
            "plot_png_base64": _b64(fig)}


PIPELINE = {
    "predict_eta":         ["Linear Regression", "Random Forest", "Neural Network"],
    "estimate_fare":       ["Random Forest", "KNN"],
    "cancellation_risk":   ["Logistic Regression", "Decision Tree", "AdaBoost"],
    "fraud_check":         ["SVM", "Naive Bayes"],
    "surge_zones":         ["K-Means", "Gaussian Mixture (EM)", "Hierarchical"],
    "rider_segments":      ["PCA", "Fisher Discriminant (LDA)"],
    "driver_shift":        ["Hidden Markov Model"],
    "cancellation_causes": ["Bayesian Network"],
}
