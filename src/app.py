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

from fastapi import FastAPI, File, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from loader import DataLoader
from scheduler import Scheduler
from scorer import compute_results, compute_score
from output import write_outputs

app = FastAPI(title="PS1 Railway Track Scheduler", version="1.0.0")

BASE_DIR = Path(__file__).parent.parent
DEFAULT_DATA = BASE_DIR.parent / "NebulaX-Hackathon-ProblemStatement" / "PS1" / "01_data"

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
