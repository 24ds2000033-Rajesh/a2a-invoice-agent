import os
import aiosqlite

DB_PATH = os.getenv("DATABASE_PATH", "/tmp/agent.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        
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
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS idempotency (
                principal TEXT NOT NULL,
                message_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                task_id TEXT NOT NULL,
                PRIMARY KEY (principal, message_id)
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS package_cache (
                package_hash TEXT PRIMARY KEY,
                decision_json TEXT NOT NULL
            );
        """)
        
        await db.commit()
