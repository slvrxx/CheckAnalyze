.PHONY: install lint test bot imap

install:
	pip install -r requirements.txt

lint:
	ruff check .
	black --check --line-length=100 .
	isort --check-only --profile=black --line-length=100 .
	mypy src

test:
	pytest

bot:
	python -m checkanalyze.cli telegram

imap:
	python -m checkanalyze.imap_fetcher
