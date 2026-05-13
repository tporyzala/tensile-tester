from __future__ import annotations

from pydantic import BaseModel, Field


class ArmRunRequest(BaseModel):
    method_id: int
    sample_name: str = Field(min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)


class MachineCommandResponse(BaseModel):
    ok: bool
    message: str
