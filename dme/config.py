"""Central configuration. Everything tunable lives here, nothing hidden in modules."""

from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CASES_DIR = DATA_DIR / "cases"  # event logs, one JSONL per case (Step 2)
SCENARIOS_DIR = ROOT / "scenarios"

# --- Models ---------------------------------------------------------------
# Agent = the system's own voice on calls + transcript extraction.
AGENT_MODEL = "claude-opus-5-5"
# Sim = LLM role-playing the counterparties (supplier, PCP office, patient).
# Swap to "claude-haiku-5-5" to make simulated runs cheaper/faster.
SIM_MODEL = "claude-opus-5-5"

AGENT_EFFORT = "medium"
SIM_EFFORT = "low"

# --- Simulation ------------------------------------------------------------
# Fixed start time so runs are reproducible. Monday 9am Chicago time (naive).
SIM_START = datetime(2026, 11, 23, 9, 0)
OPEN_HOUR, CLOSE_HOUR = 9, 17  # calls are only placed on business days in this window
HOLIDAYS = {
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),
    date(2027, 1, 1),
    date(2027, 1, 18),
}

OUR_FAX = "(312) 555-0100"  # mocked: where the PCP office sends orders

# --- Policy knobs (deterministic; no LLM involved) --------------------------
MAX_SUPPLIER_MILES = 15.0
CALL_GAP = timedelta(minutes=15)  # simulated length of one phone call
CALLBACK_DELAY = timedelta(hours=4)
CHECKIN_RETRY = timedelta(hours=2)
MAX_DELIVERY_DAYS_OUT = 30
MAX_PATIENT_ATTEMPTS = 3
PATIENT_RETRY = timedelta(days=1, hours=3)  # a different time of day
FAX_DELAY = timedelta(hours=3)
ORDER_FOLLOWUP_BUSINESS_DAYS = 2
MAX_ORDER_FOLLOWUPS = 2
MAX_ORDER_CORRECTIONS = 2
MAX_PCP_NO_ANSWERS = 3
MAX_CALL_TURNS = 12  # agent turns per call
MAX_TASK_RETRIES = 3  # LLM/API failures before a human is told
TASK_RETRY_DELAY = timedelta(minutes=15)
