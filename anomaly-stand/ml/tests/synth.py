"""
Synthetic Demo-Shop traffic for tests: normal traffic + injected OPERATIONAL anomalies
(no security scenarios), in the same shapes the pipeline consumes:
  * records DataFrame with logs.RECORD_COLUMNS   (unit tests, no parser involved)
  * nginx JSON lines + ground-truth JSONL        (end-to-end test through parse_line)
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from anomaly.logs import RECORD_COLUMNS, normalize_route

WINDOW_S = 15
T0 = 1_759_999_995            # divisible by 15 -> windows aligned with the stream
SCENARIOS = ("error_burst", "latency_spike", "traffic_spike", "broken_links")

# (method, path template, proxied to API?, traffic weight)
ROUTES = [
    ("GET", "/", False, 0.15), ("GET", "/static/app.js", False, 0.10), ("GET", "/static/style.css", False, 0.10),
    ("GET", "/api/products", True, 0.30), ("GET", "/api/products/{id}", True, 0.20),
    ("POST", "/api/orders", True, 0.10), ("GET", "/api/orders/{id}", True, 0.05),
]


def make_ground_truth(start: float, duration_s: float, every_s: int = 900,
                      length_s: int = 180, first_s: int = 600) -> list[dict]:
    """One scenario every `every_s` seconds, cycling through SCENARIOS."""
    gt, i, t = [], 0, start + first_s
    while t + length_s < start + duration_s:
        gt.append({"scenario": SCENARIOS[i % len(SCENARIOS)], "start_ts": float(t), "end_ts": float(t + length_s)})
        i, t = i + 1, t + every_s
    return gt


def synth_records(start: float, duration_s: int, gt=(), rps: float = 4.0, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    weights = np.array([r[3] for r in ROUTES])
    weights /= weights.sum()
    rows = []
    for sec in range(int(duration_s)):
        t0 = start + sec
        kinds = {g["scenario"] for g in gt if g["start_ts"] <= t0 < g["end_ts"]}
        lam = rps * (4.0 if "traffic_spike" in kinds else 1.0)
        for _ in range(rng.poisson(lam)):
            method, tmpl, api, _ = ROUTES[rng.choice(len(ROUTES), p=weights)]
            path = tmpl.replace("{id}", str(int(rng.integers(1, 50))))
            status = 201 if method == "POST" else 200
            if api:
                rt, nbytes = float(rng.lognormal(math.log(0.025), 0.35)), int(rng.normal(1800, 300))
            else:
                rt, nbytes = float(rng.lognormal(math.log(0.002), 0.3)), int(rng.normal(6000, 500))
            if "error_burst" in kinds and api and rng.random() < 0.5:      # backend failing
                status, nbytes = 500, 60
            if "latency_spike" in kinds and api:                           # slow backend
                rt *= 30
            if "broken_links" in kinds and rng.random() < 0.5:             # bad deploy: missing assets
                method, path, api, status, nbytes = "GET", f"/static/build-{int(rng.integers(1e6))}.js", False, 404, 153
            rows.append({
                "ts": t0 + float(rng.random()), "method": method, "path": path, "route": normalize_route(path),
                "status": status, "bytes": max(nbytes, 0),
                "req_len": int(rng.normal(450 if method == "GET" else 900, 40)),
                "rt": rt, "upstream_rt": rt * 0.95 if api else math.nan,
                "client": f"10.10.0.{int(rng.integers(1, 60))}", "conn_req": int(rng.integers(1, 12)),
            })
    return (pd.DataFrame(rows, columns=RECORD_COLUMNS)
            .sort_values("ts", kind="stable").reset_index(drop=True))


def to_log_line(r: dict) -> str:
    """One record -> nginx JSON line.
    Keys mirror nginx variable names. If `json_analytics` in business/nginx/nginx.conf uses
    different keys, adjust them here: test_parse_line_contract will tell you."""
    up = r["upstream_rt"]
    up = "-" if (up is None or (isinstance(up, float) and math.isnan(up))) else f"{up:.3f}"
    return json.dumps({
        "msec": f"{r['ts']:.3f}",
        "time_iso8601": pd.Timestamp(r["ts"], unit="s", tz="UTC").isoformat(),
        "remote_addr": "172.20.0.10",
        "http_x_forwarded_for": r["client"], "x_forwarded_for": r["client"],
        "request_method": r["method"], "request_uri": r["path"], "uri": r["path"],
        "status": int(r["status"]), "body_bytes_sent": int(r["bytes"]), "request_length": int(r["req_len"]),
        "request_time": f"{r['rt']:.3f}", "upstream_response_time": up,
        "connection_requests": int(r["conn_req"]),
    })


def write_stand_files(root: Path, df: pd.DataFrame, gt: list[dict]) -> tuple[Path, Path]:
    """Lay files out like the stand's ../data directory."""
    log = root / "nginx-logs" / "access.json.log"
    gtf = root / "ground-truth" / "anomalies.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    gtf.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as fh:
        for r in df.to_dict("records"):
            fh.write(to_log_line(r) + "\n")
    with open(gtf, "w", encoding="utf-8") as fh:
        for g in gt:
            fh.write(json.dumps(g) + "\n")
    return log, gtf


def windows_of(df: pd.DataFrame, window_s: int = WINDOW_S) -> list[tuple[float, list[dict]]]:
    """Complete windows (first/last partial dropped, empty kept) -> [(window_start, records)]."""
    idx = (df["ts"].to_numpy() // window_s).astype(int)
    recs = df.to_dict("records")
    out, j = [], 0
    for k in range(idx[0] + 1, idx[-1]):
        while j < len(idx) and idx[j] < k:
            j += 1
        lo = j
        while j < len(idx) and idx[j] == k:
            j += 1
        out.append((float(k * window_s), recs[lo:j]))
    return out