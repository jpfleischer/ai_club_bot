.PHONY: up down build logs shell stop

# Bring up both Postgres and the bot in the background
up:
	docker compose up -d

# Build (or rebuild) the images
build:
	docker compose build

# Stop and remove containers
down:
	docker compose down

# Stop containers without removing them
stop:
	docker compose stop

# Follow logs from both services
logs:
	docker compose logs -f

# Shell into the bot container for debugging (assumes container name is "discordbot_bot")
shell:
	docker exec -it discordbot_bot /bin/sh

# Example for shell into the Postgres container
db-shell:
	docker exec -it discordbot_db /bin/bash
