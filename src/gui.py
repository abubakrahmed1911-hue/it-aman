#!/usr/bin/env python3
"""
IT Aman v3.0 - Printer Management Interface
=============================================
GTK3 desktop frontend. User searches for branch, selects printer.
Runs in user space, communicates with root daemon via Unix socket.

v3.0 Changes (from v2.5):
- REMOVED: Manual IP entry / "Add Printer" button (security concern)
- REMOVED: "Discover Network Printers" button (security concern)
- FIXED: Data loading from data.json works correctly on every screen
- FIXED: Update system - version persisted via daemon (root writes config)
- FIXED: Printer status only shows printers from current branch (no phantoms)
- ADDED: Branch selection on main menu + first-run branch setup
- ADDED: Config persistence via daemon (save_config command)
- FIXED: chattr +i properly handled during updates
- FIXED: Thermal printer PPD auto-detection via CUPS
- IMPROVED: Clean navigation with branch-aware filtering
- ADDED: Arabic/English language support with UI language selector
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SOCKET_PATH = "/run/it-aman/it-aman.sock"
CONFIG_PATH = "/etc/it-aman/config.json"
DATA_PATH = "/etc/it-aman/data.json"
DRIVERS_DIR = "/etc/it-aman/cache/drivers"

GITHUB_REPO = "abubakrahmed1911-hue/it-aman"
GITHUB_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/"

VIDEO_GOOGLE_DRIVE = "https://drive.google.com/file/d/1Ir08HroVj6TShF-ZOCiXvbwk8THkED1E/view?usp=drive_link"
VIDEO_DROPBOX = "https://www.dropbox.com/scl/fi/pg75dydlchtpju7j65kr2/Remove-paper-jam-inside-keyocera-UK-TECH-720p-h264.mp4?rlkey=obb9ghb14yq5l19dv4fdllwfd&st=mw2bixwi&dl=0"

MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10MB

# Update check interval in seconds.
# 0   = check every launch
# 3600 = every 1 hour
# 86400 = every 24 hours (default)
UPDATE_CHECK_INTERVAL = 0  # ← غيّر الرقم ده حسب ما تحب

# Thermal printer PPD sizes
THERMAL_SIZES = {
    "58mm": "58mmSeries.ppd.gz",
    "76mm": "76mmSeries.ppd.gz",
    "80mm": "80mmSeries.ppd.gz",
    "112mm": "112mmSeries.ppd.gz",
}

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = """
window { background-color: #f5f5f5; }
.scrolled-window { border: none; background-color: #f5f5f5; }
.title-label { font-size: 28px; font-weight: bold; color: #333; padding: 10px 0 5px 0; }
.subtitle-label { font-size: 14px; color: #777; padding-bottom: 10px; }
.menu-card { background: #fff; border-radius: 12px; padding: 18px 20px; margin: 4px 10px; border: 1px solid #e0e0e0; }
.menu-card:hover { background: #e8f4fd; border-color: #2196F3; }
.menu-card-icon { font-size: 26px; }
.menu-card-title { font-size: 18px; font-weight: bold; color: #333; }
.menu-card-desc { font-size: 13px; color: #888; }
.exit-card { background: #fff5f5; border-radius: 12px; padding: 14px 20px; margin: 4px 10px; border: 1px solid #ffcdd2; }
.exit-card:hover { background: #ffebee; border-color: #f44336; }
.section-header { font-size: 20px; font-weight: bold; color: #333; padding: 10px 15px 5px 15px; }
.printer-card { background: #fff; border-radius: 10px; padding: 14px 18px; margin: 5px 10px; border: 2px solid #e0e0e0; }
.printer-card-ok { border-color: #4CAF50; background: #f1f8e9; }
.printer-card-error { border-color: #f44336; background: #fbe9e7; }
.printer-card-warning { border-color: #FF9800; background: #fff8e1; }
.printer-card-not-installed { border-color: #9E9E9E; background: #fafafa; }
.printer-name { font-size: 17px; font-weight: bold; color: #333; }
.printer-status { font-size: 14px; color: #555; }
.printer-detail { font-size: 12px; color: #888; }
.btn-primary { background: #2196F3; color: #fff; border-radius: 8px; padding: 8px 18px; font-size: 14px; font-weight: bold; border: none; }
.btn-primary:hover { background: #1976D2; }
.btn-success { background: #4CAF50; color: #fff; border-radius: 8px; padding: 8px 18px; font-size: 14px; font-weight: bold; border: none; }
.btn-success:hover { background: #388E3C; }
.btn-danger { background: #f44336; color: #fff; border-radius: 8px; padding: 8px 18px; font-size: 14px; font-weight: bold; border: none; }
.btn-danger:hover { background: #d32f2f; }
.btn-back { background: #757575; color: #fff; border-radius: 8px; padding: 10px 28px; font-size: 15px; font-weight: bold; border: none; }
.btn-back:hover { background: #616161; }
.search-entry { border-radius: 10px; padding: 12px 15px; font-size: 16px; border: 2px solid #e0e0e0; background: #fff; }
.search-entry:focus { border-color: #2196F3; }
.step-success { color: #4CAF50; font-weight: bold; font-size: 15px; padding: 4px 0; }
.step-failed { color: #f44336; font-weight: bold; font-size: 15px; padding: 4px 0; }
.step-pending { color: #999; font-size: 15px; padding: 4px 0; }
.instruction-card { background: #fff; border-radius: 10px; padding: 12px 16px; margin: 5px 10px; border-left: 4px solid #2196F3; }
.instruction-number { font-size: 20px; font-weight: bold; color: #2196F3; }
.instruction-text { font-size: 15px; color: #333; }
.status-msg { font-size: 15px; padding: 8px 15px; color: #555; }
.error-msg { font-size: 15px; padding: 8px 15px; color: #f44336; background: #ffebee; border-radius: 8px; margin: 10px; }
.spinner-label { font-size: 16px; color: #555; padding: 5px 0; }
.progress-bar trough { background: #e0e0e0; border-radius: 6px; min-height: 10px; }
.progress-bar progress { background: #2196F3; border-radius: 6px; }
.branch-card { background: #fff; border-radius: 10px; padding: 14px 18px; margin: 4px 10px; border: 1px solid #e0e0e0; }
.branch-card:hover { background: #e3f2fd; border-color: #2196F3; }
.branch-card-active { background: #e3f2fd; border-color: #2196F3; border-width: 2px; }
.size-card { background: #fff; border-radius: 10px; padding: 12px 18px; margin: 3px 10px; border: 2px solid #e0e0e0; }
.size-card:hover { background: #e3f2fd; border-color: #2196F3; }
.size-card-active { background: #e3f2fd; border-color: #2196F3; }
.top-bar { background: #fff; border-bottom: 1px solid #e0e0e0; padding: 8px 12px; }
.back-arrow { background: transparent; color: #555; border: none; border-radius: 6px; padding: 6px 10px; font-size: 18px; }
.back-arrow:hover { background: #e0e0e0; }
.branch-indicator { font-size: 13px; color: #2196F3; padding: 3px 10px; background: #e3f2fd; border-radius: 6px; }
.lang-toggle { background: #e3f2fd; color: #2196F3; border-radius: 8px; padding: 6px 14px; font-size: 14px; font-weight: bold; border: 1px solid #2196F3; }
.lang-toggle:hover { background: #bbdefb; }
"""

# ---------------------------------------------------------------------------
# Language / Translation
# ---------------------------------------------------------------------------
_current_lang = "ar"  # "en" for English, "ar" for Arabic

TRANSLATIONS = {
    "ar": {
        # ── Main Menu ──
        "IT Aman": "IT أمان",
        "Printer Management Tool": "أداة إدارة الطابعات",
        "Paper Jam": "ورق عالق",
        "Step-by-step instructions + video": "تعليمات خطوة بخطوة + فيديو",
        "Auto Fix & Repair": "إصلاح تلقائي",
        "Detects and fixes problems": "يكتشف ويصلح المشاكل",
        "Setup New Printer": "تثبيت طابعة جديدة",
        "Search by branch -> select -> install": "بحث بالفرع -> اختر -> تثبيت",
        "Setup USB Thermal Printer": "تثبيت طابعة حرارية USB",
        "Cashier / sticker / 80mm / 58mm": "كاشير / ملصقات / 80 مم / 58 مم",
        "Printer Status": "حالة الطابعات",
        "Status of printers in your branch": "حالة طابعات فرعك",
        "Select Branch": "اختيار الفرع",
        "Set your current branch for filtering": "تعيين الفرع الحالي للتصفية",
        "Exit": "خروج",
        "Language": "اللغة",

        # ── Navigation ──
        "Back": "رجوع",

        # ── Branch Selection ──
        "Select your branch:": "اختر فرعك:",
        "Type branch name...": "اكتب اسم الفرع...",
        "No branches found in data": "لا توجد فروع في البيانات",
        "(Current) ": "(الحالي) ",
        "printer(s)": "طابعة",
        "Branch set to: ": "تم تعيين الفرع: ",
        "Failed to set branch: ": "فشل تعيين الفرع: ",

        # ── Paper Jam ──
        "Turn off the printer and unplug the power cable immediately": "أوقف الطابعة وافصل كابل الطاقة فوراً",
        "Open the paper access doors": "افتح أبواب الوصول للورق",
        "Pull the jammed paper slowly with both hands": "اسحب الورق العالق ببطء بكلتا اليدين",
        "Do not use excessive force or sharp tools": "لا تستخدم قوة مفرطة أو أدوات حادة",
        "Tutorial Videos": "فيديوهات تعليمية",
        "Video - Google Drive": "فيديو - جوجل درايف",
        "Video - Dropbox": "فيديو - دروب بوكس",

        # ── Fix Screen ──
        "Scanning printers...": "جاري فحص الطابعات...",
        "No printers found for your branch": "لا توجد طابعات لفرعك",
        "Showing printers for branch: ": "عرض طابعات الفرع: ",
        "Select a printer to fix:": "اختر طابعة للإصلاح:",
        "Online": "متصل",
        "Not Installed": "غير مثبت",
        "Stopped": "متوقف",
        "Problem: ": "مشكلة: ",
        "Unknown (": "غير معروف (",
        "stuck jobs": "مهام عالقة",
        "Fixing ": "جاري إصلاح ",
        "Fixed successfully!": "تم الإصلاح بنجاح!",
        "Fix failed": "فشل الإصلاح",
        "Failed: ": "فشل: ",

        # ── Setup Printer ──
        "Search for your branch:": "ابحث عن فرعك:",
        "Type branch name (e.g. MEGA)": "اكتب اسم الفرع (مثال: MEGA)",
        "No results found": "لا توجد نتائج",
        "No branches registered in data file": "لا توجد فروع مسجلة في ملف البيانات",
        "No printers registered for this branch": "لا توجد طابعات مسجلة لهذا الفرع",
        "Select printer (": "اختر طابعة (",
        "):": "):",
        "No details": "لا توجد تفاصيل",
        "Installing ": "جاري تثبيت ",
        " installed successfully!": " تم تثبيته بنجاح!",
        "Installation failed": "فشل التثبيت",

        # ── Thermal Printer ──
        "USB Thermal Printer Setup": "تثبيت طابعة حرارية USB",
        "Step 1: Make sure the printer is connected via USB": "الخطوة 1: تأكد أن الطابعة متصلة عبر USB",
        "Step 2: Select paper size": "الخطوة 2: اختر حجم الورق",
        "Step 3: Select the printer and it will be installed automatically": "الخطوة 3: اختر الطابعة وسيتم تثبيتها تلقائياً",
        "Select paper size:": "اختر حجم الورق:",
        "Search for USB Printers": "البحث عن طابعات USB",
        "Install Thermal Printer Driver": "تثبيت تعريف الطابعة الحرارية",
        "Driver status: ": "حالة التعريف: ",
        " PPD file(s) installed": " ملف PPD مثبت",
        "Thermal driver not installed -- click 'Install Driver' above": "تعريف الطابعة الحرارية غير مثبت -- اضغط 'تثبيت التعريف' أعلاه",
        "Note: If no PPD is available, CUPS will auto-detect (IPP Everywhere)": "ملاحظة: إذا لم يتوفر PPD، سيقوم CUPS بالكشف التلقائي",
        "Installing thermal printer driver...": "جاري تثبيت تعريف الطابعة الحرارية...",
        "Driver installed successfully!": "تم تثبيت التعريف بنجاح!",
        "Searching for USB printers...": "جاري البحث عن طابعات USB...",
        "No USB printers found": "لا توجد طابعات USB",
        "Make sure the printer is connected and powered on": "تأكد أن الطابعة متصلة ومشتغلة",
        "Search Again": "بحث مرة أخرى",
        "Found ": "تم العثور على ",
        " USB printer(s):": " طابعة USB:",
        "Already configured": "تم إعدادها مسبقاً",
        "Installing thermal printer (": "جاري تثبيت طابعة حرارية (",
        "Thermal printer installed successfully!": "تم تثبيت الطابعة الحرارية بنجاح!",

        # ── Status Screen ──
        "Scanning...": "جاري الفحص...",
        "Error occurred": "حدث خطأ",
        "Total: ": "الإجمالي: ",
        "Online: ": "متصل: ",
        "Offline: ": "غير متصل: ",
        "Errors: ": "أخطاء: ",
        "Not Installed: ": "غير مثبت: ",
        "Filtered by branch: ": "مصنف بالفرع: ",
        "WARNING: No branch selected -- showing ALL CUPS printers. Select a branch for proper filtering.": "تحذير: لم يتم اختيار فرع -- عرض جميع طابعات CUPS. اختر فرعاً للتصفية الصحيحة.",
        "Loading details...": "جاري تحميل التفاصيل...",
        "Test Print": "طباعة تجريبية",
        "Fix": "إصلاح",
        "Install Printer": "تثبيت الطابعة",
        "Working normally": "تعمل بشكل طبيعي",
        "Rejecting jobs": "ترفض المهام",
        "Default": "افتراضي",
        " job(s)": " مهمة",
        "USB": "USB",
        "Fixing...": "جاري الإصلاح...",
        "Fix failed": "فشل الإصلاح",
        "Details unavailable": "التفاصيل غير متاحة",

        # ── Update Screen ──
        "Checking for updates...": "جاري التحقق من التحديثات...",
        "Update Available": "تحديث متاح",
        "Update Available!": "تحديث متاح!",
        "Current: ": "الحالي: ",
        "New: ": "الجديد: ",
        "Update now?": "تحديث الآن؟",
        "Later": "لاحقاً",
        "Update Now": "تحديث الآن",
        "Updating...": "جاري التحديث...",
        "Restarting service...": "جاري إعادة تشغيل الخدمة...",
        "Update successful!": "تم التحديث بنجاح!",
        "Update successful! (": "تم التحديث بنجاح! (",
        " files)": " ملفات)",
        "Partial update (": "تحديث جزئي (",
        " files, ": " ملفات، ",
        " failed)": " فشل)",
        "Update failed - check internet connection": "فشل التحديث - تحقق من الاتصال بالإنترنت",

        # ── Daemon Errors ──
        "Daemon service is not running. Contact IT support.": "خدمة النظام لا تعمل. تواصل مع الدعم الفني.",
        "Error: ": "خطأ: ",

        # ── Printer network messages (from daemon) ──
        "Printer is not connected to the network": "الطابعة غير متصلة بالشبكة",
        "Printer unreachable": "الطابعة غير متاحة",
        "Network Issue": "مشكلة شبكة",
        "Not responding to ping": "لا تستجيب للـ ping",
        "Trying installation anyway": "جاري محاولة التثبيت",
        "Ping warning (ICMP may be blocked)": "تحذير ping (قد يكون ICMP محجوب)",

        # ── t_fmt keys (use {} as placeholder) ──
        "Installing {}...": "جاري تثبيت {}...",
        "{} installed successfully!": "تم تثبيت {} بنجاح!",
        "Installing thermal printer ({})...": "جاري تثبيت طابعة حرارية ({})...",
        "Fixing {}...": "جاري إصلاح {}...",
        "Filtered by branch: {}": "مصنف بالفرع: {}",
        "Showing printers for branch: {}": "عرض طابعات الفرع: {}",
        "Branch set to: {}": "تم تعيين الفرع: {}",
        "Failed to set branch: {}": "فشل تعيين الفرع: {}",
        "Found {} USB printer(s):": "تم العثور على {} طابعة USB:",
        "Driver status: {} PPD file(s) installed": "حالة التعريف: {} ملف PPD مثبت",
    },
}


def t(key: str) -> str:
    """Translate a key to the current language. Falls back to key if no translation found."""
    if _current_lang in TRANSLATIONS:
        return TRANSLATIONS[_current_lang].get(key, key)
    return key


def t_fmt(key: str, *args) -> str:
    """Translate then format with positional args: t_fmt("Installing {}...", pname)"""
    return t(key).format(*args)


def set_language(lang: str):
    """Set the current language and update text direction globally."""
    global _current_lang
    _current_lang = lang
    # Apply RTL/LTR globally — must be called before widgets are shown
    try:
        Gtk.Widget.set_default_direction(get_text_direction())
    except Exception:
        pass


def get_text_direction() -> Gtk.TextDirection:
    """Return RTL for Arabic, LTR for other languages."""
    return Gtk.TextDirection.RTL if _current_lang == "ar" else Gtk.TextDirection.LTR


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("it-aman")
logger.setLevel(logging.DEBUG)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_h)

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------
_config: Dict[str, Any] = {}
_all_data: Dict[str, Any] = {"branches": []}
_current_branch_id: str = ""

# Scan cache: avoid rescanning within SCAN_CACHE_TTL seconds
_scan_cache: Optional[Dict[str, Any]] = None
_scan_cache_time: float = 0.0
SCAN_CACHE_TTL = 30  # seconds


def get_cached_scan(force: bool = False) -> Optional[Dict[str, Any]]:
    """Return cached scan result if fresh, else None (caller must fetch)."""
    global _scan_cache, _scan_cache_time
    if force:
        _scan_cache = None
        return None
    if _scan_cache and (time.time() - _scan_cache_time) < SCAN_CACHE_TTL:
        return _scan_cache
    return None


def store_scan_cache(result: Dict[str, Any]) -> None:
    global _scan_cache, _scan_cache_time
    _scan_cache = result
    _scan_cache_time = time.time()


def load_config() -> Dict[str, Any]:
    """Load local config via daemon (daemon has root access)."""
    global _config, _current_branch_id
    try:
        result = send_command({"action": "get_config"})
        if result.get("status") == "ok":
            _config = result.get("config", {})
            _current_branch_id = _config.get("current_branch_id", "")
        else:
            _config = {"github_repo": GITHUB_REPO, "version": "3.0",
                       "last_update_check": 0, "current_branch_id": ""}
    except Exception:
        _config = {"github_repo": GITHUB_REPO, "version": "3.0",
                   "last_update_check": 0, "current_branch_id": ""}
    return _config


def save_config_via_daemon(updates: Dict[str, Any]) -> bool:
    """Save config values via daemon (root writes to /etc/it-aman/config.json)."""
    try:
        result = send_command({"action": "save_config", **updates})
        return result.get("status") == "ok"
    except Exception:
        return False


def load_data() -> Dict[str, Any]:
    """Load printer data from daemon."""
    global _all_data
    try:
        result = send_command({"action": "load_data"})
        if result.get("status") == "ok":
            _all_data = result.get("data", {"branches": []})
        else:
            # Fallback: try reading directly
            if os.path.isfile(DATA_PATH):
                with open(DATA_PATH, "r", encoding="utf-8") as f:
                    _all_data = json.load(f)
            else:
                _all_data = {"branches": []}
    except Exception:
        try:
            if os.path.isfile(DATA_PATH):
                with open(DATA_PATH, "r", encoding="utf-8") as f:
                    _all_data = json.load(f)
        except Exception:
            _all_data = {"branches": []}

    # Validate structure
    if "branches" not in _all_data or not isinstance(_all_data["branches"], list):
        _all_data = {"branches": []}
    return _all_data


def search_branches(query: str) -> List[Dict[str, Any]]:
    """Search branches by name (case insensitive)."""
    if not query:
        return _all_data.get("branches", [])
    q = query.lower()
    return [
        b for b in _all_data.get("branches", [])
        if q in b.get("branch_name", "").lower() or q in b.get("branch_id", "").lower()
    ]


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------
def _compare_versions(v1: str, v2: str) -> bool:
    """Compare two version strings (v1 > v2)."""
    try:
        parts1 = [int(x) for x in v1.split(".")]
        parts2 = [int(x) for x in v2.split(".")]
        max_len = max(len(parts1), len(parts2))
        parts1 += [0] * (max_len - len(parts1))
        parts2 += [0] * (max_len - len(parts2))
        return parts1 > parts2
    except (ValueError, AttributeError):
        return v1 > v2


# ---------------------------------------------------------------------------
# Daemon communication
# ---------------------------------------------------------------------------
def send_command(command: dict) -> dict:
    """Send a JSON command to the root daemon via Unix socket."""
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_RESPONSE_SIZE:
                return {"status": "error", "message": "Response too large"}
            if b"\n" in data:
                break
        text = data.decode("utf-8").strip()
        return json.loads(text) if text else {"status": "error", "message": "No response"}
    except FileNotFoundError:
        return {"status": "error", "message": t("Daemon service is not running. Contact IT support.")}
    except ConnectionRefusedError:
        return {"status": "error", "message": t("Daemon service is not running. Contact IT support.")}
    except Exception as exc:
        return {"status": "error", "message": f"{t('Error: ')}{exc}"}
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# GitHub update check
# ---------------------------------------------------------------------------
def fetch_github(path: str, retries: int = 3) -> Optional[str]:
    """Fetch a file from GitHub with retry logic."""
    url = GITHUB_RAW + path
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "IT-Aman/3.0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:
            logger.warning("Fetch attempt %d/%d failed for %s: %s", attempt + 1, retries, path, exc)
            if attempt < retries - 1:
                time.sleep(1 * (attempt + 1))
    return None


def check_update() -> dict:
    """Check if remote version is newer than local."""
    try:
        content = fetch_github("version.json")
        if content is None:
            return {"has_update": False, "error": "no_internet"}
        remote = json.loads(content).get("version", "0.0")
        local = _config.get("version", "0.0")
        return {"has_update": _compare_versions(remote, local),
                "remote_version": remote, "local_version": local}
    except Exception:
        return {"has_update": False, "error": "fail"}


# ---------------------------------------------------------------------------
# Threading helper
# ---------------------------------------------------------------------------
def run_in_thread(target: Callable, callback: Callable, *args, **kwargs) -> threading.Thread:
    """Run a function in a background thread, then call callback in main thread."""
    def wrapper():
        try:
            result = target(*args, **kwargs)
        except Exception as exc:
            result = {"status": "error", "message": str(exc)}
        GLib.idle_add(callback, result)
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------
def lbl(text: str, css: str = "", xalign: float = 0.5) -> Gtk.Label:
    l = Gtk.Label(label=text)
    l.set_xalign(xalign)
    if css:
        l.get_style_context().add_class(css)
    return l


def btn(text: str, css: str = "btn-primary", callback: Optional[Callable] = None) -> Gtk.Button:
    b = Gtk.Button(label=text)
    b.get_style_context().add_class(css)
    b.set_can_focus(False)
    if callback:
        b.connect("clicked", callback)
    return b


def make_menu_card(icon: str, title: str, desc: str, callback: Callable) -> Gtk.Button:
    b = Gtk.Button()
    b.get_style_context().add_class("menu-card")
    b.set_can_focus(False)
    inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
    inner.set_margin_start(5)
    inner.set_margin_end(5)
    i = lbl(icon, "menu-card-icon")
    i.set_xalign(0.0)
    tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    tb.set_hexpand(True)
    t_lbl = lbl(title, "menu-card-title")
    t_lbl.set_xalign(0.0)
    d_lbl = lbl(desc, "menu-card-desc")
    d_lbl.set_xalign(0.0)
    tb.pack_start(t_lbl, False, False, 0)
    tb.pack_start(d_lbl, False, False, 0)
    inner.pack_end(i, False, False, 0)
    inner.pack_start(tb, True, True, 0)
    b.add(inner)
    b.connect("clicked", callback)
    return b


def _clear(box: Gtk.Box) -> None:
    for c in box.get_children():
        box.remove(c)


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class ITAmanApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="IT Aman")
        self.set_default_size(680, 550)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("printer")

        # Apply RTL/LTR globally BEFORE building any widgets
        Gtk.Widget.set_default_direction(get_text_direction())

        # Load data at startup
        load_config()
        load_data()

        self._apply_css()

        # Navigation history
        self._nav_history: List[str] = []

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(250)
        self.add(self.stack)

        # Build all screens
        self._build_update_screen()
        self._build_main_menu()
        self._build_paper_jam_screen()
        self._build_fix_screen()
        self._build_setup_screen()
        self._build_thermal_screen()
        self._build_status_screen()
        self._build_branch_select_screen()

        # Start with update check
        self.stack.set_visible_child_name("update_check")
        GLib.idle_add(self._check_for_update)

        self.set_direction(get_text_direction())
        self.connect("destroy", Gtk.main_quit)

    # ── Language Switch ──
    def _toggle_language(self, _widget=None):
        """Switch between Arabic and English, rebuild UI with correct direction."""
        new_lang = "en" if _current_lang == "ar" else "ar"
        set_language(new_lang)  # also calls Gtk.Widget.set_default_direction

        # Clear scan cache so next scan uses fresh data after rebuild
        # Clear scan cache so next open gets fresh data
        global _scan_cache, _scan_cache_time
        _scan_cache = None
        _scan_cache_time = 0.0

        # Remove all old screens from stack
        for child in self.stack.get_children():
            self.stack.remove(child)

        # Rebuild all screens with new language
        self._nav_history.clear()
        self._build_update_screen()
        self._build_main_menu()
        self._build_paper_jam_screen()
        self._build_fix_screen()
        self._build_setup_screen()
        self._build_thermal_screen()
        self._build_status_screen()
        self._build_branch_select_screen()

        # Go directly to main menu
        self.stack.set_visible_child_name("main_menu")
        self.show_all()

    # ── Navigation ──
    def _navigate_to(self, page_name: str):
        current = self.stack.get_visible_child_name()
        if current and current != page_name:
            self._nav_history.append(current)
        self.stack.set_visible_child_name(page_name)

    def _go_back(self, _widget=None):
        if self._nav_history:
            prev = self._nav_history.pop()
            self.stack.set_visible_child_name(prev)
        else:
            self.stack.set_visible_child_name("main_menu")

    def _go_back_menu(self, _widget=None):
        self._nav_history.clear()
        self.stack.set_visible_child_name("main_menu")

    def _make_back_button(self, callback=None) -> Gtk.Button:
        b = Gtk.Button(label=t("Back"))
        b.get_style_context().add_class("btn-back")
        b.set_can_focus(False)
        if callback:
            b.connect("clicked", callback)
        else:
            b.connect("clicked", self._go_back)
        return b

    def _make_top_bar(self, title: str, back_callback=None) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.get_style_context().add_class("top-bar")
        back_btn = self._make_back_button(back_callback)
        bar.pack_start(back_btn, False, False, 0)
        title_lbl = lbl(title, "section-header")
        title_lbl.set_xalign(0.0)
        bar.pack_start(title_lbl, True, True, 0)
        return bar

    def _apply_css(self):
        p = Gtk.CssProvider()
        p.load_from_data(CSS.encode("utf-8"))
        s = Gdk.Screen.get_default()
        if s is None:
            s = Gdk.Display.get_default().get_default_screen()
        Gtk.StyleContext.add_provider_for_screen(s, p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── Branch Indicator ──
    def _get_branch_indicator(self) -> Optional[Gtk.Label]:
        """Return a label showing the current branch, or None if not set."""
        global _current_branch_id
        if _current_branch_id:
            # Find branch name
            for b in _all_data.get("branches", []):
                if b.get("branch_id", "") == _current_branch_id:
                    name = b.get("branch_name", _current_branch_id)
                    return lbl(f"{t('Select Branch')}: {name}", "branch-indicator")
            return lbl(f"{t('Select Branch')}: {_current_branch_id}", "branch-indicator")
        return None

    # ── Update Screen ──
    def _build_update_screen(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        self.upd_spinner = Gtk.Spinner()
        self.upd_spinner.set_size_request(60, 60)
        self.upd_spinner.start()
        self.upd_label = lbl(t("Checking for updates..."), "spinner-label")
        self.upd_progress = Gtk.ProgressBar()
        self.upd_progress.set_show_text(True)
        self.upd_progress.set_size_request(300, -1)
        self.upd_progress.set_no_show_all(True)
        box.pack_start(self.upd_spinner, False, False, 10)
        box.pack_start(self.upd_label, False, False, 5)
        box.pack_start(self.upd_progress, False, False, 10)
        self.stack.add_named(box, "update_check")

    def _check_for_update(self):
        last_check = _config.get("last_update_check", 0)
        if UPDATE_CHECK_INTERVAL > 0 and time.time() - last_check < UPDATE_CHECK_INTERVAL:
            self._go_menu()
            return

        def task():
            return check_update()

        def cb(r):
            # Save last check time via daemon
            save_config_via_daemon({"last_update_check": int(time.time())})

            if r.get("error") == "no_internet":
                self._go_menu()
                return

            if r.get("has_update"):
                self._show_update_dialog(r.get("remote_version", "?"), r.get("local_version", "?"))
            else:
                self._go_menu()

        run_in_thread(task, cb)

    def _show_update_dialog(self, remote_ver: str, local_ver: str):
        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.NONE,
            message_format=None,
        )
        dialog.set_title(t("Update Available"))
        dialog.set_markup(
            f"<big><b>{t('Update Available!')}</b></big>\n\n"
            f"{t('Current: ')}<b>{local_ver}</b>\n"
            f"{t('New: ')}<b>{remote_ver}</b>\n\n"
            f"{t('Update now?')}"
        )
        dialog.add_button(t("Later"), Gtk.ResponseType.NO)
        dialog.add_button(t("Update Now"), Gtk.ResponseType.YES)
        dialog.set_default_response(Gtk.ResponseType.YES)

        def on_response(dialog, response_id):
            dialog.destroy()
            if response_id == Gtk.ResponseType.YES:
                self._start_update()
            else:
                self._go_menu()

        dialog.connect("response", on_response)
        dialog.show()

    def _start_update(self):
        self.upd_label.set_text(t("Updating..."))
        self.upd_progress.set_no_show_all(False)
        self.upd_progress.show()
        self.upd_progress.set_fraction(0.2)
        self._anim_id = GLib.timeout_add(80, self._anim)

        run_in_thread(
            lambda: send_command({"action": "update_all"}),
            self._on_update_done
        )

    def _anim(self):
        c = self.upd_progress.get_fraction()
        if c < 0.85:
            self.upd_progress.set_fraction(c + 0.02)
            return True
        return False

    def _on_update_done(self, result):
        if hasattr(self, "_anim_id"):
            GLib.source_remove(self._anim_id)

        if result.get("status") in ("ok", "partial"):
            self.upd_progress.set_fraction(0.9)
            self.upd_label.set_text(t("Restarting service..."))

            def post_update():
                try:
                    subprocess.run(
                        ["sudo", "systemctl", "restart", "it-aman.service"],
                        capture_output=True, timeout=10
                    )
                except Exception as exc:
                    logger.warning("Daemon restart failed: %s", exc)

                time.sleep(2)

                # Get updated version from daemon
                try:
                    config_result = send_command({"action": "get_config"})
                    if config_result.get("status") == "ok":
                        new_ver = config_result.get("config", {}).get("version", "")
                        if new_ver:
                            logger.info("Updated to version %s", new_ver)
                except Exception:
                    pass

                # Reload data
                load_data()
                return True

            def on_done(success):
                self.upd_progress.set_fraction(1.0)
                updated = result.get("updated", [])
                failed = result.get("failed", [])
                if updated and not failed:
                    msg = f"{t('Update successful! (')}{len(updated)}{t(' files)')}"
                elif failed:
                    msg = f"{t('Partial update (')}{len(updated)}{t(' files, ')}{len(failed)}{t(' failed)')}"
                else:
                    msg = t("Update successful!")
                self.upd_label.set_text(msg)
                GLib.timeout_add(2000, self._go_menu)

            run_in_thread(post_update, on_done)
        else:
            self.upd_label.set_text(t("Update failed - check internet connection"))
            self.upd_progress.set_text("")
            GLib.timeout_add(3000, self._go_menu)

    def _go_menu(self):
        self.upd_spinner.stop()
        self._nav_history.clear()
        self.stack.set_visible_child_name("main_menu")
        return False

    # ── Main Menu ──
    def _build_main_menu(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.get_style_context().add_class("scrolled-window")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(15)
        box.set_margin_bottom(15)

        box.pack_start(lbl(t("IT Aman"), "title-label"), False, False, 0)
        box.pack_start(lbl(t("Printer Management Tool"), "subtitle-label"), False, False, 0)

        # Language toggle button
        lang_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        lang_box.set_halign(Gtk.Align.CENTER)
        lang_label = t("Language")
        if _current_lang == "ar":
            lang_btn_text = f"{lang_label}: العربية | English"
        else:
            lang_btn_text = f"{lang_label}: العربية | English"
        lang_btn = Gtk.Button(label=lang_btn_text)
        lang_btn.get_style_context().add_class("lang-toggle")
        lang_btn.set_can_focus(False)
        lang_btn.connect("clicked", self._toggle_language)
        lang_box.pack_start(lang_btn, False, False, 0)
        box.pack_start(lang_box, False, False, 5)

        # Branch indicator
        branch_ind = self._get_branch_indicator()
        if branch_ind:
            box.pack_start(branch_ind, False, False, 5)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 5)

        box.pack_start(make_menu_card(t("Paper Jam"), t("Paper Jam"), t("Step-by-step instructions + video"),
            lambda _: self._navigate_to("paper_jam")), False, False, 0)
        box.pack_start(make_menu_card(t("Fix"), t("Auto Fix & Repair"), t("Detects and fixes problems"),
            lambda _: self._start_scan()), False, False, 0)
        box.pack_start(make_menu_card(t("Printer"), t("Setup New Printer"), t("Search by branch -> select -> install"),
            lambda _: self._open_setup()), False, False, 0)
        box.pack_start(make_menu_card(t("Thermal"), t("Setup USB Thermal Printer"), t("Cashier / sticker / 80mm / 58mm"),
            lambda _: self._open_thermal()), False, False, 0)
        box.pack_start(make_menu_card(t("Status"), t("Printer Status"), t("Status of printers in your branch"),
            lambda _: self._start_status()), False, False, 0)
        box.pack_start(make_menu_card(t("Branch"), t("Select Branch"), t("Set your current branch for filtering"),
            lambda _: self._open_branch_select()), False, False, 0)

        eb = Gtk.Button()
        eb.get_style_context().add_class("exit-card")
        eb.set_can_focus(False)
        ei = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        ei.set_margin_start(5)
        ei.set_margin_end(5)
        el = lbl("X", "menu-card-icon")
        el.set_xalign(0.0)
        et = lbl(t("Exit"), "menu-card-title")
        et.set_xalign(0.0)
        ei.pack_end(el, False, False, 0)
        ei.pack_start(et, True, True, 0)
        eb.add(ei)
        eb.connect("clicked", lambda _: self.destroy())
        box.pack_start(eb, False, False, 0)

        sw.add(box)
        outer.pack_start(sw, True, True, 0)
        self.stack.add_named(outer, "main_menu")

    # ── Branch Selection Screen ──
    def _build_branch_select_screen(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self._make_top_bar(t("Select Branch")), False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.get_style_context().add_class("scrolled-window")
        self.branch_select_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.branch_select_content.set_margin_top(10)
        self.branch_select_content.set_margin_bottom(15)
        sw.add(self.branch_select_content)
        outer.pack_start(sw, True, True, 0)
        self.stack.add_named(outer, "branch_select")

    def _open_branch_select(self):
        self._navigate_to("branch_select")
        _clear(self.branch_select_content)

        # Reload data to get latest branches
        load_data()

        self.branch_select_content.pack_start(
            lbl(t("Select your branch:"), "status-msg"), False, False, 5)

        # Search entry
        search = Gtk.Entry()
        search.get_style_context().add_class("search-entry")
        search.set_placeholder_text(t("Type branch name..."))
        search.set_hexpand(True)
        search.set_margin_start(40)
        search.set_margin_end(40)
        self.branch_select_content.pack_start(search, False, False, 5)

        results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.branch_select_content.pack_start(results_box, True, True, 0)

        def on_search(entry):
            query = entry.get_text() if entry else ""
            _clear(results_box)
            branches = search_branches(query)
            self._show_branch_list(results_box, branches)

        search.connect("changed", on_search)
        search.connect("activate", on_search)

        # Show all branches initially
        branches = _all_data.get("branches", [])
        self._show_branch_list(results_box, branches)

        self.branch_select_content.show_all()
        search.grab_focus()

    def _show_branch_list(self, container: Gtk.Box, branches: list):
        _clear(container)
        global _current_branch_id

        if not branches:
            container.pack_start(lbl(t("No branches found in data"), "status-msg"), False, False, 10)
            container.show_all()
            return

        for branch in branches:
            bid = branch.get("branch_id", "")
            bname = branch.get("branch_name", "")
            pcount = len(branch.get("printers", []))
            is_current = (bid == _current_branch_id)

            b = Gtk.Button()
            b.get_style_context().add_class("branch-card")
            if is_current:
                b.get_style_context().add_class("branch-card-active")
            b.set_can_focus(False)
            ib = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            ib.set_margin_start(5)
            ib.set_margin_end(5)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)
            prefix = t("(Current) ") if is_current else ""
            n = lbl(f"{prefix}{bname}", "printer-name")
            n.set_xalign(0.0)
            d = lbl(f"{pcount} {t('printer(s)')}", "printer-status")
            d.set_xalign(0.0)
            info.pack_start(n, False, False, 0)
            info.pack_start(d, False, False, 0)
            ib.pack_start(info, True, True, 0)
            if not is_current:
                ib.pack_end(lbl("<", "menu-card-title"), False, False, 0)
            b.add(ib)

            def select_branch(widget, br=branch):
                self._set_current_branch(br)

            if not is_current:
                b.connect("clicked", select_branch)
            container.pack_start(b, False, False, 0)

        container.show_all()

    def _set_current_branch(self, branch: dict):
        global _current_branch_id
        bid = branch.get("branch_id", "")
        bname = branch.get("branch_name", "")

        # Save via daemon
        result = send_command({"action": "set_branch", "branch_id": bid})
        if result.get("status") == "ok":
            _current_branch_id = bid
            _config["current_branch_id"] = bid

            # Show confirmation
            dialog = Gtk.MessageDialog(
                parent=self,
                flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                message_format=f"{t('Branch set to: ')}{bname}",
            )
            dialog.connect("response", lambda d, r: d.destroy())
            dialog.show()

            # Go back to main menu
            self._go_back_menu()
        else:
            # Show error
            dialog = Gtk.MessageDialog(
                parent=self,
                flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                message_format=f"{t('Failed to set branch: ')}{result.get('message', 'Unknown error')}",
            )
            dialog.connect("response", lambda d, r: d.destroy())
            dialog.show()

    # ── Paper Jam Screen ──
    def _build_paper_jam_screen(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self._make_top_bar(t("Paper Jam")), False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.get_style_context().add_class("scrolled-window")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(15)

        for i, txt_key in enumerate([
            "Turn off the printer and unplug the power cable immediately",
            "Open the paper access doors",
            "Pull the jammed paper slowly with both hands",
            "Do not use excessive force or sharp tools",
        ], 1):
            card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            card.get_style_context().add_class("instruction-card")
            n = lbl(f"{i}.", "instruction-number")
            n.set_width_chars(3)
            n.set_xalign(0.5)
            t_lbl = lbl(t(txt_key), "instruction-text")
            t_lbl.set_xalign(0.0)
            t_lbl.set_line_wrap(True)
            t_lbl.set_max_width_chars(50)
            card.pack_start(n, False, False, 0)
            card.pack_start(t_lbl, True, True, 0)
            box.pack_start(card, False, False, 0)

        box.pack_start(Gtk.Box(), False, False, 10)
        box.pack_start(lbl(t("Tutorial Videos"), "section-header"), False, False, 5)
        box.pack_start(btn(t("Video - Google Drive"), "btn-primary",
            lambda _: webbrowser.open(VIDEO_GOOGLE_DRIVE)), False, False, 0)
        box.pack_start(btn(t("Video - Dropbox"), "btn-primary",
            lambda _: webbrowser.open(VIDEO_DROPBOX)), False, False, 0)

        sw.add(box)
        outer.pack_start(sw, True, True, 0)
        self.stack.add_named(outer, "paper_jam")

    # ── Smart Fix Screen ──
    def _build_fix_screen(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self._make_top_bar(t("Auto Fix & Repair")), False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.get_style_context().add_class("scrolled-window")
        self.fix_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.fix_box.set_margin_top(10)
        self.fix_box.set_margin_bottom(15)
        self.fix_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.fix_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        self.fix_box.pack_start(self.fix_content, True, True, 0)
        sw.add(self.fix_box)
        outer.pack_start(sw, True, True, 0)
        self.stack.add_named(outer, "smart_fix")

    def _start_scan(self):
        self._navigate_to("smart_fix")
        _clear(self.fix_content)

        # Use cache if fresh
        cached = get_cached_scan()
        if cached:
            self._show_fix_list(cached)
            return

        sp = Gtk.Spinner()
        sp.set_size_request(50, 50)
        sp.start()
        self.fix_content.pack_start(lbl(t("Scanning printers..."), "spinner-label"), False, False, 5)
        self.fix_content.pack_start(sp, False, False, 10)
        self.fix_content.show_all()

        def task():
            return send_command({"action": "scan"})
        def cb(r):
            if r.get("status") == "ok":
                store_scan_cache(r)
            self._show_fix_list(r)
        run_in_thread(task, cb)

    def _show_fix_list(self, result):
        _clear(self.fix_content)
        if result.get("status") != "ok":
            e = lbl(result.get("message", t("Error occurred")), "error-msg")
            e.set_line_wrap(True)
            self.fix_content.pack_start(e, True, True, 10)
            self.fix_content.show_all()
            return

        printers = result.get("printers", [])
        if not printers:
            self.fix_content.pack_start(lbl(t("No printers found for your branch"), "status-msg"), True, True, 10)
            self.fix_content.show_all()
            return

        # Show branch filter info
        branch_filter = result.get("summary", {}).get("branch_filter", "ALL")
        if branch_filter != "ALL":
            self.fix_content.pack_start(
                lbl(f"{t('Showing printers for branch: ')}{branch_filter}", "branch-indicator"),
                False, False, 5)

        self.fix_content.pack_start(lbl(t("Select a printer to fix:"), "status-msg"), False, False, 5)

        for p in printers:
            name = p.get("name", "?")
            state = p.get("state", "")
            status = p.get("status", "")
            jobs = p.get("jobs", 0)

            if status == "Online":
                icon, st, cls = "OK", t("Online"), "printer-card-ok"
            elif status == "Not Installed":
                icon, st, cls = "?", t("Not Installed"), "printer-card-not-installed"
            elif state == "disabled" or status == "CUPS Issue":
                icon, st, cls = "X", t("Stopped"), "printer-card-error"
            elif status in ("Network Issue", "Error", "Stuck Jobs"):
                icon, st, cls = "!", f"{t('Problem: ')}{status}", "printer-card-warning"
            else:
                icon, st, cls = "?", f"{t('Unknown (')}{status})", "printer-card-warning"

            # Don't show "fix" button for printers that are already OK
            if status == "Online":
                b = Gtk.Button()
                b.get_style_context().add_class("printer-card")
                b.get_style_context().add_class(cls)
                b.set_can_focus(False)
                ib = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                ib.pack_start(lbl(f"[{icon}] {name}", "printer-name"), False, False, 0)
                ib.pack_start(lbl(st, "printer-status"), False, False, 0)
                b.add(ib)
                b.set_sensitive(False)  # No action needed for online printers
                self.fix_content.pack_start(b, False, False, 0)
                continue

            b = Gtk.Button()
            b.get_style_context().add_class("printer-card")
            b.get_style_context().add_class(cls)
            b.set_can_focus(False)
            ib = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            ib.pack_start(lbl(f"[{icon}] {name}", "printer-name"), False, False, 0)
            detail_parts = [st]
            if jobs > 0:
                detail_parts.append(f"{jobs} {t('stuck jobs')}")
            ib.pack_start(lbl(" | ".join(detail_parts), "printer-status"), False, False, 0)
            b.add(ib)

            printer_ip = self._find_printer_ip(name, p)

            def fix_it(widget, pn=name, pip=printer_ip):
                self._do_fix(pn, pip)
            b.connect("clicked", fix_it)
            self.fix_content.pack_start(b, False, False, 0)

        self.fix_content.show_all()

    def _find_printer_ip(self, name: str, printer_data: dict = None) -> str:
        """Find printer IP from data.json or CUPS."""
        # First check the scan result data
        if printer_data and printer_data.get("ip"):
            return printer_data["ip"]

        # Then check data.json
        for branch in _all_data.get("branches", []):
            for p in branch.get("printers", []):
                if p.get("name", "") == name:
                    return p.get("ip", "")
        return ""

    def _do_fix(self, printer_name: str, printer_ip: str):
        _clear(self.fix_content)
        self.fix_content.pack_start(lbl(f"{t('Fixing ')}{printer_name}...", "section-header"), False, False, 5)

        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        steps_box.set_margin_top(10)
        steps_box.set_margin_start(20)
        self.fix_content.pack_start(steps_box, False, False, 0)
        self.fix_content.show_all()

        def task():
            return send_command({
                "action": "fix",
                "printer_name": printer_name,
                "printer_ip": printer_ip,
            })

        def cb(result):
            steps = result.get("steps", [])
            for s in steps:
                st = "step-success" if s.get("status") == "success" else "step-failed"
                ico = "[OK]" if s.get("status") == "success" else "[X]"
                l = lbl(f"{ico} {s.get('step', '')} -- {s.get('message', '')}", st)
                l.set_xalign(0.0)
                steps_box.pack_start(l, False, False, 0)

            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(10)
            steps_box.pack_start(sep, False, False, 0)

            if result.get("status") == "ok":
                steps_box.pack_start(lbl(t("Fixed successfully!"), "step-success"), False, False, 5)
            else:
                msg = result.get("message", t("Fix failed"))
                steps_box.pack_start(lbl(f"{t('Failed: ')}{msg}", "step-failed"), False, False, 5)

            steps_box.show_all()

        run_in_thread(task, cb)

    # ── Setup Network Printer Screen (Branch-based only, NO manual IP) ──
    def _build_setup_screen(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self._make_top_bar(t("Setup New Printer")), False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.get_style_context().add_class("scrolled-window")
        self.setup_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.setup_box.set_margin_top(10)
        self.setup_box.set_margin_bottom(15)
        self.setup_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.setup_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        self.setup_box.pack_start(self.setup_content, True, True, 0)
        sw.add(self.setup_box)
        outer.pack_start(sw, True, True, 0)
        self.stack.add_named(outer, "setup_printer")

    def _open_setup(self):
        self._navigate_to("setup_printer")
        self._setup_show_search()

    def _setup_show_search(self):
        _clear(self.setup_content)

        # Reload data to get latest
        load_data()

        self.setup_content.pack_start(lbl(t("Search for your branch:"), "status-msg"), False, False, 5)

        # ── Network Scan button (merged from Printers-Tools) ──────────────
        scan_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scan_row.set_halign(Gtk.Align.CENTER)
        scan_row.pack_start(
            btn(t("Scan Network for Printers"), "btn-primary",
                lambda _: self._run_network_scan()),
            False, False, 0)
        self.setup_content.pack_start(scan_row, False, False, 6)
        sep_ns = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep_ns.set_margin_bottom(4)
        self.setup_content.pack_start(sep_ns, False, False, 0)

        self.search_entry = Gtk.Entry()
        self.search_entry.get_style_context().add_class("search-entry")
        self.search_entry.set_placeholder_text(t("Type branch name (e.g. MEGA)"))
        self.search_entry.set_hexpand(True)
        self.search_entry.set_margin_start(40)
        self.search_entry.set_margin_end(40)
        self.search_entry.connect("changed", self._on_search)
        self.search_entry.connect("activate", self._on_search)
        self.setup_content.pack_start(self.search_entry, False, False, 5)

        self.search_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.setup_content.pack_start(self.search_results, True, True, 0)

        self._show_search_results(_all_data.get("branches", []))
        self.setup_content.show_all()
        self.search_entry.grab_focus()

    def _on_search(self, entry=None):
        query = entry.get_text() if entry else ""
        results = search_branches(query)
        self._show_search_results(results)

    def _show_search_results(self, branches: list):
        _clear(self.search_results)
        query = self.search_entry.get_text() if hasattr(self, 'search_entry') else ""

        if not branches:
            if query:
                self.search_results.pack_start(lbl(t("No results found"), "status-msg"), False, False, 10)
            else:
                self.search_results.pack_start(lbl(t("No branches registered in data file"), "status-msg"), False, False, 10)
            self.search_results.show_all()
            return

        for branch in branches:
            bname = branch.get("branch_name", "")
            pcount = len(branch.get("printers", []))

            b = Gtk.Button()
            b.get_style_context().add_class("branch-card")
            b.set_can_focus(False)
            ib = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            ib.set_margin_start(5)
            ib.set_margin_end(5)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)
            n = lbl(f"{bname}", "printer-name")
            n.set_xalign(0.0)
            d = lbl(f"{pcount} {t('printer(s)')}", "printer-status")
            d.set_xalign(0.0)
            info.pack_start(n, False, False, 0)
            info.pack_start(d, False, False, 0)
            ib.pack_start(info, True, True, 0)
            ib.pack_end(lbl("<", "menu-card-title"), False, False, 0)
            b.add(ib)

            def select_branch(widget, br=branch):
                self._show_branch_printers(br)
            b.connect("clicked", select_branch)
            self.search_results.pack_start(b, False, False, 0)

        self.search_results.show_all()


    def _run_network_scan(self):
        """Active subnet scan (merged from Printers-Tools). Shows discovered printers."""
        _clear(self.setup_content)
        sp = Gtk.Spinner()
        sp.set_size_request(44, 44)
        sp.start()
        self.setup_content.pack_start(
            lbl(t("Scanning network for printers..."), "spinner-label"), False, False, 6)
        self.setup_content.pack_start(sp, False, False, 6)
        self.setup_content.pack_start(
            lbl(t("This may take up to 30 seconds"), "status-msg"), False, False, 4)
        self.setup_content.pack_start(
            self._make_back_button(lambda _: self._setup_show_search()), False, False, 5)
        self.setup_content.show_all()
        def task():
            return send_command({"action": "network_scan"})
        def cb(result):
            sp.stop()
            sp.set_visible(False)
            self._show_network_scan_results(result)
        run_in_thread(task, cb)

    def _show_network_scan_results(self, result):
        _clear(self.setup_content)
        self.setup_content.pack_start(
            self._make_back_button(lambda _: self._setup_show_search()), False, False, 5)
        if result.get("status") != "ok":
            self.setup_content.pack_start(
                lbl(result.get("message", t("Scan failed")), "error-msg"), False, False, 10)
            self.setup_content.pack_start(
                btn(t("Try Again"), "btn-primary", lambda _: self._run_network_scan()),
                False, False, 5)
            self.setup_content.show_all()
            return
        printers = result.get("printers", [])
        local_ip = result.get("local_ip", "")
        if local_ip:
            self.setup_content.pack_start(
                lbl(t_fmt("Your IP: {} — subnet scanned", local_ip), "status-msg"), False, False, 4)
        if not printers:
            self.setup_content.pack_start(
                lbl(t("No network printers found"), "status-msg"), False, False, 8)
            self.setup_content.pack_start(
                btn(t("Scan Again"), "btn-primary", lambda _: self._run_network_scan()),
                False, False, 8)
            self.setup_content.show_all()
            return
        self.setup_content.pack_start(
            lbl(t_fmt("Found {} printer(s) on the network:", len(printers)), "section-header"),
            False, False, 6)
        for p in printers:
            ip    = p.get("ip", "")
            uri   = p.get("uri", f"ipp://{ip}/ipp/print")
            model = p.get("model", f"Printer @ {ip}")
            card = Gtk.Button()
            card.get_style_context().add_class("printer-card")
            ib = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            ib.pack_start(lbl(model, "printer-name"), False, False, 0)
            ib.pack_start(lbl(f"IP: {ip}   URI: {uri}", "printer-detail"), False, False, 0)
            card.add(ib)
            def _install(w, _ip=ip, _uri=uri, _model=model):
                self._install_network_printer_from_scan(_ip, _uri, _model)
            card.connect("clicked", _install)
            self.setup_content.pack_start(card, False, False, 3)
        self.setup_content.show_all()

    def _install_network_printer_from_scan(self, ip: str, uri: str, model: str):
        _clear(self.setup_content)
        sp = Gtk.Spinner()
        sp.set_size_request(40, 40)
        sp.start()
        self.setup_content.pack_start(
            lbl(t_fmt("Installing {}...", model), "spinner-label"), False, False, 5)
        self.setup_content.pack_start(sp, False, False, 6)
        self.setup_content.pack_start(
            self._make_back_button(lambda _: self._run_network_scan()), False, False, 5)
        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        steps_box.set_margin_start(20)
        steps_box.set_margin_top(8)
        self.setup_content.pack_start(steps_box, False, False, 0)
        self.setup_content.show_all()
        def task():
            return send_command({"action": "setup_printer", "name": "printer-FS",
                                  "ip": ip, "model": model})
        def cb(result):
            sp.stop()
            sp.set_visible(False)
            for s in result.get("steps", []):
                st  = "step-success" if s.get("status") in ("success", "warning") else "step-failed"
                ico = "[OK]" if s.get("status") in ("success", "warning") else "[X]"
                row = lbl(f"{ico} {s.get(chr(39)+'step'+chr(39),chr(39)+chr(39))} — {s.get(chr(39)+'message'+chr(39),chr(39)+chr(39))}", st)
                row.set_xalign(0.0)
                steps_box.pack_start(row, False, False, 0)
            steps_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 6)
            if result.get("status") == "ok":
                steps_box.pack_start(
                    lbl(t_fmt("{} installed successfully!", model), "step-success"), False, False, 5)
                global _scan_cache, _scan_cache_time
                _scan_cache = None
                _scan_cache_time = 0.0
            else:
                steps_box.pack_start(
                    lbl(t_fmt("Failed: {}", result.get("message", "")), "step-failed"), False, False, 5)
            steps_box.show_all()
        run_in_thread(task, cb)

    def _show_branch_printers(self, branch):
        _clear(self.setup_content)
        bname = branch.get("branch_name", "")

        self.setup_content.pack_start(
            self._make_back_button(lambda _: self._setup_show_search()), False, False, 5)

        printers = branch.get("printers", [])
        if not printers:
            self.setup_content.pack_start(lbl(t("No printers registered for this branch"), "status-msg"), True, True, 10)
            self.setup_content.show_all()
            return

        self.setup_content.pack_start(lbl(f"{t('Select printer (')}{bname}{t('):')}", "status-msg"), False, False, 5)

        for p in printers:
            pname = p.get("name", "")
            pmodel = p.get("model", "")
            pip = p.get("ip", "")

            b = Gtk.Button()
            b.get_style_context().add_class("printer-card")
            b.set_can_focus(False)
            ib = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            ib.pack_start(lbl(f"{pname}", "printer-name"), False, False, 0)
            detail = pmodel if pmodel else pip if pip else t("No details")
            ib.pack_start(lbl(detail, "printer-status"), False, False, 0)
            b.add(ib)

            def install_it(widget, pr=p, br=branch):
                self._do_install(pr, br)
            b.connect("clicked", install_it)
            self.setup_content.pack_start(b, False, False, 0)

        self.setup_content.show_all()

    def _do_install(self, printer, branch):
        _clear(self.setup_content)

        pname = printer.get("name", "")
        pip = printer.get("ip", "")
        pmodel = printer.get("model", "")
        pdriver = printer.get("driver", "")

        self.setup_content.pack_start(lbl(f"{t('Installing ')}{pname}...", "section-header"), False, False, 5)
        self.setup_content.pack_start(
            self._make_back_button(lambda _: self._setup_show_search()), False, False, 5)

        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        steps_box.set_margin_top(10)
        steps_box.set_margin_start(20)
        self.setup_content.pack_start(steps_box, False, False, 0)
        self.setup_content.show_all()

        def task():
            driver_path = ""
            if pdriver:
                dp = os.path.join(DRIVERS_DIR, pdriver)
                if os.path.isfile(dp):
                    driver_path = dp
                else:
                    content = fetch_github(f"drivers/{pdriver}")
                    if content:
                        os.makedirs(DRIVERS_DIR, exist_ok=True)
                        with open(dp, "w", encoding="utf-8") as f:
                            f.write(content)
                        driver_path = dp

            return send_command({
                "action": "setup_printer",
                "name": pname,
                "ip": pip,
                "model": pmodel,
                "driver_path": driver_path,
            })

        def cb(result):
            steps = result.get("steps", [])
            for s in steps:
                st = "step-success" if s.get("status") in ("success", "skipped") else "step-failed"
                ico = "[OK]" if s.get("status") in ("success", "skipped") else "[X]"
                l = lbl(f"{ico} {s.get('step', '')} -- {s.get('message', '')}", st)
                l.set_xalign(0.0)
                steps_box.pack_start(l, False, False, 0)

            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(10)
            steps_box.pack_start(sep, False, False, 0)

            if result.get("status") == "ok":
                steps_box.pack_start(lbl(f"{t('Installing ')}{pname}{t(' installed successfully!')}", "step-success"), False, False, 5)
            else:
                steps_box.pack_start(lbl(t("Installation failed"), "step-failed"), False, False, 5)

            steps_box.show_all()

        run_in_thread(task, cb)

    # ── Thermal USB Printer Screen ──
    def _build_thermal_screen(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self._make_top_bar(t("USB Thermal Printer Setup")), False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.get_style_context().add_class("scrolled-window")
        self.thermal_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.thermal_box.set_margin_top(10)
        self.thermal_box.set_margin_bottom(15)
        self.thermal_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.thermal_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        self.thermal_box.pack_start(self.thermal_content, True, True, 0)
        sw.add(self.thermal_box)
        outer.pack_start(sw, True, True, 0)
        self.stack.add_named(outer, "setup_thermal")

    def _open_thermal(self):
        self._navigate_to("setup_thermal")
        self._thermal_show_main()

    def _thermal_show_main(self):
        _clear(self.thermal_content)

        self.thermal_content.pack_start(
            lbl(t("Step 1: Connect the printer via USB"), "status-msg"), False, False, 4)
        self.thermal_content.pack_start(
            lbl(t("Step 2: Choose your printer brand below"), "status-msg"), False, False, 2)
        self.thermal_content.pack_start(
            lbl(t("Step 3: The driver will download and install automatically"), "status-msg"), False, False, 2)
        self.thermal_content.pack_start(Gtk.Box(), False, False, 6)

        # ── Brand cards (XP-80 / SPRT) ──────────────────────────────────
        brand_label = lbl(t("Select thermal printer brand:"), "section-header")
        brand_label.set_xalign(0.0)
        self.thermal_content.pack_start(brand_label, False, False, 4)

        cards_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        cards_row.set_halign(Gtk.Align.CENTER)

        self._selected_thermal_brand = "xprinter"

        brand_btns = {}
        for brand_id, brand_name, brand_desc in [
            ("xprinter", "X-Printer", "XP-80 Series — USB"),
            ("sprt",     "SPRT",      "80mm Thermal — USB"),
        ]:
            card = Gtk.Button()
            card.get_style_context().add_class("printer-card")
            if brand_id == self._selected_thermal_brand:
                card.get_style_context().add_class("printer-card-ok")
            ib = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            ib.set_halign(Gtk.Align.CENTER)
            ib.set_margin_top(8)
            ib.set_margin_bottom(8)
            ib.set_margin_start(16)
            ib.set_margin_end(16)
            ib.pack_start(lbl(brand_name, "printer-name"), False, False, 0)
            ib.pack_start(lbl(brand_desc, "printer-detail"), False, False, 0)
            card.add(ib)
            card.set_size_request(160, 70)
            brand_btns[brand_id] = card

            def _pick(widget, bid=brand_id):
                self._selected_thermal_brand = bid
                for k, v in brand_btns.items():
                    if k == bid:
                        v.get_style_context().add_class("printer-card-ok")
                    else:
                        v.get_style_context().remove_class("printer-card-ok")

            card.connect("clicked", _pick)
            cards_row.pack_start(card, False, False, 0)

        self.thermal_content.pack_start(cards_row, False, False, 8)

        # Paper size selection (for SPRT / generic)
        self.thermal_content.pack_start(
            lbl(t("Paper size (for SPRT / generic):"), "status-msg"), False, False, 4)
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        size_box.set_halign(Gtk.Align.CENTER)
        self.selected_thermal_size = "80mm"
        size_btns = {}
        for size_key in ["58mm", "76mm", "80mm", "112mm"]:
            b = Gtk.Button(label=size_key)
            b.get_style_context().add_class("size-card")
            if size_key == self.selected_thermal_size:
                b.get_style_context().add_class("size-card-active")
            b.set_can_focus(False)
            b.set_size_request(62, 36)
            size_btns[size_key] = b

            def _pick_sz(widget, sk=size_key):
                self.selected_thermal_size = sk
                for k, v in size_btns.items():
                    if k == sk:
                        v.get_style_context().add_class("size-card-active")
                    else:
                        v.get_style_context().remove_class("size-card-active")

            b.connect("clicked", _pick_sz)
            size_box.pack_start(b, False, False, 0)
        self.thermal_content.pack_start(size_box, False, False, 8)

        # Main install button
        self.thermal_content.pack_start(
            btn(t("Install Selected Brand Driver"), "btn-success",
                lambda _: self._thermal_detect_and_install()),
            False, False, 4)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(10)
        sep.set_margin_bottom(6)
        self.thermal_content.pack_start(sep, False, False, 0)

        # Manual driver install (generic SPRT/CUPS)
        self.thermal_content.pack_start(
            lbl(t("Advanced: install generic thermal driver (CUPS/SPRT)"), "status-msg"),
            False, False, 2)
        self.thermal_content.pack_start(
            btn(t("Install Generic Driver"), "btn-primary",
                lambda _: self._thermal_install_driver()),
            False, False, 4)

        # Driver status
        driver_status = self._check_thermal_driver_status()
        if driver_status:
            self.thermal_content.pack_start(
                lbl(t_fmt("Driver status: {} PPD file(s) installed", driver_status), "step-success"),
                False, False, 3)

        self.thermal_content.show_all()

    def _thermal_detect_and_install(self):
        """Detect USB printers then call install_thermal_brand for the selected brand."""
        _clear(self.thermal_content)
        brand = getattr(self, "_selected_thermal_brand", "xprinter")
        brand_label = "X-Printer XP-80" if brand == "xprinter" else "SPRT 80mm"

        sp = Gtk.Spinner()
        sp.set_size_request(40, 40)
        sp.start()
        self.thermal_content.pack_start(
            lbl(t_fmt("Detecting USB printers for {}...", brand_label), "spinner-label"),
            False, False, 5)
        self.thermal_content.pack_start(sp, False, False, 6)
        self.thermal_content.pack_start(
            self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)
        self.thermal_content.show_all()

        def task():
            return send_command({"action": "detect_usb_printers"})

        def cb(result):
            if result.get("status") != "ok":
                _clear(self.thermal_content)
                self.thermal_content.pack_start(
                    lbl(result.get("message", t("No USB printers found")), "error-msg"),
                    False, False, 10)
                self.thermal_content.pack_start(
                    self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)
                self.thermal_content.show_all()
                return

            usb_list = result.get("usb_printers", [])
            if not usb_list:
                _clear(self.thermal_content)
                self.thermal_content.pack_start(
                    lbl(t("No USB printer detected. Connect the printer and try again."), "status-msg"),
                    False, False, 10)
                self.thermal_content.pack_start(
                    btn(t("Try Again"), "btn-primary", lambda _: self._thermal_detect_and_install()),
                    False, False, 5)
                self.thermal_content.pack_start(
                    self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)
                self.thermal_content.show_all()
                return

            # More than one USB printer — show selection
            if len(usb_list) > 1:
                self._thermal_pick_usb(usb_list, brand)
            else:
                self._thermal_run_brand_install(brand, usb_list[0]["uri"])

        run_in_thread(task, cb)

    def _thermal_pick_usb(self, usb_list: list, brand: str):
        """Show USB list for user to pick one, then install."""
        _clear(self.thermal_content)
        self.thermal_content.pack_start(
            lbl(t("Multiple USB printers found — pick one:"), "section-header"),
            False, False, 6)
        self.thermal_content.pack_start(
            self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)

        for i, up in enumerate(usb_list):
            uri = up.get("uri", "")
            desc = up.get("description", up.get("make_and_model", "Unknown"))
            b = Gtk.Button()
            b.get_style_context().add_class("printer-card")
            ib = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            ib.pack_start(lbl(f"USB #{i+1} — {desc}", "printer-name"), False, False, 0)
            ib.pack_start(lbl(uri, "printer-detail"), False, False, 0)
            b.add(ib)

            def _pick(w, u=uri):
                self._thermal_run_brand_install(brand, u)

            b.connect("clicked", _pick)
            self.thermal_content.pack_start(b, False, False, 2)

        self.thermal_content.show_all()

    def _thermal_run_brand_install(self, brand: str, usb_uri: str):
        """Send install_thermal_brand to daemon and show progress."""
        _clear(self.thermal_content)
        brand_label = "X-Printer XP-80" if brand == "xprinter" else "SPRT 80mm"
        sp = Gtk.Spinner()
        sp.set_size_request(40, 40)
        sp.start()
        self.thermal_content.pack_start(
            lbl(t_fmt("Installing {}... this may take a minute", brand_label), "spinner-label"),
            False, False, 5)
        self.thermal_content.pack_start(sp, False, False, 6)
        self.thermal_content.pack_start(
            self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)

        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        steps_box.set_margin_top(10)
        steps_box.set_margin_start(20)
        self.thermal_content.pack_start(steps_box, False, False, 0)
        self.thermal_content.show_all()

        def task():
            return send_command({
                "action": "install_thermal_brand",
                "brand": brand,
                "usb_uri": usb_uri,
            })

        def cb(result):
            sp.stop()
            sp.set_visible(False)
            for s in result.get("steps", []):
                st = "step-success" if s.get("status") in ("success", "skipped") else "step-failed"
                ico = "[OK]" if s.get("status") in ("success", "skipped") else "[X]"
                row = lbl(f"{ico} {s.get('step', '')} — {s.get('message', '')}", st)
                row.set_xalign(0.0)
                steps_box.pack_start(row, False, False, 0)

            steps_box.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 6)

            if result.get("status") == "ok":
                steps_box.pack_start(
                    lbl(result.get("message", t("Thermal printer installed successfully!")), "step-success"),
                    False, False, 5)
            else:
                steps_box.pack_start(
                    lbl(t_fmt("Failed: {}", result.get("message", "")), "step-failed"),
                    False, False, 5)

            # Invalidate scan cache after new printer
            global _scan_cache, _scan_cache_time
            _scan_cache = None
            _scan_cache_time = 0.0
            steps_box.show_all()

        run_in_thread(task, cb)

    def _check_thermal_driver_status(self) -> str:
        """Check if thermal printer driver PPD files are installed."""
        ppd_dir = "/usr/share/cups/model/printer"
        found = []
        for fname in ["58mmSeries.ppd.gz", "76mmSeries.ppd.gz", "80mmSeries.ppd.gz", "112mmSeries.ppd.gz", "T5.ppd.gz"]:
            if os.path.isfile(os.path.join(ppd_dir, fname)):
                found.append(fname)
        if found:
            return f"{len(found)}{t(' PPD file(s) installed')}"
        return ""

    def _thermal_install_driver(self):
        _clear(self.thermal_content)

        self.thermal_content.pack_start(lbl(t("Installing thermal printer driver..."), "spinner-label"), False, False, 5)
        self.thermal_content.pack_start(self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)

        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        steps_box.set_margin_top(10)
        steps_box.set_margin_start(20)
        self.thermal_content.pack_start(steps_box, False, False, 0)
        self.thermal_content.show_all()

        def task():
            return send_command({"action": "install_thermal_driver"})

        def cb(result):
            steps = result.get("steps", [])
            for s in steps:
                st = "step-success" if s.get("status") in ("success", "partial") else "step-failed"
                ico = "[OK]" if s.get("status") in ("success", "partial") else "[X]"
                l = lbl(f"{ico} {s.get('step', '')} -- {s.get('message', '')}", st)
                l.set_xalign(0.0)
                steps_box.pack_start(l, False, False, 0)

            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(10)
            steps_box.pack_start(sep, False, False, 0)

            if result.get("status") == "ok":
                steps_box.pack_start(lbl(t("Driver installed successfully!"), "step-success"), False, False, 5)
            else:
                steps_box.pack_start(lbl(f"{t('Failed: ')}{result.get('message', '')}", "step-failed"), False, False, 5)

            steps_box.show_all()

        run_in_thread(task, cb)

    def _thermal_detect_usb(self):
        _clear(self.thermal_content)

        sp = Gtk.Spinner()
        sp.set_size_request(50, 50)
        sp.start()
        self.thermal_content.pack_start(lbl(t("Searching for USB printers..."), "spinner-label"), False, False, 5)
        self.thermal_content.pack_start(sp, False, False, 10)
        self.thermal_content.pack_start(self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)
        self.thermal_content.show_all()

        def task():
            return send_command({"action": "detect_usb_printers"})

        def cb(result):
            self._thermal_show_usb_list(result)

        run_in_thread(task, cb)

    def _thermal_show_usb_list(self, result):
        _clear(self.thermal_content)

        self.thermal_content.pack_start(self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)

        if result.get("status") != "ok":
            e = lbl(result.get("message", t("Error occurred")), "error-msg")
            e.set_line_wrap(True)
            self.thermal_content.pack_start(e, True, True, 10)
            self.thermal_content.show_all()
            return

        usb_printers = result.get("usb_printers", [])

        if not usb_printers:
            self.thermal_content.pack_start(lbl(t("No USB printers found"), "status-msg"), False, False, 10)
            self.thermal_content.pack_start(lbl(t("Make sure the printer is connected and powered on"), "status-msg"), False, False, 2)
            self.thermal_content.pack_start(Gtk.Box(), False, False, 5)
            self.thermal_content.pack_start(btn(t("Search Again"), "btn-primary",
                lambda _: self._thermal_detect_usb()), False, False, 5)
            self.thermal_content.show_all()
            return

        self.thermal_content.pack_start(lbl(f"{t('Found ')}{len(usb_printers)}{t(' USB printer(s):')}", "status-msg"), False, False, 5)

        for i, up in enumerate(usb_printers):
            uri = up.get("uri", "")
            description = up.get("description", up.get("make_and_model", "Unknown"))
            is_configured = up.get("configured", False)

            b = Gtk.Button()
            b.get_style_context().add_class("printer-card")
            if is_configured:
                b.get_style_context().add_class("printer-card-ok")
            b.set_can_focus(False)
            ib = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            icon = "[OK]" if is_configured else "[USB]"
            ib.pack_start(lbl(f"{icon} USB Printer #{i+1}", "printer-name"), False, False, 0)
            ib.pack_start(lbl(description, "printer-status"), False, False, 0)
            ib.pack_start(lbl(uri, "printer-detail"), False, False, 0)
            if is_configured:
                ib.pack_start(lbl(t("Already configured"), "step-success"), False, False, 0)
            b.add(ib)

            def install_thermal(widget, turi=uri, idx=i):
                printer_name = f"Thermal_USB_{idx+1}"
                self._thermal_do_setup(printer_name, turi)

            b.connect("clicked", install_thermal)
            self.thermal_content.pack_start(b, False, False, 0)

        self.thermal_content.show_all()

    def _thermal_do_setup(self, printer_name: str, device_uri: str):
        _clear(self.thermal_content)

        size = self.selected_thermal_size
        ppd_file = THERMAL_SIZES.get(size, "80mmSeries.ppd.gz")

        self.thermal_content.pack_start(lbl(f"{t('Installing thermal printer (')}{size})...", "section-header"), False, False, 5)
        self.thermal_content.pack_start(self._make_back_button(lambda _: self._thermal_show_main()), False, False, 5)

        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        steps_box.set_margin_top(10)
        steps_box.set_margin_start(20)
        self.thermal_content.pack_start(steps_box, False, False, 0)
        self.thermal_content.show_all()

        def task():
            return send_command({
                "action": "setup_thermal_usb",
                "printer_name": printer_name,
                "usb_uri": device_uri,
                "ppd_file": ppd_file,
            })

        def cb(result):
            steps = result.get("steps", [])
            for s in steps:
                st = "step-success" if s.get("status") in ("success", "skipped") else "step-failed"
                ico = "[OK]" if s.get("status") in ("success", "skipped") else "[X]"
                l = lbl(f"{ico} {s.get('step', '')} -- {s.get('message', '')}", st)
                l.set_xalign(0.0)
                steps_box.pack_start(l, False, False, 0)

            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(10)
            steps_box.pack_start(sep, False, False, 0)

            if result.get("status") == "ok":
                steps_box.pack_start(lbl(t("Thermal printer installed successfully!"), "step-success"), False, False, 5)
            else:
                steps_box.pack_start(lbl(f"{t('Failed: ')}{result.get('message', '')}", "step-failed"), False, False, 5)

            steps_box.show_all()

        run_in_thread(task, cb)

    # ── Printer Status Screen (Filtered by Branch) ──
    def _build_status_screen(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self._make_top_bar(t("Printer Status")), False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.get_style_context().add_class("scrolled-window")
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.status_box.set_margin_top(10)
        self.status_box.set_margin_bottom(15)
        self.status_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.status_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        self.status_box.pack_start(self.status_content, True, True, 0)
        sw.add(self.status_box)
        outer.pack_start(sw, True, True, 0)
        self.stack.add_named(outer, "printer_status")

    def _start_status(self):
        self._navigate_to("printer_status")
        _clear(self.status_content)

        # Use cache if fresh
        cached = get_cached_scan()
        if cached:
            self._show_status(cached)
            return

        sp = Gtk.Spinner()
        sp.set_size_request(50, 50)
        sp.start()
        self.status_content.pack_start(lbl(t("Scanning..."), "spinner-label"), False, False, 5)
        self.status_content.pack_start(sp, False, False, 10)
        self.status_content.show_all()

        def task():
            return send_command({"action": "scan"})
        def cb(r):
            if r.get("status") == "ok":
                store_scan_cache(r)
            self._show_status(r)
        run_in_thread(task, cb)

    def _show_status(self, result):
        _clear(self.status_content)
        if result.get("status") != "ok":
            e = lbl(result.get("message", t("Error occurred")), "error-msg")
            e.set_line_wrap(True)
            self.status_content.pack_start(e, True, True, 10)
            self.status_content.show_all()
            return

        printers = result.get("printers", [])
        summary = result.get("summary", {})
        total = summary.get("total_printers", len(printers))
        online = summary.get("online_printers", 0)
        offline = summary.get("offline_printers", 0)
        errors = summary.get("error_printers", 0)
        not_installed = summary.get("not_installed_printers", 0)
        branch_filter = summary.get("branch_filter", "ALL")

        # Summary bar
        summary_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
        summary_box.set_margin_start(15)
        summary_box.set_margin_end(15)
        summary_box.set_margin_top(5)
        summary_box.set_margin_bottom(5)
        summary_box.pack_start(lbl(f"{t('Total: ')}{total}", "status-msg"), False, False, 0)
        summary_box.pack_start(lbl(f"{t('Online: ')}{online}", "step-success"), False, False, 0)
        summary_box.pack_start(lbl(f"{t('Offline: ')}{offline}", "step-failed"), False, False, 0)
        summary_box.pack_start(lbl(f"{t('Errors: ')}{errors}", "step-failed"), False, False, 0)
        if not_installed > 0:
            summary_box.pack_start(lbl(f"{t('Not Installed: ')}{not_installed}", "step-pending"), False, False, 0)
        self.status_content.pack_start(summary_box, False, False, 0)

        # Branch filter indicator
        if branch_filter != "ALL":
            self.status_content.pack_start(
                lbl(f"{t('Filtered by branch: ')}{branch_filter}", "branch-indicator"),
                False, False, 5)
        else:
            # Warn if no branch is set
            self.status_content.pack_start(
                lbl(t("WARNING: No branch selected -- showing ALL CUPS printers. Select a branch for proper filtering."), "error-msg"),
                False, False, 5)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_start(15)
        sep.set_margin_end(15)
        self.status_content.pack_start(sep, False, False, 5)

        if not printers:
            self.status_content.pack_start(lbl(t("No printers found for your branch"), "status-msg"), True, True, 10)
            self.status_content.show_all()
            return

        self._status_printer_cards = {}
        for p in printers:
            name = p.get("name", "?")
            state = p.get("state", "")
            status = p.get("status", "")
            jobs = p.get("jobs", 0)

            # Status display
            if status == "Online":
                icon, st, cls = "[OK]", t("Online"), "printer-card-ok"
            elif status == "Not Installed":
                icon, st, cls = "[?]", t("Not Installed"), "printer-card-not-installed"
            elif status in ("Offline", "CUPS Issue"):
                icon, st, cls = "[X]", f"{t('Stopped')} ({status})", "printer-card-error"
            elif status == "Stuck Jobs":
                icon, st, cls = "[!]", f"{t('Stuck Jobs')}: {jobs}", "printer-card-warning"
            elif status in ("Error", "Network Issue"):
                icon, st, cls = "[!]", f"{t('Problem: ')}{status}", "printer-card-warning"
            else:
                icon, st, cls = "[?]", f"{t('Unknown (')}{status})", "printer-card-warning"

            b = Gtk.Button()
            b.get_style_context().add_class("printer-card")
            b.get_style_context().add_class(cls)
            b.set_can_focus(False)
            ib = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            ib.pack_start(lbl(f"{icon} {name}", "printer-name"), False, False, 0)
            ib.pack_start(lbl(st, "printer-status"), False, False, 0)

            detail_lbl = lbl(t("Loading details..."), "printer-detail")
            ib.pack_start(detail_lbl, False, False, 0)

            # Actions
            if status != "Not Installed":
                ab = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                ab.set_margin_top(5)
                ab.pack_start(btn(t("Test Print"), "btn-primary",
                    lambda _, pn=name: self._send_test(pn)), False, False, 0)
                if state == "disabled" or jobs > 0 or status in ("CUPS Issue", "Stuck Jobs", "Error", "Network Issue"):
                    ab.pack_start(btn(t("Fix"), "btn-danger",
                        lambda _, pn=name, pip=self._find_printer_ip(name, p): self._quick_fix(pn, pip)), False, False, 0)
                ib.pack_start(ab, False, False, 0)
            else:
                # Not installed - show setup button
                ib.pack_start(btn(t("Install Printer"), "btn-success",
                    lambda _, pn=name: self._install_from_status(pn)), False, False, 5)

            b.add(ib)
            self.status_content.pack_start(b, False, False, 0)
            self._status_printer_cards[name] = (b, ib, detail_lbl, cls)

        self.status_content.show_all()

        # Fetch real details for each printer
        for p in printers:
            pname = p.get("name", "?")
            if p.get("state", "") != "not_installed":
                self._fetch_printer_detail(pname)

    def _install_from_status(self, printer_name: str):
        """Install a printer that's in data.json but not yet in CUPS."""
        # Find the printer in data.json
        for branch in _all_data.get("branches", []):
            for p in branch.get("printers", []):
                if p.get("name", "") == printer_name:
                    self._navigate_to("setup_printer")
                    self._do_install(p, branch)
                    return

    def _fetch_printer_detail(self, printer_name: str):
        """Fetch real details for a single printer from CUPS."""
        def task():
            return send_command({"action": "get_printer_details", "printer_name": printer_name})

        def cb(result):
            if printer_name not in self._status_printer_cards:
                return

            b, ib, detail_lbl, orig_cls = self._status_printer_cards[printer_name]

            if result.get("status") == "ok":
                uri = result.get("device_uri", "")
                accepting = result.get("accepting", "unknown")
                is_default = result.get("is_default", False)
                real_jobs = result.get("jobs_count", 0)
                real_state = result.get("state", "")
                is_usb = result.get("is_usb", False)

                detail_parts = []
                if is_usb:
                    detail_parts.append(t("USB"))
                if uri:
                    short_uri = uri if len(uri) < 45 else uri[:42] + "..."
                    detail_parts.append(short_uri)
                if not accepting:
                    detail_parts.append(t("Rejecting jobs"))
                if real_jobs > 0:
                    detail_parts.append(f"{real_jobs}{t(' job(s)')}")
                if is_default:
                    detail_parts.append(t("Default"))

                detail_lbl.set_text(" | ".join(detail_parts) if detail_parts else t("Working normally"))

                # Update card style
                b.get_style_context().remove_class(orig_cls)
                if real_state in ("idle", "Online") and real_jobs == 0 and accepting:
                    b.get_style_context().add_class("printer-card-ok")
                elif real_state in ("disabled", "Offline") or not accepting:
                    b.get_style_context().add_class("printer-card-error")
                elif real_jobs > 0:
                    b.get_style_context().add_class("printer-card-warning")
                else:
                    b.get_style_context().add_class("printer-card-warning")
            else:
                detail_lbl.set_text(t("Details unavailable"))

        run_in_thread(task, cb)

    # ── Async Test Print ──
    def _send_test(self, printer_name):
        def task():
            return send_command({"action": "test_print", "printer_name": printer_name})

        def cb(result):
            if result.get("status") != "ok":
                logger.warning("Test print failed for %s: %s", printer_name, result.get("message", ""))

        run_in_thread(task, cb)

    # ── Async Quick Fix ──
    def _quick_fix(self, printer_name, printer_ip):
        if printer_name in self._status_printer_cards:
            _, _, detail_lbl, _ = self._status_printer_cards[printer_name]
            detail_lbl.set_text(t("Fixing..."))

        def task():
            return send_command({
                "action": "fix",
                "printer_name": printer_name,
                "printer_ip": printer_ip,
            })

        def cb(result):
            if result.get("status") == "ok":
                self._start_status()
            else:
                if printer_name in self._status_printer_cards:
                    _, _, detail_lbl, _ = self._status_printer_cards[printer_name]
                    detail_lbl.set_text(t("Fix failed"))

        run_in_thread(task, cb)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = ITAmanApp()
    app.show_all()
    Gtk.main()
