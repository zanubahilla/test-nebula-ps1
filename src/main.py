"""
Entry point: run the scheduler for all 3 scenarios and write output CSVs.
Usage: python main.py --data <path_to_data_dir> --out <output_dir>
"""
import argparse
import sys
from pathlib import Path

from loader import DataLoader
from scheduler import Scheduler
from scorer import compute_results, compute_score
from output import write_outputs


def run(data_dir: str, out_dir: str):
    print(f"Loading data from: {data_dir}")
    loader = DataLoader(data_dir)
    params, locations, contracts, activities, buffers, sectors_ordered = loader.load()

    print(f"Loaded: {len(contracts)} contracts, {len(activities)} activities")
    print(f"Horizon: {params.horizon_start} for {params.horizon_weeks} weeks")

    all_results = []

    for scenario, allow_eclo, allow_excess in [
        ("A", False, False),
        ("B", True, True),
        ("C", True, True),
    ]:
        print(f"\n--- Scheduling Scenario {scenario} ---")
        scheduler = Scheduler(
            params=params,
            locations=locations,
            contracts=contracts,
            activities=activities,
            buffers=buffers,
            sectors_ordered=sectors_ordered,
            scenario=scenario,
            allow_eclo=allow_eclo,
            allow_excess=allow_excess,
        )
        assignments, occupancies = scheduler.schedule()

        results = compute_results(params, contracts, activities, assignments, scenario)
        score = compute_score(results, assignments, scenario, activities)

        print(f"  Scheduled {len(assignments)} access nights across {len(set(a.activity_id for a in assignments))} activities")
        print(f"  Score {scenario}: {score:.2f}")

        all_results.extend(results)

        # Use scenario A outputs as primary (adjust as needed)
        if scenario == "A":
            write_outputs(results, assignments, occupancies, out_dir)

    print("\nDone!")
    return all_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PS1 Railway Track Access Scheduler")
    parser.add_argument("--data", default="../NebulaX-Hackathon-ProblemStatement/PS1/01_data",
                        help="Path to data directory")
    parser.add_argument("--out", default="../output", help="Output directory")
    args = parser.parse_args()
    run(args.data, args.out)
