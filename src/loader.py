"""Load and parse all PS1 CSV data into structured objects."""
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import date, timedelta


@dataclass
class Parameters:
    horizon_start: date
    horizon_weeks: int

    @property
    def weeks(self) -> List[int]:
        """Return list of week numbers (1-indexed)."""
        return list(range(1, self.horizon_weeks + 1))

    def week_start(self, week: int) -> date:
        return self.horizon_start + timedelta(weeks=week - 1)

    def date_to_week(self, d: date) -> int:
        """Convert a date to the earliest week number (1-indexed) that starts on or after d."""
        delta = (d - self.horizon_start).days
        week = delta // 7 + 1
        return max(1, min(week, self.horizon_weeks))


@dataclass
class Location:
    location_id: str
    location_kind: str
    line_code: str
    bound: str
    supply_capacity: int


@dataclass
class Contract:
    contract_number: str
    description: str
    nature_of_activity: str   # "Live", "Non-live (Consist)", "Non-live (Others)"
    contract_priority: int    # 1=highest, 3=lowest
    contract_completion_date: date
    planned_completion_date: date
    number_of_workfronts: int
    access_type: str          # "C", "PC", "PM"
    max_access_per_week: int  # 2 for Live, 3 for others


@dataclass
class Activity:
    activity_id: str
    contract_number: str
    activity_type: str
    start_location_id: str
    end_location_id: str
    total_accesses: int       # how many nights needed
    planned_start_date: date
    predecessor_id: Optional[str]
    activity_priority: int    # 1=highest, 3=lowest
    # derived
    occupied_locations: List[str] = field(default_factory=list)


@dataclass
class BufferRule:
    nature_of_works: str
    up_to_buffer_sectors: int
    opposite_bound_required: bool


class DataLoader:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)

    def load(self):
        params = self._load_params()
        locations = self._load_locations()
        contracts = self._load_contracts()
        activities = self._load_activities()
        buffers = self._load_buffers()
        sectors_ordered = self._load_sectors()

        # Compute occupied locations per activity
        for act in activities.values():
            act.occupied_locations = self._compute_occupied_locations(
                act, sectors_ordered, locations
            )

        return params, locations, contracts, activities, buffers, sectors_ordered

    def _load_params(self) -> Parameters:
        df = pd.read_csv(self.data_dir / "06_PARAMETERS.csv")
        d = dict(zip(df["key"], df["value"]))
        return Parameters(
            horizon_start=date.fromisoformat(d["horizon_start"]),
            horizon_weeks=int(d["horizon_weeks"]),
        )

    def _load_locations(self) -> Dict[str, Location]:
        df = pd.read_csv(self.data_dir / "04_LOCATION_SUPPLY.csv")
        result = {}
        for _, row in df.iterrows():
            loc = Location(
                location_id=row["location_id"],
                location_kind=row["location_kind"],
                line_code=row["line_code"],
                bound=row["bound"],
                supply_capacity=int(row["supply_capacity"]),
            )
            result[loc.location_id] = loc
        return result

    def _load_contracts(self) -> Dict[str, Contract]:
        df = pd.read_csv(self.data_dir / "07_PROJECT_DETAILS.csv")
        result = {}
        for _, row in df.iterrows():
            c = Contract(
                contract_number=row["contract_number"],
                description=row["contract_description"],
                nature_of_activity=row["nature_of_activity"],
                contract_priority=int(row["contract_priority"]),
                contract_completion_date=date.fromisoformat(row["contract_completion_date"]),
                planned_completion_date=date.fromisoformat(row["planned_completion_date"]),
                number_of_workfronts=int(row["number_of_workfronts"]),
                access_type=row["access_type"],
                max_access_per_week=int(row["number_of_maximum_access_per_week"]),
            )
            result[c.contract_number] = c
        return result

    def _load_activities(self) -> Dict[str, Activity]:
        df = pd.read_csv(self.data_dir / "08_ACTIVITY_DETAILS.csv")
        result = {}
        for _, row in df.iterrows():
            pred = row["predecessor_activity_id"]
            pred = None if pd.isna(pred) or str(pred).strip() == "" else str(pred).strip()
            a = Activity(
                activity_id=row["activity_id"],
                contract_number=row["contract_number"],
                activity_type=row["activity_type"],
                start_location_id=row["start_location_id"],
                end_location_id=row["end_location_id"],
                total_accesses=int(row["total_accesses"]),
                planned_start_date=date.fromisoformat(row["planned_start_date"]),
                predecessor_id=pred,
                activity_priority=int(row["activity_priority"]),
            )
            result[a.activity_id] = a
        return result

    def _load_buffers(self) -> Dict[str, BufferRule]:
        df = pd.read_csv(self.data_dir / "05_BUFFER_LOCATION.csv")
        result = {}
        for _, row in df.iterrows():
            b = BufferRule(
                nature_of_works=row["nature_of_works"],
                up_to_buffer_sectors=int(row["up_to_buffer_sectors"]),
                opposite_bound_required=bool(int(row["opposite_bound_required"])),
            )
            result[b.nature_of_works] = b
        return result

    def _load_sectors(self) -> Dict[str, List[str]]:
        """Returns {line_code: [sector_ids in sequence order]}"""
        df = pd.read_csv(self.data_dir / "03_SECTORS.csv")
        df = df.sort_values(["line_code", "seq"])
        result = {}
        for line, grp in df.groupby("line_code"):
            result[line] = list(grp["sector_id"])
        return result

    def _compute_occupied_locations(
        self,
        act: Activity,
        sectors_ordered: Dict[str, List[str]],
        locations: Dict[str, Location],
    ) -> List[str]:
        """
        Determine which location IDs an activity occupies.
        An activity spans from start_location_id to end_location_id (inclusive),
        on the same line and bound.
        """
        start = act.start_location_id
        end = act.end_location_id

        # Parse line and bound from start location
        # Format: SEC:LINE:FROM_TO:BOUND or PLAT:LINE:STATION:BOUND
        parts = start.split(":")
        if len(parts) < 3:
            return [start]

        line_code = parts[1]
        bound = parts[-1] if parts[-1] in ("EB", "WB") else None

        if line_code not in sectors_ordered or bound is None:
            return [start]

        # Get all locations on this line+bound in order
        ordered_locs = []
        for loc_id, loc in locations.items():
            if loc.line_code == line_code and loc.bound == bound:
                ordered_locs.append(loc_id)

        # Sort by sector sequence
        def sort_key(loc_id):
            # Extract sector base (without bound suffix)
            for i, sec in enumerate(sectors_ordered.get(line_code, [])):
                if sec in loc_id:
                    return i
            return 999

        ordered_locs.sort(key=sort_key)

        # Find start and end indices
        try:
            si = next(i for i, l in enumerate(ordered_locs) if start in l or l == start)
        except StopIteration:
            si = 0
        try:
            ei = next(i for i, l in enumerate(ordered_locs) if end in l or l == end)
        except StopIteration:
            ei = len(ordered_locs) - 1

        if si > ei:
            si, ei = ei, si

        return ordered_locs[si:ei + 1]
