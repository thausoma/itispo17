.PHONY: up down test

up:
	docker-compose up --build

down:
	docker-compose down

test:
	@echo "Run: make test WEEK=17 from course repo root"
