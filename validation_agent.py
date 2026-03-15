"""
Production Validation Agent using LangGraph
Multi-node graph that orchestrates:
1. Document parsing (extract validation cases from Excel + PDF)
2. Schedule management (trigger by time or manual)
3. Log pulling via Hadoop MCP
4. Validation execution (compare logs against expected values)
5. Notification dispatch (pre/post validation emails)
6. Retry logic (re-pull on failure)
"""
import os
import json
import uuid
from datetime import datetime, timedelta
from typing import TypedDict, List, Optional, Annotated
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import add_messages
from anthropic import Anthropic
from backend.models.schemas import (
    ValidationSetup, ValidationCase, ValidationResult,
    OverallValidationResult, LogPullRequest, ValidationStatus,
    TransactionLog, SystemType
)
from backend.tools.hadoop_mcp import hadoop_mcp
from backend.tools.email_service import (
    send_validation_setup_confirmation,
    send_manual_trigger_notification,
    send_pre_validation_email,
    send_post_validation_email
)


client = Anthropic()
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


# ─── State Definition ──────────────────────────────────────────────────────────

class ValidationState(TypedDict):
    validation_id: str
    setup: dict  # serialized ValidationSetup
    logs: List[dict]  # serialized TransactionLogs
    case_results: List[dict]  # serialized ValidationResults
    overall_result: Optional[dict]  # serialized OverallValidationResult
    attempt_number: int
    status: str
    error: Optional[str]
    phase: str  # "setup", "scheduled", "running", "complete"


# ─── Graph Nodes ───────────────────────────────────────────────────────────────

async def node_validate_setup(state: ValidationState) -> ValidationState:
    """Validate the setup configuration is complete and correct."""
    print(f"[Agent] Node: validate_setup | ID: {state['validation_id']}")
    
    setup_dict = state["setup"]
    errors = []
    
    if not setup_dict.get("feature_number"):
        errors.append("Feature number is required")
    if not setup_dict.get("launch_date"):
        errors.append("Launch date is required")
    if not setup_dict.get("systems"):
        errors.append("At least one system must be selected")
    if not setup_dict.get("validation_cases"):
        errors.append("No validation cases found - ensure documents were parsed correctly")
    
    if errors:
        return {**state, "status": "error", "error": "; ".join(errors)}
    
    return {**state, "phase": "scheduled", "status": "setup_valid"}


async def node_send_setup_emails(state: ValidationState) -> ValidationState:
    """Send confirmation email and manual trigger email if needed."""
    print(f"[Agent] Node: send_setup_emails | ID: {state['validation_id']}")
    
    setup = ValidationSetup(**state["setup"])
    
    # Always send setup confirmation to GDL
    await send_validation_setup_confirmation(setup)
    
    # If no launch time, send manual trigger email
    if not setup.launch_time:
        trigger_url = f"{BASE_URL}/api/trigger/{state['validation_id']}"
        await send_manual_trigger_notification(setup, trigger_url)
        return {**state, "phase": "waiting_trigger"}
    
    return {**state, "phase": "scheduled"}


async def node_pull_logs(state: ValidationState) -> ValidationState:
    """Pull transaction logs from Hadoop MCP based on validation case UMF fields."""
    print(f"[Agent] Node: pull_logs | attempt {state['attempt_number']} | ID: {state['validation_id']}")
    
    setup = ValidationSetup(**state["setup"])
    
    # Send pre-validation notification
    await send_pre_validation_email(setup)
    
    # Collect all UMF numbers from all validation cases
    all_umf_numbers = set()
    for case_dict in setup.validation_cases:
        case = ValidationCase(**case_dict) if isinstance(case_dict, dict) else case_dict
        for field in case.umf_fields:
            all_umf_numbers.add(field.umf_number)
    
    if not all_umf_numbers:
        return {**state, "status": "error", "error": "No UMF field numbers found in validation cases"}
    
    # Define log pull time range (5 min window from activation)
    now = datetime.utcnow()
    
    # On retry, offset time window by attempt number
    offset_minutes = (state["attempt_number"] - 1) * 5
    start_time = now - timedelta(minutes=offset_minutes + 5)
    end_time = now - timedelta(minutes=offset_minutes)
    
    log_request = LogPullRequest(
        umf_numbers=list(all_umf_numbers),
        systems=setup.systems,
        start_time=start_time,
        end_time=end_time,
        feature_number=setup.feature_number
    )
    
    logs = await hadoop_mcp.execute_log_pull(log_request)
    logs_as_dict = [
        {
            "transaction_id": log.transaction_id,
            "timestamp": log.timestamp.isoformat(),
            "system": log.system,
            "umf_fields": log.umf_fields,
            "raw_data": log.raw_data
        }
        for log in logs
    ]
    
    print(f"[Agent] Retrieved {len(logs)} logs")
    return {**state, "logs": logs_as_dict, "phase": "validating", "status": "logs_pulled"}


