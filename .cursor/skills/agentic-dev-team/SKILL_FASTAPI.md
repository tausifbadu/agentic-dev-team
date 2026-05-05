---
name: principal-fastapi-developer
description: >-
  Principal-level Python FastAPI engineering guidelines for building
  production-grade APIs. Covers project structure, Pydantic models, endpoint
  design, error handling, testing, security, and performance. Use when building,
  reviewing, or refactoring FastAPI backends, REST APIs, or Python web services.
---

# Principal Python FastAPI Developer

Expert-level FastAPI engineering guidelines. Apply when building, reviewing, or refactoring any FastAPI backend code.

## Architecture Principles

### Project Structure

```
backend/
├── main.py               # FastAPI app factory, lifespan, middleware
├── routers/              # Route modules grouped by domain
│   ├── items.py
│   ├── stats.py
│   └── health.py
├── models/               # Pydantic schemas (request/response)
│   ├── item.py
│   └── common.py
├── services/             # Business logic layer
│   └── item_service.py
├── repositories/         # Data access layer
│   └── item_repo.py
├── dependencies.py       # Shared FastAPI dependencies
├── config.py             # Settings via pydantic-settings
├── exceptions.py         # Custom exception classes + handlers
├── requirements.txt
└── tests/
    ├── conftest.py       # Shared fixtures
    ├── test_items.py
    └── test_stats.py
```

### Layered Architecture

```
Router (HTTP) → Service (Business Logic) → Repository (Data Access)
```

- **Routers** handle HTTP concerns: parse requests, return responses, set status codes
- **Services** contain business rules: validation, calculations, orchestration
- **Repositories** handle data: storage, retrieval, queries

For simple apps (in-memory storage, few endpoints), collapsing layers into `main.py` is acceptable. Scale to separate files when complexity grows.

## FastAPI App Setup

### Application Factory

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize resources
    app.state.db = {}
    yield
    # Shutdown: cleanup resources


