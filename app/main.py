import asyncio
import json
import sqlite3
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse
import aiosqlite

DB_PATH = "database.db"
SUPPORTED_A2A_VERSION = "1.0"
A2A_MEDIA_TYPE = "application/a2a+json"

db_write_lock = asyncio.Lock()

# Helper function for compliant responses
def a2a_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers={"Content-Type": A2A_MEDIA_TYPE}
    )

# -----------------------------------------------------------------------------
# Database Setup
# -----------------------------------------------------------------------------

async def get_db():
    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=30000;")
        yield db

app = FastAPI(title="A2A Agent Endpoint")

@app.on_event("startup")
async def on_startup():
    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=30000;")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS idempotency (
                principal TEXT NOT NULL,
                message_id TEXT NOT NULL,
                response TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (principal, message_id)
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                status TEXT NOT NULL,
                data TEXT
            );
        """)
        await db.commit()

# -----------------------------------------------------------------------------
# Auth & Protocol Interceptors
# -----------------------------------------------------------------------------

def verify_bearer_auth(request: Request):
    """Enforces authentication across protected endpoints."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer token",
            headers={"WWW-Authenticate": "Bearer"}
        )
    return auth_header.split(" ")[1]

def validate_a2a_protocol(request: Request):
    """Validates required A2A protocol headers (Version & Media Type)."""
    # 1. Version Check
    a2a_version = request.headers.get("x-a2a-version", SUPPORTED_A2A_VERSION)
    if a2a_version != SUPPORTED_A2A_VERSION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported A2A version: {a2a_version}"
        )

    # 2. Incoming Media Type Check for POST requests
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        if A2A_MEDIA_TYPE not in content_type:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Content-Type must be {A2A_MEDIA_TYPE}"
            )

# -----------------------------------------------------------------------------
# Agent Discovery Endpoint
# -----------------------------------------------------------------------------

@app.get("/.well-known/agent-card.json")
async def get_agent_card(request: Request):
    """Compliant Agent Card discovery endpoint."""
    base_url = str(request.base_url).rstrip("/")
    
    card_payload = {
        "name": "A2A Compliant Agent",
        "description": "Task processor agent for A2A specification",
        "version": SUPPORTED_A2A_VERSION,
        "url": base_url,
        "defaultInputModes": [A2A_MEDIA_TYPE, "application/json"],
        "defaultOutputModes": [A2A_MEDIA_TYPE, "application/json"],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True
        },
        "skills": [
            {
                "id": "task_processor",
                "name": "Task Processor",
                "description": "Executes and tracks asynchronous task operations.",
                "tags": ["task", "processing", "a2a"]
            }
        ]
    }
    return JSONResponse(content=card_payload, status_code=200)

# -----------------------------------------------------------------------------
# Protected Tasks Endpoint
# -----------------------------------------------------------------------------

@app.get("/tasks")
async def list_tasks(
    request: Request,
    token: str = Depends(verify_bearer_auth),
    db: aiosqlite.Connection = Depends(get_db)
):
    validate_a2a_protocol(request)
    principal = request.headers.get("x-principal-id", token)

    async with db.execute(
        "SELECT id, status, data FROM packages WHERE principal = ?", (principal,)
    ) as cursor:
        rows = await cursor.fetchall()
        tasks = [{"id": r[0], "status": r[1], "data": json.loads(r[2]) if r[2] else {}} for r in rows]

    return a2a_response({"tasks": tasks}, status_code=200)

# -----------------------------------------------------------------------------
# Protected Message Send Endpoint
# -----------------------------------------------------------------------------

async def process_single_package(pkg: Dict[str, Any], principal: str, db: aiosqlite.Connection) -> Dict[str, Any]:
    pkg_id = pkg.get("id", "unknown")
    result = {"pkg_id": pkg_id, "status": "processed"}

    async with db_write_lock:
        await db.execute(
            """
            INSERT OR REPLACE INTO packages (id, principal, status, data)
            VALUES (?, ?, ?, ?)
            """,
            (pkg_id, principal, "processed", json.dumps(result))
        )
        await db.commit()

    return result

@app.post("/message:send")
@app.post("/message%3Asend")
async def send_message(
    request: Request,
    token: str = Depends(verify_bearer_auth),
    db: aiosqlite.Connection = Depends(get_db)
):
    # Enforce Protocol Contract (Returns 400 for bad version, 415 for bad media type)
    validate_a2a_protocol(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    principal = request.headers.get("x-principal-id", token)
    message_id = body.get("messageId") or body.get("id") or body.get("params", {}).get("message", {}).get("messageId")

    if not message_id:
        raise HTTPException(status_code=400, detail="Missing required messageId parameter")

    # 1. Idempotency Check
    async with db.execute(
        "SELECT response FROM idempotency WHERE principal = ? AND message_id = ?",
        (principal, message_id)
    ) as cursor:
        row = await cursor.fetchone()
        if row and row[0]:
            return a2a_response(json.loads(row[0]), status_code=200)

    # 2. Task Execution
    packages = body.get("packages", body.get("params", {}).get("packages", []))
    proposals = []
    if packages:
        proposals = await asyncio.gather(
            *[process_single_package(pkg, principal, db) for pkg in packages],
            return_exceptions=False
        )

    response_payload = {
        "status": "success",
        "messageId": message_id,
        "proposals": proposals
    }

    # 3. Store Idempotency Record
    async with db_write_lock:
        try:
            await db.execute(
                """
                INSERT OR IGNORE INTO idempotency (principal, message_id, response)
                VALUES (?, ?, ?)
                """,
                (principal, message_id, json.dumps(response_payload))
            )
            await db.commit()
        except sqlite3.IntegrityError:
            pass

    return a2a_response(response_payload, status_code=200)