async def node_run_validation(state: ValidationState) -> ValidationState:
    """
    Use Claude to analyze logs against validation cases and determine pass/fail.
    """
    print(f"[Agent] Node: run_validation | cases: {len(state['setup'].get('validation_cases', []))} | logs: {len(state['logs'])}")
    
    setup = ValidationSetup(**state["setup"])
    logs = state["logs"]
    
    if not logs:
        # No logs found - fail all cases
        case_results = []
        for case_dict in setup.validation_cases:
            case = ValidationCase(**case_dict) if isinstance(case_dict, dict) else case_dict
            case_results.append({
                "validation_id": state["validation_id"],
                "feature_number": setup.feature_number,
                "case_id": case.case_id,
                "case_title": case.title,
                "status": ValidationStatus.FAILED.value,
                "logs_found": 0,
                "matched_fields": [],
                "failed_fields": [{"field": f.field_name, "umf": f.umf_number, "reason": "No logs found"}
                                   for f in case.umf_fields],
                "details": "No transaction logs were found for this time window.",
                "timestamp": datetime.utcnow().isoformat()
            })
        
        overall_status = ValidationStatus.FAILED
        summary = "Validation FAILED: No transaction logs found in the specified time window."
        
        overall = {
            "validation_id": state["validation_id"],
            "feature_number": setup.feature_number,
            "overall_status": overall_status.value,
            "case_results": case_results,
            "summary": summary,
            "timestamp": datetime.utcnow().isoformat(),
            "attempt_number": state["attempt_number"]
        }
        return {**state, "case_results": case_results, "overall_result": overall, "phase": "complete"}
    
    # Build Claude analysis prompt
    cases_json = json.dumps([
        {
            "case_id": (ValidationCase(**c) if isinstance(c, dict) else c).case_id,
            "title": (ValidationCase(**c) if isinstance(c, dict) else c).title,
            "description": (ValidationCase(**c) if isinstance(c, dict) else c).description,
            "umf_fields": [
                {"umf_number": f.umf_number, "iso_field": f.iso_field,
                 "field_name": f.field_name, "expected_value": f.expected_value,
                 "description": f.description}
                for f in (ValidationCase(**c) if isinstance(c, dict) else c).umf_fields
            ],
            "expected_outcome": (ValidationCase(**c) if isinstance(c, dict) else c).expected_outcome
        }
        for c in setup.validation_cases
    ], indent=2)
    
    # Sample logs (limit for context window)
    sample_logs = logs[:20]
    logs_json = json.dumps(sample_logs, indent=2, default=str)
    
    prompt = f"""You are a Visa payment system validation expert analyzing production transaction logs.

Feature Number: {setup.feature_number}
Validation Attempt: #{state['attempt_number']}
Total Logs Retrieved: {len(logs)}
Sample Logs (first 20): {len(sample_logs)}

=== VALIDATION CASES ===
{cases_json}

=== TRANSACTION LOGS SAMPLE ===
{logs_json}

For each validation case, analyze the transaction logs and determine:
1. How many logs contain the relevant UMF fields
2. Which fields match expected values
3. Which fields don't match or are missing
4. Overall PASS or FAIL for the case

A case PASSES if:
- Logs containing the relevant UMF fields are found
- The field values match expected values (or expected_value is null meaning any value present is OK)
- At least some transactions show the new behavior

A case FAILS if:
- No relevant logs found
- Required fields are missing from all logs  
- Field values consistently don't match expected values

Return ONLY valid JSON with this exact structure:
{{
  "case_results": [
    {{
      "case_id": "VC001",
      "case_title": "Case title",
      "status": "passed" or "failed",
      "logs_found": 15,
      "matched_fields": [
        {{"field": "UMF_124", "umf": 124, "value": "observed value", "expected": "expected value"}}
      ],
      "failed_fields": [
        {{"field": "UMF_62", "umf": 62, "value": "observed value", "expected": "expected value", "reason": "mismatch"}}
      ],
      "details": "Detailed explanation of what was found and why it passed/failed"
    }}
  ],
  "overall_status": "passed" or "failed" or "partial",
  "summary": "High level summary of validation results"
}}

overall_status is "partial" if some cases passed and some failed."""
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    response_text = response.content[0].text.strip()
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])
    
    analysis = json.loads(response_text)
    
    # Enrich results with metadata
    case_results = []
    for cr in analysis["case_results"]:
        case_results.append({
            "validation_id": state["validation_id"],
            "feature_number": setup.feature_number,
            "case_id": cr["case_id"],
            "case_title": cr["case_title"],
            "status": cr["status"],
            "logs_found": cr.get("logs_found", len(logs)),
            "matched_fields": cr.get("matched_fields", []),
            "failed_fields": cr.get("failed_fields", []),
            "details": cr.get("details", ""),
            "timestamp": datetime.utcnow().isoformat()
        })
    
    overall = {
        "validation_id": state["validation_id"],
        "feature_number": setup.feature_number,
        "overall_status": analysis["overall_status"],
        "case_results": case_results,
        "summary": analysis["summary"],
        "timestamp": datetime.utcnow().isoformat(),
        "attempt_number": state["attempt_number"]
    }
    
    print(f"[Agent] Validation complete: {analysis['overall_status']}")
    return {**state, "case_results": case_results, "overall_result": overall, "phase": "complete"}


