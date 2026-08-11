# spiderx-agents — eval + dev convenience targets.
# Evals are the regression gate: offline (no deps) + online (needs a server).
PY ?= .venv/bin/python
BASE ?= http://localhost:8765

.PHONY: eval eval-offline eval-online eval-scenario install-hooks serve

## Run the full gate: offline unit suite + online API suite.
eval: eval-offline eval-online

## Offline unit suite — voice pipeline, fish_audio shape, build lockstep, imports.
## No server / DB / keys. This is what CI + the pre-push hook always run.
eval-offline:
	$(PY) -W ignore tests/test_offline.py

## Online API/WS suite against a running server (snapshot+restore on mutations).
eval-online:
	BASE=$(BASE) $(PY) tests/eval_suite.py

## Online suite + the live-chat WebSocket scenario (needs GEMINI_API_KEY).
eval-scenario:
	BASE=$(BASE) $(PY) tests/eval_suite.py --scenario

## Wire the pre-push regression gate (.githooks/pre-push).
install-hooks:
	git config core.hooksPath .githooks
	@echo "✅ pre-push eval gate installed (bypass with: git push --no-verify)"

## Start the local dev server.
serve:
	$(PY) -m uvicorn backend.app:app --host 127.0.0.1 --port 8765 --reload
