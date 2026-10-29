#!/usr/bin/env bash
# =============================================================================
# Faultline: one entry point for all four layers of the anomaly stand.
#
#   ./anomaly-stand.sh up [layer] [--fast]     start a layer (default: all, in order)
#   ./anomaly-stand.sh down [layer] [--volumes] stop a layer (default: all, reverse order)
#   ./anomaly-stand.sh status                  containers + HTTP health of every layer
#   ./anomaly-stand.sh check                   smoke checks from the layer READMEs
#   ./anomaly-stand.sh train                   run the ML pipeline and print the report
#   ./anomaly-stand.sh test                    promtool + pytest for observability and ML
#   ./anomaly-stand.sh logs <layer>            follow logs of one layer
#   ./anomaly-stand.sh reset [-y]              stop everything, wipe volumes and data/
#
# Layers: business (1), observability (2), ml (3), plugin (4), all.
# NOT a production tool: the stand has no TLS, no auth and default credentials.
# =============================================================================
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STAND_DIR="${ROOT_DIR}/anomaly-stand"
OBS_DIR="${STAND_DIR}/observability"
ML_DIR="${STAND_DIR}/ml"
NETWORK="anomaly-stand-net"
PLUGIN_ID="faultline-anomaly-panel"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

# ---- colors (disabled when not a TTY or NO_COLOR is set) --------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\e[0m'; C_BOLD=$'\e[1m'; C_RED=$'\e[31m'; C_GREEN=$'\e[32m'
  C_YELLOW=$'\e[33m'; C_BLUE=$'\e[34m'; C_DIM=$'\e[2m'
else
  C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_DIM=""
fi
step() { printf '\n%s==> %s%s\n' "${C_BOLD}${C_BLUE}" "$*" "${C_RESET}"; }
info() { printf '%s    %s%s\n' "${C_DIM}" "$*" "${C_RESET}"; }
ok()   { printf '%s  ✔ %s%s\n' "${C_GREEN}" "$*" "${C_RESET}"; }
warn() { printf '%s  ! %s%s\n' "${C_YELLOW}" "$*" "${C_RESET}" >&2; }
die()  { printf '%s  ✘ %s%s\n' "${C_RED}" "$*" "${C_RESET}" >&2; exit 1; }
trap 'printf "%s  ✘ failed at line %s: %s%s\n" "${C_RED}" "$LINENO" "$BASH_COMMAND" "${C_RESET}" >&2' ERR

# ---- settings: shell env > layer .env file > compose default ----------------
env_from() {  # env_from <file> <key>: last value of KEY=... in a .env file
  [[ -f "$1" ]] || return 0
  sed -n "s/^[[:space:]]*$2=//p" "$1" | tail -n1 | tr -d "\"'\r"
}
NGINX_HTTP_PORT="${NGINX_HTTP_PORT:-$(env_from "${STAND_DIR}/.env" NGINX_HTTP_PORT)}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-$(env_from "${OBS_DIR}/.env" PROMETHEUS_PORT)}"
GRAFANA_PORT="${GRAFANA_PORT:-$(env_from "${OBS_DIR}/.env" GRAFANA_PORT)}"
GRAFANA_ADMIN_USER="${GRAFANA_ADMIN_USER:-$(env_from "${OBS_DIR}/.env" GRAFANA_ADMIN_USER)}"
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-$(env_from "${OBS_DIR}/.env" GRAFANA_ADMIN_PASSWORD)}"
ML_PORT="${ML_PORT:-$(env_from "${ML_DIR}/.env" ML_PORT)}"
NGINX_HTTP_PORT="${NGINX_HTTP_PORT:-8080}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
GRAFANA_ADMIN_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-admin}"
ML_PORT="${ML_PORT:-9200}"

SHOP_URL="http://localhost:${NGINX_HTTP_PORT}"
PROM_URL="http://localhost:${PROMETHEUS_PORT}"
GRAFANA_URL="http://localhost:${GRAFANA_PORT}"
ML_URL="http://localhost:${ML_PORT}"

# The default observability compose leaves the unsigned-plugin list empty.
# Export it so Grafana always loads the step-4 panel.
GRAFANA_UNSIGNED_PLUGINS="${GRAFANA_UNSIGNED_PLUGINS:-$(env_from "${OBS_DIR}/.env" GRAFANA_UNSIGNED_PLUGINS)}"
export GRAFANA_UNSIGNED_PLUGINS="${GRAFANA_UNSIGNED_PLUGINS:-$PLUGIN_ID}"

# ---- helpers ----------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

require_tools() {
  have docker || die "docker is not installed"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 ('docker compose') is required"
  docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you in the docker group?)"
  have curl || die "curl is required"
  have make || die "make is required"
  have jq || warn "jq not found: some checks will print raw JSON"
}

