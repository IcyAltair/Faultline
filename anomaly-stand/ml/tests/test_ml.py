"""
ML-layer tests: parsing contract, labels, train/serve parity, detection quality,
model persistence, online alert debounce, full pipeline run.
    make test-py             (in Docker)
    pytest -q ml/tests       (locally, from anomaly-stand/)
"""
from __future__ import annotations

import dataclasses
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from anomaly.config import Settings
from anomaly.features import FEATURES, FeatureBuilder, WindowAggregator, WindowData, build_feature_frame, compute_features
from anomaly.labels import label_windows, load_ground_truth
from anomaly.logs import normalize_route, parse_line, parse_upstream_time
from anomaly.model import ModelBundle, anomaly_score, explain_stats, load_bundle, make_pipeline, save_bundle

from synth import T0, WINDOW_S as W, make_ground_truth, synth_records, to_log_line, windows_of, write_stand_files

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _frame(df):
    return build_feature_frame(df, W, 40, 5, 8)


def _bundle(train_frame: pd.DataFrame, q: float = 0.999) -> ModelBundle:
    """Small model bundle trained on clean windows (threshold = high in-sample quantile)."""
    X = train_frame[FEATURES].to_numpy(float)
    pipe = make_pipeline(150, 128, 1.0, 42).fit(X)
    center, scale = explain_stats(pipe, X)
    return ModelBundle(pipeline=pipe, threshold=float(np.quantile(anomaly_score(pipe, X), q)),
                       explain_center=center, explain_scale=scale, version="test", trained_at="test",
                       window_s=W, baseline_windows=40, min_history=5, params={"train_on_clean": True})


# ------------------------------------------------------------------ parsing / labels
def test_normalize_route():
    assert normalize_route("/api/products/42") == "/api/products/{id}"
    assert normalize_route("/api/orders/abc") == "/api/orders/{id}"
    assert normalize_route("/static/app.js") == "/static/app.js"
    assert normalize_route("/definitely/not/here") == "other"


def test_parse_upstream_time():
    assert math.isnan(parse_upstream_time("-"))
    assert math.isnan(parse_upstream_time(""))
    assert parse_upstream_time("0.031") == 0.031
    assert abs(parse_upstream_time("0.002, 0.031") - 0.033) < 1e-9
    assert abs(parse_upstream_time("0.01 : 0.02") - 0.03) < 1e-9


def test_parse_line_contract():
    """Synthetic lines must parse exactly like real nginx lines (guards the e2e test)."""
    r = synth_records(T0, 5, seed=0).iloc[0].to_dict()
    rec = parse_line(to_log_line(r))
    assert rec is not None, "tests/synth.py:to_log_line keys differ from nginx json_analytics"
    assert abs(rec["ts"] - r["ts"]) < 1e-3
    assert rec["status"] == r["status"] and rec["path"] == r["path"]
    assert parse_line("not json") is None


def test_label_windows():
    gt = pd.DataFrame([{"scenario": "error_burst", "start_ts": 100.0, "end_ts": 160.0}])
    ws = np.array([75.0, 90.0, 105.0, 135.0, 150.0, 165.0])
    labels, scen = label_windows(ws, W, gt, 0.5)
    #   75-90: none | 90-105: 5/15 partial | 105-120,135-150: full | 150-165: 10/15 | 165-: none
    assert labels.tolist() == [0, -1, 1, 1, 1, 0]
    assert scen[2] == "error_burst"


def test_load_ground_truth_missing(tmp_path):
    assert load_ground_truth(tmp_path / "nope.jsonl").empty


# ------------------------------------------------------------------ features
def test_empty_window_features():
    f = compute_features(WindowData.empty(), hist_median=10.0, window_s=W)
    assert f["rps"] == 0.0 and f["rps_rel"] == 0.0 and f["ratio_5xx"] == 0.0


