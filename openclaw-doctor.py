#!/usr/bin/env python3
"""
OPENCLAW DOCTOR v2 — Autonomous Self-Healing Daemon with Agentic Loop

Runs as a macOS LaunchAgent. Completely independent of OpenClaw.
Haiku for monitoring/diagnosis. Sonnet for code fixes.
Maintains conversation history for multi-turn problem solving.

SETUP: Before running, set these environment variables in your LaunchAgent plist:
  ANTHROPIC_API_KEY  — from console.anthropic.com
  TELEGRAM_CHAT_ID   — your Telegram group chat ID (ask your agent: "what's our chat ID?")
  AGENT_NAME         — your agent's name (e.g. "Lynx")
  OPENCLAW_USER      — your Mac username (run: whoami)

Optional (for SMS alerts that survive Telegram outages):
  TWILIO_ACCOUNT_SID — AC... from console.twilio.com
  TWILIO_API_KEY     — SK... API Key SID
  TWILIO_API_SECRET  — API Key Secret
  TWILIO_FROM        — your Twilio phone number (+1...)
  ALERT_PHONE        — your personal phone number (+1...)

CUSTOMIZATION:
  1. Set CRON_EXPECTED below to match your actual cron jobs (run: crontab -l)
  2. Set WORKSPACE to your OpenClaw workspace path
  3. Set OPENCLAW_JSON to your openclaw.json path
"""

import subprocess
import json
import time
import os
import requests
import logging
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIG — customize these for your setup
# ============================================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "")
AGENT_NAME        = os.environ.get("AGENT_NAME", "Agent")
OPENCLAW_USER     = os.environ.get("OPENCLAW_USER", os.environ.get("USER", ""))

# Paths — auto-derived from OPENCLAW_USER, or override manually
OPENCLAW_HOME  = f"/Users/{OPENCLAW_USER}/.openclaw"
OPENCLAW_JSON  = f"{OPENCLAW_HOME}/openclaw.json"
WORKSPACE      = f"{OPENCLAW_HOME}/workspace"
OPENCLAW_BIN   = "/opt/homebrew/bin/openclaw"
PYTHON_BIN     = "/opt/homebrew/bin/python3"
LOG_FILE       = f"/tmp/{AGENT_NAME.lower()}-doctor.log"
STATE_FILE     = f"/tmp/{AGENT_NAME.lower()}-doctor-state.json"
CRASH_LOG      = f"{WORKSPACE}/memory/crash-forensics.md"

# Twilio (optional — for SMS when Telegram is down)
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_API_KEY     = os.environ.get("TWILIO_API_KEY", "")
TWILIO_API_SECRET  = os.environ.get("TWILIO_API_SECRET", "")
TWILIO_FROM        = os.environ.get("TWILIO_FROM", "")
ALERT_PHONE        = os.environ.get("ALERT_PHONE", "")

# Tuning
CHECK_INTERVAL         = 300   # seconds between health checks (5 min)
AI_DIAGNOSIS_THRESHOLD = 2     # failures before calling Claude

# !! CUSTOMIZE THIS !! Run `crontab -l` and list the script names you expect.
# Leave empty [] to skip cron monitoring.
CRON_EXPECTED = [
    # "my-script.sh",
    # "my-other-script.py",
]

# Models
HAIKU  = "claude-haiku-4-5-20251001"  # Fast, cheap — monitoring + diagnosis
SONNET = "claude-sonnet-4-6"           # Smart — code fixes + patches

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger()

# ============================================================
# STATE
# ============================================================
def load_state():
    try:
        return json.loads(Path(STATE_FILE).read_text())
    except:
        return {}

def save_state(state):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))

def increment_failure(state, key):
    state[key] = state.get(key, 0) + 1
    return state

def clear_failure(state, key):
    state[key] = 0
    return state

# ============================================================
# ALERTING
# ============================================================
def get_telegram_token():
    try:
        d = json.loads(Path(OPENCLAW_JSON).read_text())
        return d.get("channels", {}).get("telegram", {}).get("botToken", "")
    except:
        return ""

