.PHONY: up down restart logs status test dev
COMPOSE = docker compose -f docker-compose.minibooru.yml --env-file .env.minibooru

up:              ## build and start Mini Booru in Docker
	$(COMPOSE) up -d --build

down:            ## stop it (data stays)
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f --tail=100

status:
	$(COMPOSE) ps

test:            ## run the test suite
	.venv/bin/python -m pytest -q tests

dev:             ## run without Docker on http://127.0.0.1:8090
	.venv/bin/python -m minibooru serve --host 127.0.0.1 --port 8090
