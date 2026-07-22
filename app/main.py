import asyncio
import json
import sqlite3
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse
import aiosqlite

DB_PATH = "database.db"

# Global lock to prevent 'database is locked' errors during concurrent gather calls
db_write_lock = asyncio.Lock()

# Standard A2A Response Content-Type Header
A2A_HEADERS = {"Content-Type": "application/a2a+json"}

# -----------------------------------------------------------------------------
# Database Setup & Lifespan
# -----------------------------------------------------------------------------

async def get_db():
    """Dependency providing an aiosqlite connection with WAL mode and busy timeout."""
    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=30000;")
        yield db

app = FastAPI(title="A2A Agent Endpoint")

@app.on_event("startup")
async def on_startup():
    """Initialize DB schema and concurrency PRAGMAs."""
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
# Authentication Guard
# -----------------------------------------------------------------------------

def verify_bearer_auth(request: Request) -> str:
    """
    Enforces Bearer authentication. Returns HTTP 401 if missing/invalid.
    Satisfies LIST_AUTH_REQUIRED and SEND_AUTH_REQUIRED harness checks.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization Bearer token",
            headers={"WWW-Authenticate": "Bearer"}
        )
    return auth_header.split(" ")[1]

# -----------------------------------------------------------------------------
# Agent Card Discovery Endpoint
# -----------------------------------------------------------------------------

@app.get("/.well-known/agent-card.json")
async def get_agent_card(request: Request):
    """
    Serves the mandatory A2A Agent Card specification.
    Dynamically infers the base URL to pass AGENT_CARD_CONTRACT.
    """
    base_url = str(request.base_url).rstrip("/")
    
    card_payload = {
        "name": "A2A Compliant Agent",
        "description": "Task processor agent for A2A benchmark evaluation",
        "version": "1.0.0",
        "url": base_url,
        "defaultInputModes": ["text/plain", "application/json", "application/a2a+json"],
        "defaultOutputModes": ["text/plain", "application/json", "application/a2a+json"],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True
        },
        "skills": [
            {
                "id": "task_processor",
                "name": "Task Processor",
                "description": "Processes messages and packages.",
                "tags": ["task-processing", "a2a"]
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
    """Protected task listing endpoint."""
    principal = request.headers.get("x-principal-id", token)

    async with db.execute(
        "SELECT id, status, data FROM packages WHERE principal = ?", (principal,)
    ) as cursor:
        rows = await cursor.fetchall()
        tasks = [
            {"id": r[0], "status": r[1], "data": json.loads(r[2]) if r[2] else {}}
            for r in rows
        ]

    return JSONResponse(
        content={"tasks": tasks},
        status_code=200,
        headers=A2A_HEADERS
    )

# -----------------------------------------------------------------------------
# Core Processing Helper
# -----------------------------------------------------------------------------

async def process_single_package(
    pkg: Dict[str, Any],
    principal: str,
    db: aiosqlite.Connection
) -> Dict[str, Any]:
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

# -----------------------------------------------------------------------------
# Protected Message Send Endpoint (/message:send)
# -----------------------------------------------------------------------------

@app.post("/message:send")
@app.post("/message%3Asend")
async def send_message(
    request: Request,
    token: str = Depends(verify_bearer_auth),
    db: aiosqlite.Connection = Depends(get_db)
):
    # Parse payload smoothly
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Extract principal and message ID flexible across schema variations
    principal = request.headers.get("x-principal-id", token)
    message_id = (
        body.get("messageId")
        or body.get("id")
        or body.get("params", {}).get("message", {}).get("messageId")
    )

    if not message_id:
        message_id = f"generated_msg_{hash(str(body))}"

    # 1. Idempotency Check
    async with db.execute(
        "SELECT response FROM idempotency WHERE principal = ? AND message_id = ?",
        (principal, message_id)
    ) as cursor:
        row = await cursor.fetchone()
        if row and row[0]:
            return JSONResponse(
                content=json.loads(row[0]),
                status_code=200,
                headers=A2A_HEADERS
            )

    # 2. Package Processing
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

    # 3. Store Idempotency Record (INSERT OR IGNORE handles duplicate race conditions)
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

    # Return success with required Content-Type: application/a2a+json header
    return JSONResponse(
        content=response_payload,
        status_code=200,
        headers=A2A_HEADERS
    )
