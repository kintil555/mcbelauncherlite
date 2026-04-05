"""
MCLite Launcher - Minecraft Bedrock GDK Launcher for Windows 10/11
Optimized: fast boot, low RAM, single-instance dengan bring-to-front
"""

# ── Import minimal di awal (lazy load sisanya) ─────────────────────────────────
import sys
import os
import ctypes
import ctypes.wintypes

# Windows API — load sekali, reuse
_kernel32 = ctypes.windll.kernel32
_user32   = ctypes.windll.user32

# ── Single Instance via Named Mutex + BringToFront ─────────────────────────────
_MUTEX_NAME = "MCLiteLauncher_SingleInstance_v1"
_WND_TITLE  = "MCLite Launcher"

def _ensure_single_instance():
    """
    Jika sudah ada instance lain:
    - Kirim WM_USER pesan ke window yang ada untuk munculin dirinya
    - Exit proses ini
    Pakai Named Mutex agar bekerja antar-session.
    """
    mutex = _kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    err   = _kernel32.GetLastError()
    if err == 183:  # ERROR_ALREADY_EXISTS
        # Cari window instance pertama dan bawa ke depan
        hwnd = _user32.FindWindowW(None, _WND_TITLE)
        if hwnd:
            # Restore kalau minimize
            _user32.ShowWindow(hwnd, 9)       # SW_RESTORE
            _user32.SetForegroundWindow(hwnd)
            _user32.BringWindowToTop(hwnd)
        sys.exit(0)
    return mutex  # Keep reference agar mutex tidak di-GC


_mutex_ref = _ensure_single_instance()

# ── Lazy imports setelah single-instance check ────────────────────────────────
import time
import threading
import subprocess
import configparser
from pathlib import Path
from datetime import datetime

# PyQt6 — import hanya yang dipakai
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QComboBox, QGroupBox,
    QProgressBar, QSystemTrayIcon, QMenu, QTabWidget, QSpinBox,
    QFileDialog, QMessageBox, QFrame, QTextEdit
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui  import QColor, QFont, QPainter, QAction

import psutil

# ── Konstanta ──────────────────────────────────────────────────────────────────

APP_NAME    = "MCLite Launcher"
APP_VERSION = "1.0.0"
_APPDATA    = Path(os.getenv("APPDATA", ""))
CONFIG_FILE = _APPDATA / "MCLiteLauncher" / "config.ini"
LOG_FILE    = _APPDATA / "MCLiteLauncher" / "launcher.log"

MC_EXE_NAMES = ["Minecraft.Windows.exe", "Minecraft.exe"]

COLORS = {
    "bg_dark":    "#0D1117",
    "bg_card":    "#161B22",
    "bg_hover":   "#1C2128",
    "accent":     "#4CAF50",
    "accent2":    "#8BC34A",
    "accent_dim": "#2E7D32",
    "text":       "#E6EDF3",
    "text_dim":   "#8B949E",
    "border":     "#30363D",
    "danger":     "#F85149",
    "warning":    "#E3B341",
    "success":    "#3FB950",
}

# ── Windows API helpers ────────────────────────────────────────────────────────

_psapi = ctypes.windll.psapi

def trim_process_memory(pid: int) -> bool:
    try:
        handle = _kernel32.OpenProcess(0x1F0FFF, False, pid)
        if not handle:
            return False
        result = _psapi.EmptyWorkingSet(handle)
        _kernel32.CloseHandle(handle)
        return bool(result)
    except Exception:
        return False

def set_process_priority(pid: int, priority: str) -> bool:
    _map = {
        "normal":       0x00000020,
        "above_normal": 0x00008000,
        "high":         0x00000080,
    }
    try:
        handle = _kernel32.OpenProcess(0x1F0FFF, False, pid)
        if not handle:
            return False
        result = _kernel32.SetPriorityClass(handle, _map.get(priority, 0x20))
        _kernel32.CloseHandle(handle)
        return bool(result)
    except Exception:
        return False

def find_minecraft_process():
    targets = {"minecraft.windows.exe", "minecraft.exe"}
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.info["name"].lower() in targets:
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None

# Cache path deteksi — hindari PowerShell berulang kali
_mc_path_cache: Path | None = None

