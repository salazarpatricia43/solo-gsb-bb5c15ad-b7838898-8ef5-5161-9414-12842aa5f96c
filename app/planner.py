"""Core planning logic built on OR-Tools CP-SAT.

Pipeline:
  1. normalize & validate the request (unknown references, duplicates)
  2. detect dependency cycles
  3. CP-SAT model: minimum number of contiguous, ordered waves subject to
     dependencies, per-wave concurrency and fault-domain anti-affinity
  4. when an explicit ``max_waves`` window is infeasible, emit structured,
     localizable reasons (critical path, capacity bound, conflict bound,
     exact required waves)

Determinism: fixed random seed and a single search worker; a second solve
phase then canonicalizes service positions within the proven minimum number
of waves, and wave member lists are sorted. Repeated identical requests
yield identical plans.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .config import Settings
from .errors import PlanningError
from .graph import build_adjacency, find_cycles, longest_path_length
from .schemas import Issue, PlanCreate

FEASIBLE_STATUSES = (cp_model.OPTIMAL, cp_model.FEASIBLE)


@dataclass
class PlanOutput:
    waves: list[list[str]]  # services of each wave, sorted
    wave_count: int
    optimal: bool


# ---------------------------------------------------------------------------
# Validation and normalization
# ---------------------------------------------------------------------------


def _validate_request(
    req: PlanCreate,
) -> tuple[list[str], list[tuple[str, str]], list[dict], list[list[str]]]:
    """Return (services, edges, domains, fault_cliques)."""
    issues: list[Issue] = []

    # Duplicate service declarations.
    seen: set[str] = set()
    services: list[str] = []
    for s in req.services:
        if s in seen:
            issues.append(
                Issue(
                    code="duplicate_service",
                    message=f"Service '{s}' is declared more than once.",
                    constraint="services",
                    service=s,
                )
            )
            continue
        seen.add(s)
        services.append(s)
    known = set(services)

    # Dependencies: unknown references (duplicate edges are silently merged).
    edges: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for dep in req.dependencies:
        edge = (dep.service, dep.depends_on)
        if edge in seen_edges:
            continue
        seen_edges.add(edge)
        edges.append(edge)
        if dep.service not in known:
            issues.append(
                Issue(
                    code="unknown_dependency_service",
                    message=(
                        f"Dependency source '{dep.service}' is not in the "
                        "service list."
                    ),
                    constraint="dependencies",
                    service=dep.service,
                    related_services=[dep.depends_on],
                )
            )
        if dep.depends_on not in known:
            issues.append(
                Issue(
                    code="unknown_dependency_target",
                    message=(
                        f"Service '{dep.service}' depends on "
                        f"'{dep.depends_on}', which is not in the service list."
                    ),
                    constraint="dependencies",
                    service=dep.depends_on,
                    related_services=[dep.service],
                )
            )

    # Fault domains.
    domains: list[dict] = []
    fault_cliques: list[list[str]] = []
    seen_domains: set[str] = set()
    for fd in req.fault_domains:
        if fd.domain in seen_domains:
            issues.append(
                Issue(
                    code="duplicate_fault_domain",
                    message=(
                        f"Fault domain '{fd.domain}' is declared more than once; "
                        "merge its member lists instead."
                    ),
                    constraint="fault_domains",
                    context={"domain": fd.domain},
                )
            )
            continue
        seen_domains.add(fd.domain)

        members: list[str] = []
        member_seen: set[str] = set()
        for m in fd.services:
            if m in member_seen:
                issues.append(
                    Issue(
                        code="duplicate_fault_domain_member",
                        message=(
                            f"Service '{m}' appears more than once in fault "
                            f"domain '{fd.domain}'."
                        ),
                        constraint="fault_domains",
                        service=m,
                        context={"domain": fd.domain},
                    )
                )
                continue
            member_seen.add(m)
            if m not in known:
                issues.append(
                    Issue(
                        code="unknown_fault_domain_member",
                        message=(
                            f"Fault domain '{fd.domain}' references unknown "
                            f"service '{m}'."
                        ),
                        constraint="fault_domains",
                        service=m,
                        context={"domain": fd.domain},
                    )
                )
                continue
            members.append(m)
        domains.append({"domain": fd.domain, "services": members})

        # Each fault domain is one anti-affinity clique.
        if len(members) >= 2:
            fault_cliques.append(sorted(members))

    if issues:
        raise PlanningError(
            "The planning request contains invalid references or duplicates.",
            issues,
        )

    return services, edges, domains, fault_cliques


# ---------------------------------------------------------------------------
# CP-SAT model
# ---------------------------------------------------------------------------


def _make_solver(settings: Settings, time_limit: float | None = None) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = settings.solver_num_search_workers
    solver.parameters.random_seed = 42
    solver.parameters.max_time_in_seconds = (
        time_limit if time_limit is not None else settings.solver_time_limit_seconds
    )
    return solver


def _build_wave_model(
    services: list[str],
    edges: list[tuple[str, str]],
    fault_cliques: list[list[str]],
    horizon: int,
    concurrency: int,
):
    """Build the CP-SAT model.

    Each service is a unit-length interval starting at integer wave position
    ``pos[s]``. ``AddCumulative`` enforces the per-wave concurrency cap in
    O(n) constraints (no n*horizon boolean matrix); dependencies are strict
    ordering and each fault-domain clique is a ``NoOverlap`` constraint on
    unit intervals. Minimizing the maximum position guarantees the used waves
    are contiguous (a gap would let everything above it shift down).
    """
    model = cp_model.CpModel()
    pos = {
        s: model.NewIntVar(0, horizon - 1, f"pos_{s}") for s in services
    }

    # Dependencies release in a strictly earlier wave.
    for a, b in edges:
        model.Add(pos[a] >= pos[b] + 1)

    # Every member of a fault domain occupies a distinct wave. NoOverlap on
    # unit intervals propagates much faster than AllDifferent on large cliques.
    for clique in fault_cliques:
        if len(clique) >= 2:
            clique_intervals = [
                model.NewIntervalVar(
                    pos[s], 1, pos[s] + 1, f"fdiv_{len(clique)}_{s}"
                )
                for s in clique
            ]
            model.AddNoOverlap(clique_intervals)

    # Per-wave concurrency cap: unit jobs consume one slot of the cap.
    intervals = [
        model.NewIntervalVar(pos[s], 1, pos[s] + 1, f"iv_{s}")
        for s in services
    ]
    model.AddCumulative(intervals, [1] * len(services), concurrency)

    max_pos = model.NewIntVar(0, horizon - 1, "max_pos")
    model.AddMaxEquality(max_pos, [pos[s] for s in services])
    return model, pos, max_pos


@dataclass
class _SolveResult:
    feasible: bool
    optimal: bool
    wave_count: int
    assignment: dict[str, int]


def _solve(
    services: list[str],
    edges: list[tuple[str, str]],
    fault_cliques: list[list[str]],
    horizon: int,
    concurrency: int,
    settings: Settings,
) -> _SolveResult:
    model, pos, max_pos = _build_wave_model(
        services, edges, fault_cliques, horizon, concurrency
    )
    # Number of waves = max position + 1.
    model.Minimize(max_pos)
    solver = _make_solver(settings)
    status = solver.Solve(model)

    if status not in FEASIBLE_STATUSES:
        return _SolveResult(False, False, 0, {})

    optimal = status == cp_model.OPTIMAL
    wave_count = int(solver.Value(max_pos)) + 1
    assignment = {s: int(solver.Value(pos[s])) for s in services}

    # Phase 2 only canonicalizes placement within the proven minimum. It is
    # never allowed to downgrade the optimality of the wave count: seed it
    # with the phase-1 solution and a short budget, keeping phase 1's
    # placement if the tie-break cannot be improved in time.
    if optimal:
        # Maximize Σ rank(s)·pos(s): high-rank (lexicographically late) ids
        # move to later waves, so small ids settle as early as possible.
        ranked = {s: rank for rank, s in enumerate(sorted(services))}
        model2, pos2, max_pos2 = _build_wave_model(
            services, edges, fault_cliques, horizon, concurrency
        )
        model2.Add(max_pos2 == wave_count - 1)
        model2.Maximize(sum(ranked[s] * pos2[s] for s in services))
        model2.AddHint(max_pos2, wave_count - 1)
        for s, w in assignment.items():
            model2.AddHint(pos2[s], w)
        tie_break_budget = min(3.0, max(0.5, settings.solver_time_limit_seconds / 2))
        solver2 = _make_solver(settings, time_limit=tie_break_budget)
        status2 = solver2.Solve(model2)
        if status2 in FEASIBLE_STATUSES:
            assignment = {s: int(solver2.Value(pos2[s])) for s in services}

    return _SolveResult(True, optimal, wave_count, assignment)


def _to_output(result: _SolveResult) -> PlanOutput:
    groups: dict[int, list[str]] = {}
    for s, w in result.assignment.items():
        groups.setdefault(w, []).append(s)
    # Renumber contiguous 1..k (optimal solutions already have no gaps).
    ordered = [sorted(groups[old]) for old in sorted(groups)]
    return PlanOutput(waves=ordered, wave_count=len(ordered), optimal=result.optimal)


# ---------------------------------------------------------------------------
# Infeasibility diagnostics
# ---------------------------------------------------------------------------


def _conflict_chromatic_number(
    services: list[str],
    fault_cliques: list[list[str]],
    settings: Settings,
) -> int | None:
    """Fault-domain-only minimum wave count (a true lower bound).

    Reuses the wave model with a permissive capacity, so only the
    ``NoOverlap`` anti-affinity constraints remain. Returns ``None`` if the
    bound cannot be proven within the time budget.
    """
    if not fault_cliques:
        return 1
    n = len(services)
    model, _pos, max_color = _build_wave_model(
        services, [], fault_cliques, n, n
    )
    model.Minimize(max_color)
    solver = _make_solver(settings)
    status = solver.Solve(model)
    if status == cp_model.OPTIMAL:
        return int(solver.Value(max_color)) + 1
    return None


def _format_cycle(cycle: list[str]) -> str:
    return " -> ".join(cycle + [cycle[0]])


def _diagnose_window(
    services: list[str],
    edges: list[tuple[str, str]],
    domains: list[dict],
    fault_cliques: list[list[str]],
    concurrency: int,
    max_waves: int,
    settings: Settings,
) -> list[Issue]:
    issues: list[Issue] = []
    n = len(services)

    # Individual, always-exact lower bounds.
    critical_path, chain = longest_path_length(services, edges)
    capacity_bound = math.ceil(n / concurrency)
    conflict_bound = _conflict_chromatic_number(services, fault_cliques, settings)

    if critical_path > max_waves:
        issues.append(
            Issue(
                code="dependency_chain_too_long",
                message=(
                    f"The longest dependency chain needs {critical_path} waves "
                    f"(critical path {_format_cycle(chain)}), but only "
                    f"{max_waves} wave(s) are allowed."
                ),
                constraint="dependencies",
                service=chain[0],
                related_services=chain,
                context={
                    "required_waves": critical_path,
                    "max_waves": max_waves,
                    "chain": chain,
                },
            )
        )

    if capacity_bound > max_waves:
        issues.append(
            Issue(
                code="concurrency_cap_too_low",
                message=(
                    f"{n} services at most {concurrency} per wave need at least "
                    f"{capacity_bound} waves, but only {max_waves} are allowed."
                ),
                constraint="max_concurrency",
                context={
                    "service_count": n,
                    "max_concurrency": concurrency,
                    "required_waves": capacity_bound,
                    "max_waves": max_waves,
                },
            )
        )

    witness: list[str] = []
    oversized_domains: list[dict] = []
    for fd in domains:
        members = fd["services"]
        if len(members) > max_waves:
            oversized_domains.append(
                {"domain": fd["domain"], "size": len(members), "services": members}
            )
            if not witness:
                witness = members
    if conflict_bound is not None and conflict_bound > max_waves:
        issues.append(
            Issue(
                code="fault_domain_anti_affinity",
                message=(
                    f"Fault-domain conflicts require at least {conflict_bound} "
                    f"distinct waves, but only {max_waves} are allowed."
                ),
                constraint="fault_domains",
                related_services=witness,
                context={
                    "required_waves": conflict_bound,
                    "max_waves": max_waves,
                    "oversized_domains": oversized_domains,
                },
            )
        )

    # Exact answer of the combined model (when proven) is the definitive number.
    unbounded = _solve(
        services, edges, fault_cliques, n, concurrency, settings
    )
    required = unbounded.wave_count if unbounded.optimal else None
    lower_bounds = {
        "dependencies": critical_path,
        "max_concurrency": capacity_bound,
        "fault_domains": conflict_bound,
    }

    if not issues or required is not None:
        issues.append(
            Issue(
                code="release_window_infeasible",
                message=(
                    "No schedule fits the release window under the combined "
                    "dependencies, concurrency and fault-domain constraints."
                    + (
                        f" An unconstrained window needs {required} wave(s)."
                        if required is not None
                        else ""
                    )
                ),
                constraint="max_waves",
                context={
                    "max_waves": max_waves,
                    "required_waves": required,
                    "lower_bounds": lower_bounds,
                },
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan_release(req: PlanCreate, settings: Settings) -> tuple[PlanOutput, list[dict]]:
    services, edges, domains, fault_cliques = _validate_request(req)

    adj = build_adjacency(services, edges)
    cycles = find_cycles(services, adj, settings.max_reported_cycles)
    if cycles:
        issues = [
            Issue(
                code="dependency_cycle",
                message=(
                    f"Dependency graph contains {len(cycles)} cycle(s); "
                    "a release order cannot be determined. First cycle: "
                    f"{_format_cycle(cycles[0])}"
                ),
                constraint="dependencies",
                service=cycles[0][0],
                related_services=cycles[0],
                context={"cycles": cycles, "cycle_count": len(cycles)},
            )
        ]
        raise PlanningError("The dependency graph contains a cycle.", issues)

    horizon = min(len(services), req.max_waves) if req.max_waves else len(services)
    result = _solve(
        services,
        edges,
        fault_cliques,
        horizon,
        req.max_concurrency,
        settings,
    )

    if result.feasible:
        return _to_output(result), domains

    # Infeasible only happens with an explicit release-window cap.
    if req.max_waves is not None:
        issues = _diagnose_window(
            services,
            edges,
            domains,
            fault_cliques,
            req.max_concurrency,
            req.max_waves,
            settings,
        )
        raise PlanningError(
            f"Services cannot be scheduled within {req.max_waves} wave(s).",
            issues,
        )

    # Defensive: with horizon == n, waves of size 1 always work for DAGs.
    raise RuntimeError("Unexpected solver infeasibility without a wave window cap")
