"""
Production Validator API
FastAPI backend exposing validation setup, trigger, and results endpoints.
"""
import os
import uuid
import json
import asyncio
import tempfile
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.models.schemas import (
    ValidationSetup, ValidationStatus, ReleaseType, SystemType,
    OverallValidationResult, ValidationResult
)
from backend.tools.document_parser import (
    parse_excel_test_plan, parse_pdf_tech_letter, extract_validation_cases
)
from backend.agents.validation_agent import (
    run_validation_setup_phase, run_validation_execution_phase
)
from backend.utils.store import (
    save_validation, get_validation, list_validations,
    update_validation_status, save_result, get_result, get_attempt_count
)


app = FastAPI(title="Production Validator", version="1.0.0")
scheduler = AsyncIOScheduler()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    scheduler.start()
    print("[API] Production Validator started")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


# ─── Document Parsing Endpoint ─────────────────────────────────────────────────

@app.post("/api/parse-documents")
async def parse_documents(
    test_plan: UploadFile = File(...),
    tech_letter: Optional[UploadFile] = File(None),
    feature_number: str = Form(...),
    custom_notes: Optional[str] = Form(None)
):
    """
    Parse uploaded Excel test plan and optional PDF tech letter.
    Returns extracted validation cases for user review.
    """
    # Save Excel to temp file
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
        content = await test_plan.read()
        tf.write(content)
        excel_path = tf.name
    
    # Parse Excel
    excel_text, _ = parse_excel_test_plan(excel_path)
    os.unlink(excel_path)
    
    # Parse PDF if provided
    pdf_text = None
    if tech_letter and tech_letter.filename:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            content = await tech_letter.read()
            tf.write(content)
            pdf_path = tf.name
        pdf_text = parse_pdf_tech_letter(pdf_path)
        os.unlink(pdf_path)
    
    # Extract validation cases using Claude
    cases = extract_validation_cases(excel_text, pdf_text, custom_notes, feature_number)
    
    return {
        "validation_cases": [c.model_dump() for c in cases],
        "count": len(cases)
    }


# ─── Setup Endpoint ────────────────────────────────────────────────────────────

@app.post("/api/setup")
async def setup_validation(
    background_tasks: BackgroundTasks,
    test_plan: UploadFile = File(...),
    tech_letter: Optional[UploadFile] = File(None),
    feature_number: str = Form(...),
    release_type: str = Form(...),
    launch_date: str = Form(...),
    launch_time: Optional[str] = Form(None),
    systems: str = Form(...),  # JSON array string
    gdl_email: str = Form(...),
    custom_validation_notes: Optional[str] = Form(None),
    validation_cases_override: Optional[str] = Form(None)  # JSON - if user edited cases
):
    """
    Set up a new production validation.
    Parses documents, generates validation cases, schedules execution.
    """
    validation_id = f"VAL-{feature_number}-{uuid.uuid4().hex[:8].upper()}"
    
    # Parse systems
    systems_list = json.loads(systems)
    system_enums = [SystemType(s) for s in systems_list]
    
    # Process documents
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
        content = await test_plan.read()
        tf.write(content)
        excel_path = tf.name
    
    excel_text, _ = parse_excel_test_plan(excel_path)
    os.unlink(excel_path)
    
    pdf_text = None
    if tech_letter and tech_letter.filename:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            content = await tech_letter.read()
            tf.write(content)
            pdf_path = tf.name
        pdf_text = parse_pdf_tech_letter(pdf_path)
        os.unlink(pdf_path)
    
    # Use provided cases or extract fresh ones
    if validation_cases_override:
        from .models.schemas import ValidationCase, UMFField
        raw_cases = json.loads(validation_cases_override)
        validation_cases = []
        for c in raw_cases:
            umf_fields = [UMFField(**f) for f in c.get("umf_fields", [])]
            validation_cases.append(ValidationCase(
                case_id=c["case_id"], title=c["title"], description=c["description"],
                umf_fields=umf_fields, expected_outcome=c["expected_outcome"],
                source=c.get("source", "test_plan")
            ))
    else:
        validation_cases = extract_validation_cases(
            excel_text, pdf_text, custom_validation_notes, feature_number
        )
    
    setup = ValidationSetup(
        feature_number=feature_number,
        release_type=ReleaseType(release_type),
        launch_date=launch_date,
        launch_time=launch_time,
        systems=system_enums,
        gdl_email=gdl_email,
        custom_validation_notes=custom_validation_notes,
        validation_cases=validation_cases,
        status=ValidationStatus.PENDING,
        created_at=datetime.utcnow(),
        validation_id=validation_id
    )
    
    save_validation(setup)
    
    # Run setup phase in background (sends emails, schedules)
    background_tasks.add_task(run_setup_and_schedule, setup)
    
    return {
        "validation_id": validation_id,
        "setup": setup.model_dump(mode="json"),
        "message": "Validation configured successfully"
    }