def find_minecraft_exe(use_cache=True) -> Path | None:
    global _mc_path_cache
    if use_cache and _mc_path_cache and _mc_path_cache.exists():
        return _mc_path_cache

    # Cek registry dulu (lebih cepat dari PowerShell)
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\MCLiteLauncher")
        saved, _ = winreg.QueryValueEx(key, "mc_path")
        winreg.CloseKey(key)
        p = Path(saved)
        if p.exists():
            _mc_path_cache = p
            return p
    except Exception:
        pass

    # Scan path umum tanpa PowerShell (jauh lebih cepat)
    scan_bases = [
        Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps",
        Path("C:/Program Files/WindowsApps"),
    ]
    for base in scan_bases:
        try:
            if not base.exists():
                continue
            for folder in base.iterdir():
                name_l = folder.name.lower()
                if "minecraft" not in name_l:
                    continue
                for exe in MC_EXE_NAMES:
                    for sub in ["", "Content/"]:
                        p = folder / sub / exe
                        if p.exists():
                            _mc_path_cache = p
                            return p
        except (PermissionError, OSError):
            continue

    # Fallback PowerShell — async, jalan di background
    return None

def find_minecraft_exe_powershell() -> Path | None:
    """Deteksi via PowerShell — dipanggil di background thread."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-AppxPackage 'Microsoft.MinecraftUWP').InstallLocation"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0 and r.stdout.strip():
            loc = Path(r.stdout.strip())
            for exe in MC_EXE_NAMES:
                for sub in ["", "Content/"]:
                    p = loc / sub / exe
                    if p.exists():
                        return p
    except Exception:
        pass
    return None

# ── Config (ringan, pakai configparser bukan QSettings) ───────────────────────

class Config:
    _DEFAULTS = {
        "general": {"mc_path": "", "auto_detect": "true", "close_on_launch": "false"},
        "memory":  {"enabled": "true", "limit_mb": "4096", "trim_interval_s": "30", "trim_threshold": "80"},
        "boost":   {"priority": "above_normal", "cpu_affinity": "0", "timer_resolution": "true"},
        "fps":     {"overlay_enabled": "false", "position": "top_left", "font_size": "14", "update_interval": "500"},
        "fixes":   {"disable_improved_input_response": "false", "flush_dns_on_launch": "false"},
    }

    def __init__(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._c = configparser.ConfigParser()
        for s, v in self._DEFAULTS.items():
            self._c[s] = v
        if CONFIG_FILE.exists():
            self._c.read(CONFIG_FILE, encoding="utf-8")

    def get(self, s, k):
        return self._c.get(s, k, fallback=self._DEFAULTS.get(s, {}).get(k, ""))

    def set(self, s, k, v):
        if s not in self._c:
            self._c[s] = {}
        self._c[s][k] = str(v)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            self._c.write(f)

    def bool(self, s, k):  return self.get(s, k).lower() == "true"
    def int(self, s, k):
        try: return int(self.get(s, k))
        except ValueError: return int(self._DEFAULTS.get(s, {}).get(k, 0))

cfg = Config()

# ── Logger (tulis ke file, tidak ke stdout di release) ────────────────────────

class _Logger:
    def __init__(self):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _w(self, lvl, msg):
        line = f"[{datetime.now():%H:%M:%S}] [{lvl}] {msg}\n"
        with self._lock:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    def info(self, m):  self._w("INFO",  m)
    def warn(self, m):  self._w("WARN",  m)
    def error(self, m): self._w("ERROR", m)

log = _Logger()

# ── Memory Monitor Thread ──────────────────────────────────────────────────────

class MemThread(QThread):
    stats   = pyqtSignal(dict)
    trimmed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("MemThread")
        self._stop = False

    def run(self):
        while not self._stop:
            proc = find_minecraft_process()
            if proc:
                try:
                    rss  = proc.memory_info().rss >> 20   # bytes → MB (bit shift lebih cepat)
                    cpu  = proc.cpu_percent(interval=0.3)
                    sysr = psutil.virtual_memory()
                    self.stats.emit({
                        "running":  True,
                        "pid":      proc.pid,
                        "rss_mb":   rss,
                        "cpu_pct":  cpu,
                        "sys_pct":  sysr.percent,
                        "sys_tot":  sysr.total >> 20,
                    })
                    if cfg.bool("memory", "enabled"):
                        lim = cfg.int("memory", "limit_mb")
                        thr = cfg.int("memory", "trim_threshold")
                        if rss > lim or sysr.percent > thr:
                            before = rss
                            if trim_process_memory(proc.pid):
                                time.sleep(1.5)
                                try:
                                    after = proc.memory_info().rss >> 20
                                    saved = max(0, before - after)
                                    if saved > 0:
                                        self.trimmed.emit(saved)
                                        log.info(f"Trim: {before}→{after}MB (-{saved}MB)")
                                except Exception:
                                    pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self.stats.emit({"running": False})
            else:
                self.stats.emit({"running": False})

            interval = cfg.int("memory", "trim_interval_s")
            # Sleep dalam potongan kecil agar bisa stop cepat
            for _ in range(max(1, interval)):
                if self._stop:
                    return
                time.sleep(1)

    def stop(self):
        self._stop = True

# ── FPS Overlay ────────────────────────────────────────────────────────────────

class FPSOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(180, 62)
        self._rss = 0.0
        self._cpu = 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(max(200, cfg.int("fps", "update_interval")))
        self._reposition()

    def _reposition(self):
        pos = cfg.get("fps", "position")
        scr = QApplication.primaryScreen().geometry()
        m   = 12
        positions = {
            "top_left":     (m, m),
            "top_right":    (scr.width() - 190, m),
            "bottom_left":  (m, scr.height() - 74),
            "bottom_right": (scr.width() - 190, scr.height() - 74),
        }
        x, y = positions.get(pos, (m, m))
        self.move(x, y)

    def _poll(self):
        proc = find_minecraft_process()
        if proc:
            try:
                self._rss = proc.memory_info().rss >> 20
                self._cpu = proc.cpu_percent()
            except Exception:
                pass
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(0, 0, 0, 160))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(self.rect(), 6, 6)
        p.setFont(QFont("Consolas", cfg.int("fps", "font_size"), QFont.Weight.Bold))
        p.setPen(QColor("#00FF41"))
        p.drawText(
            self.rect().adjusted(8, 6, -8, -6),
            Qt.AlignmentFlag.AlignLeft,
            f"RAM: {self._rss} MB\nCPU: {self._cpu:.1f}%"
        )

# ── Launch Thread ──────────────────────────────────────────────────────────────

class LaunchThread(QThread):
    ok     = pyqtSignal(int)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, path: Path):
        super().__init__()
        self._path = path

    def run(self):
        if cfg.bool("fixes", "flush_dns_on_launch"):
            try:
                subprocess.run(["ipconfig", "/flushdns"],
                               capture_output=True, timeout=5)
                self.status.emit("DNS flushed ✓")
            except Exception:
                pass

        if cfg.bool("boost", "timer_resolution"):
            try:
                ntdll = ctypes.windll.ntdll
                cur = ctypes.c_ulong()
                ntdll.NtSetTimerResolution(5000, True, ctypes.byref(cur))
            except Exception:
                pass

        self.status.emit("Meluncurkan Minecraft...")
        log.info(f"Launch: {self._path}")

        pid = None
        try:
            proc = subprocess.Popen(
                [str(self._path)],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
            pid = proc.pid
        except Exception as e:
            try:
                subprocess.Popen(["explorer.exe", str(self._path)],
                                 creationflags=subprocess.DETACHED_PROCESS)
                time.sleep(4)
            except Exception as e2:
                self.failed.emit(str(e2))
                return

        time.sleep(3)
        mc = find_minecraft_process()
        if mc:
            pid = mc.pid

        if not pid:
            self.failed.emit("Minecraft tidak terdeteksi setelah launch.")
            return

        p = cfg.get("boost", "priority")
        if p != "normal":
            set_process_priority(pid, p)

        cores = cfg.int("boost", "cpu_affinity")
        if cores > 0:
            try:
                psutil.Process(pid).cpu_affinity(
                    list(range(min(cores, psutil.cpu_count())))
                )
            except Exception:
                pass

        log.info(f"Launched OK PID={pid}")
        self.ok.emit(pid)

# ── Stylesheet (build sekali, cache) ──────────────────────────────────────────

def _stylesheet():
    c = COLORS
    return f"""
