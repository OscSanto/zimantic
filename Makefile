UV ?= uv
PYTHON ?= python3
VERSION ?=
DEV ?=
DEPLOY_HOST ?= 192.168.1.219
DEPLOY_PATH ?= /opt/iiab/zimantic

.PHONY: build install dev test release release-dry-run deploy release-deploy

build:
	$(UV) build --clear

install:
	$(UV) tool install .

dev:
	$(UV) sync
	$(UV) run python -m zimantic serve & \
		server_pid=$$!; \
		trap 'kill $$server_pid 2>/dev/null || true' EXIT INT TERM; \
		$(UV) run python -m webbrowser http://127.0.0.1:8090/; \
		wait $$server_pid

test:
	$(UV) run pytest

release: test
	UV="$(UV)" $(PYTHON) scripts/release.py $(if $(VERSION),--version $(VERSION),)

release-dry-run:
	UV="$(UV)" $(PYTHON) scripts/release.py --dry-run $(if $(VERSION),--version $(VERSION),)

deploy:
	DEPLOY_HOST="$(DEPLOY_HOST)" DEPLOY_PATH="$(DEPLOY_PATH)" VERSION="$(VERSION)" \
		./scripts/deploy.sh $(if $(DEV),--dev,)

release-deploy: release
	$(MAKE) deploy VERSION="$(VERSION)"