def send_telegram(msg):
    token = get_telegram_token()
    if not token or not TELEGRAM_CHAT:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=8
        )
        return r.json().get("ok", False)
    except:
        return False

def send_sms(msg):
    """Optional SMS via Twilio — survives Telegram outages."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_API_KEY, TWILIO_API_SECRET, TWILIO_FROM, ALERT_PHONE]):
        return False
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_API_KEY, TWILIO_API_SECRET),
            data={"From": TWILIO_FROM, "To": ALERT_PHONE, "Body": msg[:1600]},
            timeout=10
        )
        return r.status_code == 201
    except:
        return False

def send_macos_notification(msg):
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{msg[:200]}" with title "🚨 {AGENT_NAME} Doctor" sound name "Basso"'],
        capture_output=True
    )

def alert(msg, sms=False):
    log.warning(f"ALERT: {msg}")
    telegram_ok = send_telegram(f"🚨 *{AGENT_NAME.upper()} DOCTOR*\n{msg}")
    if sms or not telegram_ok:
        send_sms(f"{AGENT_NAME} DOCTOR: {msg}")
    send_macos_notification(msg)

# ============================================================
# SHELL RUNNER
# ============================================================
def run(cmd, timeout=30):
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        return False, "", str(e)

# ============================================================
# AGENTIC CONVERSATION LOOP
# Core of v2 — multi-turn Claude conversation for hard problems
# ============================================================
SYSTEM_PROMPT = f"""You are an expert systems engineer diagnosing and fixing {AGENT_NAME} — an OpenClaw AI agent system running on a Mac.

SYSTEM CONTEXT:
- OpenClaw gateway at ws://127.0.0.1:18789 (LaunchAgent: ai.openclaw.gateway)
- Two Pythons: /usr/bin/python3 (BAD, no packages) vs /opt/homebrew/bin/python3 (GOOD)
- NEVER edit openclaw.json manually — use: openclaw config set
- NEVER set gateway.auth.mode to "off" — only valid mode is "token"
- Correct gateway probe: openclaw health check (NOT openclaw gateway status — it lies)
- Cron scripts MUST have PATH export on line 2: export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
- Gateway crashes: use openclaw gateway install then openclaw gateway restart — NO pkill
- SIGTERM to gateway = something killed it intentionally, not a crash
- openclaw.json is at: {OPENCLAW_JSON}
- Workspace is at: {WORKSPACE}

YOUR ROLE:
- Diagnose what went wrong with precision
- Propose exact bash commands or code patches to fix it
- When writing code fixes, make them production-ready and defensive
- Be concise
- Flag any risk before suggesting destructive actions

RESPONSE FORMAT for diagnosis:
DIAGNOSIS: [one sentence]
FIX: [exact commands or code]
RISK: [any concerns]
CONFIDENCE: [high/medium/low]

