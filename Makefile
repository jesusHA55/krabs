.PHONY: help test backtest paper doctor status rust lint clean

PY ?= python3
export PYTHONPATH := src

help:
	@echo "Krabs targets:"
	@echo "  make test       run the unit test suite (stdlib only)"
	@echo "  make backtest   offline synthetic backtest"
	@echo "  make sample-data    download sector-divided daily bars"
	@echo "  make backtest-daily backtest the daily sample"
	@echo "  make pearl-pairs backtest the Pearl OLS pairs example"
	@echo "  make pearl-johansen backtest the Pearl Johansen example"
	@echo "  make etf-pairs  backtest the sector ETF pairs strategy"
	@echo "  make optimize   search parameters with holdout (DE)"
	@echo "  make sweep      simple grid sweep, ranked by Sharpe"
	@echo "  make paper      Alpaca paper account + live data (needs keys)"
	@echo "  make doctor     Raspberry Pi / broker pre-flight"
	@echo "  make status     kill switch, heartbeat, last run"
	@echo "  make dashboard  web dashboard on 127.0.0.1:8787"
	@echo "  make dev-service  user systemd service for Ubuntu testing"
	@echo "  make rust       build the optional Rust indicator core"
	@echo "  make lint       ruff + mypy (if installed)"

test:
	$(PY) -m unittest discover -s tests -v

backtest:
	./krabs backtest -c configs/example.toml

sample-data:
	python3 scripts/fetch_sample_data.py

backtest-daily:
	./krabs backtest -c configs/sample-daily.toml

pearl-pairs:
	./krabs backtest -c configs/pearl-pairs.toml

pearl-johansen:
	./krabs backtest -c configs/pearl-johansen.toml

etf-pairs:
	./krabs backtest -c configs/etf-pairs.toml

optimize:
	./krabs optimize -c configs/sample-daily.toml --strategy sma_cross \
		--space fast=5:30 --space slow=40:120 --space allocation=0.1:0.3 \
		--method de --trials 40 --folds 4 --holdout 0.2

sweep:
	$(PY) scripts/sweep.py --strategy sma_cross --grid fast=5,10,20 --grid slow=30,60,90 --days 30

paper:
	./krabs paper -c configs/alpaca-paper.toml

doctor:
	./krabs doctor -c configs/example.toml

status:
	./krabs status -c configs/example.toml

dashboard:
	./krabs dashboard -c configs/offline.toml --allow-controls

dev-service:
	./deploy/install-dev.sh

rust:
	./scripts/build_rust.sh

lint:
	@command -v ruff >/dev/null && ruff check src tests || echo "ruff not installed"
	@command -v mypy >/dev/null && mypy src || echo "mypy not installed"

clean:
	rm -rf reports var .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