biz()     { (cd "$STAND_DIR" && docker compose "$@"); }
obs()     { (cd "$OBS_DIR" && docker compose "$@"); }
obs_make() { make --no-print-directory -C "$OBS_DIR" "$@"; }
ml_make()  { make --no-print-directory -C "$ML_DIR" "$@"; }

http_ok() { curl -fsS -o /dev/null --max-time 3 "$@"; }

wait_http() {  # wait_http <name> <url> [extra curl args]
  local name=$1 url=$2; shift 2
  local deadline=$((SECONDS + WAIT_TIMEOUT))
  printf '    waiting for %s ' "$name"
  until http_ok "$@" "$url"; do
    if (( SECONDS >= deadline )); then
      printf '\n'; die "$name is not ready after ${WAIT_TIMEOUT}s ($url)"
    fi
    printf '.'; sleep 2
  done
  printf '\n'; ok "$name is up: $url"
}

need_network() {
  docker network inspect "$NETWORK" >/dev/null 2>&1 \
    || die "network '$NETWORK' not found. Start the business layer first: $0 up business"
}

pretty() { if have jq; then jq "$@"; else cat; echo; fi; }

# ---- up ---------------------------------------------------------------------
up_business() {
  step "Step 1/4: business layer (nginx, Demo Shop, traffic generator)"
  if [[ "$FAST" == 1 ]]; then
    export WARMUP_S="${WARMUP_S:-60}" ANOMALY_MIN_GAP_S="${ANOMALY_MIN_GAP_S:-60}" \
           ANOMALY_MAX_GAP_S="${ANOMALY_MAX_GAP_S:-120}"
    info "fast mode: WARMUP_S=$WARMUP_S, gaps ${ANOMALY_MIN_GAP_S}-${ANOMALY_MAX_GAP_S}s"
  fi
  biz up -d --build
  wait_http "Demo Shop" "${SHOP_URL}/api/health"
}

up_observability() {
  step "Step 2/4: observability layer (Prometheus, Grafana, exporters)"
  need_network
  obs_make up
  wait_http "Prometheus" "${PROM_URL}/-/ready"
  wait_http "Grafana" "${GRAFANA_URL}/api/health"
}

up_ml() {
  step "Step 3/4: ML layer (Isolation Forest detector)"
  need_network
  http_ok "${PROM_URL}/-/ready" || warn "Prometheus is not running: the detector will start but nobody scrapes it"
  ml_make up
  wait_http "ml-detector" "${ML_URL}/health"
  info "no model yet? it trains itself after ~1 simulated day, or run: $0 train"
}

up_plugin() {
  step "Step 4/4: Grafana plugin (${PLUGIN_ID})"
  [[ -f "${OBS_DIR}/grafana/plugins/${PLUGIN_ID}/plugin.json" ]] \
    || die "plugin not found in ${OBS_DIR}/grafana/plugins/${PLUGIN_ID}"
  need_network
  info "GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=${GRAFANA_UNSIGNED_PLUGINS}"
  obs up -d --force-recreate grafana   # plugins are loaded only at Grafana start
  wait_http "Grafana" "${GRAFANA_URL}/api/health"
  if http_ok -u "${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASSWORD}" \
       "${GRAFANA_URL}/api/plugins/${PLUGIN_ID}/settings"; then
    ok "plugin registered"
  else
    warn "Grafana did not register ${PLUGIN_ID}. Recent plugin log lines:"
    obs logs grafana 2>&1 | grep -i "$PLUGIN_ID" | tail -n 5 >&2 || true
  fi
}

summary() {
  step "Faultline is running"
  printf '    %-22s %s\n' \
    "Demo Shop UI"  "${SHOP_URL}" \
    "Swagger"       "${SHOP_URL}/docs" \
    "Grafana"       "${GRAFANA_URL}  (${GRAFANA_ADMIN_USER} / ${GRAFANA_ADMIN_PASSWORD})" \
    "Prometheus"    "${PROM_URL}/targets" \
    "ml-detector"   "${ML_URL}/api/status"
  info "Dashboard: Anomaly Stand -> Anomaly Detection (Isolation Forest)"
}

# ---- down -------------------------------------------------------------------
down_business()      { step "Stopping business layer";      biz down; }
down_observability() { step "Stopping observability layer"; obs down ${VOLUMES:+-v}; }
down_ml()            { step "Stopping ML layer";            ml_make down; }

# ---- status / check ---------------------------------------------------------
probe() {  # probe <name> <url> [extra curl args]
  local name=$1 url=$2; shift 2
  if http_ok "$@" "$url"; then ok "$(printf '%-16s %s' "$name" "$url")"
  else printf '%s  ✘ %-16s %s%s\n' "${C_RED}" "$name" "$url" "${C_RESET}"; fi
}

