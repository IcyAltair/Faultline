<div align="center">

# 🌋 Faultline

### Detecting operational anomalies in nginx logs with Isolation Forest

[![License](https://img.shields.io/badge/license-Apache--2.0-blue?style=for-the-badge)](LICENSE)
![Docker](https://img.shields.io/badge/Docker_Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Python](https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![nginx](https://img.shields.io/badge/nginx_1.27-009639?style=for-the-badge&logo=nginx&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus_2.55-E6522C?style=for-the-badge&logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-F46800?style=for-the-badge&logo=grafana&logoColor=white)
![scikit-learn](https://img.shields.io/badge/scikit--learn-F7931E?style=for-the-badge&logo=scikitlearn&logoColor=white)

**Demo shop → JSON access logs → Prometheus features → Isolation Forest → Grafana panel**

</div>

> [!WARNING]
> **This is an experimental stand, not a production setup.** It has no TLS, no auth (the chaos API is open to anyone), no log rotation, default Grafana credentials and an unsigned Grafana plugin. Run it only on a local machine or in an isolated lab.

---

## 📑 Contents

- [What it is](#-what-it-is)
- [Architecture](#-architecture)
- [Prerequisites](#-prerequisites)
- [Step-by-step setup](#-step-by-step-setup)
  - [Step 0. Clone](#-step-0-clone)
  - [Step 1. Business layer](#-step-1-business-layer)
  - [Step 2. Observability layer](#-step-2-observability-layer)
  - [Step 3. ML layer](#-step-3-ml-layer)
  - [Step 4. Grafana plugin](#-step-4-grafana-plugin)
- [Ports and URLs](#-ports-and-urls)
- [Anomaly scenarios](#-anomaly-scenarios)
- [Configuration](#️-configuration)
- [Testing](#-testing)
- [Operations](#-operations)
- [Troubleshooting](#-troubleshooting)
- [License](#-license)

---

## 🎯 What it is

Faultline is a self-contained Docker stand for detecting **operational** anomalies such as latency degradation, error bursts, traffic spikes, 404 storms and heavy payloads. It doesn't cover security attacks. Anomalies are found in nginx JSON access logs.

A synthetic traffic generator drives a demo shop and injects anomalies on a schedule. Every injected anomaly is written to a **ground truth** file, so you can compare simple fixed-threshold Prometheus alerts with the Isolation Forest model against real labels.

| Step | Layer | Folder | Main components |
|:---:|---|---|---|
| 🟢 **1** | Business | `anomaly-stand/` | nginx, FastAPI Demo Shop, UI, traffic generator |
| 🔵 **2** | Observability | `anomaly-stand/observability/` | Prometheus, Grafana, nginx-exporter, nginx-log-exporter, gt-annotator |
| 🟣 **3** | ML | `anomaly-stand/ml/` | ml-trainer (offline), ml-detector (online, `:9200`) |
| 🟠 **4** | Visualization | `anomaly-stand/observability/grafana/plugins/` | `faultline-anomaly-panel` + *Anomaly Detection* dashboard |

---

## 🏗 Architecture
```mermaid
flowchart LR
    subgraph S1["🟢 Step 1: Business"]
        TG[traffic-generator] --&gt;|HTTP| NG[nginx :8080]
        NG --&gt; APP[app FastAPI :8000]
        TG -.-&gt;|chaos API| APP
        NG --&gt;|JSON| LOG[(data/nginx-logs/&lt;br/&gt;access.json.log)]
        TG --&gt;|labels| GT[(data/ground-truth/&lt;br/&gt;anomalies.jsonl)]
    end

    subgraph S2["🔵 Step 2: Observability"]
        NLE[nginx-log-exporter :9102]
        NE[nginx-exporter :9113]
        GTA[gt-annotator :9103]
        PROM[Prometheus :9090]
        GRAF[Grafana :3000]
    end

    subgraph S3["🟣 Step 3: ML"]
        TR[ml-trainer] --&gt;|model.joblib| DET[ml-detector :9200]
    end

    LOG --&gt; NLE
    LOG --&gt; TR
    LOG --&gt; DET
    GT --&gt; GTA
    GT --&gt; TR
    NG --&gt;|stub_status :8081| NE
    APP --&gt;|/metrics| PROM
    NLE --&gt; PROM
    NE --&gt; PROM
    GTA --&gt; PROM
    GTA --&gt;|annotations| GRAF
    DET --&gt;|/metrics| PROM
    PROM --&gt;|PromQL| GRAF
    GRAF --- PLG["🟠 Step 4:&lt;br/&gt;faultline-anomaly-panel"]
```

All layers share one external Docker network, **`anomaly-stand-net`**, which step 1 creates, and one data directory, **`anomaly-stand/data/`**. Because of this, the layers must be started **in order**.

---

## 🧰 Prerequisites

| Tool | Why |
|---|---|
| 🐳 Docker + **Docker Compose v2** (`docker compose`) | Runs every layer |
| 🛠 `make` | Helper targets for steps 2 and 3 |
| 🌐 `curl`, `jq` | Smoke checks and reading logs |
| 💾 A few GB of free disk space | The logs grow by about 8 lines/s and aren't rotated |

---

## 🚀 Step-by-step setup

### 📥 Step 0. Clone
```bash
git clone https://github.com/IcyAltair/Faultline.git
cd Faultline/anomaly-stand
```

---

### 🟢 Step 1. Business layer

This step starts the Demo Shop, nginx with JSON access logs (the ML data source) and the synthetic traffic generator.

> [!TIP]
> **To see anomalies within minutes**, create `anomaly-stand/.env` before you start:
> ```env
> WARMUP_S=60
> ANOMALY_MIN_GAP_S=60
> ANOMALY_MAX_GAP_S=120
> ```
> By default, the first anomaly appears only after a 30-minute clean warm-up (`WARMUP_S=1800`).
```bash
# from anomaly-stand/
docker compose up -d --build
docker compose ps

# app, nginx, traffic-generator should be healthy
```

✅ **Check it:**
```bash
curl -s localhost:8080/api/health

# {"status":"ok",...}
curl -s 'localhost:8080/api/products?limit=2' | jq
wc -l data/nginx-logs/access.json.log

# grows by about 8 lines/s

# pretty live tail of the access log
tail -f data/nginx-logs/access.json.log | \
  jq -c '{t:.time_iso8601,m:.request_method,u:.request_uri,s:.status,rt:.request_time}'

# injected anomalies (ground truth)
cat data/ground-truth/anomalies.jsonl | jq .
docker compose logs -f traffic-generator
```

🌐 UI: <http://localhost:8080> · 📘 Swagger: <http://localhost:8080/docs>

<details>
<summary>📡 <b>Demo Shop API</b></summary>

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness + stats |
| GET | `/api/products?category=&limit=&offset=` | Paginated catalog |
| GET | `/api/products/{id}` | Product details (404 if missing) |
| POST | `/api/orders` `{"product_id":1,"quantity":2}` | Create order (201 / 404 / 409 / 422) |
| GET | `/api/orders/{id}` | Order details |
| GET/POST/DELETE | `/api/chaos` | Fault injection state |
| GET | `/metrics` | Prometheus metrics (app container only, not exposed through nginx) |

**Manual chaos:**
```bash
curl -X POST localhost:8080/api/chaos -H 'Content-Type: application/json' \
  -d '{"latency_ms":1500,"latency_jitter_ms":300,"error_rate":0.1,"endpoints":["/api/products"],"duration_s":120}'
curl -X DELETE localhost:8080/api/chaos
```

Each chaos config has a TTL (`duration_s`) and turns itself off when it expires. Chaos that you start by hand is **not** written to ground truth, so ML reports count it as false alarms.

</details>

<details>
<summary>📄 <b>nginx log format</b> (<code>data/nginx-logs/access.json.log</code>)</summary>
```json
{"time_iso8601":"2026-10-08T12:00:01+00:00","msec":1791460801.123,"request_id":"5f0c...",
 "remote_addr":"172.20.0.4","x_forwarded_for":"198.51.100.17","request_method":"GET",
 "request_uri":"/api/products?limit=10&amp;offset=0","server_protocol":"HTTP/1.1","status":200,
 "body_bytes_sent":1432,"request_length":182,"request_time":0.031,"upstream_addr":"172.20.0.2:8000",
 "upstream_status":"200","upstream_connect_time":"0.000","upstream_response_time":"0.031",
 "http_referer":"","http_user_agent":"Mozilla/5.0 ...","connection":42,"connection_requests":7}
```

The `upstream_*` fields are strings. They are empty for static files and can hold comma-separated lists on retries. `/healthz` and `/favicon.ico` are not logged.

</details>

> [!IMPORTANT]
> For good ML training, collect **at least 2–3 simulated days** of logs. One simulated day is `DAY_PERIOD_S`, 3600 s by default. The anomaly-free warm-up period (`WARMUP_S`) works as a clean training set.

---

### 🔵 Step 2. Observability layer

This step adds Prometheus (5 s scrape, recording rules used as features, baseline alerts), Grafana with provisioned dashboards, and three exporters.

| Service | Purpose |
|---|---|
| `prometheus` | TSDB, `stand:*` recording rules, baseline detectors |
| `grafana` | Datasource + dashboards, plugin directory for step 4 |
| `nginx-exporter` | Official `stub_status` exporter (connections, total requests) |
| `nginx-log-exporter` 🐍 | Tails the JSON log and exports status, route, latency and size metrics |
| `gt-annotator` 🐍 | Turns `anomalies.jsonl` into Grafana region annotations and `stand_ground_truth_*` metrics |

> [!NOTE]
> A log exporter is needed because `stub_status` only reports connections. All five anomaly scenarios are visible only in the access log fields (`status`, `request_time`, `body_bytes_sent`).

> [!NOTE]
> The observability README mentions an `observability/.env` file (versions, ports, credentials, exporter settings), but it isn't in the repository. The compose file has defaults. Create the file only if you need to override something.
```bash
cd observability
make up

# same as: docker compose up -d --build
docker compose ps

# all services healthy in about 30 s
```

✅ **Check it:**

| URL | What you should see |
|---|---|
| <http://localhost:3000> | Grafana (`admin` / `admin`; anonymous users get Viewer). The home page is *Business overview* |
| <http://localhost:9090/targets> | `app`, `nginx`, `nginx-logs`, `ground-truth`, `grafana`, `prometheus` are all **UP** |
| <http://localhost:9090/alerts> | Health + baseline alerts |
```bash
curl -s 'localhost:9090/api/v1/query?query=stand:requests:rate1m' | jq '.data.result[0].value'

# about 8 rps
curl -s 'localhost:9090/api/v1/query?query=stand_ground_truth_anomaly_active' \
  | jq '.data.result[] | [.metric.scenario, .value[1]]'
```

<details>
<summary>📏 <b>Baseline detectors</b> (the reference the ML model is compared with)</summary>

| Alert | Rule | Scenario |
|---|---|---|
| `BaselineTrafficSpike` | rps > 2.5 × 30 m avg, for 1 m | traffic_spike |
| `BaselineHighLatencyP95` | p95 > 1 s, for 1 m | latency_degradation |
| `BaselineHighErrorRate` | 5xx ratio > 10 %, for 30 s | error_burst |
| `BaselineNotFoundStorm` | 4xx ratio > 25 %, for 1 m | not_found_storm |
| `BaselineHeavyPayload` | avg size > 5 × 30 m avg, for 1 m | heavy_payload |

They are labelled `detector="baseline"`. The health alerts are `StandTargetDown`, `NginxLogExporterStale` and `NginxLogParseErrors`. There is no Alertmanager. Alerts are shown in Prometheus and in the dashboard's *Firing alerts* panels.

The recording rules are evaluated every 5 s: `stand:requests:rate1m` (also by route and by status class), `stand:errors_5xx:ratio1m`, `stand:errors_4xx:ratio1m`, `stand:latency_p{50,95,99}_seconds:1m`, `stand:response_size_bytes:avg1m`, plus 30-minute `…:avg30m` baselines.

</details>

<details>
<summary>📊 <b>Exporter metrics</b></summary>

**nginx-log-exporter** (`job="nginx-logs"`)

| Metric | Type | Labels |
|---|---|---|
| `nginx_log_requests_total` | counter | `method, route, status, status_class` |
| `nginx_log_request_duration_seconds` | histogram (5 ms … 10 s) | `route` |
| `nginx_log_response_size_bytes` | histogram (256 B … 16 MiB) | `route` |
| `nginx_log_lines_total`, `nginx_log_parse_errors_total`, `nginx_log_file_reopens_total` | counter | — |
| `nginx_log_last_event_timestamp_seconds`, `nginx_log_lag_seconds`, `nginx_log_routes_tracked` | gauge | — |

Routes are normalized (`/api/products/17` → `/api/products/:id`). A 404 on a path that has never been seen with another status becomes `__unknown__`, so `not_found_storm` doesn't blow up label cardinality. After `MAX_ROUTES` distinct routes, new routes go to `__other__`. If your `log_format` uses different field names, override them with `FIELD_TIME`, `FIELD_STATUS`, `FIELD_METHOD`, `FIELD_URI`, `FIELD_REQUEST_TIME` and `FIELD_BYTES`.

</details>

---

### 🟣 Step 3. ML layer

This step adds the Isolation Forest pipeline: an offline trainer (prepare, then train, then test) and an online detector that tails the same log and uses the same feature code.
```mermaid
flowchart LR
    L[(access.json.log)] --&gt; P[parse_line] --&gt; W[15 s windows] --&gt; F[20 features]
    F --&gt; T["ml-trainer&lt;br/&gt;block split + grid search&lt;br/&gt;+ threshold"]
    T --&gt; M[(model.joblib)]
    M --&gt;|hot reload| D["ml-detector&lt;br/&gt;score → k-of-n debounce"]
    F --&gt; D
    D --&gt;|/metrics| PR[Prometheus] --&gt; G[Grafana]
```
```bash
cd ../ml

# i.e. anomaly-stand/ml
make up

# enables the Prometheus target + starts ml-detector on :9200
make train

# prepare → train → test; the detector hot-reloads the model
make report

# metrics of the last run
make status

# model + stream state
make alerts

# debounced alert intervals
```

> [!TIP]
> With `AUTO_TRAIN=true` (the default), the detector trains its first model by itself once the log covers `AUTO_TRAIN_MIN_SPAN_S` (3900 s, about one simulated day). Run `make train` to start training earlier.

`make up` copies `observability/prometheus/targets/ml-detector.yml.example` to `ml-detector.yml`. Prometheus picks up the new target through `file_sd` within 30 s, with no restart. To stop scraping, run `make scrape-off`.

<details>
<summary>🧠 <b>How the model works</b></summary>

* **Features** (one vector per 15 s window): rps, rps relative to the 10-min median, 4xx/404/5xx shares, latency mean/p50/p95/max, upstream p95, body bytes mean/p95/max, request length, POST/API/unknown-route shares, URI diversity, distinct clients, keep-alive reuse. Training and serving share the same code, and `test_offline_online_parity` checks this.
* **Labels** come from `data/ground-truth/anomalies.jsonl`. A window counts as anomalous when at least 50 % of it falls inside an injected scenario. Boundary windows are excluded.
* **Model**: `log1p` on heavy-tailed features, then `IsolationForest`. By default it trains on clean windows only (`TRAIN_ON_CLEAN=true`). The score is `-score_samples`: about 0.5 for normal windows and closer to 1 for anomalies.
* **Threshold** is chosen on the validation split.
* **Explanation**: every alert carries the 3 features with the largest robust z-score against clean training data.
* **Alerting**: an alert fires when `ALERT_K` of the last `ALERT_N` windows are above the threshold (default 2 of 3).

</details>

<details>
<summary>📦 <b>Artifacts, metrics and REST API</b></summary>

**Artifacts** (`data/ml/`)

| Path | Content |
|---|---|
| `datasets/features.csv` | Window features + labels + split |
| `models/model.joblib` | Current model bundle (replaced atomically) |
| `models/model-<version>.joblib` | History of trained models |
| `reports/latest.md` | Validation/test metrics of the last run |

**Prometheus metrics** (`job="ml-detector"`): `nginx_anomaly_score`, `nginx_anomaly_threshold`, `nginx_anomaly_is_anomaly`, `nginx_anomaly_alert`, `nginx_anomaly_feature_value{feature}`, `nginx_anomaly_feature_zscore{feature}`, `nginx_anomaly_model_info{version,threshold,val_ap,test_f1,…}`, plus the stream counters `nginx_anomaly_windows_total`, `nginx_anomaly_alerts_total`, `…_parse_errors_total` and `…_late_lines_total`.

The alert rules are in `observability/prometheus/rules/ml-alerts.yml`: `MLAnomalyDetected`, `MLDetectorDown`, `MLDetectorStalled` and `MLModelMissing`.

**REST API** (`:9200`)

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/metrics` | Prometheus |
| GET | `/api/status` | Model, metrics, open alert, stream counters |
| GET | `/api/windows?limit=120` | Last scored windows with features and top deviations |
| GET | `/api/alerts?since=<epoch>` | Alert intervals (`start_ms`/`end_ms`) |
| POST | `/api/reload` | Force a model reload |
| POST | `/api/train` | Run the pipeline in the background (409 if it's already running) |

</details>

---

### 🟠 Step 4. Grafana plugin

This step adds `faultline-anomaly-panel`, a custom panel that shows Isolation Forest results next to the ground truth. It's a hand-written AMD module, so there is **no Node/npm build step**.

What the panel shows:

* 🚦 **Status bar**: `ALERT`, `anomalous window`, `normal` or `no model`, plus the last score, the threshold and the model version.
* 📈 **Score timeline**: the score line and a dashed threshold. Red bands are debounced alerts and the purple strip is ground truth. Click a band to zoom in.
* 🎯 **Live event-level quality** for the visible time range: precision, recall and mean detection delay.
* 🔍 **"Why" sidebar**: the top features by robust z-score (for example `ratio_5xx z=+14`).
```bash
cd ../observability
docker compose up -d --force-recreate grafana

# plugins are loaded when Grafana starts
docker compose logs grafana | grep -i faultline

# expect "Plugin registered" (the unsigned warning is normal)
```

Open <http://localhost:3000> → **Anomaly Stand → Anomaly Detection (Isolation Forest)**. Start chaos from the shop UI or wait for a scheduled scenario. Red bands (alerts) should cover the purple bands (ground truth).

<details>
<summary>🔌 <b>Panel data contract</b> (Prometheus queries, matched by refId)</summary>

| refId | Query | Required |
|---|---|---|
| A | `max(nginx_anomaly_score)` | yes |
| B | `max(nginx_anomaly_threshold)` | yes |
| C | `max(nginx_anomaly_alert)` | yes |
| D | `max(stand_ground_truth_anomaly_active) or vector(0)` | no |
| E | `max by (feature) (nginx_anomaly_feature_zscore)` (instant) | no |
| F | `nginx_anomaly_model_info` (instant) | no |

</details>

🎉 **The whole stack is now running.**

---

## 🌐 Ports and URLs

| Service | Address | Exposed on host |
|---|---|:---:|
| 🛒 Demo Shop UI / API | <http://localhost:8080> | ✅ |
| 📘 Swagger | <http://localhost:8080/docs> | ✅ |
| 📊 Grafana | <http://localhost:3000> | ✅ |
| 🔥 Prometheus | <http://localhost:9090> | ✅ |
| 🧠 ml-detector | <http://localhost:9200> | ✅ |
| app | `app:8000` | internal |
| nginx stub_status | `nginx:8081` | internal |
| nginx-log-exporter / gt-annotator / nginx-exporter | `:9102` / `:9103` / `:9113` | internal |

---

## 💥 Anomaly scenarios

| Scenario | Mechanism | Log signature |
|---|---|---|
| 🚀 `traffic_spike` | Client rate × 4–8 | Request count ↑ |
| 🐢 `latency_degradation` | +0.8–2.5 s on `/api/products*` | `request_time` ↑, possible 504s |
| 💣 `error_burst` | 20–50 % of API calls return 500/503 | 5xx ratio ↑ |
| 👻 `not_found_storm` | +10–25 rps to missing resources | 4xx ratio ↑, new URIs |
| 🐘 `heavy_payload` | Catalog response × 20–100 | `body_bytes_sent` ↑ |

The traffic rate is `BASE_RPS × diurnal(t) × noise` with Poisson arrivals. The action mix is page load 10 %, catalog 35 %, product view 30 %, order 15 % and order check 10 %. There are also normal background errors: about 2 % product 404s and about 3 % order 422s.

---

## ⚙️ Configuration

### Business layer (`anomaly-stand/.env`)

| Variable | Default | Meaning |
|---|---|---|
| `NGINX_HTTP_PORT` | `8080` | Host port |
| `PRODUCT_COUNT` / `SEED` | `50` / `42` | Catalog size / deterministic seed |
| `BASE_RPS` | `8` | Mean load |
| `DAY_PERIOD_S` | `3600` | Length of a simulated day |
| `DIURNAL_AMPLITUDE` | `0.6` | Day/night amplitude (0..1) |
| `WARMUP_S` | `1800` | Clean period before the first anomaly |
| `ANOMALIES_ENABLED` | `true` | Turn scenarios on or off |
| `ANOMALY_MIN_GAP_S` / `ANOMALY_MAX_GAP_S` | `300` / `900` | Pause between scenarios |
| `SCENARIOS` | all | Comma-separated subset |

### Observability layer

| Variable | Effect |
|---|---|
| `LOG_START_AT=beginning` | Replays the existing access log once. Replayed samples get the current time as their timestamp |
| `GRAFANA_UNSIGNED_PLUGINS` | Allows the unsigned plugin to load |
| `FIELD_*`, `MAX_ROUTES`, `NGINX_STUB_STATUS_URL` | Exporter tuning |

### ML layer (env in `ml/docker-compose.yml`)

The trainer and the detector must use **the same values**:

`WINDOW_S` (retrain after changing it) · `BASELINE_WINDOWS` · `TRAIN_ON_CLEAN` · `N_ESTIMATORS` · `MAX_SAMPLES_GRID` · `MAX_FEATURES_GRID` · `THRESHOLD_QUANTILE` · `THRESHOLD_FLOOR_QUANTILE` · `ALERT_K` / `ALERT_N` · `SPLIT_PATTERN` · `BLOCK_WINDOWS` · `AUTO_TRAIN` · `AUTO_TRAIN_MIN_SPAN_S`

---

## 🧪 Testing
```bash
# observability (from anomaly-stand/observability)
make check-prometheus

# promtool check config
make test-rules

# promtool unit tests for recording + alert rules
make test-py

# pytest for the exporters (in python:3.12-slim)
make test

# all of the above

# ML (from anomaly-stand/ml)
make test-py

# unit + end-to-end tests on synthetic logs, in Docker (~1 min)
make test-local

# the same on the host (Python ≥ 3.11 + requirements.txt + pytest)
```

---

## 🔧 Operations
```bash
# Business layer (anomaly-stand/)
docker compose stop traffic-generator

# pause the load
docker compose restart nginx

# needed after the app container is recreated
docker compose down

# stop; logs stay in ./data
rm -rf data/

# reset collected data (deletes logs, ground truth and models)

# Observability (anomaly-stand/observability/)
make reload-prometheus

# apply rule/config edits without a restart
docker compose down

# stop, keep volumes
docker compose down -v

# stop and wipe the Prometheus TSDB + Grafana DB

# ML (anomaly-stand/ml/)
make prepare | make fit | make evaluate

# run pipeline stages one at a time
make reload

# force the detector to reload the model
make logs

# follow ml-detector logs
make down

# stop the detector
```

> [!CAUTION]
> To shut everything down, stop the layers in **reverse order** (ML → observability → business). Step 1 owns the `anomaly-stand-net` network.

---

## 🩺 Troubleshooting

| Symptom | Fix |
|---|---|
| `network anomaly-stand-net declared as external, but could not be found` | Start step 1 first |
| `nginx-logs` target is UP but all rates are 0 | Check that `data/nginx-logs/access.json.log` is growing. Check `nginx_log_parse_errors_total` and set `FIELD_*` if needed |
| `nginx` target is DOWN | stub_status must listen on `nginx:8081`. Check `NGINX_STUB_STATUS_URL` |
| No ground-truth annotations | No anomaly has been injected yet (warm-up is 1800 s by default). Check `docker compose logs gt-annotator` |
| A changed Grafana password has no effect | The password is applied only on first start. Run `docker compose down -v` |
| `model_loaded=false` for a long time | The log is shorter than `AUTO_TRAIN_MIN_SPAN_S`. Run `make train` |
| `parse_errors_total` keeps growing | The nginx `json_analytics` format changed. Update `logs.parse_line` |
| `Model window_s != WINDOW_S` | You changed the window size. Retrain |
| Too many ML alerts / missed incidents | Raise / lower `THRESHOLD_QUANTILE` or `ALERT_K` |
| *Panel plugin not found* | Check `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=faultline-anomaly-panel`, then recreate Grafana |
| Panel says *"No model loaded"* | Run `make train` in `ml/` |
| Panel is empty | Check `targets/ml-detector.yml` and `up{job="ml-detector"}` |
| Edits to `module.js` don't show up | Hard-reload the browser (Ctrl+Shift+R) |

<details>
<summary>⚠️ <b>Known limitations</b></summary>

* No TLS and no auth. The chaos API is open.
* No log rotation, so clean up `data/` from time to time.
* App state is in memory, so a restart resets orders.
* The app runs a single uvicorn worker because chaos state is per process.
* The Grafana plugin is unsigned. For a real product, rewrite it with `npx @grafana/create-plugin` (TypeScript) and sign it.

</details>

---

## 📜 License

Distributed under the [Apache License 2.0](LICENSE).

<div align="center">

**Made with ☕ and 🌲🌲🌲 Isolation Forests**

</div>