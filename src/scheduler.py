"""
Core greedy scheduler for PS1 Railway Track Access Optimisation.

Assigns each activity's access nights (weeks) while respecting:
- Predecessor constraints
- Planned start date (earliest week)
- Weekly per-contract access cap
- Per-night workfront limits per contract
- Location capacity (supply_capacity per location per night)
- Buffer zones (sectors ahead/behind worksite based on nature_of_works)
- Opposite-bound mirroring for Live works
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from datetime import date, timedelta
from collections import defaultdict

from loader import Parameters, Location, Contract, Activity, BufferRule


@dataclass
class AccessAssignment:
    activity_id: str
    access_seq: int       # 1-indexed access number for this activity
    week: int
    eclo: bool            # whether ECLO was used this night
    access_night: int     # night within week (1-7), simplified to 1 here


@dataclass
class OccupancyRecord:
    activity_id: str
    week: int
    location_id: str
    co_share_group: str


class Scheduler:
    def __init__(
        self,
        params: Parameters,
        locations: Dict[str, Location],
        contracts: Dict[str, Contract],
        activities: Dict[str, Activity],
        buffers: Dict[str, BufferRule],
        sectors_ordered: Dict[str, List[str]],
        scenario: str = "A",
        allow_eclo: bool = False,
        allow_excess: bool = False,
    ):
        self.params = params
        self.locations = locations
        self.contracts = contracts
        self.activities = activities
        self.buffers = buffers
        self.sectors_ordered = sectors_ordered
        self.scenario = scenario
        self.allow_eclo = allow_eclo  # Scenario B, C
        self.allow_excess = allow_excess  # Scenario B, C

        # State tracking
        # week -> contract -> list of activity_ids scheduled that week
        self.week_contract_accesses: Dict[int, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        # (week, location_id) -> count of activities occupying it
        self.week_location_usage: Dict[Tuple[int, str], int] = defaultdict(int)
        # activity_id -> list of weeks assigned
        self.activity_schedule: Dict[str, List[int]] = defaultdict(list)
        # activity_id -> co_share_group counter per week
        self.co_share_counter: Dict[Tuple[int, str], int] = defaultdict(int)

        self.assignments: List[AccessAssignment] = []
        self.occupancies: List[OccupancyRecord] = []

    def schedule(self) -> Tuple[List[AccessAssignment], List[OccupancyRecord]]:
        """Main scheduling loop using greedy priority-based approach."""

        # Sort activities by priority (contract priority, then activity priority, then planned_start)
        sorted_acts = sorted(
            self.activities.values(),
            key=lambda a: (
                self.contracts[a.contract_number].contract_priority,
                a.activity_priority,
                a.planned_start_date,
            ),
        )

        # Resolve predecessor ordering: topological sort
        sorted_acts = self._topo_sort(sorted_acts)

        for act in sorted_acts:
            self._schedule_activity(act)

        return self.assignments, self.occupancies

    def _topo_sort(self, acts: List[Activity]) -> List[Activity]:
        """Topological sort respecting predecessor relationships."""
        id_map = {a.activity_id: a for a in acts}
        visited = set()
        result = []

        def visit(a: Activity):
            if a.activity_id in visited:
                return
            if a.predecessor_id and a.predecessor_id in id_map:
                visit(id_map[a.predecessor_id])
            visited.add(a.activity_id)
            result.append(a)

        for a in acts:
            visit(a)
        return result

    def _schedule_activity(self, act: Activity):
        """Assign `act.total_accesses` nights to this activity."""
        contract = self.contracts[act.contract_number]
        earliest_week = self.params.date_to_week(act.planned_start_date)

        # If has predecessor, must start after predecessor finishes
        if act.predecessor_id and act.predecessor_id in self.activity_schedule:
            pred_weeks = self.activity_schedule[act.predecessor_id]
            if pred_weeks:
                earliest_week = max(earliest_week, max(pred_weeks) + 1)

        accesses_remaining = act.total_accesses
        access_seq = 1
        week = earliest_week

        while accesses_remaining > 0 and week <= self.params.horizon_weeks:
            if self._can_schedule(act, contract, week):
                # Assign this access
                eclo = False
                self._assign(act, contract, week, access_seq, eclo)
                access_seq += 1
                accesses_remaining -= 1
            week += 1

        # If accesses still remaining (couldn't fit in horizon), we've done our best

    def _can_schedule(self, act: Activity, contract: Contract, week: int) -> bool:
        """Check all hard constraints for scheduling act in this week."""

        # 1. Weekly access cap per contract
        used_this_week = len(self.week_contract_accesses[week][contract.contract_number])
        # Count distinct activities scheduled this week for this contract
        distinct_acts = set(self.week_contract_accesses[week][contract.contract_number])
        # Each access night counts as one; cap is max_access_per_week
        if used_this_week >= contract.max_access_per_week:
            return False

        # 2. Workfront limit: max concurrent activities per night per contract
        if len(distinct_acts) >= contract.number_of_workfronts:
            return False

        # 3. Location capacity
        for loc_id in act.occupied_locations:
            if loc_id not in self.locations:
                continue
            cap = self.locations[loc_id].supply_capacity
            used = self.week_location_usage[(week, loc_id)]
            if used >= cap:
                return False

        # 4. Buffer zone conflicts
        if not self._check_buffer_ok(act, contract, week):
            return False

        return True

    def _check_buffer_ok(self, act: Activity, contract: Contract, week: int) -> bool:
        """Check that buffer zones don't conflict with already-scheduled activities."""
        buffer_rule = self.buffers.get(contract.nature_of_activity)
        if buffer_rule is None or buffer_rule.up_to_buffer_sectors == 0:
            return True

        # Get all locations this activity occupies plus buffer zones
        act_locs = set(act.occupied_locations)
        buffer_locs = self._get_buffer_locations(act, buffer_rule)

        # Check no other activity this week occupies these buffer locations
        for (w, loc_id), count in self.week_location_usage.items():
            if w == week and count > 0:
                if loc_id in buffer_locs and loc_id not in act_locs:
                    return False

        return True

    def _get_buffer_locations(self, act: Activity, buffer_rule: BufferRule) -> Set[str]:
        """Get buffer zone location IDs for this activity."""
        if not act.occupied_locations:
            return set()

        # Parse line and bound from first location
        first_loc = act.occupied_locations[0]
        parts = first_loc.split(":")
        if len(parts) < 3:
            return set()

        line_code = parts[1]
        bound = parts[-1] if parts[-1] in ("EB", "WB") else None
        if bound is None:
            return set()

        opp_bound = "WB" if bound == "EB" else "EB"

        # Get all location IDs on same line+bound in order
        same_bound_locs = sorted(
            [lid for lid, loc in self.locations.items()
             if loc.line_code == line_code and loc.bound == bound],
            key=lambda lid: self._loc_order(lid, line_code)
        )

        act_loc_set = set(act.occupied_locations)
        buffer_locs = set()

        # Find indices of activity's locations
        act_indices = [i for i, lid in enumerate(same_bound_locs) if lid in act_loc_set]
        if not act_indices:
            return set()

        min_idx = min(act_indices)
        max_idx = max(act_indices)
        n = buffer_rule.up_to_buffer_sectors

        # Add buffer sectors ahead and behind
        for i in range(max(0, min_idx - n), min_idx):
            buffer_locs.add(same_bound_locs[i])
        for i in range(max_idx + 1, min(len(same_bound_locs), max_idx + n + 1)):
            buffer_locs.add(same_bound_locs[i])

        # For Live work: add opposite bound locations
        if buffer_rule.opposite_bound_required:
            opp_locs = [lid for lid, loc in self.locations.items()
                        if loc.line_code == line_code and loc.bound == opp_bound]
            buffer_locs.update(opp_locs)

        return buffer_locs

    def _loc_order(self, loc_id: str, line_code: str) -> int:
        """Return ordering index of a location based on sector sequence."""
        for i, sec in enumerate(self.sectors_ordered.get(line_code, [])):
            if sec in loc_id:
                return i
        return 999

    def _assign(self, act: Activity, contract: Contract, week: int, seq: int, eclo: bool):
        """Record the assignment."""
        self.week_contract_accesses[week][contract.contract_number].append(act.activity_id)
        for loc_id in act.occupied_locations:
            self.week_location_usage[(week, loc_id)] += 1

        self.activity_schedule[act.activity_id].append(week)

        # Co-share group: activities sharing the same location on the same night get same group
        # Use a letter-based group id per (week, first_location)
        group_key = (week, act.occupied_locations[0] if act.occupied_locations else "NONE")
        group_num = self.co_share_counter[group_key]
        self.co_share_counter[group_key] += 1
        co_share_group = f"g{week}_{group_num}"

        assignment = AccessAssignment(
            activity_id=act.activity_id,
            access_seq=seq,
            week=week,
            eclo=eclo,
            access_night=1,
        )
        self.assignments.append(assignment)

        for loc_id in act.occupied_locations:
            self.occupancies.append(OccupancyRecord(
                activity_id=act.activity_id,
                week=week,
                location_id=loc_id,
                co_share_group=co_share_group,
            ))
