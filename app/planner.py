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
from .schemas import Issue, PlanCreate, SimulationRequest

FEASIBLE_STATUSES = (cp_model.OPTIMAL, cp_model.FEASIBLE)


@dataclass
class PlanOutput:
    waves: list[list[str]]  # services of each wave, sorted
    wave_count: int
    optimal: bool


@dataclass
class SimulationOutput:
    waves: list[list[str]]  # services of each wave, sorted
    wave_count: int
    assignment: dict[str, int]
    optimal: bool
    concurrency: int
    affected_services: list[str]


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
# Simulation solver: lexicographic optimization
#   1. minimum waves
#   2. among minimum-wave schedules, minimum number of moved services
#   3. canonical placement (lexicographically late ids pushed later)
#
# Every lexicographic step fixes the previous objective to its proven optimum
# via an equality constraint (not a weighted sum), so the tie-break can never
# trade away a better primary objective. Fixed seed + one worker keeps the
# whole pipeline deterministic.
# ---------------------------------------------------------------------------


@dataclass
class _SimSolveResult:
    feasible: bool
    optimal: bool
    wave_count: int
    assignment: dict[str, int]


def _tie_break_budget(settings: Settings) -> float:
    return min(3.0, max(0.5, settings.solver_time_limit_seconds / 2))


def _solve_simulation(
    services: list[str],
    edges: list[tuple[str, str]],
    fault_cliques: list[list[str]],
    concurrency: int,
    original_assignment: dict[str, int],
    settings: Settings,
) -> _SimSolveResult:
    n = len(services)

    # Phase 1: minimum number of waves.
    model, pos, max_pos = _build_wave_model(
        services, edges, fault_cliques, n, concurrency
    )
    model.Minimize(max_pos)
    solver = _make_solver(settings)
    status = solver.Solve(model)
    if status not in FEASIBLE_STATUSES:
        # Defensive: with horizon == n, waves of size 1 always work for DAGs.
        return _SimSolveResult(False, False, 0, {})

    wave_count = int(solver.Value(max_pos)) + 1
    assignment = {s: int(solver.Value(pos[s])) for s in services}
    optimal = status == cp_model.OPTIMAL

    if not optimal:
        return _SimSolveResult(True, False, wave_count, assignment)

    def _hint(m: cp_model.CpModel, p: dict, mp, known: dict, wcount: int) -> None:
        m.AddHint(mp, wcount - 1)
        for s, w in known.items():
            m.AddHint(p[s], w)

    # Phase 2: within the proven minimum, minimize moved services. A service
    # is "moved" iff its new wave differs from its wave in the stored plan.
    model2, pos2, max_pos2 = _build_wave_model(
        services, edges, fault_cliques, n, concurrency
    )
    model2.Add(max_pos2 == wave_count - 1)
    moved_var: dict[str, cp_model.IntVar] = {}
    for s in services:
        m = model2.NewBoolVar(f"moved_{s}")
        orig = original_assignment[s]
        model2.Add(pos2[s] == orig).OnlyEnforceIf(m.Not())
        model2.Add(pos2[s] != orig).OnlyEnforceIf(m)
        moved_var[s] = m
    total_moved = model2.NewIntVar(0, len(services), "total_moved")
    model2.Add(total_moved == sum(moved_var.values()))
    model2.Minimize(total_moved)
    _hint(model2, pos2, max_pos2, assignment, wave_count)
    solver2 = _make_solver(settings, time_limit=_tie_break_budget(settings))
    status2 = solver2.Solve(model2)

    min_moved: int | None = None
    if status2 in FEASIBLE_STATUSES:
        assignment = {s: int(solver2.Value(pos2[s])) for s in services}
        if status2 == cp_model.OPTIMAL:
            min_moved = int(solver2.Value(total_moved))
        optimal = status2 == cp_model.OPTIMAL

    # Phase 3: among minimum-move schedules, canonicalize positions the same
    # way the regular planner does, so identical simulations are identical.
    if min_moved is not None:
        model3, pos3, max_pos3 = _build_wave_model(
            services, edges, fault_cliques, n, concurrency
        )
        model3.Add(max_pos3 == wave_count - 1)
        moved3: list[cp_model.IntVar] = []
        for s in services:
            m = model3.NewBoolVar(f"moved3_{s}")
            orig = original_assignment[s]
            model3.Add(pos3[s] == orig).OnlyEnforceIf(m.Not())
            model3.Add(pos3[s] != orig).OnlyEnforceIf(m)
            moved3.append(m)
        model3.Add(sum(moved3) == min_moved)
        ranked = {s: rank for rank, s in enumerate(sorted(services))}
        model3.Maximize(sum(ranked[s] * pos3[s] for s in services))
        _hint(model3, pos3, max_pos3, assignment, wave_count)
        solver3 = _make_solver(settings, time_limit=_tie_break_budget(settings))
        status3 = solver3.Solve(model3)
        if status3 in FEASIBLE_STATUSES:
            assignment = {s: int(solver3.Value(pos3[s])) for s in services}

    return _SimSolveResult(True, optimal, wave_count, assignment)


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


