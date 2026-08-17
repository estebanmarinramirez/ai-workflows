PREFIX ?= $(HOME)/.local

.PHONY: install test check
install:
	./packaging/install

test:
	./tests/run

check:
	bash -n bin/agent-workspaces lib/core.sh packaging/install packaging/migrate packaging/rollback tests/run
	./tests/run
