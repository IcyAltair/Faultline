"""
Online anomaly detector (FastAPI + Prometheus).

Loop (background thread):
  tail nginx JSON log -> bucket by $msec into WINDOW_S windows -> when a window is
  complete (+GRACE_S): features (same code as training) -> IsolationForest score
  -> raw flag (score >= threshold) -> k-of-n debounced alert -> Prometheus gauges.

Model lifecycle: hot reload when data/ml/models/model.joblib changes; with
AUTO_TRAIN=true the first model is trained automatically (pipeline subprocess)
once the log covers AUTO_TRAIN_MIN_SPAN_S.

Endpoints: /health, /metrics, /api/status, /api/windows, /api/alerts,
           POST /api/reload, POST /api/train
"""
from __future__ import annotations

import logging
import math
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from . import __version__
from .config import Settings
from .features import FEATURES, FeatureBuilder, WindowAggregator, WindowData
from .logs import LogTailer, log_time_span, parse_line, read_tail_records
from .model import INFO_LABELS, ModelBundle, load_bundle

cfg = Settings()
logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ml-detector")

# ------------------------------------------------------------------ Prometheus metrics
SCORE = Gauge("nginx_anomaly_score", "Isolation Forest score of the last complete window (higher = more anomalous)")
THRESHOLD = Gauge("nginx_anomaly_threshold", "Decision threshold of the loaded model")
IS_ANOMALY = Gauge("nginx_anomaly_is_anomaly", "1 if the last window score >= threshold (raw, not debounced)")
ALERT = Gauge("nginx_anomaly_alert", "1 while the k-of-n debounced alert is active")
FEATURE_VALUE = Gauge("nginx_anomaly_feature_value", "Feature value of the last window", ["feature"])
FEATURE_Z = Gauge("nginx_anomaly_feature_zscore", "Robust z-score of the feature vs clean training data", ["feature"])
WINDOW_REQUESTS = Gauge("nginx_anomaly_window_requests", "Log lines in the last complete window")
LAST_WINDOW = Gauge("nginx_anomaly_last_window_timestamp_seconds", "End time of the last processed window")
WINDOWS = Counter("nginx_anomaly_windows", "Complete windows processed")
WINDOWS_ANOM = Counter("nginx_anomaly_windows_anomalous", "Windows with score >= threshold")
ALERTS = Counter("nginx_anomaly_alerts", "Alerts raised (rising edges)")
LINES = Counter("nginx_anomaly_log_lines", "Parsed log lines")
PARSE_ERRORS = Counter("nginx_anomaly_parse_errors", "Unparseable log lines")
LATE_LINES = Counter("nginx_anomaly_late_lines", "Lines that arrived after their window was scored")
MODEL_LOADED = Gauge("nginx_anomaly_model_loaded", "1 if a model is loaded")
MODEL_INFO = Gauge("nginx_anomaly_model_info", "Loaded model metadata (value is always 1)", INFO_LABELS)
TRAINING = Gauge("nginx_anomaly_training_in_progress", "1 while a training pipeline subprocess runs")


def _plain(o):
    """Recursively convert numpy / NaN / exotic objects into strict-JSON-safe Python values.
    Starlette's JSONResponse uses allow_nan=False, and model metrics can contain NaN (e.g. AP
    when a split has no positives), so this is required for /api/status."""
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, deque)):
        return [_plain(v) for v in o]
    if isinstance(o, np.ndarray):
        return _plain(o.tolist())
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    return str(o)


