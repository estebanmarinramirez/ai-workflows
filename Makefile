PREFIX ?= $(HOME)/.local

.PHONY: install install-macos test check portability-check
install:
	./packaging/install

install-macos:
	./packaging/install-macos

test:
	./tests/run
	./tests/catalog
	./tests/manager-tree

check:
	bash -n bin/agent-workspaces lib/core.sh packaging/install packaging/migrate packaging/rollback tests/run tests/catalog tests/manager-tree
	./tests/run

portability-check:
	bash -n bin/agent-workspaces lib/core.sh packaging/install-macos compat/portable/*
	jq -e . config/config.json config/providers/*.json >/dev/null
	! rg -n '/home/[^/]+|colombus|trading-system' README.md LICENSE config bin lib packaging/install-macos compat/portable