async def node_send_results(state: ValidationState) -> ValidationState:
    """Send post-validation result email."""
    print(f"[Agent] Node: send_results | status: {state['overall_result']['overall_status']}")
    
    setup = ValidationSetup(**state["setup"])
    overall_dict = state["overall_result"]
    
    # Reconstruct result objects
    from backend.models.schemas import ValidationResult as VR, OverallValidationResult as OVR
    
    case_results = []
    for cr in overall_dict["case_results"]:
        case_results.append(VR(
            validation_id=cr["validation_id"],
            feature_number=cr["feature_number"],
            case_id=cr["case_id"],
            case_title=cr["case_title"],
            status=ValidationStatus(cr["status"]),
            logs_found=cr["logs_found"],
            matched_fields=cr["matched_fields"],
            failed_fields=cr["failed_fields"],
            details=cr["details"],
            timestamp=datetime.fromisoformat(cr["timestamp"])
        ))
    
    overall = OVR(
        validation_id=overall_dict["validation_id"],
        feature_number=overall_dict["feature_number"],
        overall_status=ValidationStatus(overall_dict["overall_status"]),
        case_results=case_results,
        summary=overall_dict["summary"],
        timestamp=datetime.fromisoformat(overall_dict["timestamp"]),
        attempt_number=overall_dict["attempt_number"]
    )
    
    retry_url = f"{BASE_URL}/api/retry/{state['validation_id']}"
    await send_post_validation_email(overall, setup, retry_url)
    
    final_status = overall_dict["overall_status"]
    return {**state, "status": f"complete_{final_status}"}


# ─── Routing Functions ─────────────────────────────────────────────────────────

def route_after_setup(state: ValidationState) -> str:
    if state["status"] == "error":
        return "error_handler"
    return "send_setup_emails"


def route_after_setup_emails(state: ValidationState) -> str:
    if state["phase"] == "waiting_trigger":
        return END  # Wait for manual trigger
    return "pull_logs"


def route_after_logs(state: ValidationState) -> str:
    if state["status"] == "error":
        return "error_handler"
    return "run_validation"


async def node_error_handler(state: ValidationState) -> ValidationState:
    """Handle errors in the workflow."""
    print(f"[Agent] ERROR: {state.get('error', 'Unknown error')}")
    return {**state, "phase": "error", "status": "error"}


# ─── Graph Construction ────────────────────────────────────────────────────────

def build_validation_graph() -> StateGraph:
    """Build and compile the LangGraph validation workflow."""
    
    graph = StateGraph(ValidationState)
    
    # Add nodes
    graph.add_node("validate_setup", node_validate_setup)
    graph.add_node("send_setup_emails", node_send_setup_emails)
    graph.add_node("pull_logs", node_pull_logs)
    graph.add_node("run_validation", node_run_validation)
    graph.add_node("send_results", node_send_results)
    graph.add_node("error_handler", node_error_handler)
    
    # Add edges
    graph.add_edge(START, "validate_setup")
    graph.add_conditional_edges("validate_setup", route_after_setup, {
        "send_setup_emails": "send_setup_emails",
        "error_handler": "error_handler"
    })
    graph.add_conditional_edges("send_setup_emails", route_after_setup_emails, {
        "pull_logs": "pull_logs",
        END: END
    })
    graph.add_conditional_edges("pull_logs", route_after_logs, {
        "run_validation": "run_validation",
        "error_handler": "error_handler"
    })
    graph.add_edge("run_validation", "send_results")
    graph.add_edge("send_results", END)
    graph.add_edge("error_handler", END)
    
    return graph.compile()


# Global compiled graph instance
validation_graph = build_validation_graph()


async def run_validation_setup_phase(setup: ValidationSetup) -> dict:
    """Run the initial setup phase of validation (up to scheduling)."""
    
    initial_state: ValidationState = {
        "validation_id": setup.validation_id,
        "setup": setup.model_dump(mode="json"),
        "logs": [],
        "case_results": [],
        "overall_result": None,
        "attempt_number": 1,
        "status": "initializing",
        "error": None,
        "phase": "setup"
    }
    
    # Run only through setup and email nodes
    # For a partial run (stop at waiting_trigger), we run the full graph
    # but it will END early if no launch_time
    result = await validation_graph.ainvoke(initial_state)
    return result


async def run_validation_execution_phase(validation_id: str, setup: ValidationSetup, attempt: int = 1) -> dict:
    """Run the validation execution phase (log pull + analysis)."""
    
    state: ValidationState = {
        "validation_id": validation_id,
        "setup": setup.model_dump(mode="json"),
        "logs": [],
        "case_results": [],
        "overall_result": None,
        "attempt_number": attempt,
        "status": "executing",
        "error": None,
        "phase": "running"
    }
    
    # Run from pull_logs onwards
    # We directly invoke the sub-workflow nodes
    state = await node_pull_logs(state)
    if state["status"] == "error":
        return state
    
    state = await node_run_validation(state)
    state = await node_send_results(state)
    
    return state
