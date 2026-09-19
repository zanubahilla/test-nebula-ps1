"""
FastAPI web application for PS1 Railway Track Access Optimisation.
Accepts uploaded CSV files or uses default dataset, runs scheduler, returns output CSVs.
"""
import io
import os
import zipfile
import tempfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

from loader import DataLoader
from scheduler import Scheduler
from scorer import compute_results, compute_score
from output import write_outputs

app = FastAPI(title="PS1 Railway Track Scheduler", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent.parent
DEFAULT_DATA = BASE_DIR.parent / "NebulaX-Hackathon-ProblemStatement" / "PS1" / "01_data"

load_dotenv(BASE_DIR / ".env")

RISK_BY_PRIORITY = {1: "HIGH", 2: "AMBER", 3: "LOW"}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = BASE_DIR / "templates" / "index.html"
    return HTMLResponse(content=html_file.read_text(encoding="utf-8"))


@app.post("/solve")
async def solve(
    scenario: str = Form("A"),
    use_default: bool = Form(True),
    lines_file: Optional[UploadFile] = File(None),
    stations_file: Optional[UploadFile] = File(None),
    sectors_file: Optional[UploadFile] = File(None),
    location_supply_file: Optional[UploadFile] = File(None),
    buffer_file: Optional[UploadFile] = File(None),
    parameters_file: Optional[UploadFile] = File(None),
    project_file: Optional[UploadFile] = File(None),
    activity_file: Optional[UploadFile] = File(None),
):
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()

        if use_default or not activity_file:
            # Copy default data files
            import shutil
            for f in DEFAULT_DATA.glob("*.csv"):
                shutil.copy(f, data_dir / f.name)
        else:
            # Save uploaded files
            file_map = {
                "01_LINES.csv": lines_file,
                "02_STATIONS.csv": stations_file,
                "03_SECTORS.csv": sectors_file,
                "04_LOCATION_SUPPLY.csv": location_supply_file,
                "05_BUFFER_LOCATION.csv": buffer_file,
                "06_PARAMETERS.csv": parameters_file,
                "07_PROJECT_DETAILS.csv": project_file,
                "08_ACTIVITY_DETAILS.csv": activity_file,
            }
            for fname, upload in file_map.items():
                if upload:
                    content = await upload.read()
                    (data_dir / fname).write_bytes(content)
                else:
                    # Fall back to default for missing files
                    import shutil
                    default_file = DEFAULT_DATA / fname
                    if default_file.exists():
                        shutil.copy(default_file, data_dir / fname)

        # Load and solve
        loader = DataLoader(str(data_dir))
        params, locations, contracts, activities, buffers, sectors_ordered = loader.load()

        scenarios = ["A", "B", "C"] if scenario == "ALL" else [scenario]
        all_assignments = []
        all_occupancies = []
        all_results = []

        for sc in scenarios:
            allow_eclo = sc in ("B", "C")
            allow_excess = sc in ("B", "C")
            scheduler = Scheduler(
                params=params,
                locations=locations,
                contracts=contracts,
                activities=activities,
                buffers=buffers,
                sectors_ordered=sectors_ordered,
                scenario=sc,
                allow_eclo=allow_eclo,
                allow_excess=allow_excess,
            )
            assignments, occupancies = scheduler.schedule()
            results = compute_results(params, contracts, activities, assignments, sc)
            score = compute_score(results, assignments, sc, activities)
            all_results.extend(results)
            if sc == scenarios[-1]:
                all_assignments = assignments
                all_occupancies = occupancies

        # Write to temp output dir
        out_dir = Path(tmpdir) / "output"
        write_outputs(all_results, all_assignments, all_occupancies, str(out_dir))

        # Bundle into zip
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for csv_file in out_dir.glob("*.csv"):
                zf.write(csv_file, csv_file.name)
        zip_buffer.seek(0)

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=ps1_results.zip"},
        )


@app.get("/api/summary")
async def summary():
    """Return summary of the default dataset."""
    loader = DataLoader(str(DEFAULT_DATA))
    params, locations, contracts, activities, buffers, sectors_ordered = loader.load()

    scheduler = Scheduler(
        params=params,
        locations=locations,
        contracts=contracts,
        activities=activities,
        buffers=buffers,
        sectors_ordered=sectors_ordered,
        scenario="A",
    )
    assignments, occupancies = scheduler.schedule()
    results = compute_results(params, contracts, activities, assignments, "A")

    return {
        "horizon_start": str(params.horizon_start),
        "horizon_weeks": params.horizon_weeks,
        "contracts": len(contracts),
        "activities": len(activities),
        "scheduled_accesses": len(assignments),
        "results": results,
    }


