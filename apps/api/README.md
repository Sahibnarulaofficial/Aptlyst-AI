# Aptlyst AI – Backend API

FastAPI + PostgreSQL + Alembic foundation.

## Quick start

```bash
# 1. Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and set DATABASE_URL

# 4. Run database migrations
alembic upgrade head

# 5. Start the API
uvicorn app.main:app --reload

# 6. Run tests
pytest
```

## Endpoints

| Method | Path          | Description                       |
|--------|---------------|-----------------------------------|
| GET    | /health       | API liveness check                |
| GET    | /health/db    | PostgreSQL connectivity check     |
| GET    | /docs         | Swagger UI (development only)     |
