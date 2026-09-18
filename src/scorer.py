"""
Compute scores for Scenarios A, B, C.
"""
from typing import Dict, List
from datetime import date, timedelta

from loader import Parameters, Contract, Activity
from scheduler import AccessAssignment


PRIORITY_WEIGHTS = {1: 100, 2: 10, 3: 1}
ACTIVITY_NUDGE = {1: 0.3, 2: 0.2, 3: 0.0}

EXCESS_NIGHT_COST = 7
ECLO_NIGHT_COST = 5


def compute_results(
    params: Parameters,
    contracts: Dict[str, Contract],
    activities: Dict[str, Activity],
    assignments: List[AccessAssignment],
    scenario: str,
) -> List[Dict]:
    """
    Compute per-contract results for the given scenario.
    Returns list of dicts with keys: scenario, contract_number, simulated_completion_date, overrun_days
    """
    # Build activity -> last assigned week
    act_last_week: Dict[str, int] = {}
    act_accesses_done: Dict[str, int] = {}

    for a in assignments:
        act_last_week[a.activity_id] = max(act_last_week.get(a.activity_id, 0), a.week)
        act_accesses_done[a.activity_id] = act_accesses_done.get(a.activity_id, 0) + 1

    results = []
    for cn, contract in contracts.items():
        # Find all activities for this contract
        contract_acts = [a for a in activities.values() if a.contract_number == cn]
        if not contract_acts:
            continue

        # Simulated completion = week after last activity finishes
        last_week = max(
            (act_last_week.get(a.activity_id, 0) for a in contract_acts),
            default=0
        )
        if last_week == 0:
            sim_completion = contract.planned_completion_date
        else:
            # Week `last_week` ends on horizon_start + last_week * 7
            sim_completion = params.horizon_start + timedelta(weeks=last_week)

        planned = contract.planned_completion_date
        overrun = max(0, (sim_completion - planned).days)

        results.append({
            "scenario": scenario,
            "contract_number": cn,
            "simulated_completion_date": sim_completion.isoformat(),
            "overrun_days": overrun,
        })

    return results


def compute_score(results: List[Dict], assignments: List[AccessAssignment], scenario: str, activities: Dict[str, Activity]) -> float:
    """Compute the scenario score."""

    if scenario == "A":
        total = 0.0
        for r in results:
            cn = r["contract_number"]
            overrun = r["overrun_days"]
            if overrun == 0:
                continue
            # Find contract acts for priority nudge
            # Use contract-level priority from results (we need contracts dict — pass through)
            total += overrun  # simplified; full weight needs contracts dict
        return total

    elif scenario == "B":
        excess_nights = sum(1 for a in assignments if a.week > 30)  # simplified
        eclo_nights = sum(1 for a in assignments if a.eclo)
        return EXCESS_NIGHT_COST * excess_nights + ECLO_NIGHT_COST * eclo_nights

    elif scenario == "C":
        overrun_score = sum(r["overrun_days"] for r in results)
        excess_nights = 0
        eclo_nights = sum(1 for a in assignments if a.eclo)
        return overrun_score + EXCESS_NIGHT_COST * excess_nights + ECLO_NIGHT_COST * eclo_nights

    return 0.0
