# Step 4 — Grafana plugin: Faultline Anomaly Panel

A custom **panel plugin** (`faultline-anomaly-panel`) that shows Isolation Forest results from step 3
next to the ground truth injected in step 1. NOT a production setup: the plugin is unsigned.

## What the panel shows

* **Status bar**: current state (`ALERT` / `anomalous window` / `normal` / `no model`), last score and threshold, loaded model version.
* **Score timeline** (SVG): score line, dashed threshold, red bands = k-of-n debounced alerts,
  purple strip = ground-truth scenarios. Hover for values; click a band to zoom into it.
* **Event-level quality for the visible time range**: alerts, precision (alerts that overlap a GT window),
  recall (GT windows caught), mean detection delay. Live check of the model without opening reports.
* **"Why" sidebar**: top-N features by robust z-score vs clean training data (e.g. `ratio_5xx z=+14`).

## Data contract (Prometheus, matched by refId; editable in panel options)

| refId | Query | Required |
|---|---|---|
| A | `max(nginx_anomaly_score)` | yes |
| B | `max(nginx_anomaly_threshold)` | yes |
| C | `max(nginx_anomaly_alert)` | yes |
| D | `max(stand_ground_truth_anomaly_active) or vector(0)` | no |
| E | `max by (feature) (nginx_anomaly_feature_zscore)` (instant) | no |
| F | `nginx_anomaly_model_info` (instant) | no |

## Why no build step

`module.js` is a hand-written AMD module (the format the official webpack toolchain emits)
using `React.createElement` and the `react` / `@grafana/data` / `@grafana/ui` modules Grafana already ships.
So there's no Node/npm, nothing to compile, and editing the file + reloading the browser is the dev loop.
For a real product you'd rewrite it with `npx @grafana/create-plugin` (TypeScript) and sign it.

## Files

```
observability/
├── docker-compose.yml                         # UPDATED: allow unsigned faultline-anomaly-panel, plugins mounted :ro
└── grafana/
    ├── plugins/faultline-anomaly-panel/
    │   ├── plugin.json                        # plugin manifest (type: panel, Grafana >= 10.4)
    │   ├── module.js                          # the panel (AMD, ~17 KB, commented)
    │   └── img/logo.svg
    └── dashboards/stand/anomaly-detection.json  # provisioned dashboard "Anomaly Detection (Isolation Forest)"
```

The dashboard also has two annotation layers: `ground-truth` (written by gt-annotator)
and `ML alert` (Prometheus `ALERTS{alertname="MLAnomalyDetected"}`), so you can see alerts on the other panels too.

## Run

```bash
# steps 1-3 running
cd anomaly-stand/observability
docker compose up -d --force-recreate grafana   # plugin is loaded at Grafana start
docker compose logs grafana | grep -i faultline   # expect: "Plugin registered" (warning about unsigned is normal)
```
Open http://localhost:3000 → *Anomaly Stand → Anomaly Detection (Isolation Forest)*.
Trigger chaos from the business UI or wait for the traffic generator's scenarios; red bands should cover the purple ones.

## Troubleshooting

* *Panel plugin not found*: check `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=faultline-anomaly-panel`, then recreate Grafana.
* *"No model loaded"*: the detector has no model yet. Run `make train` in `ml/`.
* *Empty panel*: Prometheus doesn't scrape the detector. Check `targets/ml-detector.yml` and `up{job="ml-detector"}`.
* After editing `module.js`: hard-reload the browser (Ctrl+Shift+R); Grafana caches plugin modules.