cmd_status() {
  step "Containers"
  biz ps 2>/dev/null || true
  obs ps 2>/dev/null || true
  (cd "$ML_DIR" && docker compose ps 2>/dev/null) || true
  step "HTTP health"
  probe "Demo Shop"   "${SHOP_URL}/api/health"
  probe "Prometheus"  "${PROM_URL}/-/ready"
  probe "Grafana"     "${GRAFANA_URL}/api/health"
  probe "ml-detector" "${ML_URL}/health"
  probe "plugin"      "${GRAFANA_URL}/api/plugins/${PLUGIN_ID}/settings" \
        -u "${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASSWORD}"
}

cmd_check() {
  local log="${STAND_DIR}/data/nginx-logs/access.json.log"
  local gt="${STAND_DIR}/data/ground-truth/anomalies.jsonl"
  step "Business layer"
  curl -fsS "${SHOP_URL}/api/health" | pretty -c .
  curl -fsS "${SHOP_URL}/api/products?limit=2" | pretty -c '.'
  if [[ -f "$log" ]]; then info "access log lines: $(wc -l <"$log") (grows by ~8/s)"
  else warn "no access log yet: $log"; fi
  if [[ -s "$gt" ]]; then info "injected anomalies so far: $(wc -l <"$gt")"
  else info "no anomalies injected yet (warm-up)"; fi
  info "app /metrics (first lines):"
  biz exec -T app python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode()[:400])" || true

  if http_ok "${PROM_URL}/-/ready"; then
    step "Observability layer"
    info "Prometheus targets:"
    if have jq; then
      curl -fsS "${PROM_URL}/api/v1/targets" \
        | jq -r '.data.activeTargets[] | "      \(.labels.job)\t\(.health)"' | sort -u
    else curl -fsS "${PROM_URL}/api/v1/targets"; echo; fi
    info "stand:requests:rate1m:"
    curl -fsS "${PROM_URL}/api/v1/query" --data-urlencode 'query=stand:requests:rate1m' \
      | pretty -c '.data.result[0].value'
  else warn "Prometheus is not running, skipping observability checks"; fi

  if http_ok "${ML_URL}/health"; then
    step "ML layer"
    curl -fsS "${ML_URL}/api/status" | pretty .
  else warn "ml-detector is not running, skipping ML checks"; fi
}

# ---- other commands ---------------------------------------------------------
cmd_train() {
  step "Training Isolation Forest (prepare -> train -> test)"
  need_network
  ml_make train
  ml_make report
}

cmd_test() {
  step "Observability tests (promtool + pytest)"
  obs_make test
  step "ML tests (pytest in Docker)"
  ml_make test-py
  ok "all tests passed"
}

cmd_logs() {
  case "$LAYER" in
    business)      biz logs -f --tail=100 ;;
    observability|plugin) obs_make logs ;;
    ml)            ml_make logs ;;
    *)             die "logs needs one layer: business | observability | ml" ;;
  esac
}

cmd_reset() {
  if [[ "$YES" != 1 ]]; then
    printf '%sThis stops every layer, deletes Prometheus/Grafana volumes and %s (logs, ground truth, models). Continue? [y/N] %s' \
      "${C_YELLOW}" "${STAND_DIR}/data" "${C_RESET}"
    local answer; read -r answer
    [[ "$answer" =~ ^[Yy]$ ]] || { info "aborted"; return 0; }
  fi
  VOLUMES=1
  down_ml; down_observability; down_business
  step "Removing ${STAND_DIR}/data"
  rm -rf -- "${STAND_DIR:?}/data" \
    || die "could not delete data/ (files were created by containers as root). Run: sudo rm -rf '${STAND_DIR}/data'"
  ok "stand reset"
}

usage() { sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# ---- argument parsing -------------------------------------------------------
CMD="${1:-help}"; [[ $# -gt 0 ]] && shift
LAYER="all"; FAST=0; VOLUMES=""; YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    1|business)      LAYER=business ;;
    2|observability) LAYER=observability ;;
    3|ml)            LAYER=ml ;;
    4|plugin)        LAYER=plugin ;;
    all)             LAYER=all ;;
    --fast)          FAST=1 ;;
    -v|--volumes)    VOLUMES=1 ;;
    -y|--yes)        YES=1 ;;
    -h|--help)       usage; exit 0 ;;
    *)               die "unknown argument: $1 (see $0 help)" ;;
  esac
  shift
done

case "$CMD" in
  up)
    require_tools
    case "$LAYER" in
      all) up_business; up_observability; up_ml; up_plugin; summary ;;
      *)   "up_${LAYER}" ;;
    esac ;;
  down)
    require_tools
    case "$LAYER" in
      all)    down_ml; down_observability; down_business ;;
      plugin) die "the plugin is part of Grafana: use '$0 down observability'" ;;
      *)      "down_${LAYER}" ;;
    esac ;;
  status) require_tools; cmd_status ;;
  check)  require_tools; cmd_check ;;
  train)  require_tools; cmd_train ;;
  test)   require_tools; cmd_test ;;
  logs)   require_tools; cmd_logs ;;
  reset)  require_tools; cmd_reset ;;
  help|-h|--help) usage ;;
  *) usage; die "unknown command: $CMD" ;;
esac
