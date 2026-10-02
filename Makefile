PREFIX ?= $(HOME)/.local

.PHONY: install install-macos test check portability-check gesture-transport-check
gesture-transport-check:
	python3 tests/gesture-transport
install:
	./packaging/install

install-macos:
	./packaging/install-macos

test:
	./tests/shadow
	./tests/benchmark
	./tests/sessions
	./tests/routing
	./tests/menu
	./tests/models
	./tests/layouts
	./tests/desktop-layout
	./tests/model-policy
	./tests/live-config
	./tests/assignments
	./tests/agent-controls
	./tests/empty-repository
	./tests/gestures
	python3 tests/gesture-camera
	./tests/run
	./tests/catalog
	./tests/manager-tree

check: test
	bash -n bin/agent-workspaces lib/core.sh packaging/install packaging/migrate packaging/rollback tests/run tests/catalog tests/manager-tree

portability-check:
	command -v rg >/dev/null
	bash -n bin/agent-workspaces lib/core.sh packaging/install-macos compat/portable/*
	jq -e . config/config.json config/providers/*.json >/dev/null
	! rg -n '/home/[^/]+|colombus|trading-system' README.md LICENSE config bin lib packaging/install-macos compat/portable
