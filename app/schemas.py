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


class SimulationRequest(BaseModel):
    """What-if constraint patch applied on top of a stored plan.

    The service set is immutable: only the concurrency cap, dependency edges
    and fault domains may change. Dependencies have no mutable attributes, so
    "modify" is expressed as remove + add. Fault domains support explicit
    add (new zone), update (replace the member list of an existing zone) and
    remove (drop the zone). Nothing is persisted.
    """

    max_concurrency: int | None = Field(
        default=None,
        ge=1,
        description="If set, replaces the stored per-wave concurrency cap; "
        "if omitted, the stored cap is kept",
    )
    add_dependencies: list[Dependency] = Field(default_factory=list)
    remove_dependencies: list[Dependency] = Field(default_factory=list)
    add_fault_domains: list[FaultDomain] = Field(default_factory=list)
    update_fault_domains: list[FaultDomain] = Field(
        default_factory=list,
        description="Replaces the full member list of an existing fault domain",
    )
    remove_fault_domains: list[str] = Field(default_factory=list)

    @field_validator("remove_fault_domains")
    @classmethod
    def _strip_domain_ids(cls, values: list[str]) -> list[str]:
        cleaned = [s.strip() for s in values]
        if any(not s for s in cleaned):
            raise ValueError("domain id must not be blank")
        return cleaned


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


# ---- Simulation models -----------------------------------------------------


class ServiceWaveChange(BaseModel):
    service: str
    original_wave: int
    new_wave: int


class SimulationResult(BaseModel):
    plan_id: int
    waves: list[Wave]
    wave_count: int
    original_wave_count: int
    waves_delta: int = Field(
        ...,
        description="new wave count minus original wave count (negative means "
        "the simulated plan uses fewer waves)",
    )
    waves_added: int = Field(..., description="max(waves_delta, 0)")
    waves_removed: int = Field(..., description="max(-waves_delta, 0)")
    changed_services: list[ServiceWaveChange] = Field(
        ...,
        description="Per-service original/new wave for every service assigned "
        "to a different wave, sorted by service id",
    )
    changed_service_count: int
    affected_services: list[str] = Field(
        ...,
        description="Sorted services directly violated by the *added* "
        "constraints in the original plan: endpoints of an added dependency "
        "whose original waves do not satisfy the new ordering, and members of "
        "an added/updated fault domain that originally shared a wave",
    )
    max_concurrency: int = Field(
        ..., description="Concurrency cap actually applied in the simulation"
    )
    optimal: bool


# ---- Error envelope --------------------------------------------------------


class ErrorBody(BaseModel):
    error: Literal["planning_failed", "not_found", "validation_error", "internal_error"]
    message: str
    issues: list[Issue] = Field(default_factory=list)