@app.get("/api/dashboard")
async def dashboard():
    """
    Real data for the ssl-rail-dispatch UI: per-scenario scores + a queue of
    upcoming possessions, built from the default dataset. Fictional telemetry
    (substation feeders, live train positions, signal aspects) has no
    equivalent in the PS1 data model and is not produced here.
    """
    loader = DataLoader(str(DEFAULT_DATA))
    params, locations, contracts, activities, buffers, sectors_ordered = loader.load()

    scenarios = {}
    possessions = []

    for sc, allow_eclo, allow_excess in [("A", False, False), ("B", True, True), ("C", True, True)]:
        scheduler = Scheduler(
            params=params,
            locations=locations,
            contracts=contracts,
            activities=activities,
            buffers=buffers,
            sectors_ordered=sectors_ordered,
            scenario=sc,
            allow_eclo=allow_eclo,
            allow_excess=allow_excess,
        )
        assignments, occupancies = scheduler.schedule()
        results = compute_results(params, contracts, activities, assignments, sc)
        score = compute_score(results, assignments, sc, activities)

        overrun_days_total = sum(r["overrun_days"] for r in results)
        contracts_overrunning = sum(1 for r in results if r["overrun_days"] > 0)
        eclo_nights_total = sum(1 for a in assignments if a.eclo)

        scenarios[sc] = {
            "score": round(score, 1),
            "overrun_days_total": overrun_days_total,
            "contracts_overrunning": contracts_overrunning,
            "eclo_nights_total": eclo_nights_total,
            "nights_scheduled": len(assignments),
        }

        if sc == "A":
            # Build the possessions queue from Scenario A's earliest-scheduled activities.
            occ_by_activity_week = {}
            for o in occupancies:
                occ_by_activity_week.setdefault((o.activity_id, o.week), []).append(o.location_id)

            earliest_by_activity = {}
            for a in assignments:
                key = a.activity_id
                if key not in earliest_by_activity or a.week < earliest_by_activity[key].week:
                    earliest_by_activity[key] = a

            ordered = sorted(earliest_by_activity.values(), key=lambda a: (a.week, a.activity_id))[:8]
            for idx, a in enumerate(ordered):
                activity = activities[a.activity_id]
                contract = contracts.get(activity.contract_number)
                locs = occ_by_activity_week.get((a.activity_id, a.week), [])
                possessions.append({
                    "id": f"q-{a.activity_id}",
                    "code": f"#{a.activity_id}-{activity.contract_number}",
                    "contractor": f"{activity.contract_number} • {activity.activity_type}",
                    "workDesc": (contract.description if contract else activity.activity_type),
                    "timeWindow": f"Week {a.week}" + (" (ECLO)" if a.eclo else ""),
                    "trackSector": locs[0] if locs else activity.start_location_id,
                    "status": "ACTIVE" if idx == 0 else ("STANDBY" if idx >= len(ordered) - 2 else "QUEUED"),
                    "riskLevel": RISK_BY_PRIORITY.get(activity.activity_priority, "LOW"),
                })

    return {"scenarios": scenarios, "possessions": possessions}


@app.get("/api/schedule")
async def schedule(scenario: str = "C"):
    """
    Full per-activity / per-location schedule detail for one scenario, built
    from the default dataset. Backs the Schedule Calendar (per-location
    weekly occupancy) and Kanban Board (per-contract activity duration)
    views: every field here traces back to the scheduler's real output,
    nothing decorative.
    """
    if scenario not in ("A", "B", "C"):
        scenario = "C"

    loader = DataLoader(str(DEFAULT_DATA))
    params, locations, contracts, activities, buffers, sectors_ordered = loader.load()

    allow_eclo = scenario in ("B", "C")
    allow_excess = scenario in ("B", "C")
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

    weeks_by_activity: dict = {}
    eclo_weeks_by_activity: dict = {}
    for a in assignments:
        weeks_by_activity.setdefault(a.activity_id, set()).add(a.week)
        if a.eclo:
            eclo_weeks_by_activity.setdefault(a.activity_id, set()).add(a.week)

    activities_out = []
    for act in activities.values():
        assigned_weeks = sorted(weeks_by_activity.get(act.activity_id, []))
        activities_out.append({
            "activity_id": act.activity_id,
            "contract_number": act.contract_number,
            "activity_type": act.activity_type,
            "activity_priority": act.activity_priority,
            "start_location_id": act.start_location_id,
            "end_location_id": act.end_location_id,
            "total_accesses": act.total_accesses,
            "planned_start_week": params.date_to_week(act.planned_start_date),
            "assigned_weeks": assigned_weeks,
            "eclo_weeks": sorted(eclo_weeks_by_activity.get(act.activity_id, [])),
        })

    contracts_out = [
        {
            "contract_number": c.contract_number,
            "description": c.description,
            "nature_of_activity": c.nature_of_activity,
            "contract_priority": c.contract_priority,
            "planned_completion_date": c.planned_completion_date.isoformat(),
        }
        for c in contracts.values()
    ]

    locations_out = [
        {
            "location_id": loc.location_id,
            "location_kind": loc.location_kind,
            "line_code": loc.line_code,
            "bound": loc.bound,
            "supply_capacity": loc.supply_capacity,
        }
        for loc in locations.values()
    ]

    occupancy_out: dict = {}
    for o in occupancies:
        week_map = occupancy_out.setdefault(o.location_id, {})
        week_map.setdefault(str(o.week), []).append({
            "activity_id": o.activity_id,
            "co_share_group": o.co_share_group,
        })

    return {
        "scenario": scenario,
        "horizon_weeks": params.horizon_weeks,
        "activities": activities_out,
        "contracts": contracts_out,
        "locations": locations_out,
        "occupancy": occupancy_out,
    }