def create_app() -> FastAPI:
    app = FastAPI(
        title="My API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(items_router)
    app.include_router(stats_router)
    return app

app = create_app()
```

### CORS

Always configure CORS middleware. For local development, `allow_origins=["*"]` is acceptable. For production, specify exact origins.

## Pydantic Models

### Request/Response Separation

Always define separate models for requests and responses:

```python
from pydantic import BaseModel, EmailStr, Field
from enum import Enum


class ItemStatus(str, Enum):
    pending = "pending"
    active = "active"
    completed = "completed"
    archived = "archived"


class ItemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    category: str = Field(..., min_length=1)


class ItemUpdate(BaseModel):
    status: ItemStatus


class ItemResponse(BaseModel):
    id: str
    title: str
    description: str
    category: str
    status: ItemStatus
    created_at: str
```

### Model Rules

- Use `str, Enum` for string enums (auto-serializes to string)
- Use `Field(...)` for required fields with validation constraints
- Use `Field(default=...)` for optional fields with defaults
- Never expose internal data structures — always map to response models
- Use `response_model` on every endpoint to document and enforce the shape

### Validation

- Use Pydantic's built-in validators: `min_length`, `max_length`, `ge`, `le`, `pattern`
- Use `@field_validator` for complex custom validation
- Validation errors automatically return 422 with field-level details

```python
from pydantic import field_validator

class ItemCreate(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        if len(v.strip()) < 2:
            raise ValueError("Title must be at least 2 characters")
        return v.strip()
```

## Endpoint Design

### RESTful Conventions

| Operation | Method | Path | Status Code |
|-----------|--------|------|-------------|
| Create | POST | `/api/items` | 201 |
| List | GET | `/api/items` | 200 |
| Get one | GET | `/api/items/{id}` | 200 |
| Full update | PUT | `/api/items/{id}` | 200 |
| Partial update | PATCH | `/api/items/{id}` | 200 |
| Delete | DELETE | `/api/items/{id}` | 204 |

### Endpoint Implementation

```python
from fastapi import APIRouter, HTTPException, Query
from typing import Optional

router = APIRouter(prefix="/api", tags=["items"])


@router.post("/items", response_model=ItemResponse, status_code=201)
def create_item(payload: ItemCreate):
    for existing in store.values():
        if existing["title"] == payload.title:
            raise HTTPException(status_code=409, detail="Item with this title already exists")

    item = {
        "id": str(uuid4()),
        "title": payload.title,
        "description": payload.description,
        "category": payload.category,
        "status": ItemStatus.pending,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    store[item["id"]] = item
    return item


@router.get("/items", response_model=list[ItemResponse])
def list_items(status: Optional[ItemStatus] = Query(default=None)):
    items = list(store.values())
    if status is not None:
        items = [i for i in items if i["status"] == status]
    return items


@router.get("/items/{item_id}", response_model=ItemResponse)
def get_item(item_id: str):
    item = store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.put("/items/{item_id}", response_model=ItemResponse)
def update_item(item_id: str, payload: ItemUpdate):
    item = store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    item["status"] = payload.status
    return item
```

### Query Parameters

- Use `Query()` with type annotations for filtering and pagination
- Use `Optional[Type] = Query(default=None)` for optional filters
- Document allowed values via Enum types
- Use `limit` and `offset` for pagination (default limit=50, max limit=200)

### Path Parameters

- Use descriptive names: `item_id` not `id`
- Validate format if applicable (UUID pattern, etc.)
- Always handle the "not found" case with 404

## Error Handling

### Consistent Error Response

All errors return the same shape:

```json
{ "detail": "Human-readable error message" }
```

### HTTP Status Codes

| Code | When |
|------|------|
| 200 | Successful GET, PUT, PATCH |
| 201 | Successful POST (resource created) |
| 204 | Successful DELETE (no body) |
| 400 | Invalid input that Pydantic can't catch (business rule violation) |
| 404 | Resource not found |
| 409 | Conflict (duplicate unique field) |
| 422 | Validation error (auto from Pydantic) |
| 500 | Unexpected server error |

### Helper Pattern

```python
def get_or_404(store: dict, resource_id: str, name: str = "Resource"):
    item = store.get(resource_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{name} not found")
    return item
```

### Custom Exception Handlers

```python
from fastapi import Request
from fastapi.responses import JSONResponse


class DuplicateError(Exception):
    def __init__(self, field: str, value: str):
        self.field = field
        self.value = value


@app.exception_handler(DuplicateError)
async def duplicate_handler(request: Request, exc: DuplicateError):
    return JSONResponse(
        status_code=409,
        content={"detail": f"{exc.field} '{exc.value}' already exists"},
    )
```

## Data Storage

### In-Memory (Development/Prototyping)

```python
# Simple dict-based store attached to app state
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.items = {}
    yield

# Access via request or direct reference
@router.post("/items")
def create(payload: ItemCreate):
    store = app.state.items
    # ...
```

### Database (Production)

- Use SQLAlchemy 2.0+ with async session for PostgreSQL/SQLite
- Use Alembic for migrations
- Repositories return domain objects, not ORM models
- Use dependency injection for database sessions

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session

@router.get("/items")
async def list_items(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Item))
    return result.scalars().all()
```

## Dependencies

### Dependency Injection

Use FastAPI's `Depends()` for shared logic:

```python
from fastapi import Depends, Header, HTTPException


def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@router.get("/items", dependencies=[Depends(require_api_key)])
def list_items():
    ...
```

### Configuration

Use `pydantic-settings` for environment-based config:

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    app_name: str = "My API"
    debug: bool = False
    database_url: str = "sqlite:///./app.db"

    model_config = {"env_file": ".env"}

settings = Settings()
```

## Testing

### Setup with TestClient

```python
import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def client():
    app.state.items = {}
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sample_item(client):
    res = client.post("/api/items", json={
        "title": "Sample Item",
        "description": "A test item",
        "category": "General",
    })
    return res.json()
```

### Test Patterns

```python
def test_create_item(client):
    res = client.post("/api/items", json={
        "title": "New Item",
        "description": "Description here",
        "category": "General",
    })
    assert res.status_code == 201
    data = res.json()
    assert data["title"] == "New Item"
    assert data["status"] == "pending"
    assert "id" in data


def test_duplicate_title_returns_409(client, sample_item):
    res = client.post("/api/items", json={
        "title": sample_item["title"],
        "description": "Different",
        "category": "Other",
    })
    assert res.status_code == 409
    assert "already exists" in res.json()["detail"].lower()


def test_get_unknown_item_returns_404(client):
    res = client.get("/api/items/nonexistent-id")
    assert res.status_code == 404


def test_list_items_filter_by_status(client, sample_item):
    res = client.get("/api/items?status=pending")
    assert res.status_code == 200
    assert len(res.json()) == 1


def test_update_status(client, sample_item):
    item_id = sample_item["id"]
    res = client.put(f"/api/items/{item_id}", json={"status": "completed"})
    assert res.status_code == 200
    assert res.json()["status"] == "completed"


def test_stats_counts(client, sample_item):
    res = client.get("/api/stats")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["pending"] == 1
```

### Testing Rules

- Test happy path AND error cases for every endpoint
- Test status codes explicitly (`assert res.status_code == 201`)
- Test response body shape and values
- Use fixtures for common setup (create sample item, authenticate)
- Reset state between tests (fresh store per test via fixture)
- Test query parameter filtering
- Test validation errors (missing fields, invalid values)
- Name tests descriptively: `test_<action>_<condition>_<expected_result>`

## Performance

### Async Endpoints

Use `async def` for I/O-bound endpoints (database, external API calls). Use plain `def` for CPU-bound or in-memory operations (FastAPI runs them in a thread pool).

```python
# I/O bound — use async
@router.get("/items")
async def list_items(db: AsyncSession = Depends(get_db)):
    ...

# CPU/memory bound — use sync (thread pool)
@router.get("/stats")
def get_stats():
    ...
```

### Response Optimization

- Use `response_model_exclude_none=True` to omit null fields
- Use `ORJSONResponse` for faster JSON serialization on large payloads
- Paginate list endpoints (never return unbounded results)
- Use HTTP caching headers for rarely-changing data

## Security Checklist

| Rule | Implementation |
|------|---------------|
| Input validation | Pydantic models on every endpoint |
| SQL injection | Use parameterized queries (SQLAlchemy handles this) |
| CORS | Configure explicitly for production origins |
| Rate limiting | Use `slowapi` or reverse proxy limits |
| Secrets | Never hardcode — use environment variables |
| Error details | Don't leak stack traces in production (use exception handlers) |
| Dependencies | Pin versions in `requirements.txt`, audit regularly |
| HTTPS | Enforce in production via reverse proxy |

## Anti-Patterns to Avoid

| Anti-Pattern | Do Instead |
|-------------|-----------|
| Business logic in route handlers | Extract to service layer |
| Raw dict responses without `response_model` | Always define Pydantic response models |
| Catching `Exception` broadly | Catch specific exceptions |
| Mutable default arguments | Use `Field(default_factory=list)` |
| Global mutable state without thread safety | Use `app.state` or proper locks |
| Mixing sync DB calls in async endpoints | Use async drivers or `run_in_executor` |
| Missing status codes on POST (defaults to 200) | Explicitly set `status_code=201` |
| No error handling for external calls | Always try/except with meaningful errors |
| `from module import *` | Import specific names |
| Ignoring type hints | Annotate all function signatures |
| Print statements for logging | Use `logging` module |
| Tests without state isolation | Reset store/DB in fixtures |

## requirements.txt Template

```
fastapi>=0.100.0
uvicorn>=0.20.0
pydantic>=2.0.0
pytest>=7.0.0
httpx>=0.24.0
```

For database projects, add:
```
sqlalchemy>=2.0.0
alembic>=1.10.0
asyncpg>=0.28.0
```
