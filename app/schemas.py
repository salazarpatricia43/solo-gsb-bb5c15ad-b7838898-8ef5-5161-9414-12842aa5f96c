"""Pydantic request/response models.

Fault-domain semantics (important): every entry in ``fault_domains`` is an
*anti-affinity* zone. Two services sharing the same zone id must NOT be
released in the same wave, so listing one zone per physical fault domain
expresses "services in this domain cannot co-locate in a wave".
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---- Request models --------------------------------------------------------


class Dependency(BaseModel):
    service: str = Field(..., min_length=1, description="Dependending service")
    depends_on: str = Field(..., min_length=1, description="Required service")

    @field_validator("service", "depends_on")
    @classmethod
    def _strip_and_reject_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("service id must not be blank")
        return v


class FaultDomain(BaseModel):
    domain: str = Field(
        ...,
        min_length=1,
        description="Fault-domain / zone id; members of one domain cannot "
        "share a wave",
    )
    services: list[str] = Field(..., min_length=1)

    @field_validator("domain")
    @classmethod
    def _strip_domain(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("domain id must not be blank")
        return v

    @field_validator("services")
    @classmethod
    def _strip_services(cls, values: list[str]) -> list[str]:
        cleaned = [s.strip() for s in values]
        if any(not s for s in cleaned):
            raise ValueError("service id must not be blank")
        return cleaned


class PlanCreate(BaseModel):
    name: str | None = Field(
        default=None,
        max_length=200,
        description="Optional human-readable label",
    )
    services: list[str] = Field(..., min_length=1)
    dependencies: list[Dependency] = Field(default_factory=list)
    """service -> depends_on edges; a dependency releases in an earlier wave."""
    max_concurrency: int = Field(..., ge=1, description="Services per wave cap")
    fault_domains: list[FaultDomain] = Field(default_factory=list)
    max_waves: int | None = Field(
        default=None,
        ge=1,
        description="Optional release-window cap; error 422 with reasons if "
        "the optimum needs more waves",
    )

    @field_validator("services", "name")
    @classmethod
    def _strip_service_ids(cls, v: Any) -> Any:
        if isinstance(v, list):
            cleaned = [s.strip() for s in v]
            if any(not s for s in cleaned):
                raise ValueError("service id must not be blank")
            return cleaned
        if isinstance(v, str):
            return v.strip()
        return v


# ---- Response models -------------------------------------------------------


class Wave(BaseModel):
    index: int = Field(..., ge=1, description="1-based wave number")
    services: list[str] = Field(..., description="Sorted service ids in wave")


class Issue(BaseModel):
    """A single, localizable reason that makes planning impossible."""

    code: str
    message: str
    constraint: str
    service: str | None = None
    related_services: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class PlanResult(BaseModel):
    id: int
    name: str | None
    services: list[str]
    waves: list[Wave]
    wave_count: int
    max_concurrency: int
    max_waves: int | None
    dependencies: list[dict[str, str]]
    fault_domains: list[dict[str, Any]]
    optimal: bool
    created_at: str


class PlanSummary(BaseModel):
    id: int
    name: str | None
    wave_count: int
    service_count: int
    max_concurrency: int
    optimal: bool
    created_at: str


class PlanList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[PlanSummary]


class PlanDeleted(BaseModel):
    id: int
    deleted: bool = True


# ---- Error envelope --------------------------------------------------------


class ErrorBody(BaseModel):
    error: Literal["planning_failed", "not_found", "validation_error", "internal_error"]
    message: str
    issues: list[Issue] = Field(default_factory=list)
