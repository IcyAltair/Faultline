# Step 3 — ML layer: Isolation Forest on nginx logs

Detects **operational** anomalies (errors, slowness, traffic changes, broken deploys) in the
nginx JSON access log. Security detection is out of scope. Not a production setup.

## How it works
```
data/nginx-logs/access.json.log ──► parse_line ──► 15 s windows ──► 20 features
                                                           │
            training (ml-trainer, offline)                 │   serving (ml-detector, online)
  prepare: whole log -&gt; features.csv + GT labels           │   tail -F log, same feature code
  train:   chronological block split, grid search          │   IsolationForest score
           (max_samples × max_features), threshold         ▼   k-of-n debounce -&gt; alert
  test:    window + event metrics -&gt; reports/latest.md   model.joblib (hot reload)
                                                               │
                                     /metrics ──► Prometheus ──► Grafana (step 4 plugin)
                                     /api/alerts ─────────────────┘
```

* **Features**, one vector per window (`features.py`): rps, rps relative to the 10-min median,
  4xx/404/5xx shares, latency mean/p50/p95/max, upstream p95, body bytes mean/p95/max,
  request length, POST/API/unknown-route shares, URI diversity, distinct clients,
  keep-alive reuse. Training and serving use the same code (`test_offline_online_parity`).
* **Labels** come from `data/ground-truth/anomalies.jsonl`, which the step-1 traffic generator
  writes. A window counts as an anomaly when at least 50 % of it falls inside an injected
  scenario; boundary windows are excluded. Chaos you trigger by hand from the UI isn't in the
  ground truth, so in reports it shows up as false alarms.
* **Model:** `log1p(heavy-tailed) -> IsolationForest`. By default it trains only on clean
  windows (`TRAIN_ON_CLEAN=true`). Score = `-score_samples`, roughly 0.5 for normal windows
  and closer to 1 for anomalies.
* **Threshold** is picked on the validation split (quantile/floor settings in `config.py`).
* **Explanation:** the 3 features with the largest robust z-score against clean training data
  are attached to every alert.
* **Alerting:** an alert fires when `ALERT_K` of the last `ALERT_N` windows are above the
  threshold (default 2 of 3), which suppresses one-off spikes.

## Run

Steps 1 and 2 must be running (they create the `anomaly-stand-net` network and `../data`).
```bash
cd anomaly-stand/ml
make up

# enables the Prometheus target + starts ml-detector on :9200
make train

# prepare -&gt; train -&gt; test; the detector hot-reloads the new model
make report

# metrics of the last run
make status

# model + stream state
make alerts

# alert intervals
make test-py

# tests in Docker (synthetic logs, ~1 min)
```

With `AUTO_TRAIN=true` (the default), the detector trains the first model by itself once the
log covers `AUTO_TRAIN_MIN_SPAN_S` (3900 s, about one simulated day for the generator).

## Artifacts (`data/ml/`)

| Path | Content |
|---|---|
| `datasets/features.csv` | window features + labels + split |
| `models/model.joblib` | current model bundle (atomically replaced) |
| `models/model-<version>.joblib` | history of trained models |
| `reports/latest.md` | validation/test metrics of the last run |

## Prometheus metrics (job `ml-detector`)

| Metric | Meaning |
|---|---|
| `nginx_anomaly_score` / `nginx_anomaly_threshold` | last window score / decision threshold |
| `nginx_anomaly_is_anomaly` | raw flag of the last window |
| `nginx_anomaly_alert` | debounced alert (0/1) |
| `nginx_anomaly_feature_value{feature}` / `nginx_anomaly_feature_zscore{feature}` | per-feature values and deviations |
| `nginx_anomaly_model_info{version,threshold,val_ap,test_f1,…}` | loaded model metadata |
| `nginx_anomaly_windows_total`, `nginx_anomaly_alerts_total`, `…_parse_errors_total`, `…_late_lines_total` | stream health |

Alert rules live in `observability/prometheus/rules/ml-alerts.yml`:
`MLAnomalyDetected`, `MLDetectorDown`, `MLDetectorStalled`, `MLModelMissing`.

## REST API (:9200)

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/metrics` | Prometheus |
| GET | `/api/status` | model, metrics, open alert, stream counters |
| GET | `/api/windows?limit=120` | last scored windows with features and top deviations |
| GET | `/api/alerts?since=<epoch>` | alert intervals (`start_ms`/`end_ms` for Grafana annotations) |
| POST | `/api/reload` | force model reload |
| POST | `/api/train` | start the pipeline in the background (409 if already running) |

## Tuning (env in `docker-compose.yml`, must match for trainer and detector)

`WINDOW_S` (changing it requires retraining), `BASELINE_WINDOWS`, `TRAIN_ON_CLEAN`,
`N_ESTIMATORS`, `MAX_SAMPLES_GRID`, `MAX_FEATURES_GRID`, `THRESHOLD_QUANTILE`,
`THRESHOLD_FLOOR_QUANTILE`, `ALERT_K`/`ALERT_N`, `SPLIT_PATTERN`, `BLOCK_WINDOWS`.

## Troubleshooting

* `model_loaded=false` for a long time: the log is shorter than `AUTO_TRAIN_MIN_SPAN_S`. Run `make train`.
* `parse_errors_total` grows: the nginx `json_analytics` format changed. Update `logs.parse_line`.
* `Model window_s != WINDOW_S` in the logs: you changed the window. Retrain.
* Too many alerts: raise `THRESHOLD_QUANTILE` or `ALERT_K`. Missed incidents: lower them.