async def run_setup_and_schedule(setup: ValidationSetup):
    """Background task: run setup phase and optionally schedule execution."""
    try:
        update_validation_status(setup.validation_id, ValidationStatus.SCHEDULED.value)
        
        # Run setup phase (sends setup + manual trigger emails)
        await run_validation_setup_phase(setup)
        
        # If launch_time provided, schedule auto-execution 10 min after
        if setup.launch_time:
            try:
                launch_dt = datetime.strptime(
                    f"{setup.launch_date} {setup.launch_time}", "%Y-%m-%d %H:%M"
                )
                execute_at = launch_dt + timedelta(minutes=10)
                
                if execute_at > datetime.utcnow():
                    scheduler.add_job(
                        execute_validation,
                        "date",
                        run_date=execute_at,
                        args=[setup.validation_id],
                        id=f"auto_{setup.validation_id}",
                        replace_existing=True
                    )
                    print(f"[Scheduler] Scheduled validation {setup.validation_id} at {execute_at}")
                else:
                    # Time already passed - run immediately
                    asyncio.create_task(execute_validation(setup.validation_id))
            except ValueError:
                print(f"[Scheduler] Invalid time format for {setup.validation_id}")
    
    except Exception as e:
        print(f"[Setup Error] {setup.validation_id}: {e}")
        update_validation_status(setup.validation_id, "error")


async def execute_validation(validation_id: str, attempt: int = 1):
    """Execute the actual validation (log pull + analysis)."""
    setup = get_validation(validation_id)
    if not setup:
        print(f"[Execute] Validation {validation_id} not found")
        return
    
    update_validation_status(validation_id, ValidationStatus.RUNNING.value)
    
    try:
        result_state = await run_validation_execution_phase(validation_id, setup, attempt)
        
        if result_state.get("overall_result"):
            overall = result_state["overall_result"]
            update_validation_status(validation_id, overall["overall_status"])
        
        print(f"[Execute] Validation {validation_id} complete")
    
    except Exception as e:
        print(f"[Execute Error] {validation_id}: {e}")
        update_validation_status(validation_id, "error")


# ─── Trigger Endpoints ─────────────────────────────────────────────────────────

@app.post("/api/trigger/{validation_id}")
async def manual_trigger(validation_id: str, background_tasks: BackgroundTasks):
    """Manual trigger for validation (used when no launch_time was provided)."""
    setup = get_validation(validation_id)
    if not setup:
        raise HTTPException(status_code=404, detail="Validation not found")
    
    current_attempt = get_attempt_count(validation_id) + 1
    
    background_tasks.add_task(execute_validation, validation_id, current_attempt)
    update_validation_status(validation_id, ValidationStatus.RUNNING.value)
    
    return {"message": "Validation triggered", "validation_id": validation_id, "attempt": current_attempt}


@app.post("/api/retry/{validation_id}")
async def retry_validation(validation_id: str, background_tasks: BackgroundTasks):
    """Retry validation after failure - pulls a new set of logs."""
    setup = get_validation(validation_id)
    if not setup:
        raise HTTPException(status_code=404, detail="Validation not found")
    
    current_attempt = get_attempt_count(validation_id) + 1
    if current_attempt > 5:
        raise HTTPException(status_code=400, detail="Maximum retry attempts reached")
    
    background_tasks.add_task(execute_validation, validation_id, current_attempt)
    update_validation_status(validation_id, ValidationStatus.RUNNING.value)
    
    return {"message": "Retry triggered", "validation_id": validation_id, "attempt": current_attempt}


# ─── Query Endpoints ───────────────────────────────────────────────────────────

@app.get("/api/validations")
async def list_validations_endpoint():
    """List all validations."""
    return {"validations": list_validations()}


@app.get("/api/validations/{validation_id}")
async def get_validation_endpoint(validation_id: str):
    """Get a specific validation setup."""
    setup = get_validation(validation_id)
    if not setup:
        raise HTTPException(status_code=404, detail="Validation not found")
    return setup.model_dump(mode="json")


@app.get("/api/results/{validation_id}")
async def get_results(validation_id: str):
    """Get validation results."""
    result = get_result(validation_id)
    if not result:
        setup = get_validation(validation_id)
        if not setup:
            raise HTTPException(status_code=404, detail="Validation not found")
        return {"status": setup.status.value, "result": None}
    return {"status": result.get("overall_status"), "result": result}


@app.get("/api/status/{validation_id}")
async def get_status(validation_id: str):
    """Get current status of a validation."""
    setup = get_validation(validation_id)
    if not setup:
        raise HTTPException(status_code=404, detail="Validation not found")
    
    result = get_result(validation_id)
    return {
        "validation_id": validation_id,
        "status": setup.status.value,
        "has_result": result is not None,
        "attempt_number": get_attempt_count(validation_id)
    }


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