# ---------------------------------------------------------------------------
# Constraint-change simulation (never persisted by the planning layer)
# ---------------------------------------------------------------------------


def _patch_error(code: str, message: str, constraint: str, **kw: object) -> Issue:
    return Issue(code=code, message=message, constraint=constraint, **kw)


def _apply_simulation_patch(
    services: list[str],
    base_edges: list[tuple[str, str]],
    base_domains: list[dict],
    patch: SimulationRequest,
) -> tuple[
    list[tuple[str, str]],
    list[dict],
    list[list[str]],
    set[tuple[str, str]],
    dict[str, list[str]],
]:
    """Validate the patch against the stored plan and compute the new inputs.

    Returns (new_edges, new_domains, new_fault_cliques, added_edges,
    added_domain_members), where the last two describe what the patch
    *introduced* (used for affected-service computation).
    """
    known = set(services)
    issues: list[Issue] = []

    # --- Dependencies -------------------------------------------------------
    new_edges = set(base_edges)
    added_edges: set[tuple[str, str]] = set()
    seen_add: set[tuple[str, str]] = set()
    seen_remove: set[tuple[str, str]] = set()
    effective_adds: set[tuple[str, str]] = set()

    for dep in patch.add_dependencies:
        edge = (dep.service, dep.depends_on)
        if edge in seen_add:
            continue
        seen_add.add(edge)
        for endpoint, other, code in (
            (dep.service, dep.depends_on, "simulation_unknown_dependency_service"),
            (dep.depends_on, dep.service, "simulation_unknown_dependency_target"),
        ):
            if endpoint not in known:
                issues.append(
                    _patch_error(
                        code,
                        (
                            f"Added dependency references '{endpoint}', which "
                            "is not part of this plan's service list (a "
                            "simulation cannot create services)."
                        ),
                        "add_dependencies",
                        service=endpoint,
                        related_services=[other],
                    )
                )
        if dep.service not in known or dep.depends_on not in known:
            continue
        if edge in new_edges:
            issues.append(
                _patch_error(
                    "simulation_dependency_exists",
                    (
                        f"Dependency '{dep.service}' -> '{dep.depends_on}' "
                        "already exists in the stored plan; remove it first "
                        "or drop the addition."
                    ),
                    "add_dependencies",
                    service=dep.service,
                    related_services=[dep.depends_on],
                )
            )
        else:
            new_edges.add(edge)
            added_edges.add(edge)
            effective_adds.add(edge)

    for dep in patch.remove_dependencies:
        edge = (dep.service, dep.depends_on)
        if edge in seen_remove:
            continue
        seen_remove.add(edge)
        if edge in effective_adds:
            issues.append(
                _patch_error(
                    "simulation_dependency_conflicting_ops",
                    (
                        f"Dependency '{dep.service}' -> '{dep.depends_on}' is "
                        "both added and removed in the same simulation."
                    ),
                    "remove_dependencies",
                    service=dep.service,
                    related_services=[dep.depends_on],
                )
            )
        if dep.service not in known or dep.depends_on not in known:
            issues.append(
                _patch_error(
                    "simulation_removed_dependency_unknown",
                    (
                        f"Removed dependency references '{dep.service}' / "
                        f"'{dep.depends_on}', which are not both part of this "
                        "plan's service list."
                    ),
                    "remove_dependencies",
                    service=dep.service,
                    related_services=[dep.depends_on],
                )
            )
        elif edge not in base_edges:
            issues.append(
                _patch_error(
                    "simulation_dependency_not_found",
                    (
                        f"Dependency '{dep.service}' -> '{dep.depends_on}' does "
                        "not exist in the stored plan and cannot be removed."
                    ),
                    "remove_dependencies",
                    service=dep.service,
                    related_services=[dep.depends_on],
                )
            )
        else:
            new_edges.discard(edge)

    # --- Fault domains ------------------------------------------------------
    domains_by_name: dict[str, list[str]] = {
        d["domain"]: list(d["services"]) for d in base_domains
    }
    new_domains: list[dict] = []
    # domain -> members introduced by the patch (sorted input lists)
    added_domain_members: dict[str, list[str]] = {}

    def _check_domain_members(
        section: str, domain: str, members: list[str]
    ) -> None:
        for m in members:
            if m not in known:
                issues.append(
                    _patch_error(
                        "simulation_unknown_fault_domain_member",
                        (
                            f"Fault domain change for '{domain}' references "
                            f"unknown service '{m}'."
                        ),
                        section,
                        service=m,
                        context={"domain": domain},
                    )
                )

    seen_add_domains: set[str] = set()
    effective_add_domains: set[str] = set()
    for fd in patch.add_fault_domains:
        if fd.domain in seen_add_domains:
            issues.append(
                _patch_error(
                    "simulation_duplicate_fault_domain_op",
                    f"Fault domain '{fd.domain}' is added more than once.",
                    "add_fault_domains",
                    context={"domain": fd.domain},
                )
            )
            continue
        seen_add_domains.add(fd.domain)
        _check_domain_members("add_fault_domains", fd.domain, fd.services)
        if fd.domain in domains_by_name:
            issues.append(
                _patch_error(
                    "simulation_fault_domain_exists",
                    (
                        f"Fault domain '{fd.domain}' already exists in the "
                        "stored plan; use update_fault_domains to replace its "
                        "members or remove_fault_domains to drop it."
                    ),
                    "add_fault_domains",
                    context={"domain": fd.domain},
                )
            )
            continue
        members = sorted(set(fd.services))
        domains_by_name[fd.domain] = members
        added_domain_members[fd.domain] = members
        effective_add_domains.add(fd.domain)

    seen_update_domains: set[str] = set()
    for fd in patch.update_fault_domains:
        if fd.domain in seen_update_domains:
            issues.append(
                _patch_error(
                    "simulation_duplicate_fault_domain_op",
                    f"Fault domain '{fd.domain}' is updated more than once.",
                    "update_fault_domains",
                    context={"domain": fd.domain},
                )
            )
            continue
        seen_update_domains.add(fd.domain)
        _check_domain_members("update_fault_domains", fd.domain, fd.services)
        if fd.domain in effective_add_domains:
            issues.append(
                _patch_error(
                    "simulation_fault_domain_conflicting_ops",
                    (
                        f"Fault domain '{fd.domain}' is both added and updated "
                        "in the same simulation."
                    ),
                    "update_fault_domains",
                    context={"domain": fd.domain},
                )
            )
            continue
        if fd.domain not in domains_by_name:
            issues.append(
                _patch_error(
                    "simulation_fault_domain_not_found",
                    (
                        f"Fault domain '{fd.domain}' does not exist in the "
                        "stored plan and cannot be updated; add it instead."
                    ),
                    "update_fault_domains",
                    context={"domain": fd.domain},
                )
            )
            continue
        old_members = set(domains_by_name[fd.domain])
        members = sorted(set(fd.services))
        domains_by_name[fd.domain] = members
        added_domain_members[fd.domain] = sorted(set(members) - old_members)

    seen_remove_domains: set[str] = set()
    for name in patch.remove_fault_domains:
        if name in seen_remove_domains:
            issues.append(
                _patch_error(
                    "simulation_duplicate_fault_domain_op",
                    f"Fault domain '{name}' is removed more than once.",
                    "remove_fault_domains",
                    context={"domain": name},
                )
            )
            continue
        seen_remove_domains.add(name)
        if name in effective_add_domains:
            issues.append(
                _patch_error(
                    "simulation_fault_domain_conflicting_ops",
                    (
                        f"Fault domain '{name}' is both added and removed in "
                        "the same simulation."
                    ),
                    "remove_fault_domains",
                    context={"domain": name},
                )
            )
            continue
        if name not in domains_by_name:
            issues.append(
                _patch_error(
                    "simulation_fault_domain_not_found",
                    (
                        f"Fault domain '{name}' does not exist in the stored "
                        "plan and cannot be removed."
                    ),
                    "remove_fault_domains",
                    context={"domain": name},
                )
            )
            continue
        domains_by_name.pop(name)
        added_domain_members.pop(name, None)

    if issues:
        raise PlanningError(
            "The simulation patch is invalid for this plan.", issues
        )

    for name in sorted(domains_by_name):
        new_domains.append(
            {"domain": name, "services": list(domains_by_name[name])}
        )
    new_fault_cliques = [
        members for members in domains_by_name.values() if len(members) >= 2
    ]
    return (
        sorted(new_edges),
        new_domains,
        new_fault_cliques,
        added_edges,
        added_domain_members,
    )


