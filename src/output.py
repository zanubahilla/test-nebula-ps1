"""Generate output CSV files matching submission format."""
import csv
from pathlib import Path
from typing import List, Dict

from scheduler import AccessAssignment, OccupancyRecord


def write_outputs(
    results: List[Dict],
    assignments: List[AccessAssignment],
    occupancies: List[OccupancyRecord],
    out_dir: str,
):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # RESULTS.csv
    with open(out_path / "RESULTS.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["scenario", "contract_number", "simulated_completion_date", "overrun_days"])
        w.writeheader()
        w.writerows(results)

    # SCHEDULE_ACCESS.csv
    with open(out_path / "SCHEDULE_ACCESS.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["activity_id", "access_seq", "week", "eclo", "access_night"])
        w.writeheader()
        for a in sorted(assignments, key=lambda x: (x.activity_id, x.access_seq)):
            w.writerow({
                "activity_id": a.activity_id,
                "access_seq": a.access_seq,
                "week": a.week,
                "eclo": int(a.eclo),
                "access_night": a.access_night,
            })

    # SCHEDULE_OCCUPANCY.csv
    with open(out_path / "SCHEDULE_OCCUPANCY.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["activity_id", "week", "location_id", "co_share_group"])
        w.writeheader()
        for o in sorted(occupancies, key=lambda x: (x.activity_id, x.week, x.location_id)):
            w.writerow({
                "activity_id": o.activity_id,
                "week": o.week,
                "location_id": o.location_id,
                "co_share_group": o.co_share_group,
            })

    print(f"Outputs written to {out_path}")
    print(f"  RESULTS.csv:           {len(results)} rows")
    print(f"  SCHEDULE_ACCESS.csv:   {len(assignments)} rows")
    print(f"  SCHEDULE_OCCUPANCY.csv:{len(occupancies)} rows")
