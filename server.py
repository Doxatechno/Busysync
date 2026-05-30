import sqlite3
import json
import secrets
import logging
import os
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

CURRENT_AGENT_VERSION = "1.0.0"
ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me-in-production")
DB_PATH   = Path("busy_sync.db")

DEFAULT_AGENT_CONFIG = {
    "poll_every_seconds": 120,
    "lookback_days":      1,
    "invoice_sql": (
        "SELECT VchCode, VchNo, Date, VchType, MasterName1 AS PartyName, "
        "GrandTotal, TaxAmt, NetAmt, Narration "
        "FROM tran1 "
        "WHERE VchType = 9 AND Date >= '{since_date}' "
        "ORDER BY Date DESC"
    ),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("BusyServer")

app = FastAPI(title="BUSY Sync API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                api_key       TEXT UNIQUE NOT NULL,
                created_at    TEXT NOT NULL,
                last_seen     TEXT,
                agent_version TEXT,
                custom_config TEXT
            );
            CREATE TABLE IF NOT EXISTS invoices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   INTEGER NOT NULL,
                vch_code    TEXT,
                vch_no      TEXT,
                date        TEXT,
                party_name  TEXT,
                grand_total TEXT,
                tax_amt     TEXT,
                net_amt     TEXT,
                narration   TEXT,
                raw_data    TEXT,
                synced_at   TEXT NOT NULL,
                FOREIGN KEY (client_id) REFERENCES clients(id),
                UNIQUE(client_id, vch_code)
            );
            CREATE INDEX IF NOT EXISTS idx_invoices_client ON invoices(client_id);
            CREATE INDEX IF NOT EXISTS idx_invoices_date   ON invoices(date);
        """)
    log.info("Database ready.")

def get_client(x_api_key: str = Header(...)):
    with get_db() as db:
        row = db.execute("SELECT * FROM clients WHERE api_key = ?", (x_api_key,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")
    with get_db() as db:
        db.execute(
            "UPDATE clients SET last_seen = ? WHERE api_key = ?",
            (datetime.utcnow().isoformat(), x_api_key),
        )
    return dict(row)

def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

class SyncPayload(BaseModel):
    synced_at:     str
    count:         int
    agent_version: Optional[str] = None
    invoices:      list[dict]

class NewClient(BaseModel):
    name: str

class UpdateConfig(BaseModel):
    config: dict

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def root():
    return {"status": "ok", "service": "BUSY Sync API", "version": "2.0.0"}

@app.get("/api/version")
def get_version(client: dict = Depends(get_client)):
    return {
        "current_version":  CURRENT_AGENT_VERSION,
        "update_available": False,
        "update_url":       None,
    }

@app.get("/api/config")
def get_agent_config(client: dict = Depends(get_client)):
    config = DEFAULT_AGENT_CONFIG.copy()
    if client.get("custom_config"):
        try:
            config.update(json.loads(client["custom_config"]))
        except Exception:
            pass
    return config

@app.post("/api/sync/invoices")
def sync_invoices(payload: SyncPayload, client: dict = Depends(get_client)):
    if payload.agent_version:
        with get_db() as db:
            db.execute(
                "UPDATE clients SET agent_version = ? WHERE id = ?",
                (payload.agent_version, client["id"]),
            )
    inserted = 0
    skipped  = 0
    with get_db() as db:
        for inv in payload.invoices:
            try:
                db.execute(
                    """INSERT OR IGNORE INTO invoices
                       (client_id, vch_code, vch_no, date, party_name,
                        grand_total, tax_amt, net_amt, narration, raw_data, synced_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        client["id"],
                        inv.get("VchCode"),
                        inv.get("VchNo"),
                        inv.get("Date"),
                        inv.get("PartyName"),
                        inv.get("GrandTotal"),
                        inv.get("TaxAmt"),
                        inv.get("NetAmt"),
                        inv.get("Narration"),
                        json.dumps(inv),
                        payload.synced_at,
                    ),
                )
                inserted += (db.execute("SELECT changes()").fetchone()[0])
            except Exception as e:
                log.warning(f"Skipped invoice {inv.get('VchCode')}: {e}")
                skipped += 1
    log.info(f"[{client['name']}] Sync: {inserted} new, {skipped} skipped")
    return {"status": "ok", "inserted": inserted, "skipped": skipped}

@app.get("/api/invoices")
def get_invoices(
    client:     dict = Depends(get_client),
    from_date:  Optional[str] = Query(None),
    to_date:    Optional[str] = Query(None),
    party_name: Optional[str] = Query(None),
    limit:      int = Query(100, le=1000),
    offset:     int = Query(0),
):
    conds  = ["client_id = ?"]
    params = [client["id"]]
    if from_date:  conds.append("date >= ?");          params.append(from_date)
    if to_date:    conds.append("date <= ?");          params.append(to_date)
    if party_name: conds.append("party_name LIKE ?"); params.append(f"%{party_name}%")
    where = " AND ".join(conds)
    with get_db() as db:
        rows  = db.execute(
            f"SELECT * FROM invoices WHERE {where} ORDER BY date DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM invoices WHERE {where}", params).fetchone()[0]
    return {"total": total, "invoices": [dict(r) for r in rows]}

@app.get("/api/invoices/summary")
def invoice_summary(client: dict = Depends(get_client)):
    with get_db() as db:
        stats = db.execute(
            """SELECT COUNT(*) AS total, SUM(CAST(grand_total AS REAL)) AS total_value,
               MAX(date) AS latest_date, MAX(synced_at) AS last_synced
               FROM invoices WHERE client_id = ?""",
            (client["id"],)
        ).fetchone()
    return dict(stats)

@app.post("/admin/clients")
def create_client(body: NewClient, _=Depends(require_admin)):
    api_key = secrets.token_urlsafe(32)
    with get_db() as db:
        db.execute(
            "INSERT INTO clients (name, api_key, created_at) VALUES (?,?,?)",
            (body.name, api_key, datetime.utcnow().isoformat()),
        )
    log.info(f"Created client: {body.name}")
    return {"name": body.name, "api_key": api_key}

@app.get("/admin/clients")
def list_clients(_=Depends(require_admin)):
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, created_at, last_seen, agent_version FROM clients"
        ).fetchall()
    return [dict(r) for r in rows]

@app.patch("/admin/clients/{client_id}/config")
def update_client_config(client_id: int, body: UpdateConfig, _=Depends(require_admin)):
    with get_db() as db:
        db.execute(
            "UPDATE clients SET custom_config = ? WHERE id = ?",
            (json.dumps(body.config), client_id),
        )
    return {"status": "updated", "client_id": client_id, "config": body.config}