class Detector:
    def __init__(self, settings: Settings) -> None:
        self.cfg = settings
        self.lock = threading.Lock()              # guards bundle / flags / recent / alerts
        self._train_lock = threading.Lock()       # guards the training subprocess handle
        self.stop_event = threading.Event()
        self.bundle: ModelBundle | None = None
        self._model_mtime: float | None = None
        self.tailer = LogTailer(settings.log_file, start_at_end=True)
        self.builder = FeatureBuilder(settings.window_s, settings.baseline_windows, settings.min_history)
        self.agg: WindowAggregator | None = None
        self.flags: deque[int] = deque(maxlen=settings.alert_n)
        self.recent: deque[dict] = deque(maxlen=settings.recent_windows)
        self.alerts: deque[dict] = deque(maxlen=200)
        self.open_alert: dict | None = None
        self._late_seen = 0
        self._proc: subprocess.Popen | None = None
        self._next_train_try = 0.0
        self.last_train: dict | None = None
        self.started_at = time.time()
        MODEL_LOADED.set(0)
        ALERT.set(0)

    # ---------------------------------------------------------- model lifecycle
    def maybe_reload(self, force: bool = False) -> bool:
        path = self.cfg.model_path
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return False
        if not force and mtime == self._model_mtime:
            return False
        self._model_mtime = mtime
        try:
            bundle = load_bundle(path)
        except Exception as exc:  # corrupt / incompatible model: keep the old one
            log.error("Cannot load model %s: %r", path, exc)
            return False
        if bundle.window_s != self.cfg.window_s:
            log.error("Model window_s=%s != WINDOW_S=%s: retrain with the current settings",
                      bundle.window_s, self.cfg.window_s)
            return False
        with self.lock:
            self.bundle = bundle
            self.flags.clear()
            # A new model means a new threshold: close the alert raised by the old one
            if self.open_alert is not None:
                self.open_alert["ongoing"] = False
                self.open_alert["closed_by"] = "model_reload"
                self.open_alert = None
        ALERT.set(0)
        MODEL_LOADED.set(1)
        THRESHOLD.set(bundle.threshold)
        MODEL_INFO.clear()
        MODEL_INFO.labels(**bundle.info_labels()).set(1)
        log.info("Loaded model %s (threshold %.4f)", bundle.version, bundle.threshold)
        return True

    def start_training(self, reason: str) -> bool:
        with self._train_lock:
            if self._proc is not None:
                return False
            log.warning("Starting training pipeline (%s)", reason)
            self._proc = subprocess.Popen([sys.executable, "-m", "anomaly.pipeline", "run"])
            self.last_train = {"reason": reason, "started_at": time.time(), "returncode": None}
            TRAINING.set(1)
            return True

    def _check_training(self) -> None:
        with self._train_lock:
            if self._proc is None or self._proc.poll() is None:
                return
            rc = self._proc.returncode
            self._proc = None
            TRAINING.set(0)
            self.last_train["returncode"] = rc
            self.last_train["finished_at"] = time.time()
        if rc != 0:
            self._next_train_try = time.time() + self.cfg.auto_train_retry_s
            log.error("Training failed (rc=%s), next auto attempt in %.0fs", rc, self.cfg.auto_train_retry_s)
        self.maybe_reload()

    def _maybe_auto_train(self) -> None:
        if not self.cfg.auto_train or self.bundle is not None or self._proc is not None:
            return
        if time.time() < self._next_train_try:
            return
        span = log_time_span(self.cfg.log_file)
        if span >= self.cfg.auto_train_min_span_s:
            self.start_training(f"auto: log span {span:.0f}s")
        else:
            log.info("No model yet: log span %.0fs < %.0fs, waiting for more data",
                     span, self.cfg.auto_train_min_span_s)
            self._next_train_try = time.time() + self.cfg.auto_train_check_s

    # ---------------------------------------------------------- streaming
    def _prime(self) -> None:
        """Seed rps_rel history from the log tail, so scores are valid right after start."""
        W = self.cfg.window_s
        now = time.time()
        records = read_tail_records(self.cfg.log_file, self.cfg.prime_bytes)
        self.tailer.read_lines()                      # open the log at its current end
        self.agg = WindowAggregator(W, self.cfg.grace_s, now)
        if not records:
            return
        counts: dict[int, int] = {}
        for r in records:
            k = int(r["ts"] // W)
            counts[k] = counts.get(k, 0) + 1
        cur = int(now // W)
        first = min(counts) + 1                       # first window of the tail may be partial
        ks = range(max(first, cur - self.cfg.baseline_windows), cur)
        self.builder.prime(counts.get(k, 0) for k in ks)
        log.info("Primed rps history with %d windows", len(ks))

    def _process_window(self, ws: float, records: list[dict]) -> dict:
        """Score one complete window, update metrics + alert state. Returns the window row."""
        W = self.cfg.window_s
        w = WindowData.from_records(records)
        feats = self.builder.push(w)
        WINDOWS.inc()
        WINDOW_REQUESTS.set(w.n)
        LAST_WINDOW.set(ws + W)
        for name in FEATURES:
            FEATURE_VALUE.labels(name).set(feats[name])
        row = {"window_start": ws, "window_end": ws + W, "n_requests": w.n,
               "features": {k: round(v, 4) for k, v in feats.items()},
               "score": None, "threshold": None, "is_anomaly": None, "alert": None, "top_features": []}
        with self.lock:
            bundle = self.bundle
            if bundle is not None:
                x = np.array([[feats[f] for f in FEATURES]], dtype=float)
                score = float(bundle.score(x)[0])
                z = bundle.zscores(x)[0]
                flag = int(score >= bundle.threshold)
                self.flags.append(flag)
                alert = sum(self.flags) >= self.cfg.alert_k
                top = [{"feature": FEATURES[i], "z": round(float(z[i]), 2)} for i in np.argsort(-np.abs(z))[:3]]

                SCORE.set(score)
                IS_ANOMALY.set(flag)
                ALERT.set(int(alert))
                if flag:
                    WINDOWS_ANOM.inc()
                for i, name in enumerate(FEATURES):
                    FEATURE_Z.labels(name).set(float(z[i]))

                # alert intervals for the REST API (dicts are shared with self.alerts -> updated in place)
                if alert and self.open_alert is None:
                    ALERTS.inc()
                    self.open_alert = {"start": ws, "end": ws + W, "max_score": score,
                                       "threshold": bundle.threshold, "model_version": bundle.version,
                                       "top_features": top, "ongoing": True}
                    self.alerts.append(self.open_alert)
                    log.warning("ALERT start score=%.3f top=%s", score, top)
                elif alert:
                    self.open_alert["end"] = ws + W
                    if score > self.open_alert["max_score"]:
                        self.open_alert["max_score"] = score
                        self.open_alert["top_features"] = top   # explanation of the peak window
                elif self.open_alert is not None:
                    self.open_alert["ongoing"] = False
                    log.warning("ALERT end duration=%.0fs max_score=%.3f",
                                self.open_alert["end"] - self.open_alert["start"], self.open_alert["max_score"])
                    self.open_alert = None

                row.update(score=round(score, 4), threshold=round(bundle.threshold, 4),
                           is_anomaly=flag, alert=int(alert), top_features=top)
            self.recent.append(row)
        return row

    def _ingest(self) -> None:
        """Read new log lines and put them into window buckets."""
        for line in self.tailer.read_lines():
            rec = parse_line(line)
            if rec is None:
                PARSE_ERRORS.inc()
                continue
            LINES.inc()
            self.agg.add(rec)
        if self.agg.late > self._late_seen:          # counter delta from the aggregator
            LATE_LINES.inc(self.agg.late - self._late_seen)
            self._late_seen = self.agg.late

    def run(self) -> None:
        """Main loop (runs in a daemon thread started by the FastAPI lifespan)."""
        self.maybe_reload(force=True)
        try:
            self._prime()
        except Exception:
            log.exception("Priming failed, starting with empty history")
            self.agg = WindowAggregator(self.cfg.window_s, self.cfg.grace_s, time.time())
        next_reload = 0.0
        while not self.stop_event.is_set():
            try:
                now = time.time()
                if now >= next_reload:
                    self.maybe_reload()
                    next_reload = now + self.cfg.model_reload_s
                self._check_training()
                self._maybe_auto_train()
                self._ingest()
                for ws, recs in self.agg.flush(time.time()):
                    self._process_window(ws, recs)
            except Exception:  # never let one bad iteration kill the detector
                log.exception("Detector loop error")
            self.stop_event.wait(self.cfg.poll_interval_s)
        self.tailer.close()

    def stop(self) -> None:
        self.stop_event.set()
        with self._train_lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()

    # ---------------------------------------------------------- REST views
    def status(self) -> dict:
        with self.lock:
            b = self.bundle
            model = None if b is None else {
                "version": b.version, "trained_at": b.trained_at, "threshold": b.threshold,
                "window_s": b.window_s, "features": list(b.features), "params": b.params, "metrics": b.metrics,
            }
            last = self.recent[-1] if self.recent else None
            open_alert = dict(self.open_alert) if self.open_alert else None
            n_alerts = len(self.alerts)
        return _plain({
            "version": __version__,
            "uptime_s": time.time() - self.started_at,
            "model_loaded": b is not None,
            "model": model,
            "alert_active": open_alert is not None,
            "open_alert": open_alert,
            "alerts_kept": n_alerts,
            "last_window": last,
            "training": {"running": self._proc is not None, "last": self.last_train},
            "stream": {
                "log_file": str(self.cfg.log_file),
                "late_lines": self.agg.late if self.agg else 0,
                "skipped_windows": self.agg.skipped if self.agg else 0,
                "tailer_reopens": self.tailer.reopens,
            },
            "settings": {"window_s": self.cfg.window_s, "alert_k": self.cfg.alert_k, "alert_n": self.cfg.alert_n,
                         "auto_train": self.cfg.auto_train},
        })

    def windows(self, limit: int) -> list[dict]:
        with self.lock:
            rows = list(self.recent)[-limit:]
        return _plain(rows)

    def alert_list(self, since: float, limit: int) -> list[dict]:
        with self.lock:
            items = [dict(a) for a in self.alerts if a["end"] >= since][-limit:]
        for a in items:  # ms timestamps: convenient for Grafana annotations (step 4 plugin)
            a["start_ms"], a["end_ms"] = int(a["start"] * 1000), int(a["end"] * 1000)
            a["duration_s"] = a["end"] - a["start"]
        return _plain(items)


# ------------------------------------------------------------------ FastAPI app
detector = Detector(cfg)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    thread = threading.Thread(target=detector.run, name="detector-loop", daemon=True)
    thread.start()
    log.info("Detector started: log=%s window=%ss k-of-n=%s/%s", cfg.log_file, cfg.window_s, cfg.alert_k, cfg.alert_n)
    app.state.thread = thread
    yield
    detector.stop()
    thread.join(timeout=5)


app = FastAPI(title="nginx anomaly detector (Isolation Forest)", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    """Liveness: 200 while the process is up. Model state is informational (no model != unhealthy)."""
    thread = getattr(app.state, "thread", None)
    return {"status": "ok", "loop_alive": bool(thread and thread.is_alive()),
            "model_loaded": detector.bundle is not None}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/status")
def api_status() -> dict:
    return detector.status()


@app.get("/api/windows")
def api_windows(limit: int = Query(120, ge=1, le=10_000)) -> list[dict]:
    """Last scored windows with features, score and top deviating features."""
    return detector.windows(limit)


@app.get("/api/alerts")
def api_alerts(since: float = Query(0.0, description="epoch seconds; alerts ending after this"),
               limit: int = Query(100, ge=1, le=200)) -> list[dict]:
    """Debounced alert intervals (oldest first). Consumed by the Grafana plugin in step 4."""
    return detector.alert_list(since, limit)


@app.post("/api/reload")
def api_reload() -> dict:
    if not cfg.model_path.exists():
        raise HTTPException(404, f"No model at {cfg.model_path}; run the training pipeline first")
    ok = detector.maybe_reload(force=True)
    if not ok:
        raise HTTPException(500, "Model could not be loaded, see detector logs")
    return {"reloaded": True, "version": detector.bundle.version, "threshold": detector.bundle.threshold}


@app.post("/api/train")
def api_train() -> dict:
    if not detector.start_training("manual: API"):
        raise HTTPException(409, "Training is already running")
    return {"started": True}