RESPONSE FORMAT for code patches:
PATCH_FILE: [full path]
PATCH: [complete fixed function or section]
TEST: [how to verify it worked]"""

def claude_conversation(initial_message, logs, model=HAIKU, max_turns=4):
    """
    Multi-turn conversation with Claude.
    Haiku for diagnosis, Sonnet for code patches.
    Returns (final_response, conversation_history).
    """
    if not ANTHROPIC_API_KEY:
        return "No API key available.", []

    messages = [{"role": "user", "content": f"{initial_message}\n\nRECENT LOGS:\n{logs[:4000]}"}]
    conversation_history = []
    final_response = ""

    for turn in range(max_turns):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": model,
                    "max_tokens": 1000,
                    "system": SYSTEM_PROMPT,
                    "messages": messages
                },
                timeout=45
            )
            response = r.json()["content"][0]["text"]
            conversation_history.append({"turn": turn + 1, "model": model, "response": response})
            final_response = response
            messages.append({"role": "assistant", "content": response})

            if turn < max_turns - 1:
                followup = get_followup_data(response)
                if followup:
                    messages.append({"role": "user", "content": followup})
                    log.info(f"🔄 Conversation turn {turn + 2} — gathering more data")
                else:
                    break

        except Exception as e:
            log.error(f"Claude API error on turn {turn + 1}: {e}")
            break

    return final_response, conversation_history

def get_followup_data(response):
    """Auto-detect what Claude wants to check and fetch it."""
    followups = []
    today = datetime.now().strftime("%Y-%m-%d")

    log_hints = [
        ("openclaw log",  f"tail -30 /tmp/openclaw/openclaw-{today}.log"),
        ("gateway.log",   f"tail -30 {OPENCLAW_HOME}/logs/gateway.log"),
        ("crontab",       "crontab -l"),
        ("lsof",          "lsof -i :18789"),
        ("process",       "ps aux | grep -E 'openclaw|python' | grep -v grep"),
        ("disk",          "df -h /"),
        ("memory",        "vm_stat | head -10"),
    ]

    for hint, cmd in log_hints:
        if hint.lower() in response.lower():
            ok, out, err = run(cmd, timeout=10)
            if out:
                followups.append(f"You asked about {hint}:\n```\n{out[:500]}\n```")

    if followups:
        return "\n\n".join(followups) + "\n\nDoes this change your diagnosis or fix?"
    return None

def get_recent_logs():
    """Gather recent logs from all available sources."""
    logs = []
    today = datetime.now().strftime("%Y-%m-%d")
    sources = [
        f"/tmp/openclaw/openclaw-{today}.log",
        f"{OPENCLAW_HOME}/logs/gateway.log",
        f"/tmp/{AGENT_NAME.lower()}-guardian.log",
        f"/tmp/{AGENT_NAME.lower()}-healthcheck.log",
    ]
    for src in sources:
        try:
            lines = Path(src).read_text().splitlines()[-20:]
            logs.append(f"=== {src} ===\n" + "\n".join(lines))
        except:
            pass
    return "\n\n".join(logs)

# ============================================================
# CRASH FORENSICS + SELF-IMPROVEMENT
# ============================================================
def attempt_autonomous_fix(diagnosis_response, situation):
    """Attempt known safe fixes autonomously. Returns True if fixed."""
    safe_auto_fixes = {
        "gateway install": f"{OPENCLAW_BIN} gateway install",
        "gateway restart": f"{OPENCLAW_BIN} gateway restart",
        "crontab restore": f"bash {WORKSPACE}/scripts/crontab-restore.sh",
    }

    response_lower = diagnosis_response.lower()
    for fix_key, fix_cmd in safe_auto_fixes.items():
        if fix_key in response_lower and "CONFIDENCE: high" in diagnosis_response:
            log.info(f"🔧 Auto-applying safe fix: {fix_key}")
            ok, out, err = run(fix_cmd, timeout=30)
            if ok:
                log.info(f"✅ Auto-fix applied: {fix_key}")
                send_telegram(f"⚡ *{AGENT_NAME.upper()} DOCTOR AUTO-FIX*\nApplied: {fix_key}\nResult: Success")
                return True
    return False

def request_code_patch(situation, diagnosis, logs):
    """Use Sonnet to write a production-quality code patch."""
    log.info("🧠 Requesting Sonnet code patch...")
    patch_prompt = f"""A component of the {AGENT_NAME} system failed.

SITUATION: {situation}
DIAGNOSIS: {diagnosis}

