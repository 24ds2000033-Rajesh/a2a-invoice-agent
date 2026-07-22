import os
import sqlite3
import aiosqlite
import json

DB_PATH = os.getenv("DATABASE_PATH", "/tmp/agent.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        
        # Tasks Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                context_id TEXT NOT NULL,
                status TEXT NOT NULL,
                history_json TEXT NOT NULL,
                artifacts_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Idempotency Table: (principal, message_hash) -> task_id
        await db.execute("""
            CREATE TABLE IF NOT EXISTS idempotency (
                principal TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                task_id TEXT NOT NULL,
                PRIMARY KEY (principal, message_hash)
            );
        """)

        # Package Decision Cache: Canonical Package Hash -> Decision JSON
        await db.execute("""
            CREATE TABLE IF NOT EXISTS package_cache (
                package_hash TEXT PRIMARY KEY,
                decision_json TEXT NOT NULL
            );
        """)
        
        await db.commit()

async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        yield db
