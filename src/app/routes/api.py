from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.schemas import ArmRunRequest, MachineCommandResponse
from app.services.calibration_service import update_calibration
from app.services.method_service import add_step, create_method
from app.services.run_service import arm_run, export_run_csv
from app.services.settings_service import update_settings


router = APIRouter(prefix="/api", tags=["api"])


def notice(message: str, tone: str = "success") -> HTMLResponse:
    return HTMLResponse(f'<div class="notice {tone}">{message}</div>')


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    return request.app.state.machine.public_snapshot()


@router.post("/run/arm", response_model=MachineCommandResponse)
async def arm_run_route(
    payload: ArmRunRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> MachineCommandResponse:
    machine = request.app.state.machine
    if machine.snapshot.state != "IDLE":
        raise HTTPException(status_code=409, detail="Machine must be IDLE before arming a run.")
    try:
        run = arm_run(db, payload.method_id, payload.sample_name, payload.notes)
        await machine.arm_run(db, run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MachineCommandResponse(ok=True, message=f"Run {run.run_name} armed.")


@router.post("/machine/start", response_model=MachineCommandResponse)
async def start_machine(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.start_run()
    return MachineCommandResponse(ok=True, message="Start command sent.")


@router.post("/machine/cancel-arm", response_model=MachineCommandResponse)
async def cancel_armed_machine(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.cancel_armed_run()
    return MachineCommandResponse(ok=True, message="Armed run cancelled.")


@router.post("/machine/pause", response_model=MachineCommandResponse)
async def pause_machine(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.pause()
    return MachineCommandResponse(ok=True, message="Pause command sent.")


@router.post("/machine/resume", response_model=MachineCommandResponse)
async def resume_machine(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.resume()
    return MachineCommandResponse(ok=True, message="Resume command sent.")


@router.post("/machine/return-zero", response_model=MachineCommandResponse)
async def return_zero_machine(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.return_zero()
    return MachineCommandResponse(ok=True, message="Return-to-zero command sent.")


@router.post("/machine/abort", response_model=MachineCommandResponse)
async def abort_machine(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.abort()
    return MachineCommandResponse(ok=True, message="Abort command sent.")


@router.post("/machine/setup/enter", response_model=MachineCommandResponse)
async def enter_setup(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.enter_setup()
    return MachineCommandResponse(ok=True, message="Enter setup command sent.")


@router.post("/machine/setup/exit", response_model=MachineCommandResponse)
async def exit_setup(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.exit_setup()
    return MachineCommandResponse(ok=True, message="Exit setup command sent.")


@router.post("/machine/zero", response_model=MachineCommandResponse)
async def zero_load(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.zero_load()
    return MachineCommandResponse(ok=True, message="Zero-load command sent.")


@router.post("/machine/reset-fault", response_model=MachineCommandResponse)
async def reset_fault(request: Request) -> MachineCommandResponse:
    await request.app.state.machine.reset_fault()
    return MachineCommandResponse(ok=True, message="Reset command sent.")


@router.post("/methods", response_class=HTMLResponse)
async def create_method_route(
    name: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        method = create_method(db, name, description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A method with that name already exists.") from exc
    return notice(f"Created method {method.name}. Reload to add steps.")


@router.post("/methods/{method_id}/steps", response_class=HTMLResponse)
async def add_step_route(
    method_id: int,
    step_type: Annotated[str, Form()],
    target_force_n: Annotated[float, Form()],
    rate_n_per_s: Annotated[float | None, Form()] = None,
    timeout_s: Annotated[float | None, Form()] = None,
    duration_s: Annotated[float | None, Form()] = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        step = add_step(
            db,
            method_id,
            step_type,
            target_force_n,
            rate_n_per_s,
            timeout_s,
            duration_s,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return notice(f"Added step {step.position} to method {method_id}. Reload to review sequence.")


@router.post("/calibration", response_class=HTMLResponse)
async def update_calibration_route(
    slope: Annotated[float, Form()],
    intercept: Annotated[float, Form()],
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if request.app.state.machine.snapshot.state != "IDLE":
        raise HTTPException(status_code=409, detail="Calibration is editable only while the machine is IDLE.")
    calibration = update_calibration(db, slope, intercept)
    await request.app.state.machine.reload_config()
    return notice(f"Calibration updated. slope={calibration.slope:.9f}, intercept={calibration.intercept:.9f}")


@router.post("/settings", response_class=HTMLResponse)
async def update_settings_route(
    request: Request,
    p_gain: Annotated[float, Form()],
    i_gain: Annotated[float, Form()],
    d_gain: Annotated[float, Form()],
    deadband_n: Annotated[float, Form()],
    overload_threshold_n: Annotated[float, Form()],
    microstepping: Annotated[int, Form()],
    jog_speed_steps_s: Annotated[float, Form()],
    max_step_rate_steps_s: Annotated[float, Form()],
    max_acceleration_steps_s2: Annotated[float, Form()],
    return_to_zero_rate_n_s: Annotated[float, Form()],
    invert_motor_direction: Annotated[str | None, Form()] = None,
    invert_load_cell_sign: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if request.app.state.machine.snapshot.state != "IDLE":
        raise HTTPException(status_code=409, detail="Admin settings are editable only while the machine is IDLE.")
    settings = update_settings(
        db,
        {
            "p_gain": p_gain,
            "i_gain": i_gain,
            "d_gain": d_gain,
            "deadband_n": deadband_n,
            "overload_threshold_n": overload_threshold_n,
            "microstepping": microstepping,
            "jog_speed_steps_s": jog_speed_steps_s,
            "max_step_rate_steps_s": max_step_rate_steps_s,
            "max_acceleration_steps_s2": max_acceleration_steps_s2,
            "return_to_zero_rate_n_s": return_to_zero_rate_n_s,
            "invert_motor_direction": invert_motor_direction == "on",
            "invert_load_cell_sign": invert_load_cell_sign == "on",
        },
    )
    await request.app.state.machine.reload_config()
    return notice(f"Settings updated for IDLE controller reload. max rate={settings.max_step_rate_steps_s:.1f} steps/s.")


@router.get("/runs/{run_id}/telemetry.csv")
async def export_run(run_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    try:
        filename, contents = export_run_csv(db, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    response = StreamingResponse(iter([contents]), media_type="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@router.websocket("/ws/telemetry")
async def telemetry_socket(websocket: WebSocket) -> None:
    app = websocket.app
    manager = app.state.websocket_manager
    await manager.connect(websocket)
    await websocket.send_json({"type": "snapshot", "payload": app.state.machine.public_snapshot()})
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        manager.disconnect(websocket)