def _build_schedule_context() -> str:
    """
    Plain-text summary of the real scheduler output across all 3 scenarios --
    this is the ONLY source of truth handed to the chatbot. Nothing here is
    invented; it's built the same way /api/dashboard and /api/schedule are.
    """
    loader = DataLoader(str(DEFAULT_DATA))
    params, locations, contracts, activities, buffers, sectors_ordered = loader.load()

    lines = [f"Planning horizon: {params.horizon_weeks} weeks, starting {params.horizon_start.isoformat()}.", ""]

    lines.append("CONTRACTS:")
    for c in contracts.values():
        lines.append(
            f"- {c.contract_number}: {c.description} | priority P{c.contract_priority} | "
            f"{c.nature_of_activity} | planned completion {c.planned_completion_date.isoformat()}"
        )

    for sc, allow_eclo, allow_excess in [("A", False, False), ("B", True, True), ("C", True, True)]:
        scheduler = Scheduler(
            params=params, locations=locations, contracts=contracts, activities=activities,
            buffers=buffers, sectors_ordered=sectors_ordered, scenario=sc,
            allow_eclo=allow_eclo, allow_excess=allow_excess,
        )
        assignments, occupancies = scheduler.schedule()
        results = compute_results(params, contracts, activities, assignments, sc)
        score = compute_score(results, assignments, sc, activities)

        weeks_by_activity: dict = {}
        eclo_by_activity: dict = {}
        for a in assignments:
            weeks_by_activity.setdefault(a.activity_id, set()).add(a.week)
            if a.eclo:
                eclo_by_activity.setdefault(a.activity_id, set()).add(a.week)

        lines.append(f"\n=== SCENARIO {sc} (score {round(score, 1)}, {len(assignments)} access-nights scheduled) ===")
        lines.append("Contract completion:")
        for r in results:
            lines.append(f"  {r['contract_number']}: completes {r['simulated_completion_date']}, overrun {r['overrun_days']} days")

        lines.append("Activities:")
        for act in activities.values():
            wks = sorted(weeks_by_activity.get(act.activity_id, []))
            eclo = sorted(eclo_by_activity.get(act.activity_id, []))
            lines.append(
                f"  {act.activity_id} (contract {act.contract_number}, type {act.activity_type}, "
                f"priority P{act.activity_priority}): {act.start_location_id} -> {act.end_location_id}, "
                f"needs {act.total_accesses} nights, scheduled weeks {wks}"
                + (f", ECLO weeks {eclo}" if eclo else "")
            )

    return "\n".join(lines)


class ChatMessage(BaseModel):
    role: str  # "user" or "model"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


@app.post("/api/chat")
async def chat(req: ChatRequest):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return JSONResponse(status_code=500, content={"error": "GEMINI_API_KEY is not set on the server."})

    from google import genai
    from google.genai import types

    context = _build_schedule_context()
    system_instruction = (
        "You are a planning assistant for LTA's PS1 railway track access scheduler, used by a works "
        "controller. Answer questions using ONLY the real schedule data below -- never invent activity "
        "IDs, contract numbers, weeks, or scores that aren't in it. If the data doesn't answer the "
        "question, say so plainly. Be concise and cite exact IDs/weeks/numbers from the data. "
        "Reply in PLAIN TEXT ONLY -- no Markdown (no **, no #, no backticks). For lists, use a plain "
        "dash and a line break, nothing else.\n\n"
        f"{context}"
    )

    contents = [
        types.Content(role=("model" if m.role == "model" else "user"), parts=[types.Part(text=m.content)])
        for m in req.history
    ]
    contents.append(types.Content(role="user", parts=[types.Part(text=req.message)]))

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_instruction),
        )
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Gemini request failed: {e}"})

    return {"reply": response.text}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
