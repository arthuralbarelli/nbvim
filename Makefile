.PHONY: format lint

format:
	uv run black src

lint:
	uv run black --check src
