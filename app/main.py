import asyncio
import json
import sqlite3
from typing import List, Any, Dict, Optional
from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.responses import JSONResponse
import aiosqlite

DB_PATH = "database.db"

# Global lock to serialize database writes during asyncio.gather
db_write_lock = asyncio.Lock()

# -----------------------------------------------------------------------------
# Database Setup & Lifespan / Dependency
# -----------------------------------------------------------------------------

async def get_db():
    """Dependency that provides an aiosqlite connection configured for concurrency."""
    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
        # Enable Write-Ahead Logging (WAL) and set a 30-second busy timeout
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=30000;")
        yield db

app = FastAPI(title="A2A Agent Endpoint")


@app.on_event("startup")
async def on_startup():
    """Ensure database tables and pragmas are properly set up on server launch."""
    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=30000;")
        
        # Ensure Idempotency table exists with primary/unique constraint
        await db.execute("""
            CREATE TABLE IF NOT EXISTS idempotency (
                principal TEXT NOT NULL,
                message_id TEXT NOT NULL,
                response TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (principal, message_id)
            );
        """)
        # Example table setup for packages/tasks processing
        await db.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                data TEXT
            );
        """)
        await db.commit()

# -----------------------------------------------------------------------------
# Core Processing Helpers
# -----------------------------------------------------------------------------

async def process_single_package(pkg: Dict[str, Any], db: aiosqlite.Connection) -> Dict[str, Any]:
    """
    Processes an individual package safely under a database write lock
    to prevent 'database is locked' errors during concurrent gather execution.
    """
    # 1. Non-DB Async operations (e.g. LLM calls, logic processing) can happen here...
    result = {"pkg_id": pkg.get("id"), "status": "processed"}

    # 2. Acquire write lock when writing back to SQLite
    async with db_write_lock:
        await db.execute(
            """
            INSERT OR REPLACE INTO packages (id, status, data)
            VALUES (?, ?, ?)
            """,
            (pkg.get("id"), "processed", json.dumps(result))
        )
        await db.commit()

    return result

# -----------------------------------------------------------------------------
# A2A Protocol Route (/message:send)
# -----------------------------------------------------------------------------

@app.post("/message:send")
@app.post("/message%3Asend")
async def send_message(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db)
):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Extract A2A protocol parameters (adapt key names to your exact schema)
    principal = request.headers.get("x-principal-id", "default_principal")
    message_id = body.get("messageId") or body.get("id") or body.get("params", {}).get("message", {}).get("messageId")

    # Fallback default if message_id is absent
    if not message_id:
        message_id = f"generated_msg_{hash(str(body))}"

    # -------------------------------------------------------------------------
    # 1. Idempotency Check
    # -------------------------------------------------------------------------
    async with db.execute(
        "SELECT response FROM idempotency WHERE principal = ? AND message_id = ?",
        (principal, message_id)
    ) as cursor:
        row = await cursor.fetchone()
        if row and row[0]:
            # Return cached response instantly for duplicate/idempotent requests
            return JSONResponse(content=json.loads(row[0]), status_code=200)

    # -------------------------------------------------------------------------
    # 2. Process Packages / Tasks safely with gather
    # -------------------------------------------------------------------------
    packages = body.get("packages", body.get("params", {}).get("packages", []))
    
    proposals = []
    if packages:
        # Uses process_single_package which is protected by db_write_lock
        proposals = await asyncio.gather(
            *[process_single_package(pkg, db) for pkg in packages],
            return_exceptions=False
        )

    # Build response payload according to your A2A protocol spec
    response_payload = {
        "status": "success",
        "messageId": message_id,
        "proposals": proposals
    }

    # -------------------------------------------------------------------------
    # 3. Store Idempotency Key (INSERT OR IGNORE to prevent UNIQUE constraint error)
    # -------------------------------------------------------------------------
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
            # Safe failover if another concurrent task inserted the key during processing
            pass

    return JSONResponse(content=response_payload, status_code=200)
