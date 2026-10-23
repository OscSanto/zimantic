UV ?= uv
PYTHON ?= python3
VERSION ?=
DEPLOY_HOST ?= 192.168.1.219
DEPLOY_PATH ?= /opt/iiab/zimantic

.PHONY: build install dev test release release-dry-run deploy release-deploy

build:
	$(UV) build

install:
	$(UV) tool install .

dev:
	$(UV) sync

test:
	$(UV) run pytest

release: test
	UV="$(UV)" $(PYTHON) scripts/release.py $(if $(VERSION),--version $(VERSION),)

release-dry-run:
	UV="$(UV)" $(PYTHON) scripts/release.py --dry-run $(if $(VERSION),--version $(VERSION),)

deploy:
	DEPLOY_HOST="$(DEPLOY_HOST)" DEPLOY_PATH="$(DEPLOY_PATH)" VERSION="$(VERSION)" \
		./scripts/deploy.sh

release-deploy: release
	$(MAKE) deploy VERSION="$(VERSION)"
