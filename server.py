import json
import secrets
import logging
import os
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

CURRENT_AGENT_VERSION = "1.0.0"
ADMIN_KEY    = os.getenv("ADMIN_KEY", "change-me-in-production")
DATABASE_URL = os.getenv("DATABASE_URL", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("BusyServer")

app = FastAPI(title="BUSY Sync API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DEFAULT_AGENT_CONFIG = {
    "poll_every_seconds": 120,
    "lookback_days":      1,
    "invoice_sql": (
        "SELECT VchCode, VchNo, Date, VchType, MasterName1 AS PartyName, "
        "GrandTotal, TaxAmt, NetAmt, Narration "
        "FROM tran1 WHERE VchType = 9 AND Date >= '{since_date}' ORDER BY Date DESC"
    ),
}

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def init_db():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id            SERIAL PRIMARY KEY,
            name          TEXT NOT NULL,
            api_key       TEXT UNIQUE NOT NULL,
            created_at    TEXT NOT NULL,
            last_seen     TEXT,
            agent_version TEXT,
            custom_config TEXT
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id          SERIAL PRIMARY KEY,
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
    conn.commit()
    cur.close()
    conn.close()
    log.info("Database ready.")

def get_client(x_api_key: str = Header(...)):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM clients WHERE api_key = %s", (x_api_key,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(status_code=401, detail="Invalid API key")
    cur.execute("UPDATE clients SET last_seen = %s WHERE api_key = %s",
                (datetime.utcnow().isoformat(), x_api_key))
    conn.commit()
    cur.close(); conn.close()
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
    return {"current_version": CURRENT_AGENT_VERSION, "update_available": False, "update_url": None}

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
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("UPDATE clients SET agent_version = %s WHERE id = %s",
                    (payload.agent_version, client["id"]))
        conn.commit()
        cur.close(); conn.close()

    inserted = 0
    skipped  = 0
    conn = get_db()
    cur  = conn.cursor()
    for inv in payload.invoices:
        try:
            cur.execute("""
                INSERT INTO invoices
                (client_id, vch_code, vch_no, date, party_name,
                 grand_total, tax_amt, net_amt, narration, raw_data, synced_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (client_id, vch_code) DO NOTHING
            """, (
                client["id"], inv.get("VchCode"), inv.get("VchNo"),
                inv.get("Date"), inv.get("PartyName"), inv.get("GrandTotal"),
                inv.get("TaxAmt"), inv.get("NetAmt"), inv.get("Narration"),
                json.dumps(inv), payload.synced_at,
            ))
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            log.warning(f"Skipped invoice {inv.get('VchCode')}: {e}")
            skipped += 1
    conn.commit()
    cur.close(); conn.close()
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
    conds  = ["client_id = %s"]
    params = [client["id"]]
    if from_date:  conds.append("date >= %s");           params.append(from_date)
    if to_date:    conds.append("date <= %s");           params.append(to_date)
    if party_name: conds.append("party_name ILIKE %s"); params.append(f"%{party_name}%")
    where = " AND ".join(conds)
    conn  = get_db()
    cur   = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT * FROM invoices WHERE {where} ORDER BY date DESC LIMIT %s OFFSET %s",
                params + [limit, offset])
    rows = cur.fetchall()
    cur.execute(f"SELECT COUNT(*) FROM invoices WHERE {where}", params)
    total = cur.fetchone()["count"]
    cur.close(); conn.close()
    return {"total": total, "invoices": [dict(r) for r in rows]}

@app.get("/api/invoices/summary")
def invoice_summary(client: dict = Depends(get_client)):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT COUNT(*) AS total,
               SUM(CAST(grand_total AS FLOAT)) AS total_value,
               MAX(date) AS latest_date,
               MAX(synced_at) AS last_synced
        FROM invoices WHERE client_id = %s
    """, (client["id"],))
    stats = cur.fetchone()
    cur.close(); conn.close()
    return dict(stats)

@app.post("/admin/clients")
def create_client(body: NewClient, _=Depends(require_admin)):
    api_key = secrets.token_urlsafe(32)
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("INSERT INTO clients (name, api_key, created_at) VALUES (%s,%s,%s)",
                (body.name, api_key, datetime.utcnow().isoformat()))
    conn.commit()
    cur.close(); conn.close()
    log.info(f"Created client: {body.name}")
    return {"name": body.name, "api_key": api_key}

@app.get("/admin/clients")
def list_clients(_=Depends(require_admin)):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, created_at, last_seen, agent_version FROM clients")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [dict(r) for r in rows]

@app.patch("/admin/clients/{client_id}/config")
def update_client_config(client_id: int, body: UpdateConfig, _=Depends(require_admin)):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE clients SET custom_config = %s WHERE id = %s",
                (json.dumps(body.config), client_id))
    conn.commit()
    cur.close(); conn.close()
    return {"status": "updated", "client_id": client_id, "config": body.config}