def test_offline_online_parity():
    """Training (build_feature_frame) and serving (WindowAggregator+FeatureBuilder) give identical features."""
    df = synth_records(T0, 1800, make_ground_truth(T0, 1800, every_s=600, first_s=300), seed=1)
    offline = _frame(df)

    builder = FeatureBuilder(W, 40, 5)
    agg = WindowAggregator(W, 0.0, start_ts=float(df["ts"].iloc[0]))
    for r in df.to_dict("records"):
        agg.add(r)
    now = (df["ts"].iloc[-1] // W) * W
    online = pd.DataFrame([{"window_start": ws, **builder.push(WindowData.from_records(recs))}
                           for ws, recs in agg.flush(now)])

    assert len(online) == len(offline) > 100
    assert np.allclose(online["window_start"], offline["window_start"])
    assert np.allclose(online[FEATURES].to_numpy(float), offline[FEATURES].to_numpy(float), equal_nan=True)


# ------------------------------------------------------------------ model
def test_isolation_forest_detects_operational_anomalies():
    dur = 7200
    gt = make_ground_truth(T0, dur)
    frame = _frame(synth_records(T0, dur, gt, seed=2))
    y, _ = label_windows(frame["window_start"].to_numpy(), W, pd.DataFrame(gt), 0.5)

    cut = int(len(frame) * 0.6)                       # chronological split, train on clean windows
    pipe = make_pipeline(200, 256, 1.0, 42).fit(frame.iloc[:cut][y[:cut] == 0][FEATURES].to_numpy(float))
    test, yt = frame.iloc[cut:], y[cut:]
    keep = yt >= 0                                    # drop ambiguous boundary windows
    scores = anomaly_score(pipe, test[FEATURES].to_numpy(float))[keep]
    assert yt[keep].sum() > 10
    assert average_precision_score(yt[keep], scores) > 0.7


def test_bundle_roundtrip(tmp_path):
    frame = _frame(synth_records(T0, 3600, seed=3))
    bundle = _bundle(frame)
    save_bundle(bundle, tmp_path / "models")
    loaded = load_bundle(tmp_path / "models" / "model.joblib")
    X = frame[FEATURES].to_numpy(float)[:20]
    assert np.allclose(loaded.score(X), bundle.score(X))
    assert loaded.zscores(X).shape == (20, len(FEATURES))
    assert set(loaded.info_labels()) >= {"version", "threshold"}


# ------------------------------------------------------------------ online detector
def test_detector_k_of_n_alert(tmp_path):
    from anomaly.detector import Detector

    cfg = dataclasses.replace(Settings(), log_file=tmp_path / "access.json.log", data_dir=tmp_path / "ml",
                              alert_k=2, alert_n=3, auto_train=False)
    det = Detector(cfg)
    det.bundle = _bundle(_frame(synth_records(T0, 3600, seed=4)))

    start = T0 + 3600
    gt = [{"scenario": "error_burst", "start_ts": start + 600.0, "end_ts": start + 780.0},
          {"scenario": "latency_spike", "start_ts": start + 600.0, "end_ts": start + 780.0}]
    for ws, recs in windows_of(synth_records(start, 1200, gt, seed=5)):
        det._process_window(ws, recs)

    assert det.alerts, "no alert raised during the injected incident"
    first = det.alerts[0]
    assert start + 540 <= first["start"] <= start + 780
    assert first["top_features"]
    assert det.open_alert is None and det.alerts[-1]["ongoing"] is False   # closed after recovery
    assert det.status()["model_loaded"] is True


# ------------------------------------------------------------------ end-to-end
def test_pipeline_end_to_end(tmp_path):
    """prepare -> train -> test via the real CLI on 3 h of synthetic nginx logs."""
    dur = 3 * 3600
    gt = make_ground_truth(T0, dur)
    log, gtf = write_stand_files(tmp_path, synth_records(T0, dur, gt, seed=6), gt)
    env = {**os.environ, "NGINX_LOG_FILE": str(log), "GROUND_TRUTH_FILE": str(gtf),
           "ML_DATA_DIR": str(tmp_path / "ml"), "N_ESTIMATORS": "100", "MPLBACKEND": "Agg",
           "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run([sys.executable, "-m", "anomaly.pipeline", "run"], env=env,
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]

    bundle = load_bundle(tmp_path / "ml" / "models" / "model.joblib")
    assert bundle.window_s == W and math.isfinite(bundle.threshold)
    assert (tmp_path / "ml" / "datasets" / "features.csv").exists()
    assert any((tmp_path / "ml" / "reports").iterdir())