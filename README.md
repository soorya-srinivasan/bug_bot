Bug Bot

 To run locally

  # Terminal 1: Start infrastructure
  docker compose up -d

  # Terminal 2: Install + run migrations
  pip install -e ".[dev]"
  alembic upgrade head

  # Terminal 3: Start FastAPI
  uvicorn bug_bot.main:app --reload --port 8000

  # Terminal 4: Start Temporal Worker
  python -m bug_bot.worker

  psql --host=localhost --port=5433 --username=bugbot --dbname=bugbot


