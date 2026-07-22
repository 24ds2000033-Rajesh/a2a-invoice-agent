import os
import json
import uuid
import hashlib
import asyncio
import aiosqlite
from typing import Dict, Any
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.models import (
    A2A_MEDIA_TYPE, INVOICE_BATCH_TYPE, PROPOSALS_TYPE,
    RESULTS_TYPE, RECEIPTS_TYPE
)
from app.database import init_db, DB_PATH
from app.reasoning import hash_package_canonical, analyze_invoice_package

app = FastAPI(title="A2A Invoice Action Agent")

# Global Exception Handlers for Protocol Compliance
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers={"Content-Type": A2A_MEDIA_TYPE, "A2A-Version": "1.0"}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": "Bad Request", "details": exc.errors()},
        headers={"Content-Type": A2A_MEDIA_TYPE, "A2A-Version": "1.0"}
    )

@app.on_event("startup")
async def startup():
    await init_db()

def get_bearer_token(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return authorization.split("Bearer ")[1].strip()

def check_headers(
    a2a_version: str = Header(None),
    content_type: str = Header(None)
):
    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="Invalid A2A-Version. Must be 1.0")
    if content_type and A2A_MEDIA_TYPE not in content_type:
        raise HTTPException(status_code=415, detail="Unsupported Media Type. Must be application/a2a+json")

def hash_message_canonical(message_dict: Dict[str, Any]) -> str:
    """Recursively key-sorts and hashes the message payload ignoring configuration."""
    serialized = json.dumps(message_dict, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

def make_a2a_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"Content-Type": A2A_MEDIA_TYPE, "A2A-Version": "1.0"}
    )

async def process_single_package(pkg: Dict[str, Any], db: aiosqlite.Connection) -> Dict[str, Any]:
    pkg_id = pkg.get("packageId")
    pkg_hash = hash_package_canonical(pkg)

    async with db.execute("SELECT decision_json FROM package_cache WHERE package_hash = ?", (pkg_hash,)) as cursor:
        c_row = await cursor.fetchone()

    if c_row:
        decision = json.loads(c_row[0])
    else:
        decision = await analyze_invoice_package(pkg)
        await db.execute(
            "INSERT OR IGNORE INTO package_cache (package_hash, decision_json) VALUES (?, ?)",
            (pkg_hash, json.dumps(decision))
        )

    action_id = f"act_{uuid.uuid4().hex[:12]}"
    return {
        "packageId": pkg_id,
        "actionId": action_id,
        "action": decision["action"],
        "facts": decision["facts"],
        "evidenceRefs": decision["evidenceRefs"],
        "rationale": decision["rationale"]
    }

@app.get("/.well-known/agent-card.json")
async def agent_card(request: Request):
    base_url = str(request.base_url).rstrip("/")
    if base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://")
        
    card = {
        "name": "Invoice Action Agent",
        "description": "Autonomous AI agent for invoice claim batch processing.",
        "version": "1.0.0",
        "capabilities": {
            "invoiceProcessing": True
        },
        "skills": [
            {
                "id": "invoice_action_agent",
                "name": "Invoice Action Skill",
                "description": "Processes invoice packages and generates action proposals.",
                "tags": ["invoice", "finance", "automation"]
            }
        ],
        "supportedInterfaces": [
            {
                "url": base_url,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0"
            }
        ],
        "defaultInputModes": [INVOICE_BATCH_TYPE],
        "defaultOutputModes": [PROPOSALS_TYPE, RECEIPTS_TYPE]
    }
    return JSONResponse(
        content=card,
        headers={"Content-Type": A2A_MEDIA_TYPE, "A2A-Version": "1.0"}
    )

