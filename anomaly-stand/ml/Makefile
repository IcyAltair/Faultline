# Helper targets for the ML layer (run from anomaly-stand/ml)
COMPOSE ?= docker compose
ML_URL  ?= http://localhost:9200
TARGETS := ../observability/prometheus/targets

.PHONY: up down logs train prepare fit evaluate report reload status alerts \
        scrape-on scrape-off test-py test-local

up: scrape-on  ## build + start the online detector and enable Prometheus scraping
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100 ml-detector

train:         ## full pipeline: prepare -> train -> test (detector hot-reloads the model)
	$(COMPOSE) run --rm ml-trainer

prepare:       ## individual steps
	$(COMPOSE) run --rm ml-trainer python -m anomaly.pipeline prepare
fit:
	$(COMPOSE) run --rm ml-trainer python -m anomaly.pipeline train
evaluate:
	$(COMPOSE) run --rm ml-trainer python -m anomaly.pipeline test

report:        ## last test report
	cat ../data/ml/reports/latest.md

reload:        ## force model reload in the detector
	curl -fsS -X POST $(ML_URL)/api/reload && echo

status:        ## detector + model state
	curl -fsS $(ML_URL)/api/status && echo

alerts:        ## debounced alert intervals
	curl -fsS $(ML_URL)/api/alerts && echo

scrape-on:     ## let step-2 Prometheus scrape the detector (file_sd, no restart)
	@cp -n $(TARGETS)/ml-detector.yml.example $(TARGETS)/ml-detector.yml 2>/dev/null || true
	@echo "Prometheus target enabled: $(TARGETS)/ml-detector.yml"

scrape-off:
	rm -f $(TARGETS)/ml-detector.yml

test-py:       ## unit + end-to-end tests on synthetic logs (in Docker)
	$(COMPOSE) run --rm ml-trainer sh -c "pip install -q pytest==8.3.4 && pytest -q /srv/tests"

test-local:    ## same, on the host (python>=3.11 + requirements.txt + pytest)
	cd .. && python -m pytest -q ml/tests