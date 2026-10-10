UV ?= uv

.PHONY: build install test

build:
	$(UV) build

install:
	$(UV) sync

test:
	$(UV) run pytest
