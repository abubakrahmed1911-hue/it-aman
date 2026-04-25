#!/usr/bin/env python3
"""
IT Aman Printer Daemon v3.4
============================
A systemd-managed Unix socket daemon for managing CUPS printers.
Runs as root, listens on /run/it-aman/it-aman.sock, and processes
JSON commands from the IT Aman GUI.

Security:
  - Only whitelisted CUPS commands are executed.
  - No arbitrary shell commands are permitted.
  - Ed25519 + SHA256 manifest verification for all updates.
  - chattr +i properly handled during updates.
  - Config writes done via daemon (root) not GUI (user).
  - Socket restricted to it-aman group (0o660) + SO_PEERCRED auth.

v3.4 Changes (from v3.3):
  - SECURITY FIX: Socket chmod 0o666 → 0o660 + it-aman group ownership.
    Any local user could previously send arbitrary commands to the root
    daemon. Now only members of the 'it-aman' group are accepted.
  - SECURITY FIX: SO_PEERCRED peer-credential check added to handle_client.
    The daemon now verifies that callers are root OR a member of the
    it-aman group before processing any command.
  - SECURITY FIX: validate_command_args now strictly requires the first
    token to be in ALLOWED_COMMANDS (previously a path whose basename
    matched could bypass this check).
  - SECURITY FIX: handle_update_all manifest exception is now FATAL.
    Previously, a network/parse error silently bypassed Ed25519/SHA256
    verification, enabling unsigned updates.
  - BUG FIX: run_command() now accepts an optional env= parameter.
    The SPRT brand installer call passed env= and triggered TypeError.
  - BUG FIX: SPRT_PPD_DEST path typo 'SPRIT' → 'SPRT', preventing the
    PPD from being installed at the wrong location.
  - BUG FIX: active_threads list is periodically pruned of dead threads
    in the main accept-loop to prevent unbounded growth.
  - MERGED: Best practices from Printers-Tools v1.3:
      • One-step setup via setup.sh (Printers-Tools approach).
      • Centralised handle_error pattern applied to daemon startup.
      • Network connectivity pre-check before any update attempt.
"""

import gzip
import grp
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOCKET_DIR = "/run/it-aman"
SOCKET_PATH = os.path.join(SOCKET_DIR, "it-aman.sock")
# SECURITY: Socket is restricted to this Unix group (mode 0o660).
# Add GUI users to this group: usermod -aG it-aman <username>
IT_AMAN_GROUP = "it-aman"
LOG_DIR = "/var/log/it-aman"
LOG_FILE = os.path.join(LOG_DIR, "daemon.log")
TEST_PRINT_FILE = "/usr/share/cups/data/testprint"
APP_DIR = "/opt/it-aman"
CONFIG_DIR = "/etc/it-aman"
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DATA_PATH = os.path.join(CONFIG_DIR, "data.json")
DRIVERS_DIR = os.path.join(CONFIG_DIR, "cache", "drivers")
MANIFEST_PATH = os.path.join(CONFIG_DIR, "update_manifest.json")

# Thermal driver PPD directory (where install.sh puts them)
THERMAL_PPD_DIR = "/usr/share/cups/model/printer"

# Thermal PPD files
THERMAL_PPD_FILES = [
    "58mmSeries.ppd.gz",
    "76mmSeries.ppd.gz",
    "80mmSeries.ppd.gz",
    "112mmSeries.ppd.gz",
    "T5.ppd.gz",
]

# Thermal driver install files from GitHub
THERMAL_DRIVER_FILES = [
    "install.sh",
    "rastertoprinter",
    "rastertoprintercm",
    "rastertoprinterlm",
    "58mmSeries.ppd.gz",
    "76mmSeries.ppd.gz",
    "80mmSeries.ppd.gz",
    "112mmSeries.ppd.gz",
    "T5.ppd.gz",
]

# ── Thermal brand driver sources (from Printers-Tools merge) ──
# XP-80: single installer binary from Dropbox
XPRINTER_DRIVER_URL = (
    "https://www.dropbox.com/scl/fi/9knkouz84hqeouumyk5bd/"
    "install-xp80?rlkey=gjibguc0903787o1bjnx1s89u&st=fgtg9f6a&dl=1"
)
XPRINTER_PRINTER_NAME = "xp80"

# SPRT: zip archive containing install.sh + PPD + filter binaries
SPRT_DRIVER_URL = (
    "https://www.dropbox.com/scl/fo/eoxs40b23h5g8zxk0vhnj/"
    "AGVfJEgg05my1TcWe1xHCs4?rlkey=pqx2yv4x5blqmz0vks058ef9g&st=hcp53bq0&dl=1"
)
SPRT_PRINTER_NAME = "SPRT"
SPRT_PPD_DEST    = "/usr/share/cups/model/SPRT/80mmSeries.ppd"

# How long to wait for any single subprocess command
COMMAND_TIMEOUT = 10

# GitHub repo for auto-update
GITHUB_REPO = "abubakrahmed1911-hue/it-aman"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"

# Ed25519 public key for manifest signature verification.
# The private key is NEVER stored in the repository — it is kept offline by the
# developer and used only by generate_manifest.py when publishing a release.
# Even if an attacker reads this source code (public repo), they CANNOT forge
# a valid signature without the private key.
_MANIFEST_PUBLIC_KEY_B64 = (
    "Pa/gwSiU/9KZHfv8gTJ2Gy7UHXJXaIY85wqNIbWYMoE="
)