def _simulation_cycle_issue(
    cycles: list[list[str]],
    added_edges: set[tuple[str, str]],
) -> Issue:
    """Cycle diagnostic, annotated with which closing edges the patch adds."""
    cycle = cycles[0]
    closing_edges = [
        [cycle[i], cycle[(i + 1) % len(cycle)]] for i in range(len(cycle))
    ]
    introduced = [
        edge
        for edge in closing_edges
        if (edge[0], edge[1]) in added_edges
    ]
    return Issue(
        code="dependency_cycle",
        message=(
            f"The simulated constraints contain {len(cycles)} dependency "
            "cycle(s); no release order exists. First cycle: "
            f"{_format_cycle(cycle)}"
        ),
        constraint="add_dependencies" if introduced else "dependencies",
        service=cycle[0],
        related_services=cycle,
        context={
            "cycles": cycles,
            "cycle_count": len(cycles),
            "added_edges_in_cycle": introduced,
        },
    )


def simulate_plan_change(
    base_request: PlanCreate,
    base_waves: list[list[str]],
    patch: SimulationRequest,
    settings: Settings,
) -> SimulationOutput:
    """Re-plan a stored plan under a constraint patch, without persisting.

    The service set comes from the stored (already validated) request; the
    stored ``max_waves`` window is intentionally ignored — a simulation is
    always solved against the unconstrained horizon so the minimum-wave
    optimum under the new constraints can be reported.
    """
    services = list(dict.fromkeys(base_request.services))
    base_edges = list(
        dict.fromkeys((d.service, d.depends_on) for d in base_request.dependencies)
    )
    base_domains = [
        {"domain": fd.domain, "services": sorted(set(fd.services))}
        for fd in base_request.fault_domains
    ]

    (
        new_edges,
        new_domains,
        fault_cliques,
        added_edges,
        added_domain_members,
    ) = _apply_simulation_patch(services, base_edges, base_domains, patch)

    concurrency = (
        patch.max_concurrency
        if patch.max_concurrency is not None
        else base_request.max_concurrency
    )

    adj = build_adjacency(services, new_edges)
    cycles = find_cycles(services, adj, settings.max_reported_cycles)
    if cycles:
        raise PlanningError(
            "The simulated dependency graph contains a cycle.",
            [_simulation_cycle_issue(cycles, added_edges)],
        )

    # Original placement, derived from the stored wave member lists.
    original_assignment: dict[str, int] = {
        s: i for i, members in enumerate(base_waves) for s in members
    }
    # Defensive fallback if the stored result is ever short on members.
    for s in services:
        original_assignment.setdefault(s, 0)

    # Services the *added* constraints directly violate in the original plan.
    affected: set[str] = set()
    for a, b in added_edges:
        if original_assignment[a] <= original_assignment[b]:
            affected.add(a)
            affected.add(b)
    new_membership = {d["domain"]: d["services"] for d in new_domains}
    for domain, added_members in added_domain_members.items():
        # A pair is newly anti-affine iff at least one endpoint was added to
        # the domain; compare each added member against the full new list.
        full_members = new_membership.get(domain, [])
        for member in added_members:
            for other in full_members:
                if (
                    other != member
                    and original_assignment[member]
                    == original_assignment[other]
                ):
                    affected.add(member)
                    affected.add(other)

    result = _solve_simulation(
        services,
        new_edges,
        fault_cliques,
        concurrency,
        original_assignment,
        settings,
    )
    if not result.feasible:
        # Defensive: with horizon == n, waves of size 1 always work for DAGs.
        raise RuntimeError("Unexpected simulator infeasibility")

    groups: dict[int, list[str]] = {}
    for s, w in result.assignment.items():
        groups.setdefault(w, []).append(s)
    waves = [sorted(groups[old]) for old in sorted(groups)]

    return SimulationOutput(
        waves=waves,
        wave_count=len(waves),
        assignment=result.assignment,
        optimal=result.optimal,
        concurrency=concurrency,
        affected_services=sorted(affected),
    )