@app.post("/message:send")
async def send_message(
    request: Request,
    token: str = Depends(get_bearer_token),
    _ver: None = Depends(check_headers)
):
    body = await request.json()
    msg_data = body.get("message", {})
    msg_id = msg_data.get("messageId")
    if not msg_id:
        raise HTTPException(status_code=400, detail="Missing messageId")
    
    msg_hash = hash_message_canonical(msg_data)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        
        # Idempotency & Conflict Check
        async with db.execute(
            "SELECT message_hash, task_id FROM idempotency WHERE principal = ? AND message_id = ?",
            (token, msg_id)
        ) as cursor:
            id_row = await cursor.fetchone()

        if id_row:
            stored_hash, stored_task_id = id_row
            if stored_hash != msg_hash:
                raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
            
            # Replay stored task
            async with db.execute(
                "SELECT id, context_id, status, history_json, artifacts_json FROM tasks WHERE id = ?",
                (stored_task_id,)
            ) as t_cursor:
                t_row = await t_cursor.fetchone()
                
            task_obj = {
                "id": t_row[0], "contextId": t_row[1], "status": t_row[2],
                "history": json.loads(t_row[3]), "artifacts": json.loads(t_row[4])
            }
            return make_a2a_response({"task": task_obj})

        parts = msg_data.get("parts", [])
        if not parts:
            raise HTTPException(status_code=400, detail="No parts in message")

        first_part = parts[0]
        media_type = first_part.get("mediaType")

        # Initial Invoice Batch -> Create Proposal (INPUT_REQUIRED)
        if media_type == INVOICE_BATCH_TYPE:
            batch_data = first_part.get("data", {})
            batch_id = batch_data.get("batchId", str(uuid.uuid4()))
            packages = batch_data.get("packages", [])

            task_id = f"task_{uuid.uuid4().hex[:16]}"
            context_id = f"ctx_{uuid.uuid4().hex[:16]}"
            
            proposals = await asyncio.gather(*[process_single_package(pkg, db) for pkg in packages])

            proposal_artifact = {
                "mediaType": PROPOSALS_TYPE,
                "data": {
                    "batchId": batch_id,
                    "proposals": list(proposals)
                }
            }

            task_obj = {
                "id": task_id,
                "contextId": context_id,
                "status": "TASK_STATE_INPUT_REQUIRED",
                "history": [msg_data],
                "artifacts": [proposal_artifact]
            }

            await db.execute(
                "INSERT INTO tasks (id, principal, context_id, status, history_json, artifacts_json) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, token, context_id, task_obj["status"], json.dumps(task_obj["history"]), json.dumps(task_obj["artifacts"]))
            )
            await db.execute(
                "INSERT INTO idempotency (principal, message_id, message_hash, task_id) VALUES (?, ?, ?, ?)",
                (token, msg_id, msg_hash, task_id)
            )
            await db.commit()
            return make_a2a_response({"task": task_obj})

        # Continuation Results -> Complete Task (COMPLETED)
        elif media_type == RESULTS_TYPE:
            target_task_id = msg_data.get("taskId")
            target_ctx_id = msg_data.get("contextId")

            async with db.execute("SELECT id, principal, context_id, status, history_json, artifacts_json FROM tasks WHERE id = ?", (target_task_id,)) as t_cursor:
                t_row = await t_cursor.fetchone()
            
            if not t_row or t_row[1] != token:
                raise HTTPException(status_code=404, detail="Task not found")

            curr_status = t_row[3]
            if curr_status in ["TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"]:
                raise HTTPException(status_code=409, detail="Task is in terminal state")

            stored_ctx_id = t_row[2]
            if target_ctx_id and target_ctx_id != stored_ctx_id:
                raise HTTPException(status_code=400, detail="Context ID mismatch")

            history = json.loads(t_row[4])
            artifacts = json.loads(t_row[5])

            proposals_artifact = next((a for a in artifacts if a.get("mediaType") == PROPOSALS_TYPE), None)
            if not proposals_artifact:
                raise HTTPException(status_code=400, detail="No stored proposals found")

            proposal_map = {p["packageId"]: p for p in proposals_artifact["data"]["proposals"]}
            
            results_data = first_part.get("data", {})
            results = results_data.get("results", [])
            
            executions = []
            for res in results:
                pkg_id = res.get("packageId")
                matched_prop = proposal_map.get(pkg_id)
                
                if not matched_prop or matched_prop["actionId"] != res.get("actionId") or matched_prop["action"] != res.get("action"):
                    raise HTTPException(status_code=400, detail="Continuation result mismatch")

                if res.get("outcome") == "ACCEPTED":
                    executions.append({
                        "packageId": pkg_id,
                        "actionId": matched_prop["actionId"],
                        "action": matched_prop["action"],
                        "receiptNonce": res.get("receiptNonce"),
                        "facts": matched_prop["facts"],
                        "evidenceRefs": matched_prop["evidenceRefs"]
                    })

            receipt_artifact = {
                "mediaType": RECEIPTS_TYPE,
                "data": {
                    "batchId": results_data.get("batchId"),
                    "executions": executions
                }
            }

            history.append(msg_data)
            artifacts.append(receipt_artifact)
            new_status = "TASK_STATE_COMPLETED"

            await db.execute(
                "UPDATE tasks SET status = ?, history_json = ?, artifacts_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'TASK_STATE_INPUT_REQUIRED'",
                (new_status, json.dumps(history), json.dumps(artifacts), target_task_id)
            )
            if db.total_changes == 0:
                raise HTTPException(status_code=409, detail="Task already transitioned or canceled")

            await db.execute(
                "INSERT INTO idempotency (principal, message_id, message_hash, task_id) VALUES (?, ?, ?, ?)",
                (token, msg_id, msg_hash, target_task_id)
            )
            await db.commit()

            task_obj = {
                "id": target_task_id, "contextId": stored_ctx_id, "status": new_status,
                "history": history, "artifacts": artifacts
            }
            return make_a2a_response({"task": task_obj})

        else:
            raise HTTPException(status_code=400, detail="Unsupported mediaType")

