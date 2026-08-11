# -*- coding: utf-8 -*-
"""
Stage 5 -- ordering by weighted latency.

The driver works one night and stops when the night ends, so the deliverable
is not a fast tour -- it is an order in which *every prefix* is worth as much
as it can be. That is the weighted minimum latency problem (weighted
travelling repairman):

    minimize  sum over stops i of  envelope_count[i] * arrival_time[i]

This is emphatically not what a TSP minimizes, and it is not what OR-Tools'
routing objective minimizes either. SetSpanCostCoefficient penalizes route
*span* -- the total width of the time window -- which says nothing about when
each individual stop is reached. So OR-Tools is used only to build a decent
seed tour, and the real work is a local search that accepts a move only when
it lowers the weighted-latency objective above.

Move operators are Or-opt (relocate a run of 1-3 stops, orientation intact)
and optionally 2-opt. 2-opt is off by default: reversing a segment on an
asymmetric matrix re-costs every arc inside it, which is both expensive and
prone to producing routes that fight the one-way grid.

    python -m src.stage5_order
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.normalize import canonicalize_street

EPSILON = 1e-9


# --------------------------------------------------------------------------
# Problem instance
# --------------------------------------------------------------------------

class Instance:
    """Everything the objective needs, precomputed once."""

    def __init__(self, stops: list[dict], durations: list[list[float]]) -> None:
        self.stops = stops
        self.size = len(stops)

        # Seconds -> minutes. Unreachable pairs are never replaced by a
        # straight-line guess; they are made prohibitively expensive so the
        # search routes around them, and are reported separately.
        self.unreachable: list[tuple[int, int]] = []
        self.travel = [[0.0] * self.size for _ in range(self.size)]
        for i in range(self.size):
            for j in range(self.size):
                value = durations[i][j]
                if value is None:
                    self.unreachable.append((i, j))
                    self.travel[i][j] = float("inf")
                else:
                    self.travel[i][j] = value / 60.0

        self.envelopes = [s["envelope_count"] for s in stops]
        self.service = [
            config.BASE_SERVICE_TIME_MINUTES
            + config.PER_ENVELOPE_TIME_MINUTES * s["envelope_count"]
            for s in stops
        ]

        self.street = [canonicalize_street(s["street"])[0] for s in stops]
        arterials = {canonicalize_street(name)[0] for name in config.ARTERIAL_STREETS}
        self.is_arterial = [street in arterials for street in self.street]

        self.street_totals = Counter(self.street)
        self.total_envelopes = sum(self.envelopes)

        self.neighbors = self._build_neighbor_lists()

    def _build_neighbor_lists(self, k: int = 12) -> list[list[int]]:
        """Nearest stops by the cheaper of the two directions."""
        neighbors = []
        for i in range(self.size):
            ranked = sorted(
                (j for j in range(self.size) if j != i),
                key=lambda j: min(self.travel[i][j], self.travel[j][i]),
            )
            neighbors.append(ranked[:k])
        return neighbors

    def leg_time(self, source: int, target: int) -> float:
        """Travel minutes, with the arterial surcharge applied."""
        minutes = self.travel[source][target]
        if self.is_arterial[source] or self.is_arterial[target]:
            # OSRM /table gives no per-leg street breakdown, so a leg is
            # treated as touching an arterial when either endpoint sits on
            # one. Approximate, but it biases the route toward quiet interior
            # streets, which is the intent.
            minutes *= config.ARTERIAL_PENALTY_MULTIPLIER
        return minutes


# --------------------------------------------------------------------------
# The objective
# --------------------------------------------------------------------------

def evaluate(instance: Instance, order: list[int]) -> dict:
    """
    Walk the route and accumulate weighted latency.

    Street-revisit penalties are history-dependent -- whether re-entering a
    street costs anything depends on whether it was abandoned with stops
    still on it -- so the objective is computed by a full forward pass rather
    than from cached arc deltas.
    """
    travel_total = 0.0
    clock = 0.0
    latency = 0.0
    revisit_penalties = 0
    arrivals = [0.0] * len(order)

    remaining = dict(instance.street_totals)
    last_position: dict[str, int] = {}
    abandoned: set[str] = set()

    street = instance.street
    envelopes = instance.envelopes
    service = instance.service
    threshold = config.DETOUR_RETURN_THRESHOLD_STOPS
    penalty = config.STREET_REVISIT_PENALTY_MINUTES

    for position, stop in enumerate(order):
        current_street = street[stop]

        if position > 0:
            previous = order[position - 1]
            leg = instance.leg_time(previous, stop)
            travel_total += leg
            clock += service[previous] + leg

            if current_street in last_position:
                intervening = position - last_position[current_street] - 1
                # A short branch that comes straight back is cheap and
                # desirable, and must not be discouraged. Only a genuine
                # return to an abandoned street is charged.
                if intervening > threshold and current_street in abandoned:
                    clock += penalty
                    revisit_penalties += 1
                    abandoned.discard(current_street)

        arrivals[position] = clock
        latency += envelopes[stop] * clock

        remaining[current_street] -= 1
        last_position[current_street] = position

        is_last = position == len(order) - 1
        leaving = is_last or street[order[position + 1]] != current_street
        if leaving and remaining[current_street] > 0:
            abandoned.add(current_street)

    finish = clock + (service[order[-1]] if order else 0.0)

    return {
        "latency": latency,
        "arrivals": arrivals,
        "travel_minutes": travel_total,
        "total_minutes": finish,
        "revisit_penalties": revisit_penalties,
    }


def latency_of(instance: Instance, order: list[int]) -> float:
    return evaluate(instance, order)["latency"]


# --------------------------------------------------------------------------
# Seeds
# --------------------------------------------------------------------------

def seed_ortools_tsp(instance: Instance) -> list[int]:
    """
    Minimum-total-time open tour.

    RoutingModel has no depot-free mode, so a dummy node with zero cost to and
    from every real stop is added and the route starts and ends there. The
    dummy contributes no travel and no service time, which makes the result a
    genuine open route -- verified explicitly in verify_open_route().
    """
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    size = instance.size
    dummy = size
    scale = 100  # OR-Tools needs integer arc costs

    manager = pywrapcp.RoutingIndexManager(size + 1, 1, dummy)
    routing = pywrapcp.RoutingModel(manager)

    def arc_cost(from_index: int, to_index: int) -> int:
        source = manager.IndexToNode(from_index)
        target = manager.IndexToNode(to_index)
        if source == dummy or target == dummy:
            return 0
        minutes = instance.leg_time(source, target) + instance.service[target]
        if minutes == float("inf"):
            return 10**9
        return int(round(minutes * scale))

    transit = routing.RegisterTransitCallback(arc_cost)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    parameters.time_limit.FromSeconds(config.SOLVER_TIME_LIMIT_SECONDS)

    solution = routing.SolveWithParameters(parameters)
    if solution is None:
        raise SystemExit("OR-Tools found no seed solution.")

    order: list[int] = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node != dummy:
            order.append(node)
        index = solution.Value(routing.NextVar(index))

    return order


def seed_greedy_ratio(instance: Instance, start: int) -> list[int]:
    """
    Repeatedly append whichever stop offers the most envelopes per minute of
    delay it adds. A latency-aware construction, unlike nearest-neighbour.
    """
    remaining = set(range(instance.size)) - {start}
    order = [start]

    while remaining:
        current = order[-1]
        best, best_score = None, -1.0
        for candidate in remaining:
            cost = instance.leg_time(current, candidate) + instance.service[current]
            if cost == float("inf"):
                continue
            score = instance.envelopes[candidate] / max(cost, 1e-6)
            if score > best_score:
                best, best_score = candidate, score
        if best is None:
            best = next(iter(remaining))
        order.append(best)
        remaining.discard(best)

    return order


def seed_density_greedy(instance: Instance) -> list[int]:
    """Baseline: densest buildings first, geography ignored entirely."""
    return sorted(
        range(instance.size),
        key=lambda i: (-instance.envelopes[i], i),
    )


# --------------------------------------------------------------------------
# Local search on the latency objective
# --------------------------------------------------------------------------

def or_opt_search(instance: Instance, order: list[int], deadline: float) -> tuple[list[int], int]:
    """
    First-improvement Or-opt on weighted latency.

    Candidate insertion points are restricted to the neighbourhood of the
    moved segment; a full n^2 scan buys very little here and costs a lot.
    """
    best = list(order)
    best_value = latency_of(instance, best)
    evaluations = 0
    improved = True

    while improved and time.monotonic() < deadline:
        improved = False

        for length in config.OR_OPT_SEGMENT_LENGTHS:
            for start in range(len(best) - length + 1):
                if time.monotonic() >= deadline:
                    break

                segment = best[start:start + length]
                rest = best[:start] + best[start + length:]

                candidate_positions = set()
                for stop in (segment[0], segment[-1]):
                    for neighbor in instance.neighbors[stop]:
                        if neighbor in rest:
                            index = rest.index(neighbor)
                            candidate_positions.add(index)
                            candidate_positions.add(index + 1)
                candidate_positions.add(0)

                for position in sorted(candidate_positions):
                    if position == start:
                        continue
                    trial = rest[:position] + segment + rest[position:]
                    value = latency_of(instance, trial)
                    evaluations += 1
                    if value < best_value - EPSILON:
                        best, best_value = trial, value
                        improved = True
                        break

                if improved:
                    break
            if improved:
                break

    return best, evaluations


def two_opt_search(instance: Instance, order: list[int], deadline: float) -> list[int]:
    """Optional reversing pass. Off by default -- see the module docstring."""
    best = list(order)
    best_value = latency_of(instance, best)
    improved = True

    while improved and time.monotonic() < deadline:
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 2, len(best)):
                trial = best[:i] + best[i:j][::-1] + best[j:]
                value = latency_of(instance, trial)
                if value < best_value - EPSILON:
                    best, best_value = trial, value
                    improved = True
                    break
            if improved:
                break

    return best


def optimize(instance: Instance, seeds: dict[str, list[int]]) -> tuple[list[int], dict]:
    deadline = time.monotonic() + config.LATENCY_LOCAL_SEARCH_TIME_LIMIT_SECONDS
    results = {}
    best_order, best_value, best_seed = None, float("inf"), None

    for name, seed in seeds.items():
        order, evaluations = or_opt_search(instance, seed, deadline)
        if config.ENABLE_TWO_OPT:
            order = two_opt_search(instance, order, deadline)

        value = latency_of(instance, order)
        results[name] = {
            "seed_latency": latency_of(instance, seed),
            "optimized_latency": value,
            "evaluations": evaluations,
        }
        print(f"  seed {name:<14} {results[name]['seed_latency']:>12.1f} -> "
              f"{value:>12.1f}  ({evaluations} evaluations)")

        if value < best_value:
            best_order, best_value, best_seed = order, value, name

    return best_order, {"per_seed": results, "winning_seed": best_seed}


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def verify_open_route(instance: Instance, order: list[int]) -> list[str]:
    """The dummy node must contribute nothing and the route must not close."""
    checks = []
    checks.append(f"route length {len(order)} == stop count {instance.size}: "
                  f"{len(order) == instance.size}")
    checks.append(f"dummy node absent from output: {max(order) < instance.size}")
    checks.append(f"first stop {order[0]} != last stop {order[-1]}: "
                  f"{order[0] != order[-1]}")
    checks.append("return leg last->first not charged: "
                  "True (objective sums arrivals only, no closing arc)")
    return checks


def sanity_checks(instance: Instance, order: list[int]) -> list[str]:
    problems = []

    if len(order) != instance.size:
        problems.append(f"route has {len(order)} stops, expected {instance.size}")
    if len(set(order)) != len(order):
        duplicates = [s for s, c in Counter(order).items() if c > 1]
        problems.append(f"duplicated stops: {duplicates}")
    missing = set(range(instance.size)) - set(order)
    if missing:
        problems.append(f"missing stops: {sorted(missing)}")

    routed_envelopes = sum(instance.envelopes[i] for i in order)
    if routed_envelopes != instance.total_envelopes:
        problems.append(f"envelope total {routed_envelopes} != "
                        f"{instance.total_envelopes}")

    for position in range(len(order) - 1):
        if instance.travel[order[position]][order[position + 1]] == float("inf"):
            problems.append(f"unreachable leg at position {position}")

    return problems


# --------------------------------------------------------------------------
# Baseline comparison
# --------------------------------------------------------------------------

def prefix_points(size: int) -> list[int]:
    configured = [p for p in config.BASELINE_REPORT_PREFIXES if p <= size]
    if configured:
        points = configured
    else:
        # Dataset smaller than the configured reporting prefixes -- fall back
        # to quartiles so the comparison still says something.
        points = sorted({max(1, size // 4), max(1, size // 2), max(1, 3 * size // 4)})
    if size not in points:
        points.append(size)
    return points


def prefix_stats(instance: Instance, order: list[int], points: list[int]) -> dict:
    result = evaluate(instance, order)
    stats = {"latency": result["latency"], "prefixes": {}}
    cumulative = 0

    for position, stop in enumerate(order, start=1):
        cumulative += instance.envelopes[stop]
        if position in points:
            stats["prefixes"][position] = {
                "envelopes": cumulative,
                "minutes": result["arrivals"][position - 1]
                + instance.service[stop],
            }
    return stats


def comparison_table(instance: Instance, orderings: dict[str, list[int]]) -> str:
    points = prefix_points(instance.size)

    header = "| שיטה                | " + " | ".join(f"מעטפות @{p}" for p in points)
    header += " | זמן @סוף (דק') | Latency |"
    separator = "|" + "---|" * (len(points) + 3)

    lines = [
        "=" * 78,
        "שלב 5 - השוואת שיטות סידור (יעד: מינימום Latency משוקלל)",
        "=" * 78,
        header,
        separator,
    ]

    for label, order in orderings.items():
        stats = prefix_stats(instance, order, points)
        cells = [f"{stats['prefixes'][p]['envelopes']:>6}" for p in points]
        last = stats["prefixes"][points[-1]]["minutes"]
        lines.append(
            f"| {label:<19} | " + " | ".join(cells)
            + f" | {last:>13.1f} | {stats['latency']:>10.1f} |"
        )

    lines.append("=" * 78)
    lines.append("Latency נמוך יותר = טוב יותר. כל קידומת של המסלול שווה יותר.")
    lines.append("")
    lines.append(time_budget_table(instance, orderings))
    return "\n".join(lines)


def envelopes_by_minute(instance: Instance, order: list[int], budget: float) -> int:
    """How many envelopes are delivered before the clock runs out."""
    result = evaluate(instance, order)
    delivered = 0
    for position, stop in enumerate(order):
        finished_at = result["arrivals"][position] + instance.service[stop]
        if finished_at > budget:
            break
        delivered += instance.envelopes[stop]
    return delivered


def time_budget_table(instance: Instance, orderings: dict[str, list[int]]) -> str:
    """
    The comparison that actually matches how the night works.

    Counting envelopes at a fixed *stop count* flatters any ordering that
    picks dense buildings regardless of distance, because it ignores how long
    those stops took to reach. The driver runs out of minutes, not out of
    stops, so the same orderings are compared at fixed time budgets.
    """
    reference = evaluate(instance, orderings["Latency משוקלל"])["total_minutes"]
    budgets = [round(reference * fraction) for fraction in (0.25, 0.5, 0.75, 1.0)]

    lines = [
        "-" * 78,
        "מעטפות שנמסרו בתוך תקציב זמן (המדד האמיתי - הלילה נגמר בזמן, לא בעצירות)",
        "-" * 78,
        "| שיטה                | " + " | ".join(f"{b} דק'" for b in budgets) + " |",
        "|" + "---|" * (len(budgets) + 1),
    ]
    for label, order in orderings.items():
        cells = [f"{envelopes_by_minute(instance, order, b):>6}" for b in budgets]
        lines.append(f"| {label:<19} | " + " | ".join(cells) + " |")
    lines.append("-" * 78)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    random.seed(config.RANDOM_SEED)

    if not config.STAGE3_STOPS.exists() or not config.STAGE4_MATRIX.exists():
        raise SystemExit("Run stages 3 and 4 first.")

    stops = json.loads(config.STAGE3_STOPS.read_text(encoding="utf-8"))["stops"]
    matrix_payload = json.loads(config.STAGE4_MATRIX.read_text(encoding="utf-8"))

    if matrix_payload["stop_ids"] != [s["stop_id"] for s in stops]:
        raise SystemExit(
            "travel_time_matrix.json was built for a different stop set. "
            "Re-run stage 4."
        )

    instance = Instance(stops, matrix_payload["durations"])
    if instance.unreachable:
        print(f"! {len(instance.unreachable)} unreachable pairs reported by OSRM; "
              f"routed around, never replaced with straight-line estimates")

    print("building seeds")
    tsp_order = seed_ortools_tsp(instance)

    # Greedy-ratio seeds from a few plausible openings; the densest buildings
    # and whatever the TSP chose to start from.
    candidate_starts = sorted(
        range(instance.size), key=lambda i: -instance.envelopes[i]
    )[:3] + [tsp_order[0]]
    ratio_seeds = {
        f"greedy_ratio@{start}": seed_greedy_ratio(instance, start)
        for start in dict.fromkeys(candidate_starts)
    }
    best_ratio = min(ratio_seeds.values(), key=lambda o: latency_of(instance, o))

    seeds = {"ortools_tsp": tsp_order, "greedy_ratio": best_ratio}

    print("optimizing on weighted latency")
    optimized, search_stats = optimize(instance, seeds)

    problems = sanity_checks(instance, optimized)
    if problems:
        print("\n*** SANITY CHECKS FAILED ***")
        for problem in problems:
            print(f"    - {problem}")
        raise SystemExit(1)

    print("\nopen-route verification:")
    for check in verify_open_route(instance, optimized):
        print(f"    {check}")

    orderings = {
        "צפיפות (density greedy)": seed_density_greedy(instance),
        "TSP קלאסי (זמן כולל)": tsp_order,
        "Latency משוקלל": optimized,
    }
    table = comparison_table(instance, orderings)
    print("\n" + table)

    optimized_result = evaluate(instance, optimized)
    beats_baselines = all(
        optimized_result["latency"] <= latency_of(instance, other) + EPSILON
        for label, other in orderings.items()
        if label != "Latency משוקלל"
    )
    if not beats_baselines:
        print("\n*** WARNING: optimized ordering does not beat a baseline. "
              "The objective is implemented wrong. Investigate before use. ***")
        raise SystemExit(1)

    config.OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    config.STAGE5_ORDER.write_text(
        json.dumps(
            {
                "order": optimized,
                "stop_ids": [stops[i]["stop_id"] for i in optimized],
                "latency": optimized_result["latency"],
                "travel_minutes": optimized_result["travel_minutes"],
                "total_minutes": optimized_result["total_minutes"],
                "street_revisit_penalties": optimized_result["revisit_penalties"],
                "arrivals_minutes": optimized_result["arrivals"],
                "search": search_stats,
                "unreachable_pairs": len(instance.unreachable),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config.STAGE5_COMPARISON.write_text(table, encoding="utf-8")
    print(f"\nwrote {config.STAGE5_ORDER.name}")


if __name__ == "__main__":
    main()
