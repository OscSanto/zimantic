UV ?= uv

.PHONY: build install dev test

build:
	$(UV) build

install:
	$(UV) tool install .

dev:
	$(UV) sync

test:
	$(UV) run pytest
