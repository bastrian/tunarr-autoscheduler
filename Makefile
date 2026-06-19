.PHONY: install run docker-build docker-run docker-deploy docker-test docker-lint docker-typecheck lint typecheck test clean

install:
	pip install -e ".[dev]"

run:
	python -m tunarr_autoscheduler.main

docker-build:
	docker build --target runtime -t tunarr-autoscheduler .

docker-run:
	docker compose up -d

docker-deploy:
	docker stack deploy -c docker-stack.yml tunarr-autoscheduler

docker-logs:
	docker compose logs -f

docker-test:
	docker compose --profile test run --rm scheduler-test

docker-lint:
	docker compose --profile test run --rm scheduler-test python -m ruff check tunarr_autoscheduler tests

docker-typecheck:
	docker compose --profile test run --rm scheduler-test python -m mypy tunarr_autoscheduler

lint:
	ruff check tunarr_autoscheduler tests

typecheck:
	mypy tunarr_autoscheduler

test:
	pytest tests/

sync-channels:
	docker exec tunarr-autoscheduler python -m tunarr_autoscheduler.main sync-channels

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
