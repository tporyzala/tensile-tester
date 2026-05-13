from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database.bootstrap import ensure_singletons
from app.database.models import Base
from app.database.session import SessionLocal, engine
from app.routes.api import router as api_router
from app.routes.pages import router as pages_router
from app.services.machine_service import MachineCoordinator
from app.websocket.manager import WebSocketManager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_singletons(db)

    app.state.websocket_manager = WebSocketManager()
    app.state.machine = MachineCoordinator(app.state.websocket_manager)
    await app.state.machine.start()
    try:
        yield
    finally:
        await app.state.machine.stop()


app = FastAPI(title="Tensile Tester MVP", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(pages_router)
app.include_router(api_router)