Write a production-ready code patch to prevent this from happening again.
Specify which file to patch and provide the complete fixed function.
Make it defensive — add retries, better error handling, and clear logging."""

    patch_response, _ = claude_conversation(patch_prompt, logs, model=SONNET, max_turns=3)
    return patch_response

def capture_crash_forensics(situation, state):
    """Full agentic crash analysis: diagnose → auto-fix → patch → report."""
    log.info(f"🔍 Starting crash forensics: {situation}")
    logs = get_recent_logs()

    # Step 1: Haiku multi-turn diagnosis
    diagnosis, history = claude_conversation(
        f"Diagnose this failure: {situation}", logs, model=HAIKU, max_turns=4
    )
    log.info(f"🔍 Diagnosis complete ({len(history)} turns)")

    # Step 2: Try autonomous fix
    fixed = attempt_autonomous_fix(diagnosis, situation)
    if fixed:
        write_forensics_report(situation, diagnosis, history, "AUTO-FIXED", None)
        return state

    # Step 3: Sonnet writes a code patch
    patch = request_code_patch(situation, diagnosis, logs)
    log.info("🧠 Sonnet patch written")

    # Step 4: Save full report
    write_forensics_report(situation, diagnosis, history, "PATCH PROPOSED", patch)

    # Step 5: Alert
    short_diagnosis = diagnosis[:500]
    alert(
        f"🔍 *CRASH FORENSICS*\n"
        f"*Situation:* {situation[:200]}\n\n"
        f"*Diagnosis:* {short_diagnosis}\n\n"
        f"*Patch saved to:* crash-forensics.md\n"
        f"Tell {AGENT_NAME}: apply latest crash patch to deploy it.",
        sms=True
    )
    return state

def write_forensics_report(situation, diagnosis, history, status, patch):
    """Append timestamped incident report to crash-forensics.md."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    turns_summary = "\n".join([
        f"Turn {h['turn']} ({h['model']}): {h['response'][:300]}..."
        for h in history
    ])
    entry = f"""
---

## Crash Report — {timestamp}

**Status:** {status}

**Situation:** {situation}

### Diagnosis
{diagnosis}

### Conversation History ({len(history)} turns)
{turns_summary}

### Proposed Patch
{patch if patch else "Auto-fix applied — no patch needed"}

---
"""
    Path(CRASH_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(CRASH_LOG, "a") as f:
        f.write(entry)
    log.info(f"📋 Forensics report saved to {CRASH_LOG}")

# ============================================================
# HEALTH CHECKS
# ============================================================
def check_gateway(state):
    ok, out, err = run(f"{OPENCLAW_BIN} health check", timeout=15)
    if "Telegram: ok" in out:
        log.info("✅ Gateway: OK")
        if state.get("gateway", 0) > 0:
            log.info("⚡ Gateway: recovered from previous failure")
        return clear_failure(state, "gateway")

    log.warning("❌ Gateway: DOWN — attempting safe restart")
    run(f"{OPENCLAW_BIN} gateway restart", timeout=20)
    time.sleep(10)
    ok2, out2, _ = run(f"{OPENCLAW_BIN} health check", timeout=15)
    if "Telegram: ok" in out2:
        log.info("⚡ Gateway: Restarted successfully")
        send_telegram(f"⚡ {AGENT_NAME} Doctor auto-fixed: gateway restarted.")
        return clear_failure(state, "gateway")

    # Second attempt: reinstall then restart
    log.warning("❌ Gateway: trying reinstall")
    run(f"{OPENCLAW_BIN} gateway install", timeout=20)
    time.sleep(5)
    run(f"{OPENCLAW_BIN} gateway restart", timeout=20)
    time.sleep(10)
    ok3, out3, _ = run(f"{OPENCLAW_BIN} health check", timeout=15)
    if "Telegram: ok" in out3:
        log.info("⚡ Gateway: Recovered after reinstall")
        send_telegram(f"⚡ {AGENT_NAME} Doctor: gateway recovered after reinstall.")
        return clear_failure(state, "gateway")

    state = increment_failure(state, "gateway")
    failures = state.get("gateway", 0)
    if failures >= AI_DIAGNOSIS_THRESHOLD:
        state = capture_crash_forensics(
            f"OpenClaw gateway DOWN — won't restart after {failures} attempts. "
            f"Both restart and reinstall failed. Check for SIGTERM source.",
            state
        )
    else:
        alert(f"Gateway DOWN — attempt {failures}. Retrying next cycle.")
    return state

def check_openclaw_json(state):
    try:
        json.loads(Path(OPENCLAW_JSON).read_text())
        log.info("✅ openclaw.json: valid")
        return clear_failure(state, "config")
    except Exception as e:
        state = increment_failure(state, "config")
        bak = OPENCLAW_JSON + ".bak"
        if Path(bak).exists():
            try:
                json.loads(Path(bak).read_text())
                run(f"cp {bak} {OPENCLAW_JSON}")
                log.info("⚡ openclaw.json: restored from .bak")
                send_telegram(f"⚡ {AGENT_NAME} Doctor: openclaw.json restored from backup.")
                return clear_failure(state, "config")
            except:
                pass
        alert(f"openclaw.json CORRUPT — no valid backup! Error: {e}", sms=True)
        return state

def check_python(state):
    if not Path(PYTHON_BIN).exists():
        alert(f"CRITICAL: {PYTHON_BIN} missing!", sms=True)
        return increment_failure(state, "python")
    ok, out, err = run(f"{PYTHON_BIN} -c 'import sys; print(\"ok\")'", timeout=10)
    if out == "ok":
        log.info("✅ Python: OK")
        return clear_failure(state, "python")
    alert(f"Python broken: {err}", sms=True)
    return increment_failure(state, "python")

def check_disk(state):
    ok, out, err = run("df / | tail -1 | awk '{print $5}' | tr -d '%'")
    try:
        pct = int(out.strip())
        if pct > 90:
            alert(f"Disk at {pct}% — CRITICAL.", sms=True)
            rotate_logs()
        elif pct > 85:
            alert(f"Disk at {pct}% — getting full.")
        else:
            log.info(f"✅ Disk: {pct}%")
    except:
        pass
    return state

def check_cron(state):
    """Monitor cron jobs. Set CRON_EXPECTED at the top of this file."""
    if not CRON_EXPECTED:
        return state  # Skip if not configured

    ok, out, _ = run("crontab -l")
    missing = [j for j in CRON_EXPECTED if j not in out]
    if not missing:
        log.info(f"✅ Cron: OK ({len(CRON_EXPECTED)} jobs)")
        return clear_failure(state, "cron")

    log.warning(f"❌ Cron missing: {missing}")
    restore = f"{WORKSPACE}/scripts/crontab-restore.sh"
    if Path(restore).exists():
        run(f"bash {restore}", timeout=15)
        ok2, out2, _ = run("crontab -l")
        if all(j in out2 for j in CRON_EXPECTED):
            send_telegram(f"⚡ {AGENT_NAME} Doctor: crontab restored.")
            return clear_failure(state, "cron")

    alert(f"Cron jobs missing: {missing} — restore failed!", sms=True)
    return increment_failure(state, "cron")

def rotate_logs():
    """Rotate any log files > 5MB in the workspace."""
    for logfile in Path(WORKSPACE).rglob("*.log"):
        try:
            if logfile.stat().st_size > 5_000_000:
                logfile.write_bytes(logfile.read_bytes()[-1_000_000:])
                log.info(f"🔄 Rotated {logfile}")
        except:
            pass

# ============================================================
# DAILY SUMMARY
# ============================================================
def maybe_send_daily_summary(state):
    hour = datetime.now().hour
    today = datetime.now().strftime("%Y-%m-%d")
    if hour == 8 and state.get("last_summary") != today:
        state["last_summary"] = today
        crashes = sum(1 for k in state if isinstance(state[k], int) and state[k] > 0)
        send_telegram(
            f"🏥 *{AGENT_NAME.upper()} DOCTOR DAILY REPORT*\n"
            f"Monitoring every 5 min. Independent of OpenClaw.\n"
            f"Active failure counters: {crashes}\n"
            f"Status: ✅ Nominal"
        )
    return state

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    log.info(f"🏥 {AGENT_NAME} Doctor v2 starting — agentic mode active")
    log.info(f"Agent: {AGENT_NAME} | User: {OPENCLAW_USER} | Workspace: {WORKSPACE}")
    log.info("Haiku for diagnosis, Sonnet for code patches")
    while True:
        try:
            state = load_state()
            state = check_openclaw_json(state)
            state = check_gateway(state)
            state = check_python(state)
            state = check_disk(state)
            state = check_cron(state)
            state = maybe_send_daily_summary(state)
            save_state(state)
            log.info("✅ Doctor cycle complete")
        except Exception as e:
            log.error(f"Doctor cycle crashed: {e}")
            try:
                alert(f"{AGENT_NAME} Doctor itself crashed: {e}\nCheck {LOG_FILE}")
            except:
                pass
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