* {{ font-family: 'Segoe UI', sans-serif; color: {c['text']}; }}
QMainWindow, QWidget {{ background: {c['bg_dark']}; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; background: {c['bg_card']}; border-radius: 4px; }}
QTabBar::tab {{ background: {c['bg_dark']}; border: 1px solid {c['border']}; padding: 8px 16px; color: {c['text_dim']}; font-weight: 600; font-size: 12px; }}
QTabBar::tab:selected {{ background: {c['bg_card']}; color: {c['accent']}; border-bottom: 2px solid {c['accent']}; }}
QTabBar::tab:hover:!selected {{ background: {c['bg_hover']}; color: {c['text']}; }}
QPushButton {{ background: {c['bg_hover']}; border: 1px solid {c['border']}; border-radius: 5px; padding: 7px 14px; font-size: 12px; font-weight: 600; }}
QPushButton:hover {{ background: {c['accent_dim']}; border-color: {c['accent']}; }}
QPushButton#btn_launch {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {c['accent']},stop:1 {c['accent_dim']}); border: none; border-radius: 6px; padding: 13px 28px; font-size: 15px; font-weight: 700; color: #fff; }}
QPushButton#btn_launch:hover {{ background: {c['accent2']}; }}
QPushButton#btn_launch:disabled {{ background: {c['border']}; color: {c['text_dim']}; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 12px; padding: 10px 6px 6px 6px; font-size: 11px; font-weight: 700; color: {c['text_dim']}; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 0 6px; color: {c['accent']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {c['border']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {c['accent']}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 2px solid {c['border']}; border-radius: 3px; background: {c['bg_dark']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
QComboBox {{ background: {c['bg_hover']}; border: 1px solid {c['border']}; border-radius: 4px; padding: 5px 8px; }}
QComboBox QAbstractItemView {{ background: {c['bg_card']}; border: 1px solid {c['border']}; selection-background-color: {c['accent_dim']}; }}
QSpinBox {{ background: {c['bg_hover']}; border: 1px solid {c['border']}; border-radius: 4px; padding: 5px 8px; }}
QProgressBar {{ border: 1px solid {c['border']}; border-radius: 4px; background: {c['bg_dark']}; text-align: center; font-size: 11px; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {c['accent_dim']},stop:1 {c['accent']}); border-radius: 3px; }}
QTextEdit {{ background: {c['bg_dark']}; border: 1px solid {c['border']}; border-radius: 4px; font-family: Consolas; font-size: 11px; color: {c['text_dim']}; }}
QScrollBar:vertical {{ background: {c['bg_dark']}; width: 7px; }}
QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 3px; min-height: 20px; }}
"""

# ── Main Window ────────────────────────────────────────────────────────────────

class MCLiteLauncher(QMainWindow):
    def __init__(self):
        super().__init__()
        # Set title persis sama dengan _WND_TITLE agar FindWindowW bisa menemukan
        self.setWindowTitle(_WND_TITLE)
        self.setMinimumSize(680, 540)
        self.resize(740, 580)

        self._mc_path       = None
        self._total_trimmed = 0
        self._fps_overlay   = None
        self._mem_thread    = MemThread()

        # Load path dari config (cepat, tidak perlu scan)
        saved = cfg.get("general", "mc_path")
        if saved:
            p = Path(saved)
            if p.exists():
                self._mc_path = p

        self._build_ui()
        self._setup_tray()
        self._mem_thread.stats.connect(self._on_stats)
        self._mem_thread.trimmed.connect(self._on_trimmed)
        self._mem_thread.start(QThread.Priority.LowPriority)

        # Auto-detect di background jika belum ada path
        if not self._mc_path:
            threading.Thread(target=self._bg_detect, daemon=True).start()

    def _bg_detect(self):
        """Deteksi Minecraft di background agar UI tidak freeze."""
        p = find_minecraft_exe(use_cache=False)
        if not p:
            p = find_minecraft_exe_powershell()
        if p:
            self._mc_path = p
            cfg.set("general", "mc_path", str(p))
            # Update UI dari main thread
            QTimer.singleShot(0, self._on_path_detected)

    def _on_path_detected(self):
        self._lbl_path.setText(str(self._mc_path))
        self._btn_launch.setEnabled(True)
        self._btn_launch.setText("▶  LAUNCH MINECRAFT")
        self._set_status(f"✅ Minecraft ditemukan otomatis")

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        cw  = QWidget()
        root = QVBoxLayout(cw)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(cw)

        # Header
        hdr = QFrame()
        hdr.setFixedHeight(58)
        hdr.setStyleSheet(f"""
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #0D2B0D, stop:1 #0D1117);
            border-bottom: 2px solid {COLORS['accent_dim']};
        """)
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(18, 0, 18, 0)
        lbl = QLabel("⛏  MCLite Launcher")
        lbl.setStyleSheet(f"font-size: 19px; font-weight: 800; color: {COLORS['accent']};")
        hl.addWidget(lbl)
        hl.addStretch()
        ver = QLabel(f"v{APP_VERSION}  •  Bedrock GDK")
        ver.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        hl.addWidget(ver)
        root.addWidget(hdr)

        # Tabs
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._tab_home(),   "  🏠 HOME  ")
        tabs.addTab(self._tab_memory(), "  💾 MEMORI  ")
        tabs.addTab(self._tab_boost(),  "  ⚡ BOOST  ")
        tabs.addTab(self._tab_fps(),    "  📊 FPS  ")
        tabs.addTab(self._tab_fixes(),  "  🔧 FIX  ")
        tabs.addTab(self._tab_log(),    "  📋 LOG  ")
        root.addWidget(tabs, 1)

        # Status bar
        sb = QFrame()
        sb.setFixedHeight(26)
        sb.setStyleSheet(f"background:{COLORS['bg_card']};border-top:1px solid {COLORS['border']};")
        sl = QHBoxLayout(sb)
        sl.setContentsMargins(10, 0, 10, 0)
        self._lbl_status = QLabel("Siap.")
        self._lbl_status.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        sl.addWidget(self._lbl_status)
        sl.addStretch()
        self._lbl_mc_status = QLabel("● Minecraft tidak berjalan")
        self._lbl_mc_status.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        sl.addWidget(self._lbl_mc_status)
        root.addWidget(sb)

    def _tab_home(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        # Path
        pg = QGroupBox("Lokasi Minecraft")
        ph = QHBoxLayout(pg)
        self._lbl_path = QLabel(
            str(self._mc_path) if self._mc_path else "Mendeteksi..." if cfg.bool("general", "auto_detect") else "Belum ditemukan"
        )
        self._lbl_path.setStyleSheet(f"font-family:Consolas;font-size:11px;color:{COLORS['text_dim']};")
        self._lbl_path.setWordWrap(True)
        ph.addWidget(self._lbl_path, 1)
        btn_b = QPushButton("📂 Browse");   btn_b.clicked.connect(self._browse)
        btn_d = QPushButton("🔍 Detect");   btn_d.clicked.connect(self._manual_detect)
        ph.addWidget(btn_b); ph.addWidget(btn_d)
        lay.addWidget(pg)

        # Stats
        sg   = QGroupBox("Status Real-Time")
        slay = QHBoxLayout(sg)

        def _stat_col(attr, label, color):
            col = QVBoxLayout()
            lbl = QLabel("– ")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"font-size:20px;font-weight:700;color:{color};font-family:Consolas;")
            sub = QLabel(label)
            sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub.setStyleSheet(f"font-size:10px;color:{COLORS['text_dim']};")
            col.addWidget(lbl); col.addWidget(sub)
            setattr(self, attr, lbl)
            return col

        slay.addLayout(_stat_col("_lbl_rss",     "RAM Minecraft",  COLORS["accent"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_cpu",     "CPU Minecraft",  COLORS["warning"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_sys",     "RAM Sistem",     COLORS["accent2"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_trimmed", "Total Di-Trim",  COLORS["success"]))
        lay.addWidget(sg)

        self._ram_bar = QProgressBar()
        self._ram_bar.setRange(0, 100)
        self._ram_bar.setFixedHeight(18)
        self._ram_bar.setFormat("RAM Minecraft: menunggu...")
        lay.addWidget(self._ram_bar)

        lay.addStretch()

        self._btn_launch = QPushButton("▶  LAUNCH MINECRAFT")
        self._btn_launch.setObjectName("btn_launch")
        self._btn_launch.setFixedHeight(48)
        self._btn_launch.clicked.connect(self._launch)
        if not self._mc_path:
            self._btn_launch.setEnabled(False)
            self._btn_launch.setText("⏳ Mendeteksi Minecraft...")
        lay.addWidget(self._btn_launch)

        btn_trim = QPushButton("🧹 Trim Memory Sekarang")
        btn_trim.setFixedHeight(32)
        btn_trim.clicked.connect(self._manual_trim)
        lay.addWidget(btn_trim)
        return w

    def _tab_memory(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        info = QLabel(
            "ℹ️  Memory Manager otomatis men-trim RAM Minecraft menggunakan\n"
            "Windows API EmptyWorkingSet() — mengatasi memory leak GDK (v1.21.120+)."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"background:#0D2218;border:1px solid {COLORS['accent_dim']};"
                           f"border-radius:5px;padding:8px 12px;font-size:11px;color:{COLORS['text_dim']};")
        lay.addWidget(info)

        g   = QGroupBox("Pengaturan")
        gl  = QVBoxLayout(g)

        self._chk_mem = QCheckBox("Aktifkan Memory Manager Otomatis")
        self._chk_mem.setChecked(cfg.bool("memory", "enabled"))
        self._chk_mem.toggled.connect(lambda v: cfg.set("memory", "enabled", v))
        gl.addWidget(self._chk_mem)

        for label, key, lo, hi, step in [
            ("Batas RAM Minecraft (MB):", "limit_mb",       512, 32768, 256),
            ("Interval cek (detik):",     "trim_interval_s",  5,   300,   5),
            ("Trim jika RAM sistem > %:", "trim_threshold",  50,    99,   1),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            sp  = QSpinBox()
            sp.setRange(lo, hi); sp.setSingleStep(step)
            sp.setValue(cfg.int("memory", key))
            _k = key
            sp.valueChanged.connect(lambda v, k=_k: cfg.set("memory", k, v))
            row.addWidget(sp); row.addStretch()
            gl.addLayout(row)

        lay.addWidget(g)

        btn = QPushButton("🧹 Trim Sekarang")
        btn.clicked.connect(self._manual_trim)
        lay.addWidget(btn)
        lay.addStretch()
        return w

    def _tab_boost(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        g1  = QGroupBox("Prioritas CPU")
        g1l = QVBoxLayout(g1)
        g1l.addWidget(QLabel("Prioritas Minecraft:"))
        self._cmb_prio = QComboBox()
        self._cmb_prio.addItems(["normal", "above_normal", "high"])
        self._cmb_prio.setCurrentText(cfg.get("boost", "priority"))
        self._cmb_prio.currentTextChanged.connect(lambda v: cfg.set("boost", "priority", v))
        g1l.addWidget(self._cmb_prio)
        lay.addWidget(g1)

        g2  = QGroupBox("CPU Affinity (0 = semua core)")
        g2l = QHBoxLayout(g2)
        self._sp_cores = QSpinBox()
        self._sp_cores.setRange(0, psutil.cpu_count() or 8)
        self._sp_cores.setValue(cfg.int("boost", "cpu_affinity"))
        self._sp_cores.valueChanged.connect(lambda v: cfg.set("boost", "cpu_affinity", v))
        g2l.addWidget(self._sp_cores); g2l.addStretch()
        lay.addWidget(g2)

        g3  = QGroupBox("Optimasi Lain")
        g3l = QVBoxLayout(g3)
        self._chk_timer = QCheckBox("Timer Resolution 0.5ms (kurangi input latency)")
        self._chk_timer.setChecked(cfg.bool("boost", "timer_resolution"))
        self._chk_timer.toggled.connect(lambda v: cfg.set("boost", "timer_resolution", v))
        g3l.addWidget(self._chk_timer)
        lay.addWidget(g3)

        btn = QPushButton("⚡ Apply Boost ke Minecraft Sekarang")
        btn.clicked.connect(self._apply_boost)
        lay.addWidget(btn)
        lay.addStretch()
        return w

    def _tab_fps(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        info = QLabel("Overlay transparan tampil di atas Minecraft.\nKlik menembus ke game — tidak mengganggu input.")
        info.setStyleSheet(f"background:#0A1A2E;border:1px solid {COLORS['border']};"
                           f"border-radius:5px;padding:8px 12px;font-size:11px;color:{COLORS['text_dim']};")
        lay.addWidget(info)

        g   = QGroupBox("Pengaturan FPS Overlay")
        gl  = QVBoxLayout(g)

        self._chk_fps = QCheckBox("Aktifkan FPS Overlay")
        self._chk_fps.setChecked(cfg.bool("fps", "overlay_enabled"))
        self._chk_fps.toggled.connect(self._toggle_overlay)
        gl.addWidget(self._chk_fps)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Posisi:"))
        self._cmb_pos = QComboBox()
        self._cmb_pos.addItems(["top_left", "top_right", "bottom_left", "bottom_right"])
        self._cmb_pos.setCurrentText(cfg.get("fps", "position"))
        self._cmb_pos.currentTextChanged.connect(lambda v: (cfg.set("fps", "position", v),
            self._fps_overlay and self._fps_overlay._reposition()))
        row1.addWidget(self._cmb_pos); row1.addStretch()
        gl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Font size:"))
        sp = QSpinBox(); sp.setRange(8, 28); sp.setValue(cfg.int("fps", "font_size"))
        sp.valueChanged.connect(lambda v: cfg.set("fps", "font_size", v))
        row2.addWidget(sp); row2.addStretch()
        gl.addLayout(row2)

        lay.addWidget(g)
        lay.addStretch()
        return w

    def _tab_fixes(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        g   = QGroupBox("Perbaikan Bug GDK")
        gl  = QVBoxLayout(g)

        self._chk_iir = QCheckBox(
            "Catat peringatan: Nonaktifkan 'Improved Input Response' di Minecraft\n"
            "  → Penyebab utama memory leak ekstrem sejak v1.21.120"
        )
        self._chk_iir.setChecked(cfg.bool("fixes", "disable_improved_input_response"))
        self._chk_iir.toggled.connect(lambda v: cfg.set("fixes", "disable_improved_input_response", v))
        gl.addWidget(self._chk_iir)

        self._chk_dns = QCheckBox("Flush DNS sebelum launch (mengurangi lag multiplayer)")
        self._chk_dns.setChecked(cfg.bool("fixes", "flush_dns_on_launch"))
        self._chk_dns.toggled.connect(lambda v: cfg.set("fixes", "flush_dns_on_launch", v))
        gl.addWidget(self._chk_dns)
        lay.addWidget(g)

        g2  = QGroupBox("Folder Data GDK")
        g2l = QVBoxLayout(g2)
        g2l.addWidget(QLabel("%APPDATA%\\Minecraft Bedrock\\users\\shared\\games\\com.mojang"))
        btn = QPushButton("📂 Buka Folder")
        btn.clicked.connect(self._open_gdk)
        g2l.addWidget(btn)
        lay.addWidget(g2)
        lay.addStretch()
        return w

    def _tab_log(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 10)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        lay.addWidget(self._log_view, 1)
        bl = QHBoxLayout()
        br = QPushButton("🔄 Refresh"); br.clicked.connect(self._load_log)
        bc = QPushButton("🗑 Hapus");   bc.clicked.connect(self._clear_log)
        bl.addWidget(br); bl.addWidget(bc); bl.addStretch()
        lay.addLayout(bl)
        self._load_log()
        return w

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _vsep(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet(f"color:{COLORS['border']};"); return f

    def _set_status(self, msg):
        self._lbl_status.setText(msg)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Pilih Minecraft.Windows.exe",
            "C:/Program Files/WindowsApps", "Executable (*.exe)"
        )
        if path:
            self._mc_path = Path(path)
            cfg.set("general", "mc_path", path)
            self._lbl_path.setText(path)
            self._btn_launch.setEnabled(True)
            self._btn_launch.setText("▶  LAUNCH MINECRAFT")

    def _manual_detect(self):
        self._set_status("🔍 Mendeteksi...")
        threading.Thread(target=self._bg_detect, daemon=True).start()

    def _launch(self):
        if not self._mc_path or not self._mc_path.exists():
            QMessageBox.warning(self, "Error", "Path Minecraft tidak valid.\nGunakan Browse atau Auto Detect.")
            return
        self._btn_launch.setEnabled(False)
        self._btn_launch.setText("⏳ Meluncurkan...")
        self._lt = LaunchThread(self._mc_path)
        self._lt.ok.connect(lambda pid: (
            self._btn_launch.setEnabled(True),
            self._btn_launch.setText("▶  LAUNCH MINECRAFT"),
            self._set_status(f"✅ Minecraft berjalan (PID {pid})")
        ))
        self._lt.failed.connect(lambda e: (
            self._btn_launch.setEnabled(True),
            self._btn_launch.setText("▶  LAUNCH MINECRAFT"),
            self._set_status(f"❌ {e}"),
            QMessageBox.critical(self, "Gagal", e)
        ))
        self._lt.status.connect(self._set_status)
        self._lt.start()

    def _manual_trim(self):
        proc = find_minecraft_process()
        if not proc:
            self._set_status("Minecraft tidak berjalan."); return
        before = proc.memory_info().rss >> 20
        if trim_process_memory(proc.pid):
            time.sleep(1)
            try:
                after  = proc.memory_info().rss >> 20
                saved  = max(0, before - after)
                self._total_trimmed += saved
                self._lbl_trimmed.setText(f"{self._total_trimmed} MB")
                self._set_status(f"🧹 Trim: {before}→{after}MB (hemat {saved}MB)")
            except Exception:
                self._set_status("🧹 Trim dilakukan.")
        else:
            self._set_status("⚠ Trim gagal — coba jalankan sebagai Administrator.")

    def _apply_boost(self):
        proc = find_minecraft_process()
        if not proc:
            self._set_status("Minecraft tidak berjalan."); return
        p = cfg.get("boost", "priority")
        set_process_priority(proc.pid, p)
        c = cfg.int("boost", "cpu_affinity")
        if c > 0:
            try: psutil.Process(proc.pid).cpu_affinity(list(range(min(c, psutil.cpu_count()))))
            except Exception: pass
        self._set_status(f"⚡ Boost applied: {p}, {c or 'all'} cores")

    def _toggle_overlay(self, on):
        cfg.set("fps", "overlay_enabled", on)
        if on:
            if not self._fps_overlay:
                self._fps_overlay = FPSOverlay()
            self._fps_overlay.show()
        else:
            if self._fps_overlay:
                self._fps_overlay.hide()

    def _open_gdk(self):
        p = _APPDATA / "Minecraft Bedrock" / "users" / "shared" / "games" / "com.mojang"
        if p.exists():
            os.startfile(p)
        else:
            QMessageBox.information(self, "Info", f"Folder belum ada:\n{p}\n\nJalankan Minecraft sekali dulu.")

    def _load_log(self):
        try:
            if LOG_FILE.exists():
                lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
                self._log_view.setPlainText("\n".join(lines[-200:]))
                self._log_view.verticalScrollBar().setValue(
                    self._log_view.verticalScrollBar().maximum()
                )
            else:
                self._log_view.setPlainText("(Belum ada log)")
        except Exception as e:
            self._log_view.setPlainText(f"Error: {e}")

    def _clear_log(self):
        try: LOG_FILE.write_text("", encoding="utf-8")
        except Exception: pass
        self._log_view.setPlainText("")

    # ── Stats callback ───────────────────────────────────────────────────────

    def _on_stats(self, s: dict):
        if s.get("running"):
            rss  = s["rss_mb"]
            lim  = cfg.int("memory", "limit_mb")
            pct  = min(100, int(rss / lim * 100)) if lim else 0
            self._lbl_rss.setText(f"{rss} MB")
            self._lbl_cpu.setText(f"{s['cpu_pct']:.1f}%")
            self._lbl_sys.setText(f"{s['sys_pct']:.0f}%")
            self._ram_bar.setValue(pct)
            self._ram_bar.setFormat(f"Minecraft RAM: {rss}/{lim} MB ({pct}%)")
            self._lbl_mc_status.setText(f"● Berjalan  PID={s['pid']}")
            self._lbl_mc_status.setStyleSheet(f"font-size:11px;color:{COLORS['success']};")
        else:
            for lbl in (self._lbl_rss, self._lbl_cpu):
                lbl.setText("–")
            self._lbl_sys.setText(f"{psutil.virtual_memory().percent:.0f}%")
            self._ram_bar.setValue(0)
            self._ram_bar.setFormat("Minecraft tidak berjalan")
            self._lbl_mc_status.setText("● Tidak berjalan")
            self._lbl_mc_status.setStyleSheet(f"font-size:11px;color:{COLORS['text_dim']};")

    def _on_trimmed(self, mb: int):
        self._total_trimmed += mb
        self._lbl_trimmed.setText(f"{self._total_trimmed} MB")
        self._set_status(f"🧹 Auto-trim: hemat {mb} MB")

    # ── Tray ─────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip(APP_NAME)
        m = QMenu()
        a1 = QAction("Tampilkan", self); a1.triggered.connect(self._show_window)
        a2 = QAction("Trim Memory", self); a2.triggered.connect(self._manual_trim)
        a3 = QAction("Keluar", self); a3.triggered.connect(self._quit)
        m.addAction(a1); m.addAction(a2); m.addSeparator(); m.addAction(a3)
        self._tray.setContextMenu(m)
        self._tray.activated.connect(
            lambda r: self._show_window() if r == QSystemTrayIcon.ActivationReason.DoubleClick else None
        )
        self._tray.show()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._mem_thread.stop()
        self._mem_thread.wait(2000)
        if self._fps_overlay:
            self._fps_overlay.close()
        QApplication.quit()

    def closeEvent(self, e):
        if hasattr(self, "_tray") and self._tray.isVisible():
            self.hide()
            e.ignore()
        else:
            self._quit()
            e.accept()


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    # High DPI
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)   # Tetap jalan di tray
    app.setStyleSheet(_stylesheet())

    win = MCLiteLauncher()

    # Kalau dipanggil dengan --minimized (autostart), langsung ke tray
    if "--minimized" not in sys.argv:
        win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