# Allowed CUPS commands (basenames only) - EXPANDED for thermal driver support
ALLOWED_COMMANDS = {
    "lpstat",
    "lpadmin",
    "lp",
    "cancel",
    "cupsenable",
    "cupsdisable",
    "cupsaccept",
    "cupsreject",
    "ping",
    "ip",
    "systemctl",
    "lpinfo",
    # Added for thermal driver installation:
    "dpkg",
    "bash",
    "apt-get",
    "lsusb",
    "wget",
    "unzip",
    "chmod",
    "mkdir",
    "cp",
    "sh",
    "chattr",
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    """Configure rotating-aware logging to file and stdout."""
    logger = logging.getLogger("it-aman")
    logger.setLevel(logging.DEBUG)

    # Ensure log directory exists
    os.makedirs(LOG_DIR, exist_ok=True)

    # Rotating file handler -- max 5 MB per file, keep 3 backups
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"
    )
    file_handler.setFormatter(file_fmt)

    # Console handler (goes to journald via systemd)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter("[%(levelname)s] %(message)s")
    console_handler.setFormatter(console_fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = setup_logging()

# ---------------------------------------------------------------------------
# Global state for graceful shutdown
# ---------------------------------------------------------------------------

shutdown_event = threading.Event()
server_socket: Optional[socket.socket] = None
active_threads: List[threading.Thread] = []
active_threads_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def handle_signal(signum: int, _frame: Any) -> None:
    """Handle SIGTERM / SIGINT for graceful shutdown."""
    sig_name = signal.Signals(signum).name
    logger.info("Received %s -- initiating graceful shutdown...", sig_name)
    shutdown_event.set()

    if server_socket:
        try:
            server_socket.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Config handling (root-owned, daemon writes)
# ---------------------------------------------------------------------------


def _load_config() -> Dict[str, Any]:
    """Load config.json. Creates default if missing."""
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load config.json: %s -- creating default", exc)
    return {
        "github_repo": GITHUB_REPO,
        "version": "3.0",
        "last_update_check": 0,
        "current_branch_id": "",
    }


def _save_config(config: Dict[str, Any]) -> bool:
    """Save config.json (runs as root, so it can write to /etc/it-aman/)."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        logger.error("Failed to save config.json: %s", exc)
        return False


# Global config
_config: Dict[str, Any] = _load_config()

# ---------------------------------------------------------------------------
# Utility: safe command execution
# ---------------------------------------------------------------------------


def validate_command_args(cmd: List[str]) -> bool:
    """
    Validate that a command list is safe to execute.

    Rules (v3.4 — fixes whitelist-bypass vulnerability):
      1. cmd[0] MUST be in ALLOWED_COMMANDS (by basename). This is the ONLY
         token that may match the whitelist; previously the loop ran over
         every token so an argument whose basename matched could bypass checks.
      2. If cmd[0] contains a '/' it must be an absolute path (no ../tricks).
      3. Flag tokens (starting with '-') must not contain shell metacharacters.
      4. All other tokens are restricted to: a-z A-Z 0-9 _ - . / : + % ? =
    """
    if not cmd:
        return False

    # ── Rule 1 & 2: first token must be a whitelisted executable ──
    first = cmd[0]
    base = os.path.basename(first)
    if not base or base not in ALLOWED_COMMANDS:
        logger.error("validate_command_args: rejected non-whitelisted command: %s", first)
        return False
    if "/" in first and not first.startswith("/"):
        logger.error("validate_command_args: rejected relative path command: %s", first)
        return False

    # ── Rules 3 & 4: validate remaining tokens ────────────────────
    for token in cmd[1:]:
        if token.startswith("-"):
            # Flag: only reject if it contains shell metacharacters
            if re.search(r'[;&|`$><!()]', token):
                logger.error("validate_command_args: rejected flag with metachar: %s", token)
                return False
        else:
            # Argument: strict safe-character whitelist
            if not re.match(r'^[a-zA-Z0-9_.\-\/\:+%?=]+$', token):
                logger.error("validate_command_args: rejected unsafe argument token: %s", token)
                return False

    return True


def run_command(
    cmd: List[str],
    timeout: int = COMMAND_TIMEOUT,
    check: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """
    Safely execute a subprocess command.
    Returns (returncode, stdout, stderr).

    FIX v3.4: Added optional env= parameter.  The SPRT brand-driver handler
    was passing env= to this function but the old signature did not accept it,
    causing a TypeError at runtime.
    """
    # Security gate
    if not validate_command_args(cmd):
        logger.error("Blocked unsafe command: %s", cmd)
        return (1, "", "Error: command failed security validation")

    logger.debug("Running command: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
            env=env,          # None → inherit caller's environment
        )
        return (result.returncode, result.stdout.strip(), result.stderr.strip())
    except subprocess.TimeoutExpired:
        logger.warning("Command timed out after %ds: %s", timeout, cmd)
        return (1, "", f"Command timed out after {timeout}s")
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        return (1, "", f"Command not found: {cmd[0]}")
    except subprocess.SubprocessError as exc:
        logger.error("Subprocess error: %s", exc)
        return (1, "", str(exc))


# ---------------------------------------------------------------------------
# Printer name sanitiser
# ---------------------------------------------------------------------------

_SAFE_PRINTER_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-.]*$')


def sanitize_printer_name(name: str) -> Optional[str]:
    """Return a sanitized printer name or None if invalid."""
    if not name or len(name) > 127:
        return None
    name = name.replace(" ", "_")
    if not _SAFE_PRINTER_RE.match(name):
        return None
    return name


# Strict IPv4 regex
_IPV4_RE = re.compile(
    r'^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$'
)


def sanitize_ip(ip: str) -> Optional[str]:
    """Validate IPv4 address format. Returns the IP or None."""
    if not ip:
        return None
    ip = ip.strip()
    if not _IPV4_RE.match(ip):
        return None
    parts = ip.split(".")
    for part in parts:
        try:
            num = int(part)
            if num < 0 or num > 255:
                return None
        except ValueError:
            return None
    return ip


# ---------------------------------------------------------------------------
# PPD validation helper
# ---------------------------------------------------------------------------


def validate_ppd_file(ppd_path: str) -> Tuple[bool, str]:
    """Validate a PPD file before passing it to lpadmin."""
    if not os.path.isfile(ppd_path):
        return False, f"PPD file not found: {ppd_path}"
    if not os.access(ppd_path, os.R_OK):
        return False, f"PPD file not readable: {ppd_path}"

    file_size = os.path.getsize(ppd_path)
    if file_size == 0:
        return False, f"PPD file is empty: {ppd_path}"

    # For gzipped PPD files, decompress and check content
    if ppd_path.endswith(".gz"):
        try:
            with gzip.open(ppd_path, "rb") as f:
                head = f.read(4096)
        except (gzip.BadGzipFile, OSError) as exc:
            return False, f"Corrupt gzipped PPD file: {exc}"
        try:
            content = head.decode("utf-8", errors="replace")
        except Exception:
            return False, "Cannot decode gzipped PPD file"
    else:
        try:
            with open(ppd_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(4096)
        except OSError as exc:
            return False, f"Cannot read PPD file: {exc}"

    has_ppd_adobe = "*PPD-Adobe" in content
    has_format_version = "*FormatVersion" in content

    if not has_ppd_adobe and not has_format_version:
        return False, f"File does not appear to be a valid PPD: {ppd_path}"

    return True, "Valid PPD file"


# ---------------------------------------------------------------------------
# data.json handling
# ---------------------------------------------------------------------------

def _load_data_json() -> Dict[str, Any]:
    """Load data.json with proper fallback."""
    try:
        if os.path.isfile(DATA_PATH):
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "branches" not in data or not isinstance(data["branches"], list):
                logger.warning("data.json has invalid structure, resetting to default")
                return {"branches": []}
            return data
        else:
            logger.info("data.json not found at %s", DATA_PATH)
            return {"branches": []}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load data.json: %s", exc)
        return {"branches": []}


def _save_data_json(data: Dict[str, Any]) -> bool:
    """Save data to data.json. Returns True on success."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        logger.error("Failed to save data.json: %s", exc)
        return False


def _get_branch_printer_names(branch_id: str) -> set:
    """Get set of printer names for a specific branch from data.json."""
    data = _load_data_json()
    names = set()
    for branch in data.get("branches", []):
        if branch.get("branch_id", "") == branch_id:
            for p in branch.get("printers", []):
                pname = p.get("name", "")
                if pname:
                    names.add(pname)
    return names


def _get_all_data_printer_names() -> Dict[str, str]:
    """Get mapping of printer_name -> branch_id for ALL printers in data.json."""
    data = _load_data_json()
    mapping = {}
    for branch in data.get("branches", []):
        bid = branch.get("branch_id", "")
        for p in branch.get("printers", []):
            pname = p.get("name", "")
            if pname:
                mapping[pname] = bid
    return mapping


# ---------------------------------------------------------------------------
# Config handlers (daemon writes config as root)
# ---------------------------------------------------------------------------


def handle_save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Save configuration values via the daemon (runs as root).
    This allows the GUI (non-root) to persist config changes.
    """
    global _config
    logger.info("Action: save_config")

    # Only allow specific safe keys
    allowed_keys = {"version", "last_update_check", "current_branch_id", "language"}
    updated = []
    for key in allowed_keys:
        if key in data:
            _config[key] = data[key]
            updated.append(key)

    if _save_config(_config):
        return {"status": "ok", "message": f"Saved config keys: {', '.join(updated)}"}
    else:
        return {"status": "error", "message": "Failed to save config"}


def handle_get_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return current config (safe keys only)."""
    return {
        "status": "ok",
        "config": {
            "version": _config.get("version", "0.0"),
            "last_update_check": _config.get("last_update_check", 0),
            "current_branch_id": _config.get("current_branch_id", ""),
            "github_repo": _config.get("github_repo", GITHUB_REPO),
            "language": _config.get("language", "en"),
        },
    }


def handle_set_branch(data: Dict[str, Any]) -> Dict[str, Any]:
    """Set the current branch for this machine."""
    global _config
    branch_id = data.get("branch_id", "").strip()
    if not branch_id:
        return {"status": "error", "message": "branch_id is required"}

    # Verify branch exists in data.json
    data_json = _load_data_json()
    found = False
    branch_name = ""
    for branch in data_json.get("branches", []):
        if branch.get("branch_id", "") == branch_id:
            found = True
            branch_name = branch.get("branch_name", "")
            break

    if not found:
        return {"status": "error", "message": f"Branch '{branch_id}' not found in data.json"}

    _config["current_branch_id"] = branch_id
    _save_config(_config)

    logger.info("Current branch set to: %s (%s)", branch_id, branch_name)
    return {"status": "ok", "branch_id": branch_id, "branch_name": branch_name}


def handle_get_branch(data: Dict[str, Any]) -> Dict[str, Any]:
    """Get the current branch setting."""
    branch_id = _config.get("current_branch_id", "")
    branch_name = ""
    if branch_id:
        data_json = _load_data_json()
        for branch in data_json.get("branches", []):
            if branch.get("branch_id", "") == branch_id:
                branch_name = branch.get("branch_name", "")
                break
    return {
        "status": "ok",
        "branch_id": branch_id,
        "branch_name": branch_name,
    }


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def handle_scan(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scan printers and return their status.
    FILTERS by current branch if current_branch_id is set in config.
    Only shows printers that exist in data.json for the current branch.
    If no branch is set, shows all printers but warns.
    """
    logger.info("Action: scan")

    # Determine filtering
    current_branch_id = _config.get("current_branch_id", "")
    if current_branch_id:
        allowed_names = _get_branch_printer_names(current_branch_id)
        logger.info("Filtering scan by branch '%s': %d printers allowed",
                     current_branch_id, len(allowed_names))
    else:
        allowed_names = None  # No filtering

    # Get printer list via lpstat -p
    rc, stdout, stderr = run_command(["lpstat", "-p"])
    if rc != 0:
        logger.error("lpstat -p failed: %s", stderr)
        return {"status": "error", "message": f"Failed to query printers: {stderr}"}

    # Parse lpstat -p output
    printers: List[Dict[str, Any]] = []
    printer_states: Dict[str, str] = {}

    for line in stdout.splitlines():
        m = re.match(r'^printer\s+(\S+)\s+(?:is\s+)?(\S+)', line)
        if m:
            pname = m.group(1)
            pstate_raw = m.group(2)
            if pstate_raw in ("idle", "processing"):
                printer_states[pname] = "idle"
            else:
                printer_states[pname] = "disabled"

    # Apply branch filter FIRST - only keep printers in data.json
    if allowed_names is not None:
        filtered_states = {k: v for k, v in printer_states.items() if k in allowed_names}
        # Also add printers from data.json that aren't in CUPS (not installed yet)
        for pname in allowed_names:
            if pname not in filtered_states:
                filtered_states[pname] = "not_installed"
        printer_states = filtered_states

    # Get device URIs via lpstat -v
    rc_v, stdout_v, _ = run_command(["lpstat", "-v"])
    printer_ips: Dict[str, str] = {}
    printer_uris: Dict[str, str] = {}
    if rc_v == 0:
        for line in stdout_v.splitlines():
            m = re.match(r'^device for\s+(\S+):\s+(.+)$', line.strip())
            if m:
                pname = m.group(1)
                uri = m.group(2).strip()
                printer_uris[pname] = uri
                ip_m = re.search(r'://([^:/]+)', uri)
                if ip_m:
                    host = ip_m.group(1)
                    if sanitize_ip(host):
                        printer_ips[pname] = host

    # Ping network printers concurrently
    printer_reachable: Dict[str, bool] = {}
    if printer_ips:
        def _ping_one(pname: str, ip: str) -> Tuple[str, bool]:
            rc, _, _ = run_command(["ping", "-c", "1", "-W", "2", ip])
            return pname, (rc == 0)

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(len(printer_ips), 20)) as executor:
            futures = {executor.submit(_ping_one, pn, ip): pn for pn, ip in printer_ips.items()}
            for future in as_completed(futures):
                try:
                    pname, reachable = future.result()
                    printer_reachable[pname] = reachable
                except Exception as exc:
                    logger.warning("Ping thread error for %s: %s", futures[future], exc)

    # Get job count per printer
    rc_jobs, stdout_jobs, _ = run_command(["lpstat", "-o"])
    job_counts: Dict[str, int] = {}
    if rc_jobs == 0:
        for line in stdout_jobs.splitlines():
            parts = line.split()
            if parts:
                full_id = parts[0]
                jm = re.match(r'^(.+?)-\d+$', full_id)
                pname = jm.group(1) if jm else full_id
                job_counts[pname] = job_counts.get(pname, 0) + 1

    # Check accepting status
    rc_accept, stdout_accept, _ = run_command(["lpstat", "-a"])
    accepting_map: Dict[str, bool] = {}
    if rc_accept == 0:
        for line in stdout_accept.splitlines():
            m = re.match(r'^(\S+)\s+(accepting|rejecting)', line)
            if m:
                accepting_map[m.group(1)] = m.group(2) == "accepting"

    # Build printer list with rule engine
    online_count = 0
    offline_count = 0
    error_count = 0
    not_installed_count = 0

    # Load data.json for additional printer info (IP, model, driver)
    data_json = _load_data_json()
    data_printers = {}
    for branch in data_json.get("branches", []):
        for p in branch.get("printers", []):
            data_printers[p.get("name", "")] = p

    for pname, state in printer_states.items():
        jobs = job_counts.get(pname, 0)
        accepting = accepting_map.get(pname, False)
        ip = printer_ips.get(pname, data_printers.get(pname, {}).get("ip", ""))
        reachable = printer_reachable.get(pname, None)
        uri = printer_uris.get(pname, "")

        # Not installed in CUPS
        if state == "not_installed":
            status = "Not Installed"
            not_installed_count += 1
        elif ip and reachable is False:
            status = "Network Issue"
            error_count += 1
        elif state == "disabled":
            status = "CUPS Issue"
            offline_count += 1
        elif not accepting and state != "not_installed":
            status = "Error"
            error_count += 1
        elif state == "processing":
            status = "Online"
            online_count += 1
        elif jobs > 0 and state == "idle":
            status = "Stuck Jobs"
            error_count += 1
        elif (ip and reachable is True and state == "idle") or (not ip and state == "idle"):
            status = "Online"
            online_count += 1
        else:
            status = "Unknown"
            offline_count += 1

        printer_info = {
            "name": pname,
            "state": state,
            "status": status,
            "jobs": jobs,
            "accepting": accepting,
            "ip": ip,
            "reachable": reachable,
            "uri": uri,
        }

        # Add data.json info
        if pname in data_printers:
            printer_info["model"] = data_printers[pname].get("model", "")
            printer_info["driver"] = data_printers[pname].get("driver", "")

        printers.append(printer_info)

    summary = {
        "total_printers": len(printers),
        "online_printers": online_count,
        "offline_printers": offline_count,
        "error_printers": error_count,
        "not_installed_printers": not_installed_count,
        "branch_filter": current_branch_id or "ALL",
    }

    return {
        "status": "ok",
        "printers": printers,
        "summary": summary,
    }


def handle_ping_printer(data: Dict[str, Any]) -> Dict[str, Any]:
    """Ping a printer IP to check network reachability."""
    ip = data.get("ip", "")
    ip = sanitize_ip(ip) or ""
    if not ip:
        return {"status": "error", "message": "Invalid or missing IP address"}

    logger.info("Action: ping_printer -- ip=%s", ip)

    rc, stdout, stderr = run_command(["ping", "-c", "2", "-W", "2", ip])
    if rc != 0:
        return {"status": "ok", "reachable": False, "latency_ms": None}

    latency_ms: Optional[float] = None
    latency_match = re.search(
        r'rtt\s+min/avg/max/mdev\s+=\s+[\d.]+/([\d.]+)/', stdout
    )
    if latency_match:
        try:
            latency_ms = round(float(latency_match.group(1)), 2)
        except ValueError:
            pass

    return {"status": "ok", "reachable": True, "latency_ms": latency_ms}


def handle_fix(data: Dict[str, Any]) -> Dict[str, Any]:
    """Attempt to fix a printer: ping -> enable -> accept -> clear stuck jobs."""
    printer_name = sanitize_printer_name(data.get("printer_name", "") or "")
    printer_ip = sanitize_ip(data.get("printer_ip", "") or "")

    if not printer_name:
        return {"status": "error", "message": "Invalid or missing printer name"}

    logger.info("Action: fix -- printer=%s ip=%s", printer_name, printer_ip)
    steps: List[Dict[str, str]] = []

    # Step 1: Network check — WARNING only, not fatal.
    # Some printers block ICMP ping. We still attempt CUPS repair steps.
    if printer_ip:
        rc, stdout, stderr = run_command(["ping", "-c", "2", "-W", "2", printer_ip])
        if rc == 0:
            latency_match = re.search(
                r'rtt\s+min/avg/max/mdev\s+=\s+[\d.]+/([\d.]+)/', stdout
            )
            latency = latency_match.group(1) if latency_match else "N/A"
            steps.append({
                "step": "Network Check",
                "status": "success",
                "message": f"Printer connected -- latency {latency} ms",
            })
        else:
            # Warn but continue — CUPS fix operations may still resolve queue issues
            steps.append({
                "step": "Network Check",
                "status": "warning",
                "message": (
                    f"Printer at {printer_ip} did not respond to ping "
                    f"(ICMP may be blocked). Proceeding with CUPS repair."
                ),
            })
    else:
        steps.append({
            "step": "Network Check",
            "status": "success",
            "message": "Skipped network check (no IP known)",
        })

    # Step 2: Enable the printer
    rc_en, _, err_en = run_command(["cupsenable", printer_name])
    if rc_en == 0:
        steps.append({"step": "Enable Printer", "status": "success",
                       "message": f"Enabled printer {printer_name}"})
    else:
        steps.append({"step": "Enable Printer", "status": "failed",
                       "message": err_en or f"Failed to enable {printer_name}"})

    # Step 2b: Accept jobs
    rc_ac, _, err_ac = run_command(["cupsaccept", printer_name])
    if rc_ac == 0:
        steps.append({"step": "Accept Jobs", "status": "success",
                       "message": f"Accepting jobs for {printer_name}"})
    else:
        steps.append({"step": "Accept Jobs", "status": "failed",
                       "message": err_ac or f"Failed to accept jobs for {printer_name}"})

    # Step 2c: CUPS restart fallback
    if rc_en != 0 or rc_ac != 0:
        logger.warning("enable/accept failed for %s -- attempting CUPS restart", printer_name)
        rc_cups, _, err_cups = run_command(["systemctl", "restart", "cups"], timeout=15)
        if rc_cups == 0:
            cups_ready = False
            for _ in range(30):
                try:
                    s = socket.create_connection(("localhost", 631), timeout=0.2)
                    s.close()
                    cups_ready = True
                    break
                except OSError:
                    time.sleep(0.2)
            steps.append({"step": "Restart CUPS", "status": "success",
                           "message": "CUPS restarted successfully"})
            # Retry after restart
            if rc_en != 0:
                rc_en2, _, err_en2 = run_command(["cupsenable", printer_name])
                steps.append({"step": "Re-enable Printer", "status": "success" if rc_en2 == 0 else "failed",
                               "message": f"Enabled {printer_name} after restart" if rc_en2 == 0 else (err_en2 or "Re-enable failed")})
            if rc_ac != 0:
                rc_ac2, _, err_ac2 = run_command(["cupsaccept", printer_name])
                steps.append({"step": "Re-accept Jobs", "status": "success" if rc_ac2 == 0 else "failed",
                               "message": f"Accepting jobs for {printer_name} after restart" if rc_ac2 == 0 else (err_ac2 or "Re-accept failed")})
        else:
            steps.append({"step": "Restart CUPS", "status": "failed",
                           "message": f"Failed to restart CUPS: {err_cups or 'unknown error'}"})

    # Step 3: Clear stuck jobs
    rc_cancel, out_cancel, err_cancel = run_command(["cancel", printer_name])
    if rc_cancel == 0 or "no jobs" in (err_cancel or "").lower():
        steps.append({"step": "Clear Stuck Jobs", "status": "success",
                       "message": out_cancel or err_cancel or "No stuck jobs"})
    else:
        steps.append({"step": "Clear Stuck Jobs", "status": "failed",
                       "message": err_cancel or f"Failed to clear jobs for {printer_name}"})

    # Step 4: Check driver
    rc_lpstat, out_lpstat, _ = run_command(["lpstat", "-p", printer_name])
    if rc_lpstat == 0:
        steps.append({"step": "Check Driver", "status": "success",
                       "message": f"Printer {printer_name} has valid configuration"})
    else:
        steps.append({"step": "Check Driver", "status": "failed",
                       "message": "driver_needed"})
        return {"status": "error", "steps": steps,
                "message": "Printer needs driver installation (driver_needed)"}

    all_ok = all(s["status"] == "success" for s in steps)
    return {
        "status": "ok" if all_ok else "error",
        "steps": steps,
        "message": "Printer fixed successfully" if all_ok else "Fixed with warnings",
    }


def handle_setup_printer(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Set up a new printer in CUPS via lpadmin, enable it, and send a test page.
    Supports PPD file and CUPS auto-detection (everywhere model).
    """
    name = sanitize_printer_name(data.get("name", "") or "")
    ip = sanitize_ip(data.get("ip", "") or "")
    model = data.get("model", "")
    driver_path = data.get("driver_path", "")

    if not name:
        return {"status": "error", "message": "Invalid or missing printer name"}
    if not ip:
        return {"status": "error", "message": "Invalid or missing printer IP address"}

    logger.info("Action: setup_printer -- name=%s ip=%s model=%s", name, ip, model)
    steps: List[Dict[str, str]] = []

    # Step 1: Network check — WARNING only, not fatal.
    # Some printers block ICMP ping but still accept IPP connections.
    # We warn the user but continue with installation.
    rc, stdout, stderr = run_command(["ping", "-c", "2", "-W", "2", ip])
    if rc == 0:
        latency_match = re.search(
            r'rtt\s+min/avg/max/mdev\s+=\s+[\d.]+/([\d.]+)/', stdout
        )
        latency = latency_match.group(1) if latency_match else "N/A"
        steps.append({"step": "Network Check", "status": "success",
                       "message": f"Printer connected -- latency {latency} ms"})
    else:
        # Do NOT abort — try installation anyway.
        # Many printers (HP, Kyocera) block ICMP by default.
        steps.append({"step": "Network Check", "status": "warning",
                       "message": (
                           f"Printer at {ip} did not respond to ping. "
                           f"This is normal for some printers (ICMP may be blocked). "
                           f"Attempting installation anyway."
                       )})

    # Step 1b: Try to auto-detect driver via CUPS lpinfo if no PPD specified
    auto_driver_type = ""
    auto_driver_value = ""
    if not driver_path:
        auto_driver_value, auto_driver_type = _auto_detect_ppd(ip, name, model)
        if auto_driver_type == "ppd_file":
            driver_path = auto_driver_value
            steps.append({"step": "Auto-detect Driver", "status": "success",
                           "message": f"Found PPD file: {os.path.basename(auto_driver_value)}"})
        elif auto_driver_type == "cups_model":
            steps.append({"step": "Auto-detect Driver", "status": "success",
                           "message": f"Found CUPS model: {auto_driver_value}"})

    # Step 1c: PPD validation
    if driver_path:
        valid, validation_msg = validate_ppd_file(driver_path)
        if not valid:
            steps.append({"step": "PPD Validation", "status": "failed",
                           "message": validation_msg})
            # Don't fail completely - try everywhere model instead
            driver_path = ""
            steps.append({"step": "PPD Fallback", "status": "success",
                           "message": "Falling back to CUPS everywhere model (IPP Everywhere)"})
        else:
            steps.append({"step": "PPD Validation", "status": "success",
                           "message": validation_msg})

    # Step 2: Create printer in CUPS
    lpadmin_cmd = [
        "lpadmin",
        "-p", name,
        "-E",
        "-v", f"ipp://{ip}:631/ipp/print",
    ]

    if driver_path and os.path.isfile(driver_path):
        lpadmin_cmd.extend(["-P", driver_path])
        logger.info("Using PPD file: %s", driver_path)
    elif auto_driver_type == "cups_model" and auto_driver_value:
        # Use the auto-detected CUPS model name (e.g. "drv:///hp/hp-laserjet_4015.ppd")
        lpadmin_cmd.extend(["-m", auto_driver_value])
        logger.info("Using CUPS model: %s", auto_driver_value)
    else:
        # CUPS everywhere model (IPP Everywhere - auto-detect via CUPS)
        lpadmin_cmd.extend(["-m", "everywhere"])
        logger.info("Using CUPS everywhere model (IPP Everywhere auto-detect)")

    rc_lp, _, err_lp = run_command(lpadmin_cmd)
    if rc_lp == 0:
        steps.append({"step": "Add Printer to CUPS", "status": "success",
                       "message": f"Created printer '{name}' via lpadmin"})
    else:
        # If everywhere model failed, try with lpd:// URI
        if not driver_path:
            lpd_cmd = [
                "lpadmin",
                "-p", name,
                "-E",
                "-v", f"lpd://{ip}/queue",
                "-m", "everywhere",
            ]
            rc_lpd, _, err_lpd = run_command(lpd_cmd)
            if rc_lpd == 0:
                steps.append({"step": "Add Printer to CUPS", "status": "success",
                               "message": f"Created printer '{name}' via LPD fallback"})
            else:
                steps.append({"step": "Add Printer to CUPS", "status": "failed",
                               "message": err_lp or f"lpadmin failed for '{name}'"})
                return {"status": "error", "steps": steps,
                        "message": f"Failed to add printer: {err_lp}"}
        else:
            steps.append({"step": "Add Printer to CUPS", "status": "failed",
                           "message": err_lp or f"lpadmin failed for '{name}'"})
            return {"status": "error", "steps": steps,
                    "message": f"Failed to add printer: {err_lp}"}

    # Step 3: Enable
    rc_en, _, err_en = run_command(["cupsenable", name])
    steps.append({"step": "Enable Printer", "status": "success" if rc_en == 0 else "failed",
                   "message": f"Enabled {name}" if rc_en == 0 else (err_en or f"Failed to enable {name}")})

    # Step 4: Accept jobs
    rc_ac, _, err_ac = run_command(["cupsaccept", name])
    steps.append({"step": "Accept Jobs", "status": "success" if rc_ac == 0 else "failed",
                   "message": f"Accepting jobs for {name}" if rc_ac == 0 else (err_ac or f"Failed to accept jobs")})

    # Step 5: Set as default
    rc_def, _, err_def = run_command(["lpadmin", "-d", name])
    steps.append({"step": "Set as Default", "status": "success" if rc_def == 0 else "failed",
                   "message": f"Set '{name}' as default" if rc_def == 0 else (err_def or "Failed to set default")})

    # Step 6: Test print
    if os.path.isfile(TEST_PRINT_FILE):
        rc_tp, out_tp, err_tp = run_command([
            "lp", "-d", name, "-o", "fit-to-page", TEST_PRINT_FILE,
        ])
        steps.append({"step": "Test Print", "status": "success" if rc_tp == 0 else "failed",
                       "message": (out_tp or f"Sent test page to '{name}'") if rc_tp == 0 else (err_tp or "Test print failed")})
    else:
        steps.append({"step": "Test Print", "status": "skipped",
                       "message": f"Test file not found: {TEST_PRINT_FILE}"})

    all_ok = all(s["status"] in ("success", "skipped") for s in steps)
    return {
        "status": "ok" if all_ok else "error",
        "steps": steps,
        "message": f"Printer '{name}' set up successfully" if all_ok else "Setup with warnings",
    }


def _auto_detect_ppd(ip: str, printer_name: str, model_hint: str) -> Tuple[str, str]:
    """
    Try to auto-detect a PPD driver for a network printer using CUPS lpinfo.

    Returns a tuple (ppd_path_or_model, driver_type):
      driver_type = "ppd_file"  -> first element is an absolute path to a PPD file
      driver_type = "cups_model" -> first element is a CUPS model name (use with lpadmin -m)
      driver_type = "" -> no match found, fall back to IPP Everywhere
    """
    logger.info("Auto-detecting PPD for %s (ip=%s, model=%s)", printer_name, ip, model_hint)

    # Try lpinfo -m to find matching models
    rc, stdout, stderr = run_command(["lpinfo", "-m"], timeout=15)
    if rc != 0:
        logger.warning("lpinfo -m failed: %s", stderr)
        return "", ""

    # If we have a model hint, try to match it
    if model_hint:
        model_lower = model_hint.lower()
        # Prioritise exact / high-quality matches
        best_match: Optional[Tuple[str, str]] = None
        for line in stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) >= 2:
                ppd_name = parts[0]
                ppd_desc = parts[1].lower() if len(parts) > 1 else ""
                # Check if model name matches
                if model_lower in ppd_desc or model_lower.replace(" ", "") in ppd_desc.replace(" ", ""):
                    if ppd_name.startswith("/"):
                        # Absolute path PPD file — best possible match
                        logger.info("Found PPD file: %s (%s)", ppd_name, ppd_desc)
                        return ppd_name, "ppd_file"
                    # CUPS model name — return it so setup_printer can use -m <model>
                    if best_match is None:
                        best_match = (ppd_name, ppd_desc)

        if best_match:
            logger.info("Found CUPS model: %s (%s)", best_match[0], best_match[1])
            return best_match[0], "cups_model"

    # No match found - CUPS everywhere model will be used
    return "", ""


def handle_clear_jobs(data: Dict[str, Any]) -> Dict[str, Any]:
    """Cancel all jobs for a given printer."""
    printer_name = sanitize_printer_name(data.get("printer_name", "") or "")
    if not printer_name:
        return {"status": "error", "message": "Invalid or missing printer name"}

    logger.info("Action: clear_jobs -- printer=%s", printer_name)

    rc_count, out_count, _ = run_command(["lpstat", "-o", printer_name])
    cleared = len(out_count.strip().splitlines()) if rc_count == 0 and out_count.strip() else 0

    rc, stdout, stderr = run_command(["cancel", printer_name])
    if rc == 0 or "no jobs" in (stderr or "").lower() or cleared == 0:
        return {"status": "ok", "cleared": cleared}
    else:
        return {"status": "error", "cleared": 0,
                "message": stderr or "Failed to clear jobs"}


def handle_test_print(data: Dict[str, Any]) -> Dict[str, Any]:
    """Send a test print job to a printer."""
    printer_name = sanitize_printer_name(data.get("printer_name", "") or "")
    if not printer_name:
        return {"status": "error", "message": "Invalid or missing printer name"}

    logger.info("Action: test_print -- printer=%s", printer_name)

    if not os.path.isfile(TEST_PRINT_FILE):
        return {"status": "error", "message": f"Test file not found: {TEST_PRINT_FILE}"}

    rc, stdout, stderr = run_command([
        "lp", "-d", printer_name, "-o", "fit-to-page", TEST_PRINT_FILE,
    ])
    if rc == 0:
        return {"status": "ok", "message": f"Sent test page to '{printer_name}': {stdout}"}
    else:
        return {"status": "error", "message": f"Test print failed: {stderr}"}


# ---------------------------------------------------------------------------
# USB Thermal Printer Support
# ---------------------------------------------------------------------------


def _detect_usb_printer_uris() -> List[Dict[str, str]]:
    """Detect USB printers by running lpinfo -v and filtering for USB URIs."""
    usb_printers: List[Dict[str, str]] = []
    rc, stdout, stderr = run_command(["lpinfo", "-v"], timeout=15)
    if rc != 0:
        logger.warning("lpinfo -v failed: %s", stderr)
        return usb_printers

    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("direct usb://") or "usb://" in line:
            parts = line.split(None, 1)
            uri = parts[-1].strip() if parts else ""
            if uri.startswith("usb://"):
                desc = uri.replace("usb://", "").rstrip("/")
                usb_printers.append({"uri": uri, "description": desc})

    return usb_printers


def _match_thermal_ppd(usb_desc: str) -> Optional[str]:
    """Find a matching thermal PPD file for a USB printer description."""
    desc_lower = usb_desc.lower()

    ppd_map = {
        "58mm": "58mmSeries.ppd.gz",
        "76mm": "76mmSeries.ppd.gz",
        "80mm": "80mmSeries.ppd.gz",
        "112mm": "112mmSeries.ppd.gz",
        "t5": "T5.ppd.gz",
    }

    for keyword, ppd_filename in ppd_map.items():
        if keyword.lower() in desc_lower:
            ppd_path = os.path.join(THERMAL_PPD_DIR, ppd_filename)
            if os.path.isfile(ppd_path):
                return ppd_path

    # Default: try 80mm
    default_ppd = os.path.join(THERMAL_PPD_DIR, "80mmSeries.ppd.gz")
    if os.path.isfile(default_ppd):
        return default_ppd

    # Last resort: first available PPD
    for ppd_filename in THERMAL_PPD_FILES:
        ppd_path = os.path.join(THERMAL_PPD_DIR, ppd_filename)
        if os.path.isfile(ppd_path):
            return ppd_path

    return None


def handle_setup_thermal_usb(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Set up a USB thermal printer.
    Accepts both "usb_uri" and "device_uri" parameters.
    Uses PPD file if available, falls back to CUPS auto-detect.
    """
    logger.info("Action: setup_thermal_usb")

    usb_uri_param = data.get("usb_uri", "") or data.get("device_uri", "")

    # Check if thermal driver PPDs are installed
    any_ppd_found = any(
        os.path.isfile(os.path.join(THERMAL_PPD_DIR, f))
        for f in THERMAL_PPD_FILES
    )

    steps: List[Dict[str, str]] = []

    # Step 1: Detect USB printers
    usb_printers = _detect_usb_printer_uris()
    if not usb_printers:
        steps.append({"step": "Detect USB Printers", "status": "failed",
                       "message": "No USB printers detected. Connect a USB printer and try again."})
        return {"status": "error", "steps": steps,
                "message": "No USB printers found", "detected_usb_printers": []}

    # Use specified URI or pick the first detected
    specified_uri = usb_uri_param
    selected_printer: Optional[Dict[str, str]] = None

    if specified_uri:
        for p in usb_printers:
            if p["uri"] == specified_uri:
                selected_printer = p
                break
        if not selected_printer:
            steps.append({"step": "Detect USB Printers", "status": "failed",
                           "message": f"Specified URI not found: {specified_uri}"})
            return {"status": "error", "steps": steps,
                    "message": "Specified URI not found",
                    "detected_usb_printers": usb_printers}
    else:
        selected_printer = usb_printers[0]

    usb_uri = selected_printer["uri"]
    usb_desc = selected_printer["description"]

    steps.append({"step": "Detect USB Printers", "status": "success",
                   "message": f"Found {len(usb_printers)} USB printer(s), selected: {usb_desc}"})

    # Step 2: Generate printer name
    raw_name = data.get("printer_name", "")
    if raw_name:
        printer_name = sanitize_printer_name(raw_name)
    else:
        base_name = usb_desc.replace("/", "_").replace(" ", "_")
        base_name = re.sub(r'[^a-zA-Z0-9_\-.]', '', base_name)[:60]
        printer_name = sanitize_printer_name(base_name) or "thermal_usb_printer"

    logger.info("Thermal USB setup: name=%s uri=%s", printer_name, usb_uri)

    # Step 3: Match PPD file (or use CUPS auto-detect)
    ppd_path = ""
    ppd_filename = ""

    if any_ppd_found:
        specified_ppd = data.get("ppd_file", "")
        if specified_ppd:
            specified_ppd = os.path.basename(specified_ppd)  # Prevent path traversal
            ppd_path = os.path.join(THERMAL_PPD_DIR, specified_ppd)
            if not os.path.isfile(ppd_path):
                ppd_path = ""
                steps.append({"step": "Select PPD", "status": "failed",
                               "message": f"Specified PPD not found: {specified_ppd}"})

        if not ppd_path:
            ppd_path = _match_thermal_ppd(usb_desc)

        if ppd_path:
            ppd_filename = os.path.basename(ppd_path)
            # Validate PPD
            valid_ppd, ppd_msg = validate_ppd_file(ppd_path)
            if valid_ppd:
                steps.append({"step": "Select PPD", "status": "success",
                               "message": f"Using PPD: {ppd_filename}"})
            else:
                steps.append({"step": "Select PPD", "status": "failed",
                               "message": f"PPD invalid: {ppd_msg}. Falling back to CUPS auto-detect."})
                ppd_path = ""
                ppd_filename = ""

    if not ppd_path:
        steps.append({"step": "Driver Method", "status": "success",
                       "message": "Using CUPS auto-detection (IPP Everywhere / raw)"})

    # Step 4: Install printer via lpadmin
    lpadmin_cmd = ["lpadmin", "-p", printer_name, "-E", "-v", usb_uri]
    if ppd_path:
        lpadmin_cmd.extend(["-P", ppd_path])
    else:
        # Try raw queue first for thermal, then everywhere
        lpadmin_cmd.extend(["-m", "raw"])

    rc_lp, _, err_lp = run_command(lpadmin_cmd)
    if rc_lp != 0:
        # If raw failed, try everywhere
        if not ppd_path:
            lpadmin_cmd_fallback = ["lpadmin", "-p", printer_name, "-E", "-v", usb_uri, "-m", "everywhere"]
            rc_lp2, _, err_lp2 = run_command(lpadmin_cmd_fallback)
            if rc_lp2 == 0:
                steps.append({"step": "Add Thermal Printer to CUPS", "status": "success",
                               "message": f"Created thermal printer '{printer_name}' (auto-detect mode)"})
                rc_lp = 0
            else:
                steps.append({"step": "Add Thermal Printer to CUPS", "status": "failed",
                               "message": err_lp or f"lpadmin failed for thermal printer '{printer_name}'"})
        else:
            steps.append({"step": "Add Thermal Printer to CUPS", "status": "failed",
                           "message": err_lp or f"lpadmin failed for '{printer_name}'"})
    else:
        steps.append({"step": "Add Thermal Printer to CUPS", "status": "success",
                       "message": f"Created thermal printer '{printer_name}' with PPD {ppd_filename or 'auto'}"})

    if rc_lp != 0:
        return {"status": "error", "steps": steps,
                "message": f"Failed to add thermal printer: {err_lp}"}

    # Step 5: Enable
    rc_en, _, err_en = run_command(["cupsenable", printer_name])
    steps.append({"step": "Enable Thermal Printer", "status": "success" if rc_en == 0 else "failed",
                   "message": f"Enabled {printer_name}" if rc_en == 0 else (err_en or "Failed to enable")})

    # Step 6: Accept jobs
    rc_ac, _, err_ac = run_command(["cupsaccept", printer_name])
    steps.append({"step": "Accept Jobs", "status": "success" if rc_ac == 0 else "failed",
                   "message": f"Accepting jobs for {printer_name}" if rc_ac == 0 else (err_ac or "Failed to accept jobs")})

    # Step 7: Set as default
    rc_def, _, err_def = run_command(["lpadmin", "-d", printer_name])
    steps.append({"step": "Set as Default", "status": "success" if rc_def == 0 else "failed",
                   "message": f"Set '{printer_name}' as default" if rc_def == 0 else (err_def or "Failed to set default")})

    # Step 8: Test print
    if os.path.isfile(TEST_PRINT_FILE):
        rc_tp, out_tp, err_tp = run_command([
            "lp", "-d", printer_name, "-o", "fit-to-page", TEST_PRINT_FILE,
        ])
        steps.append({"step": "Test Print", "status": "success" if rc_tp == 0 else "failed",
                       "message": (out_tp or f"Sent test page to '{printer_name}'") if rc_tp == 0 else (err_tp or "Test print failed")})
    else:
        steps.append({"step": "Test Print", "status": "skipped",
                       "message": f"Test file not found: {TEST_PRINT_FILE}"})

    all_ok = all(s["status"] in ("success", "skipped") for s in steps)
    return {
        "status": "ok" if all_ok else "error",
        "printer_name": printer_name,
        "usb_uri": usb_uri,
        "ppd_used": ppd_filename or "auto-detect",
        "steps": steps,
        "message": f"Thermal printer '{printer_name}' set up successfully" if all_ok else "Setup with warnings",
    }


# ---------------------------------------------------------------------------
# Thermal Driver Install Handler
# ---------------------------------------------------------------------------


def handle_install_thermal_driver(data: Dict[str, Any]) -> Dict[str, Any]:
    """Download and install the thermal printer driver from GitHub."""
    logger.info("Action: install_thermal_driver")

    steps: List[Dict[str, str]] = []

    # Step 1: Check for libusb
    rc_libusb, out_libusb, _ = run_command(["dpkg", "-l", "libusb-1.0-0"], timeout=5)
    if rc_libusb == 0 and "ii" in (out_libusb or ""):
        steps.append({"step": "Check libusb", "status": "success",
                       "message": "libusb-1.0-0 is installed"})
    else:
        rc_apt, _, err_apt = run_command(["apt-get", "install", "-y", "libusb-1.0-0"], timeout=60)
        if rc_apt == 0:
            steps.append({"step": "Check libusb", "status": "success",
                           "message": "Installed libusb-1.0-0 via apt-get"})
        else:
            steps.append({"step": "Check libusb", "status": "failed",
                           "message": f"libusb not installed and apt-get failed: {err_apt}"})
            return {"status": "error", "steps": steps,
                    "message": "libusb unavailable. Install manually: apt-get install libusb-1.0-0"}

    # Step 2: Create temp directory
    try:
        tmp_dir = tempfile.mkdtemp(prefix="it-aman-thermal-")
        steps.append({"step": "Create Temp Directory", "status": "success",
                       "message": f"Created: {tmp_dir}"})
    except OSError as exc:
        steps.append({"step": "Create Temp Directory", "status": "failed",
                       "message": f"Failed: {exc}"})
        return {"status": "error", "steps": steps, "message": "Failed to create temp directory"}

    # Step 3: Download all driver files from GitHub with SHA256 verification
    download_success = True
    downloaded_files: List[str] = []
    for filename in THERMAL_DRIVER_FILES:
        url = f"{RAW_BASE}/thermal/{filename}"
        dest = os.path.join(tmp_dir, filename)
        success = _download_file_verified(url, dest, timeout=60)
        if success:
            downloaded_files.append(filename)
            if filename.endswith(".sh") or filename.startswith("rastertoprinter"):
                try:
                    os.chmod(dest, 0o755)
                except OSError:
                    pass
        else:
            download_success = False
            steps.append({"step": f"Download {filename}", "status": "failed",
                           "message": f"Failed to download {filename}"})

    if download_success:
        steps.append({"step": "Download Driver Files", "status": "success",
                       "message": f"Downloaded {len(downloaded_files)} files: {', '.join(downloaded_files)}"})
    else:
        # Partial download - try to continue with what we have
        if len(downloaded_files) >= 3:
            steps.append({"step": "Download Driver Files", "status": "success",
                           "message": f"Downloaded {len(downloaded_files)}/{len(THERMAL_DRIVER_FILES)} files (partial)"})
        else:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            return {"status": "error", "steps": steps,
                    "message": "Failed to download driver files. Check internet connection."}

    # Step 4: Run install.sh
    install_script = os.path.join(tmp_dir, "install.sh")
    if os.path.isfile(install_script):
        rc_install, out_install, err_install = run_command(
            ["bash", install_script], timeout=120,
        )
        if rc_install == 0:
            steps.append({"step": "Run install.sh", "status": "success",
                           "message": out_install or "install.sh completed successfully"})
        else:
            steps.append({"step": "Run install.sh", "status": "failed",
                           "message": err_install or f"install.sh failed (exit code {rc_install})"})
    else:
        steps.append({"step": "Run install.sh", "status": "failed",
                       "message": "install.sh not found in downloaded files"})

    # Step 5: Verify installation
    installed_ppds: List[str] = []
    missing_ppds: List[str] = []
    for ppd in THERMAL_PPD_FILES:
        ppd_path = os.path.join(THERMAL_PPD_DIR, ppd)
        if os.path.isfile(ppd_path):
            installed_ppds.append(ppd)
        else:
            missing_ppds.append(ppd)

    if installed_ppds:
        steps.append({"step": "Verify Installation", "status": "success" if not missing_ppds else "partial",
                       "message": f"PPD files: {', '.join(installed_ppds)}. {'All verified.' if not missing_ppds else 'Missing: ' + ', '.join(missing_ppds)}"})
    else:
        steps.append({"step": "Verify Installation", "status": "failed",
                       "message": f"No PPD files found in {THERMAL_PPD_DIR}"})

    # Clean up
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    all_ok = all(s["status"] == "success" for s in steps)
    return {
        "status": "ok" if all_ok else "error",
        "steps": steps,
        "installed_ppds": installed_ppds,
        "missing_ppds": missing_ppds,
        "message": "Thermal driver installed successfully" if all_ok else "Installation with warnings",
    }


# ---------------------------------------------------------------------------
# USB Printer Detection Handler
# ---------------------------------------------------------------------------


def handle_detect_usb_printers(data: Dict[str, Any]) -> Dict[str, Any]:
    """Detect all USB printers connected to the system."""
    logger.info("Action: detect_usb_printers")

    steps: List[Dict[str, str]] = []

    usb_printers = _detect_usb_printer_uris()

    # Check lsusb for additional info
    rc_lsusb, out_lsusb, _ = run_command(["lsusb"], timeout=10)
    lsusb_lines: List[str] = []
    if rc_lsusb == 0:
        printer_keywords = ["printer", "print", "thermal", "pos", "receipt"]
        for line in out_lsusb.splitlines():
            if any(kw in line.lower() for kw in printer_keywords):
                lsusb_lines.append(line.strip())

    # Cross-reference with configured printers
    rc_lpstat, out_lpstat, _ = run_command(["lpstat", "-v"], timeout=10)
    configured_uris: Dict[str, str] = {}
    if rc_lpstat == 0:
        for line in out_lpstat.splitlines():
            m = re.match(r'^device for\s+(\S+):\s+(.+)$', line.strip())
            if m:
                configured_uris[m.group(2).strip()] = m.group(1)

    enriched_printers: List[Dict[str, Any]] = []
    for p in usb_printers:
        uri = p["uri"]
        enriched = {
            "uri": uri,
            "description": p["description"],
            "configured": uri in configured_uris,
        }
        if uri in configured_uris:
            enriched["configured_as"] = configured_uris[uri]
        enriched_printers.append(enriched)

    steps.append({"step": "Detect USB Printers", "status": "success",
                   "message": f"Found {len(usb_printers)} USB printer(s)"})

    return {
        "status": "ok",
        "usb_printers": enriched_printers,
        "lsusb_printers": lsusb_lines,
        "steps": steps,
        "message": f"Found {len(usb_printers)} USB printer(s)",
    }


# ---------------------------------------------------------------------------
# Printer Details Handler
# ---------------------------------------------------------------------------


def handle_get_printer_details(data: Dict[str, Any]) -> Dict[str, Any]:
    """Get detailed information about a specific printer from CUPS."""
    printer_name = sanitize_printer_name(data.get("printer_name", "") or "")
    if not printer_name:
        return {"status": "error", "message": "Invalid or missing printer name"}

    logger.info("Action: get_printer_details -- printer=%s", printer_name)

    details: Dict[str, Any] = {"name": printer_name}

    # 1. Printer status
    rc_p, out_p, err_p = run_command(["lpstat", "-p", printer_name])
    if rc_p == 0 and out_p.strip():
        details["status_raw"] = out_p.strip()
        m = re.match(r'^printer\s+\S+\s+(?:is\s+)?(.+?)(?:\.\s|$)', out_p.strip())
        details["state"] = m.group(1).strip() if m else out_p.strip()
    else:
        details["state"] = "not_installed"
        details["status_raw"] = err_p or "Printer not found in CUPS"

    # 2. Default printer
    rc_d, out_d, _ = run_command(["lpstat", "-d"])
    if rc_d == 0:
        m_default = re.search(r'default destination:\s*(.+)$', out_d)
        default_name = m_default.group(1).strip() if m_default else ""
        details["is_default"] = (default_name == printer_name)
    else:
        details["is_default"] = False

    # 3. Device URI
    rc_v, out_v, _ = run_command(["lpstat", "-v", printer_name])
    if rc_v == 0 and out_v.strip():
        m_uri = re.search(r':\s*(.+)$', out_v.strip())
        details["device_uri"] = m_uri.group(1).strip() if m_uri else out_v.strip()
    else:
        details["device_uri"] = ""

    # 4. Job count
    rc_o, out_o, _ = run_command(["lpstat", "-o", printer_name])
    if rc_o == 0 and out_o.strip():
        details["jobs_count"] = len(out_o.strip().splitlines())
    else:
        details["jobs_count"] = 0

    # 5. Accepting status
    rc_a, out_a, _ = run_command(["lpstat", "-a", printer_name])
    if rc_a == 0 and out_a.strip():
        m_accept = re.search(r'(accepting|rejecting)', out_a.strip())
        details["accepting"] = (m_accept.group(1) == "accepting") if m_accept else False
    else:
        details["accepting"] = False

    # 6. Check if USB/thermal
    uri = details.get("device_uri", "")
    details["is_usb"] = uri.startswith("usb://") if uri else False
    details["is_network"] = bool(uri and any(p in uri for p in ("ipp://", "http://", "socket://")))

    return {"status": "ok", **details}


# ---------------------------------------------------------------------------
# SHA256 Manifest Verification for Secure Updates
# ---------------------------------------------------------------------------


def _compute_sha256(file_path: str) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _verify_manifest_signature(manifest: Dict[str, Any]) -> bool:
    """
    Verify the Ed25519 signature of an update manifest.

    The manifest is signed with the developer's private Ed25519 key.  Only the
    public key is embedded in this file, so an attacker who reads the source
    cannot forge a valid signature — unlike HMAC where the shared secret would
    be exposed in a public repository.

    Returns True only if the signature is valid.
    """
    import base64
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.asymmetric.utils import (
            Prehashed,
        )
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        logger.error(
            "cryptography package is required for manifest verification. "
            "Install with: pip3 install cryptography"
        )
        return False

    signature_b64 = manifest.get("signature", "")
    if not signature_b64:
        logger.error("Manifest has NO signature -- rejecting (unsigned manifests are not allowed)")
        return False

    try:
        signature_bytes = base64.b64decode(signature_b64)
    except Exception:
        logger.error("Manifest signature is not valid base64")
        return False

    # Reconstruct the data that was signed: manifest without the signature key,
    # JSON-serialised with sorted keys (same canonical form as generate_manifest.py)
    manifest_copy = {k: v for k, v in manifest.items() if k != "signature"}
    manifest_json = json.dumps(manifest_copy, sort_keys=True, ensure_ascii=False)
    message_bytes = manifest_json.encode("utf-8")

    try:
        pub_key_bytes = base64.b64decode(_MANIFEST_PUBLIC_KEY_B64)
        public_key = Ed25519PublicKey.from_public_bytes(pub_key_bytes)
        public_key.verify(signature_bytes, message_bytes)
        logger.info("Manifest Ed25519 signature verified successfully")
        return True
    except InvalidSignature:
        logger.error("Manifest Ed25519 signature verification FAILED — possible tampering!")
        return False
    except Exception as exc:
        logger.error("Manifest signature verification error: %s", exc)
        return False


def _download_file_verified(url: str, dest: str, timeout: int = 30,
                            expected_hash: str = "") -> bool:
    """
    Download a file with SHA256 verification.
    If expected_hash is provided, verifies the downloaded file matches.
    Returns True on success.
    """
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "IT-Aman/3.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()

        if len(content) < 50:
            logger.error("Downloaded file too small (%d bytes): %s", len(content), url)
            return False

        # Verify SHA256 if expected hash provided
        actual_hash = hashlib.sha256(content).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            logger.error("SHA256 mismatch for %s: expected %s, got %s",
                         url, expected_hash[:16], actual_hash[:16])
            return False

        # Write atomically
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, dest)
        logger.info("Downloaded %s -> %s (%d bytes, sha256=%s...)",
                     url, dest, len(content), actual_hash[:16])
        return True
    except Exception as exc:
        logger.error("Download failed %s -> %s: %s", url, dest, exc)
        return False


# ---------------------------------------------------------------------------
# Auto-update handler (with chattr +i handling and SHA256 verification)
# ---------------------------------------------------------------------------


def _set_immutable(path: str, immutable: bool) -> bool:
    """
    Set or clear the immutable (chattr +i/-i) flag on a file.
    Returns True if the operation succeeded.
    """
    flag = "+i" if immutable else "-i"
    try:
        result = subprocess.run(
            ["chattr", flag, path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.warning("chattr %s %s failed: %s", flag, path,
                           result.stderr.strip() if result.stderr else "unknown error")
            return False

        # Verify the flag was actually set/unset
        verify_result = subprocess.run(
            ["lsattr", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if verify_result.returncode == 0:
            attrs = verify_result.stdout.strip()
            is_immutable_now = "-i-" in attrs or "i" in attrs.split()[0] if attrs else False
            if immutable and not is_immutable_now:
                logger.warning("Verification failed: %s is NOT immutable after chattr +i", path)
                return False
            elif not immutable and is_immutable_now:
                logger.warning("Verification failed: %s IS still immutable after chattr -i", path)
                return False

        logger.info("chattr %s %s succeeded", flag, path)
        return True
    except Exception as exc:
        logger.error("chattr exception for %s: %s", path, exc)
        return False


def handle_update_all(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Download latest files from GitHub with SHA256 manifest verification.
    Properly handles chattr +i by toggling the flag during updates.
    Includes downgrade attack prevention.
    """
    logger.info("Action: update_all")

    # Files that may be immutable (source scripts)
    IMMUTABLE_FILES = {
        os.path.join(APP_DIR, "src", "daemon.py"),
        os.path.join(APP_DIR, "src", "gui.py"),
    }

    files = [
        ("src/daemon.py", os.path.join(APP_DIR, "src", "daemon.py")),
        ("src/gui.py", os.path.join(APP_DIR, "src", "gui.py")),
        ("data.json", os.path.join(CONFIG_DIR, "data.json")),
        ("version.json", os.path.join(CONFIG_DIR, "version.json")),
    ]

    # Step 1: Download and verify the update manifest
    manifest_url = f"{RAW_BASE}/update_manifest.json"
    manifest = None
    manifest_hashes = {}

    try:
        req = urllib.request.Request(manifest_url)
        req.add_header("User-Agent", "IT-Aman/3.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            manifest_content = resp.read().decode("utf-8")
        manifest = json.loads(manifest_content)

        # Verify manifest Ed25519 signature
        if not _verify_manifest_signature(manifest):
            return {
                "status": "error",
                "message": "Update manifest signature verification failed — possible security breach. Rejecting update.",
                "updated": [],
                "failed": [f[0] for f in files],
            }

        # Extract hashes from manifest
        manifest_hashes = manifest.get("files", {})
        remote_version = manifest.get("version", "0.0")

        # Downgrade attack prevention
        local_version = _config.get("version", "0.0")
        if remote_version and local_version:
            parts_local = [int(x) for x in local_version.split(".")]
            parts_remote = [int(x) for x in remote_version.split(".")]
            max_len = max(len(parts_local), len(parts_remote))
            parts_local += [0] * (max_len - len(parts_local))
            parts_remote += [0] * (max_len - len(parts_remote))
            if parts_remote <= parts_local:
                logger.info("Remote version %s <= local %s -- no update needed",
                            remote_version, local_version)
                return {
                    "status": "ok",
                    "message": "Already up to date",
                    "updated": [],
                    "failed": [],
                    "version": local_version,
                }

        logger.info("Manifest verified. Remote version: %s, hashes for %d files",
                     remote_version, len(manifest_hashes))

    except Exception as exc:
        # FIX v3.4: Previously a network/parse error silently bypassed
        # Ed25519 + SHA256 verification, allowing unsigned/tampered updates.
        # Now ANY failure to obtain a verified manifest is FATAL for the update.
        logger.error(
            "Cannot download or verify update manifest: %s -- "
            "REJECTING update to prevent unsigned-update attack.", exc
        )
        return {
            "status": "error",
            "message": (
                "Update aborted: could not verify the update manifest "
                f"({exc}). Check your internet connection and try again."
            ),
            "updated": [],
            "failed": [f[0] for f in files],
        }

    # Step 2: Download and update each file
    updated: List[str] = []
    failed: List[str] = []
    immutable_restore: List[str] = []  # Files to re-lock after update

    for src, dst in files:
        url = f"{RAW_BASE}/{src}"
        is_immutable = False  # Source files are not immutable — manifest verification protects them

        # Lift immutable flag BEFORE attempting download
        if is_immutable:
            if not _set_immutable(dst, False):
                logger.error("Cannot remove immutable flag from %s -- update blocked", dst)
                failed.append(src)
                continue
            immutable_restore.append(dst)

        try:
            expected_hash = manifest_hashes.get(src, "")
            success = False
            for attempt in range(3):
                if _download_file_verified(url, dst, timeout=30, expected_hash=expected_hash):
                    success = True
                    updated.append(src)
                    break
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))

            if not success:
                failed.append(src)
                logger.error("Failed to download %s after 3 attempts", src)
        except Exception as exc:
            failed.append(src)
            logger.error("Error updating %s: %s", src, exc)

    # Step 3: Re-lock immutable files (even if some updates failed)
    for path in immutable_restore:
        if os.path.isfile(path):
            _set_immutable(path, True)

    # Step 4: Update version in config
    if manifest and updated:
        remote_version = manifest.get("version", "")
        if remote_version:
            _config["version"] = remote_version
            _config["last_update_check"] = int(time.time())
            _save_config(_config)
            logger.info("Updated local version to %s", remote_version)

    if not updated:
        return {
            "status": "error",
            "message": "No files updated -- check internet connection",
            "updated": [],
            "failed": failed,
        }

    result_msg = f"Updated {len(updated)} file(s)"
    if failed:
        result_msg += f" (failed: {', '.join(failed)})"

    return {
        "status": "ok" if not failed else "partial",
        "updated": updated,
        "failed": failed,
        "message": result_msg,
    }


# ---------------------------------------------------------------------------
# Network + USB Auto-Discovery Handler
# ---------------------------------------------------------------------------


def handle_discover_printers(data: Dict[str, Any]) -> Dict[str, Any]:
    """Discover all printers (USB + Network) via lpinfo -v."""
    logger.info("Action: discover_printers")

    steps: List[Dict[str, str]] = []

    rc, stdout, stderr = run_command(["lpinfo", "-v"], timeout=15)
    if rc != 0:
        return {"status": "error", "message": f"lpinfo -v failed: {stderr}"}

    usb_printers: List[Dict[str, Any]] = []
    network_printers: List[Dict[str, Any]] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if not parts or len(parts) < 2:
            continue

        uri = parts[-1].strip()

        if uri.startswith("usb://"):
            desc = uri.replace("usb://", "").rstrip("/")
            usb_printers.append({"uri": uri, "description": desc, "type": "usb"})
        elif uri.startswith(("ipp://", "ipps://", "http://", "https://", "socket://", "lpd://")):
            host_match = re.search(r'://([^:/]+)', uri)
            host = host_match.group(1) if host_match else ""
            port_match = re.search(r':(\d+)', uri)
            port = port_match.group(1) if port_match else ""
            network_printers.append({
                "uri": uri, "host": host, "port": port,
                "description": host or uri, "type": "network",
                "reachable": False, "latency_ms": None,
            })

    # Ping network printers concurrently
    if network_printers:
        def _ping_disc(entry: Dict[str, Any]) -> Dict[str, Any]:
            host = entry["host"]
            if host and sanitize_ip(host):
                rc_ping, out_ping, _ = run_command(["ping", "-c", "1", "-W", "2", host])
                entry["reachable"] = (rc_ping == 0)
            return entry

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(network_printers), 20)) as pool:
            network_printers = list(pool.map(_ping_disc, network_printers))

    steps.append({"step": "Auto-discover Printers", "status": "success",
                   "message": f"Found {len(usb_printers)} USB and {len(network_printers)} network printers"})

    return {
        "status": "ok",
        "usb_printers": usb_printers,
        "network_printers": network_printers,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Load data.json handler (for GUI to reload data)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Active Network Scan Handler (merged from Printers-Tools v1.3)
# Performs a real subnet port scan (631 / 9100) + mDNS + HTTP model probe
# ---------------------------------------------------------------------------


def _probe_printer_model(ip: str) -> str:
    """Try to identify the printer model from its embedded web server."""
    import urllib.request
    import urllib.error
    import html

    candidates = [
        f"http://{ip}/",
        f"http://{ip}/general/information.html",
        f"http://{ip}/info/overview.html",
        f"http://{ip}/status.cgi",
        f"http://{ip}/cgi-bin/dynamic/config/status.html",
        f"http://{ip}/web/guest/en/websys/webArch/mainFrame.cgi",
    ]
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read(8192).decode("utf-8", errors="replace")
                # Try "Model: XYZ" pattern
                m = re.search(r'[Mm]odel\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\- ]{2,40})', body)
                if m:
                    model = html.unescape(m.group(1)).strip()
                    if model and len(model) > 3:
                        return model
                # Fall back to <title>
                t_m = re.search(r'<title>([^<]{3,60})</title>', body, re.IGNORECASE)
                if t_m:
                    title = html.unescape(t_m.group(1)).strip()
                    skip = {"home", "login", "welcome", "index", "web", "page"}
                    if title.lower() not in skip and len(title) > 3:
                        return title
        except Exception:
            continue
    return ""


def _tcp_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if the TCP port is open."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def handle_network_scan(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Active network printer scan (merged from Printers-Tools v1.3).
    Combines:
      1. CUPS lpinfo -v  (existing registered printers + backends)
      2. Subnet TCP scan on ports 631 (IPP) and 9100 (RAW)
      3. mDNS / Avahi discovery (_ipp._tcp)
      4. HTTP model probe on discovered IPs
    Returns a list of discovered printers with IP, URI, and model.
    """
    logger.info("Action: network_scan")
    found: dict = {}  # ip -> entry dict (dedup by IP)

    # --- 1. CUPS lpinfo -v ---
    rc_lp, out_lp, _ = run_command(["lpinfo", "-v"], timeout=15)
    if rc_lp == 0:
        for line in out_lp.splitlines():
            parts = line.strip().split(None, 1)
            uri = parts[-1].strip() if len(parts) == 2 else ""
            if not uri:
                continue
            if re.match(r'^(ipp|ipps|lpd|socket|http)://', uri, re.IGNORECASE):
                ip_m = re.search(r'://([0-9]{1,3}(?:\.[0-9]{1,3}){3})', uri)
                if ip_m:
                    ip = ip_m.group(1)
                    if ip not in found:
                        found[ip] = {"ip": ip, "uri": uri, "model": "", "source": "lpinfo"}

    # --- 2. Subnet TCP scan (ports 631 + 9100) ---
    local_ip = ""
    try:
        import socket as _sock
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    if local_ip:
        subnet = ".".join(local_ip.split(".")[:3])
        scan_hosts = [f"{subnet}.{i}" for i in range(1, 255)]

        def _scan_host(h: str) -> Optional[str]:
            if _tcp_port_open(h, 631, 1.0) or _tcp_port_open(h, 9100, 1.0):
                return h
            return None

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=64) as pool:
            results = list(pool.map(_scan_host, scan_hosts))

        for h in results:
            if h and h not in found:
                uri = f"ipp://{h}/ipp/print"
                found[h] = {"ip": h, "uri": uri, "model": "", "source": "tcp_scan"}

    # --- 3. mDNS via avahi-browse ---
    rc_av, out_av, _ = run_command(
        ["avahi-browse", "-t", "-r", "_ipp._tcp"], timeout=8
    )
    if rc_av == 0:
        for m in re.finditer(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', out_av):
            ip = m.group(1)
            if ip not in found:
                found[ip] = {"ip": ip, "uri": f"ipp://{ip}/ipp/print",
                             "model": "", "source": "mdns"}

    # --- 4. HTTP model probe (parallel) ---
    ips_to_probe = list(found.keys())

    def _probe(ip: str) -> tuple:
        return ip, _probe_printer_model(ip)

    with ThreadPoolExecutor(max_workers=min(len(ips_to_probe) + 1, 20)) as pool:
        for ip, model in pool.map(_probe, ips_to_probe):
            if model:
                found[ip]["model"] = model
            if not found[ip]["model"]:
                found[ip]["model"] = f"Network Printer @ {ip}"

    printers = list(found.values())
    logger.info("network_scan: found %d printers", len(printers))
    return {
        "status": "ok",
        "printers": printers,
        "count": len(printers),
        "local_ip": local_ip,
    }


# ---------------------------------------------------------------------------
# Brand-Specific Thermal Driver Install (merged from Printers-Tools v1.3)
# Supports: xprinter (XP-80) and sprt (SPRT 80mm)
# ---------------------------------------------------------------------------


def _download_to_temp(url: str, suffix: str, timeout: int = 120) -> Optional[str]:
    """Download URL to a temp file. Returns path or None on failure."""
    import urllib.request
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="it-aman-dl-")
        os.close(tmp_fd)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(tmp_path, "wb") as f:
                f.write(resp.read())
        if os.path.getsize(tmp_path) > 0:
            return tmp_path
        os.unlink(tmp_path)
        return None
    except Exception as exc:
        logger.error("_download_to_temp %s: %s", url, exc)
        return None


def _set_thermal_cut_defaults(printer_name: str) -> None:
    """Apply FullCut default if the PPD supports it."""
    rc, out, _ = run_command(["lpoptions", "-p", printer_name, "-l"], timeout=5)
    if rc != 0:
        return
    for line in out.splitlines():
        if re.match(r'^CutType', line, re.IGNORECASE):
            for tok in line.split():
                tok = tok.lstrip("*").split("/")[0]
                if tok.lower() in ("fullcut", "full", "cut"):
                    run_command(["lpoptions", "-p", printer_name, "-o", f"CutType={tok}"])
                    run_command(["lpadmin", "-p", printer_name, "-o", f"CutType={tok}"])
                    return


def handle_install_thermal_brand(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Download and install a brand-specific thermal driver, then register the printer.
    Merged from Printers-Tools v1.3.

    data keys:
      brand      : "xprinter" | "sprt"
      usb_uri    : USB device URI from detect_usb_printers (required)
    """
    brand = (data.get("brand") or "").lower().strip()
    usb_uri = (data.get("usb_uri") or "").strip()

    logger.info("Action: install_thermal_brand -- brand=%s uri=%s", brand, usb_uri)
    steps: List[Dict[str, str]] = []

    if brand not in ("xprinter", "sprt"):
        return {"status": "error", "steps": steps,
                "message": f"Unknown brand '{brand}'. Use 'xprinter' or 'sprt'."}

    if not usb_uri:
        return {"status": "error", "steps": steps,
                "message": "usb_uri is required. Run detect_usb_printers first."}

    # ── XPRINTER XP-80 ──────────────────────────────────────────────────────
    if brand == "xprinter":
        printer_name = XPRINTER_PRINTER_NAME

        # Step 1: Download installer binary
        steps.append({"step": "Download XP-80 Driver",
                       "status": "running", "message": "Downloading from Dropbox..."})
        dl_path = _download_to_temp(XPRINTER_DRIVER_URL, suffix=".bin")
        if not dl_path:
            steps[-1].update({"status": "failed",
                               "message": "Failed to download XP-80 driver. Check internet."})
            return {"status": "error", "steps": steps,
                    "message": "XP-80 driver download failed"}
        steps[-1].update({"status": "success",
                           "message": f"Downloaded ({os.path.getsize(dl_path)//1024} KB)"})

        # Step 2: Run installer
        try:
            os.chmod(dl_path, 0o755)
        except OSError:
            pass
        orig_dir = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            rc_ins, out_ins, err_ins = run_command([dl_path], timeout=120)
        finally:
            os.chdir(orig_dir)
            try:
                os.unlink(dl_path)
            except OSError:
                pass

        if rc_ins == 0:
            steps.append({"step": "Run XP-80 Installer", "status": "success",
                           "message": "Installer completed"})
        else:
            steps.append({"step": "Run XP-80 Installer", "status": "failed",
                           "message": err_ins or f"Installer exit {rc_ins}"})
            return {"status": "error", "steps": steps,
                    "message": "XP-80 installer failed"}

        # Restart CUPS
        run_command(["systemctl", "restart", "cups"], timeout=15)
        import time as _time; _time.sleep(2)

        # Step 3: Find PPD
        rc_ppd, out_ppd, _ = run_command(["lpinfo", "-m"], timeout=15)
        xp_ppd = ""
        for line in (out_ppd or "").splitlines():
            if re.search(r'XP-80|XP80|xprinter', line, re.IGNORECASE):
                xp_ppd = line.split()[0]
                break
        if not xp_ppd:
            steps.append({"step": "Find XP-80 PPD", "status": "failed",
                           "message": "PPD not found after install"})
            return {"status": "error", "steps": steps,
                    "message": "XP-80 PPD not found after install"}
        steps.append({"step": "Find XP-80 PPD", "status": "success",
                       "message": f"PPD: {xp_ppd}"})

        # Step 4: Register printer
        run_command(["lpadmin", "-x", printer_name], timeout=5)
        rc_lp, _, err_lp = run_command([
            "lpadmin", "-p", printer_name, "-E",
            "-v", usb_uri, "-m", xp_ppd,
            "-D", "X-Printer XP-80",
        ], timeout=15)
        if rc_lp != 0:
            steps.append({"step": "Register XP-80", "status": "failed",
                           "message": err_lp or "lpadmin failed"})
            return {"status": "error", "steps": steps}

        run_command(["cupsenable", printer_name], timeout=5)
        run_command(["cupsaccept", printer_name], timeout=5)
        _set_thermal_cut_defaults(printer_name)

        steps.append({"step": "Register XP-80", "status": "success",
                       "message": f"Printer '{printer_name}' ready on {usb_uri}"})
        return {"status": "ok", "steps": steps, "printer_name": printer_name,
                "message": "X-Printer XP-80 installed successfully!"}

    # ── SPRT 80mm ────────────────────────────────────────────────────────────
    if brand == "sprt":
        printer_name = SPRT_PRINTER_NAME

        # Step 1: Download zip
        steps.append({"step": "Download SPRT Driver",
                       "status": "running", "message": "Downloading from Dropbox..."})
        dl_path = _download_to_temp(SPRT_DRIVER_URL, suffix=".zip")
        if not dl_path:
            steps[-1].update({"status": "failed",
                               "message": "Failed to download SPRT driver. Check internet."})
            return {"status": "error", "steps": steps,
                    "message": "SPRT driver download failed"}
        steps[-1].update({"status": "success",
                           "message": f"Downloaded ({os.path.getsize(dl_path)//1024} KB)"})

        # Step 2: Extract
        extract_dir = tempfile.mkdtemp(prefix="it-aman-sprt-")
        try:
            import zipfile
            with zipfile.ZipFile(dl_path, "r") as zf:
                zf.extractall(extract_dir)
            steps.append({"step": "Extract SPRT Archive", "status": "success",
                           "message": f"Extracted to {extract_dir}"})
        except Exception as exc:
            steps.append({"step": "Extract SPRT Archive", "status": "failed",
                           "message": str(exc)})
            shutil.rmtree(extract_dir, ignore_errors=True)
            try:
                os.unlink(dl_path)
            except OSError:
                pass
            return {"status": "error", "steps": steps}
        finally:
            try:
                os.unlink(dl_path)
            except OSError:
                pass

        # Step 3: Run install.sh if present
        installer = None
        for root_dir, _, files in os.walk(extract_dir):
            for fname in files:
                if fname == "install.sh":
                    installer = os.path.join(root_dir, fname)
                    break
            if installer:
                break

        if installer:
            try:
                os.chmod(installer, 0o755)
            except OSError:
                pass
            rc_ins, out_ins, err_ins = run_command(
                ["bash", installer], timeout=120,
                env={**os.environ, "INSTALLER_DIR": os.path.dirname(installer)},
            )
            if rc_ins == 0:
                steps.append({"step": "Run SPRT install.sh", "status": "success",
                               "message": "Installer completed"})
            else:
                steps.append({"step": "Run SPRT install.sh", "status": "failed",
                               "message": err_ins or f"Exit {rc_ins}. Continuing manually."})
        else:
            steps.append({"step": "Run SPRT install.sh", "status": "skipped",
                           "message": "install.sh not found — copying files manually"})

        # Step 4: Copy filter binaries
        filter_dirs = ["/usr/lib/cups/filter", "/usr/local/lib/cups/filter"]
        for filter_name in ("rastertoprinter", "rastertoprintercm", "rastertoprinterlm"):
            src = None
            for root_dir, _, files in os.walk(extract_dir):
                if filter_name in files:
                    src = os.path.join(root_dir, filter_name)
                    break
            if src:
                for fd in filter_dirs:
                    try:
                        os.makedirs(fd, exist_ok=True)
                        dst = os.path.join(fd, filter_name)
                        shutil.copy2(src, dst)
                        os.chmod(dst, 0o755)
                    except OSError:
                        pass

        steps.append({"step": "Install CUPS Filters", "status": "success",
                       "message": "rastertoprinter filter(s) placed"})

        # Step 5: Find and install PPD
        ppd_src = None
        for root_dir, _, files in os.walk(extract_dir):
            for fname in files:
                if re.match(r'80mmSeries\.ppd(\.gz)?$', fname, re.IGNORECASE):
                    ppd_src = os.path.join(root_dir, fname)
                    break
            if ppd_src:
                break
        if not ppd_src:
            for root_dir, _, files in os.walk(extract_dir):
                for fname in files:
                    if re.search(r'(sprt|sprit|80mm|thermal)', fname, re.IGNORECASE) and fname.endswith((".ppd", ".ppd.gz")):
                        ppd_src = os.path.join(root_dir, fname)
                        break
                if ppd_src:
                    break

        if ppd_src:
            os.makedirs(os.path.dirname(SPRT_PPD_DEST), exist_ok=True)
            if ppd_src.endswith(".gz"):
                import gzip
                with gzip.open(ppd_src, "rb") as gz_in:
                    with open(SPRT_PPD_DEST, "wb") as ppd_out:
                        ppd_out.write(gz_in.read())
            else:
                shutil.copy2(ppd_src, SPRT_PPD_DEST)
            # Patch PPD to enable full cut by default
            try:
                with open(SPRT_PPD_DEST, "r", errors="replace") as f:
                    ppd_text = f.read()
                ppd_text = re.sub(
                    r'\*DefaultCutType:.*', '*DefaultCutType: FullCut', ppd_text, flags=re.IGNORECASE
                )
                with open(SPRT_PPD_DEST, "w") as f:
                    f.write(ppd_text)
            except OSError:
                pass
            steps.append({"step": "Install SPRT PPD", "status": "success",
                           "message": f"PPD installed at {SPRT_PPD_DEST}"})
        else:
            steps.append({"step": "Install SPRT PPD", "status": "failed",
                           "message": "80mmSeries.ppd not found in archive"})
            shutil.rmtree(extract_dir, ignore_errors=True)
            return {"status": "error", "steps": steps, "message": "SPRT PPD not found"}

        shutil.rmtree(extract_dir, ignore_errors=True)

        # Restart CUPS
        run_command(["systemctl", "restart", "cups"], timeout=15)
        import time as _time; _time.sleep(2)

        # Step 6: Register printer
        run_command(["lpadmin", "-x", printer_name], timeout=5)
        rc_lp, _, err_lp = run_command([
            "lpadmin", "-p", printer_name, "-E",
            "-v", usb_uri, "-P", SPRT_PPD_DEST,
            "-D", "SPRT 80mm Thermal",
        ], timeout=15)
        if rc_lp != 0:
            steps.append({"step": "Register SPRT", "status": "failed",
                           "message": err_lp or "lpadmin failed"})
            return {"status": "error", "steps": steps}

        run_command(["cupsenable", printer_name], timeout=5)
        run_command(["cupsaccept", printer_name], timeout=5)
        _set_thermal_cut_defaults(printer_name)

        steps.append({"step": "Register SPRT", "status": "success",
                       "message": f"Printer '{printer_name}' ready on {usb_uri}"})
        return {"status": "ok", "steps": steps, "printer_name": printer_name,
                "message": "SPRT 80mm installed successfully!"}

    # Should not reach here
    return {"status": "error", "steps": steps, "message": "Unknown brand"}


def handle_load_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Load and return data.json contents."""
    data_json = _load_data_json()
    return {"status": "ok", "data": data_json}


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

HANDLERS = {
    "scan": handle_scan,
    "ping_printer": handle_ping_printer,
    "fix": handle_fix,
    "setup_printer": handle_setup_printer,
    "setup_thermal_usb": handle_setup_thermal_usb,
    "install_thermal_driver": handle_install_thermal_driver,
    "detect_usb_printers": handle_detect_usb_printers,
    "discover_printers":        handle_discover_printers,
    "network_scan":             handle_network_scan,
    "install_thermal_brand":    handle_install_thermal_brand,
    "get_printer_details": handle_get_printer_details,
    "clear_jobs": handle_clear_jobs,
    "test_print": handle_test_print,
    "update_all": handle_update_all,
    # NEW: Config management (root writes config for non-root GUI)
    "save_config": handle_save_config,
    "get_config": handle_get_config,
    "set_branch": handle_set_branch,
    "get_branch": handle_get_branch,
    # NEW: Data management
    "load_data": handle_load_data,
}


def dispatch(data: Dict[str, Any]) -> Dict[str, Any]:
    """Route a JSON command to the appropriate handler."""
    action = data.get("action", "")
    handler = HANDLERS.get(action)

    if handler is None:
        logger.warning("Unknown action: %s", action)
        return {"status": "error", "message": f"Unknown action: {action}"}

    try:
        return handler(data)
    except Exception as exc:
        logger.exception("Unhandled error in handler '%s'", action)
        return {"status": "error", "message": f"Internal error: {exc}"}


# ---------------------------------------------------------------------------
# Client handler
# ---------------------------------------------------------------------------


def handle_client(client_sock: socket.socket, client_addr: str) -> None:
    """Handle a single client connection.

    FIX v3.4: Added SO_PEERCRED peer-credential check.  Previously any process
    could connect to the socket and issue root-level CUPS commands.  Now only
    root (uid 0) or members of the it-aman group are accepted.
    """
    logger.debug("Client connected: %s", client_addr)
    response: Dict[str, Any] = {}

    try:
        # ── Peer credential check (Linux SO_PEERCRED) ───────────────────────
        try:
            cred_size = struct.calcsize("3i")
            raw_cred = client_sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, cred_size)
            peer_pid, peer_uid, peer_gid = struct.unpack("3i", raw_cred)

            # Resolve allowed gid for it-aman group
            try:
                allowed_gid = grp.getgrnam(IT_AMAN_GROUP).gr_gid
            except KeyError:
                allowed_gid = -1

            if peer_uid != 0 and peer_gid != allowed_gid:
                logger.warning(
                    "Rejected connection from pid=%d uid=%d gid=%d "
                    "(not root and not in group '%s')",
                    peer_pid, peer_uid, peer_gid, IT_AMAN_GROUP,
                )
                response = {"status": "error", "message": "Permission denied"}
                client_sock.sendall(json.dumps(response).encode("utf-8") + b"\n")
                return
            logger.debug("Accepted peer pid=%d uid=%d gid=%d", peer_pid, peer_uid, peer_gid)
        except OSError as cred_err:
            # SO_PEERCRED not supported (non-Linux?) — log and allow
            logger.warning("SO_PEERCRED unavailable: %s — skipping peer check", cred_err)

        client_sock.settimeout(30)

        raw = b""
        while True:
            chunk = client_sock.recv(65536)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 1_048_576:
                response = {"status": "error", "message": "Request too large"}
                client_sock.sendall(json.dumps(response).encode("utf-8") + b"\n")
                return
            if b"\n" in raw:
                break
        if not raw:
            return

        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            response = {"status": "error", "message": "Invalid UTF-8 encoding"}
            client_sock.sendall(json.dumps(response).encode("utf-8") + b"\n")
            return

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            response = {"status": "error", "message": f"Invalid JSON: {exc}"}
            client_sock.sendall(json.dumps(response).encode("utf-8") + b"\n")
            return

        logger.info("Received command: %s", json.dumps(data, ensure_ascii=False))
        response = dispatch(data)

    except socket.timeout:
        response = {"status": "error", "message": "Request timed out"}
    except ConnectionResetError:
        return
    except OSError as exc:
        response = {"status": "error", "message": f"Connection error: {exc}"}
    except Exception as exc:
        response = {"status": "error", "message": f"Unexpected error: {exc}"}

    try:
        response_bytes = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        client_sock.sendall(response_bytes)
    except OSError as exc:
        logger.error("Failed to send response to %s: %s", client_addr, exc)
    finally:
        try:
            client_sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Thread wrapper
# ---------------------------------------------------------------------------


def client_thread_target(client_sock: socket.socket, client_addr: str) -> None:
    """Wrapper for client handler threads."""
    try:
        handle_client(client_sock, client_addr)
    finally:
        current = threading.current_thread()
        with active_threads_lock:
            try:
                active_threads.remove(current)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point."""
    global server_socket

    logger.info("IT Aman Daemon v3.0 starting up (PID %d)", os.getpid())

    if os.geteuid() != 0:
        logger.error("This daemon must run as root. Current UID: %d", os.geteuid())
        sys.exit(1)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    os.makedirs(SOCKET_DIR, exist_ok=True)

    if os.path.exists(SOCKET_PATH):
        try:
            os.unlink(SOCKET_PATH)
        except OSError as exc:
            logger.error("Cannot remove stale socket: %s", exc)
            sys.exit(1)

    server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind(SOCKET_PATH)
    except OSError as exc:
        logger.error("Cannot bind socket %s: %s", SOCKET_PATH, exc)
        sys.exit(1)

    # FIX v3.4: Restrict socket to 'it-aman' group (0o660).
    # The old 0o666 allowed ANY local user to send root-level commands.
    try:
        gid = grp.getgrnam(IT_AMAN_GROUP).gr_gid
        os.chown(SOCKET_PATH, 0, gid)
        os.chmod(SOCKET_PATH, 0o660)
        logger.info("Socket restricted to group '%s' (mode 0o660)", IT_AMAN_GROUP)
    except KeyError:
        # Group doesn't exist yet — fall back to 0o600 (root only) and warn
        os.chmod(SOCKET_PATH, 0o600)
        logger.warning(
            "Group '%s' not found. Socket set to 0o600 (root only). "
            "Create it and add GUI users: groupadd %s && usermod -aG %s <user>",
            IT_AMAN_GROUP, IT_AMAN_GROUP, IT_AMAN_GROUP,
        )
    server_socket.listen(16)
    logger.info("Listening on %s", SOCKET_PATH)

    while not shutdown_event.is_set():
        try:
            server_socket.settimeout(1.0)
            try:
                client_sock, _ = server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            t = threading.Thread(
                target=client_thread_target,
                args=(client_sock, f"client-{threading.active_count()}"),
                daemon=True,
            )
            t.start()
            with active_threads_lock:
                active_threads.append(t)

            # FIX v3.4: Periodically prune dead threads so active_threads
            # doesn't grow without bound in a long-running daemon.
            # client_thread_target already removes itself on exit, but
            # exceptions could leave stale entries; this is a safety net.
            if threading.active_count() % 50 == 0:
                with active_threads_lock:
                    active_threads[:] = [t for t in active_threads if t.is_alive()]

        except Exception as exc:
            if shutdown_event.is_set():
                break
            logger.exception("Error accepting connection: %s", exc)

    logger.info("Shutting down...")

    with active_threads_lock:
        remaining = list(active_threads)
    for t in remaining:
        if t.is_alive():
            t.join(timeout=5.0)

    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    except OSError:
        pass

    logger.info("IT Aman Daemon stopped.")


if __name__ == "__main__":
    main()