@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    token: str = Depends(get_bearer_token),
    _ver: None = Depends(check_headers)
):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, principal, context_id, status, history_json, artifacts_json FROM tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

        if not row or row[1] != token:
            raise HTTPException(status_code=404, detail="Task not found")

        task_obj = {
            "id": row[0], "contextId": row[2], "status": row[3],
            "history": json.loads(row[4]), "artifacts": json.loads(row[5])
        }
        return make_a2a_response(task_obj)

@app.get("/tasks")
async def list_tasks(
    token: str = Depends(get_bearer_token),
    _ver: None = Depends(check_headers)
):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, context_id, status, history_json, artifacts_json FROM tasks WHERE principal = ?", (token,)) as cursor:
            rows = await cursor.fetchall()

        tasks = [
            {
                "id": r[0], "contextId": r[1], "status": r[2],
                "history": json.loads(r[3]), "artifacts": json.loads(r[4])
            }
            for r in rows
        ]
        return make_a2a_response({"tasks": tasks})

@app.post("/tasks/{task_id}:cancel")
async def cancel_task(
    task_id: str,
    token: str = Depends(get_bearer_token),
    _ver: None = Depends(check_headers)
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        async with db.execute("SELECT id, principal, context_id, status, history_json, artifacts_json FROM tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

        if not row or row[1] != token:
            raise HTTPException(status_code=404, detail="Task not found")

        curr_status = row[3]
        if curr_status in ["TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"]:
            raise HTTPException(status_code=409, detail="Task is already in terminal state")

        new_status = "TASK_STATE_CANCELED"
        await db.execute(
            "UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'TASK_STATE_INPUT_REQUIRED'",
            (new_status, task_id)
        )
        if db.total_changes == 0:
            raise HTTPException(status_code=409, detail="Race condition lost: task already terminal")

        await db.commit()

        task_obj = {
            "id": row[0], "contextId": row[2], "status": new_status,
            "history": json.loads(row[4]), "artifacts": json.loads(row[5])
        }
        return make_a2a_response(task_obj)
