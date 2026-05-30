import sys, os, time, json, logging, threading, winreg
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
import requests, psutil, schedule

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

SERVER_URL = "https://busysync-production.up.railway.app"
API_KEY    = "OA1sDvOsoQKKTVH91b7okphmKObl-FCea4-K8sXDfzI"

AGENT_VERSION  = "1.0.0"
APP_NAME       = "BusySyncAgent"
BUSY_URL       = "http://localhost:981"
BUSY_PROCESSES = {"busy.exe", "busywin.exe", "busyaccount.exe", "busy21.exe", "busy20.exe", "busy19.exe"}

APP_DIR    = Path(os.getenv("APPDATA", ".")) / APP_NAME
LOG_FILE   = APP_DIR / "agent.log"
STATE_FILE = APP_DIR / "state.json"
APP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(APP_NAME)

def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"last_sync": None}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def ensure_autostart():
    try:
        exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
        winreg.CloseKey(key)
        log.info("Auto-start registered.")
    except Exception as e:
        log.warning(f"Could not set auto-start: {e}")

def fetch_config():
    defaults = {
        "poll_every_seconds": 120,
        "lookback_days": 1,
        "invoice_sql": (
            "SELECT VchCode, VchNo, Date, VchType, MasterName1 AS PartyName, "
            "GrandTotal, TaxAmt, NetAmt, Narration "
            "FROM tran1 WHERE VchType = 9 AND Date >= '{since_date}' ORDER BY Date DESC"
        ),
    }
    try:
        resp = requests.get(f"{SERVER_URL}/api/config",
            headers={"X-API-Key": API_KEY}, timeout=10)
        resp.raise_for_status()
        return {**defaults, **resp.json()}
    except Exception as e:
        log.warning(f"Could not fetch config: {e} — using defaults.")
        return defaults

def is_busy_running():
    for p in psutil.process_iter(["name"]):
        if p.info["name"] and p.info["name"].lower() in BUSY_PROCESSES:
            return True
    return False

def busy_query(sql, username, password):
    try:
        resp = requests.get(BUSY_URL, headers={
            "SC": "1", "UserName": username, "Pwd": password, "Qry": sql
        }, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        log.warning("BUSY Web Service unreachable.")
        return []
    except Exception as e:
        log.error(f"BUSY request error: {e}")
        return []
    if resp.headers.get("Result") == "F":
        log.error(f"BUSY error: {resp.headers.get('Description')}")
        return []
    try:
        root = ET.fromstring(resp.text)
        return [{child.tag: (child.text or "").strip() for child in row}
                for row in root.iter("Row")]
    except ET.ParseError as e:
        log.error(f"XML parse error: {e}")
        return []

def push_invoices(invoices):
    try:
        resp = requests.post(f"{SERVER_URL}/api/sync/invoices",
            json={"synced_at": datetime.utcnow().isoformat(), "count": len(invoices),
                  "agent_version": AGENT_VERSION, "invoices": invoices},
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            timeout=30)
        resp.raise_for_status()
        result = resp.json()
        log.info(f"Pushed {len(invoices)} invoice(s) — {result['inserted']} new.")
        return True
    except Exception as e:
        log.error(f"Push failed: {e}")
        return False

def sync_cycle():
    if not is_busy_running():
        log.debug("BUSY not running — skipping.")
        return
    log.info("BUSY detected — syncing...")
    cfg   = fetch_config()
    state = load_state()
    since = datetime.fromisoformat(state["last_sync"]) if state["last_sync"] \
            else datetime.now() - timedelta(days=cfg.get("lookback_days", 1))
    since_date = since.strftime("%d-%m-%Y")
    sql = cfg["invoice_sql"].format(since_date=since_date)
    invoices = busy_query(sql, cfg.get("busy_user", "admin"), cfg.get("busy_pass", "admin"))
    if invoices:
        if push_invoices(invoices):
            state["last_sync"] = datetime.utcnow().isoformat()
            save_state(state)
    else:
        log.info("No new invoices.")
        state["last_sync"] = datetime.utcnow().isoformat()
        save_state(state)

def build_tray_icon():
    img  = Image.new("RGB", (64, 64), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.ellipse([10, 10, 54, 54], fill=(34, 197, 94))
    return img

def run_tray(stop_event):
    if not TRAY_AVAILABLE:
        return
    def on_quit(icon, _):
        stop_event.set()
        icon.stop()
    pystray.Icon(APP_NAME, build_tray_icon(), "BUSY Sync — Running",
        menu=pystray.Menu(pystray.MenuItem("Quit", on_quit))).run()

def main():
    log.info("=" * 50)
    log.info(f"  {APP_NAME} v{AGENT_VERSION} starting")
    log.info(f"  Server: {SERVER_URL}")
    log.info("=" * 50)
    ensure_autostart()
    sync_cycle()
    cfg = fetch_config()
    interval = cfg.get("poll_every_seconds", 120)
    schedule.every(interval).seconds.do(sync_cycle)
    log.info(f"Polling every {interval}s.")
    stop_event = threading.Event()
    if TRAY_AVAILABLE:
        threading.Thread(target=run_tray, args=(stop_event,), daemon=True).start()
    try:
        while not stop_event.is_set():
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopped.")

if __name__ == "__main__":
    main()
