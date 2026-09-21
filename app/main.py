"""FastAPI application: startup wiring, exception handlers and routes."""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .config import Settings, load_settings
from .errors import PlanningError
from .planner import plan_release, simulate_plan
from .schemas import (
    ErrorBody,
    Issue,
    PlanCreate,
    PlanDeleted,
    PlanList,
    PlanResult,
    PlanSimulate,
    PlanSimulation,
    PlanSummary,
    ServiceWaveChange,
    Wave,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    # Dependencies installed in the image; tables created on every startup.
    db.init_db(settings.database_path)
    yield


app = FastAPI(
    title="Release Wave Planning API",
    version="1.1.0",
    description=(
        "Compute the minimum number of ordered release waves for a service "
        "dependency graph under per-wave concurrency caps and fault-domain "
        "anti-affinity constraints (OR-Tools CP-SAT), and simulate the "
        "impact of constraint changes on a stored plan."
    ),
    lifespan=lifespan,
)
app.state.settings = load_settings()


# ---------------------------------------------------------------------------
# Error handling — one structured envelope for every failure
# ---------------------------------------------------------------------------


@app.exception_handler(PlanningError)
async def planning_error_handler(request: Request, exc: PlanningError) -> JSONResponse:
    body = ErrorBody(
        error=exc.error_code,
        message=exc.message,
        issues=exc.issues,
    )
    return JSONResponse(
        status_code=exc.status_code, content=body.model_dump()
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    issues: list[Issue] = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", []) if p not in ("body",)]
        service = None
        if loc and loc[0] in ("services", "dependencies", "fault_domains"):
            service = loc[-1] if loc[-1].isidentifier() or "-" in loc[-1] else None
        issues.append(
            Issue(
                code="invalid_request_field",
                message=err.get("msg", "Invalid request field."),
                constraint=".".join(loc) or "body",
                service=service,
                context={"location": list(err.get("loc", [])), "type": err.get("type")},
            )
        )
    body = ErrorBody(
        error="validation_error",
        message="Request payload failed schema validation.",
        issues=issues,
    )
    return JSONResponse(status_code=422, content=body.model_dump())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_result(row: sqlite3.Row) -> PlanResult:
    stored = json.loads(row["result_json"])
    return PlanResult(
        id=row["id"],
        name=row["name"],
        services=stored["services"],
        waves=[Wave(**w) for w in stored["waves"]],
        wave_count=row["wave_count"],
        max_concurrency=stored["max_concurrency"],
        max_waves=stored.get("max_waves"),
        dependencies=stored["dependencies"],
        fault_domains=stored["fault_domains"],
        optimal=bool(row["optimal"]),
        created_at=row["created_at"],
    )


def _plan_not_found(plan_id: int) -> JSONResponse:
    body = ErrorBody(
        error="not_found",
        message=f"Plan {plan_id} does not exist.",
        issues=[
            Issue(
                code="plan_not_found",
                message=f"Plan {plan_id} does not exist.",
                constraint="plan_id",
                context={"id": plan_id},
            )
        ],
    ).model_dump()
    return JSONResponse(status_code=404, content=body)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/plans",
    response_model=PlanResult,
    status_code=status.HTTP_201_CREATED,
    tags=["plans"],
    responses={422: {"model": ErrorBody}},
)
def create_plan(
    payload: PlanCreate,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> PlanResult:
    settings: Settings = app.state.settings
    output, domains = plan_release(payload, settings)

    request_payload = payload.model_dump()
    result_payload = {
        "services": sorted(payload.services),
        "waves": [
            {"index": i + 1, "services": members}
            for i, members in enumerate(output.waves)
        ],
        "max_concurrency": payload.max_concurrency,
        "max_waves": payload.max_waves,
        "dependencies": [
            {"service": s, "depends_on": d}
            for s, d in {
                (x.service, x.depends_on) for x in payload.dependencies
            }
        ],
        "fault_domains": domains,
    }

    plan_id = db.insert_plan(
        conn,
        payload.name,
        request_payload,
        result_payload,
        output.wave_count,
        output.optimal,
    )
    row = db.fetch_plan(conn, plan_id)
    assert row is not None
    return _row_to_result(row)


@app.get("/plans", response_model=PlanList, tags=["plans"])
def list_plans(
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> PlanList:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    total, rows = db.list_plans(conn, limit, offset)
    items = [
        PlanSummary(
            id=row["id"],
            name=row["name"],
            wave_count=row["wave_count"],
            service_count=len(json.loads(row["request_json"]).get("services", [])),
            max_concurrency=json.loads(row["request_json"])["max_concurrency"],
            optimal=bool(row["optimal"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return PlanList(total=total, limit=limit, offset=offset, items=items)


@app.get(
    "/plans/{plan_id}",
    response_model=PlanResult,
    tags=["plans"],
    responses={404: {"model": ErrorBody}},
)
def get_plan(
    plan_id: int, conn: sqlite3.Connection = Depends(db.get_db)
) -> PlanResult:
    row = db.fetch_plan(conn, plan_id)
    if row is None:
        return _plan_not_found(plan_id)  # type: ignore[return-value]
    return _row_to_result(row)


@app.delete(
    "/plans/{plan_id}",
    response_model=PlanDeleted,
    tags=["plans"],
    responses={404: {"model": ErrorBody}},
)
def delete_plan(
    plan_id: int, conn: sqlite3.Connection = Depends(db.get_db)
) -> PlanDeleted:
    if not db.delete_plan(conn, plan_id):
        return _plan_not_found(plan_id)  # type: ignore[return-value]
    return PlanDeleted(id=plan_id)


@app.post(
    "/plans/{plan_id}/simulate",
    response_model=PlanSimulation,
    tags=["plans"],
    responses={404: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def simulate_plan_changes(
    plan_id: int,
    payload: PlanSimulate,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> PlanSimulation:
    """What-if re-planning with constraint changes; nothing is persisted."""
    row = db.fetch_plan(conn, plan_id)
    if row is None:
        return _plan_not_found(plan_id)  # type: ignore[return-value]

    settings: Settings = app.state.settings
    original = PlanCreate(**json.loads(row["request_json"]))
    stored = json.loads(row["result_json"])
    original_pos = {
        service: wave["index"] - 1
        for wave in stored["waves"]
        for service in wave["services"]
    }

    result = simulate_plan(original, original_pos, payload, settings)

    assignments = [
        ServiceWaveChange(
            service=service,
            from_wave=original_pos[service] + 1,
            to_wave=result.assignment[service] + 1,
        )
        for service in sorted(result.assignment)
    ]
    return PlanSimulation(
        plan_id=row["id"],
        name=row["name"],
        waves=[
            Wave(index=i + 1, services=members)
            for i, members in enumerate(result.waves)
        ],
        wave_count=result.wave_count,
        original_wave_count=row["wave_count"],
        wave_delta=result.wave_count - row["wave_count"],
        max_concurrency=result.max_concurrency,
        optimal=result.optimal,
        assignments=assignments,
        moved_services=[a.service for a in assignments if a.from_wave != a.to_wave],
        affected_services=result.affected_services,
    )
