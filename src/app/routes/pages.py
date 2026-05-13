from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.services.calibration_service import get_calibration
from app.services.method_service import list_methods
from app.services.run_service import list_runs
from app.services.settings_service import get_settings


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    methods = list_methods(db)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "methods": methods,
            "snapshot": request.app.state.machine.public_snapshot(),
        },
    )


@router.get("/methods", response_class=HTMLResponse)
async def methods_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "methods.html",
        {"methods": list_methods(db)},
    )


@router.get("/results", response_class=HTMLResponse)
async def results_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "results.html",
        {"runs": list_runs(db)},
    )


@router.get("/calibration", response_class=HTMLResponse)
async def calibration_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "calibration.html",
        {"calibration": get_calibration(db), "snapshot": request.app.state.machine.public_snapshot()},
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"settings": get_settings(db), "snapshot": request.app.state.machine.public_snapshot()},
    )

