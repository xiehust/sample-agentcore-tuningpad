.PHONY: install dev verify backend frontend stop

install:
	cd backend && uv sync
	cd frontend && npm install

dev:
	./start.py

stop:
	./stop.sh

backend:
	cd backend && uv run uvicorn app.main:app --reload --port $${TUNINGPAD_PORT:-8100}

frontend:
	cd frontend && npm run dev

verify:
	./scripts/verify.sh
