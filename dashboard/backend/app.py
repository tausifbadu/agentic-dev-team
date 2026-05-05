"""Dashboard API. FastAPI server that orchestrates the agentic dev team."""

import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.backend.routes.requirements import router as requirements_router
from dashboard.backend.routes.storypacks import router as storypacks_router
from dashboard.backend.routes.agents import router as agents_router
from dashboard.backend.routes.tests import router as tests_router
from dashboard.backend.routes.workspace import router as workspace_router
from dashboard.backend.routes.fixes import router as fixes_router
from dashboard.backend.routes.comms import router as comms_router
from dashboard.backend.routes.enhancements import router as enhancements_router
from dashboard.backend.routes.events import router as events_router
from dashboard.backend.routes.projects import router as projects_router
import state_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    state_store.init_db()
    yield


app = FastAPI(
    title="Agentic Dev Team Dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(requirements_router, prefix="/api")
app.include_router(storypacks_router, prefix="/api")
app.include_router(agents_router, prefix="/api")
app.include_router(tests_router, prefix="/api")
app.include_router(workspace_router, prefix="/api")
app.include_router(fixes_router, prefix="/api")
app.include_router(comms_router, prefix="/api")
app.include_router(enhancements_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(projects_router, prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok"}
