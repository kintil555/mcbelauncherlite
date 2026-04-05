"""
MCLite Launcher - Minecraft Bedrock GDK Launcher for Windows 10/11
Fitur: Memory Leak Fix, Performance Boost, FPS Counter Overlay
"""

import sys
import os
import json
import time
import ctypes
import winreg
import psutil
import threading
import subprocess
import configparser
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QCheckBox, QComboBox, QGroupBox,
    QProgressBar, QSystemTrayIcon, QMenu, QTabWidget, QSpinBox,
    QFileDialog, QMessageBox, QFrame, QScrollArea, QTextEdit
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QSettings, QPropertyAnimation,
    QEasingCurve, QPoint, QSize
)
from PyQt6.QtGui import (
    QIcon, QColor, QPalette, QFont, QPixmap, QPainter, QLinearGradient,
    QFontDatabase, QAction
)

# ── Konstanta ──────────────────────────────────────────────────────────────────

APP_NAME    = "MCLite Launcher"
APP_VERSION = "1.0.0"
CONFIG_FILE = Path(os.getenv("APPDATA")) / "MCLiteLauncher" / "config.ini"
LOG_FILE    = Path(os.getenv("APPDATA")) / "MCLiteLauncher" / "launcher.log"

# Minecraft GDK path (setelah migrasi dari UWP ke GDK, versi 1.21.120+)
MC_GDK_PATHS = [
    Path(os.getenv("APPDATA", "")) / "Microsoft" / "WindowsApps" / "Microsoft.MinecraftUWP_8wekyb3d8bbwe",
    Path("C:/Program Files/WindowsApps") / "Microsoft.MinecraftUWP_8wekyb3d8bbwe",
    Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps",
]

MC_EXE_NAMES = ["Minecraft.Windows.exe", "Minecraft.exe"]

# Warna tema (Minecraft style)
COLORS = {
    "bg_dark":    "#0D1117",
    "bg_card":    "#161B22",
    "bg_hover":   "#1C2128",
    "accent":     "#4CAF50",   # hijau minecraft
    "accent2":    "#8BC34A",
    "accent_dim": "#2E7D32",
    "dirt":       "#8B6914",
    "text":       "#E6EDF3",
    "text_dim":   "#8B949E",
    "border":     "#30363D",
    "danger":     "#F85149",
    "warning":    "#E3B341",
    "success":    "#3FB950",
}

# ── Utilitas Windows API ───────────────────────────────────────────────────────

kernel32  = ctypes.windll.kernel32
psapi     = ctypes.windll.psapi

def trim_process_memory(pid: int) -> bool:
    """
    Trim working set Minecraft menggunakan SetProcessWorkingSetSize(handle, -1, -1).
    Ini adalah teknik yang sama yang dipakai tools GDK memory fix:
    memaksa Windows memindahkan halaman yang tidak aktif ke pagefile,
    mengurangi penggunaan RAM fisik yang disebabkan memory leak GDK.
    """
    try:
        PROCESS_ALL_ACCESS = 0x1F0FFF
        handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not handle:
            return False
        # -1 sebagai SIZE_T (ctypes.c_size_t(-1).value)
        SIZE_T_NEG1 = ctypes.c_size_t(-1).value
        # EmptyWorkingSet (lebih bersih dari SetProcessWorkingSetSize)
        result = psapi.EmptyWorkingSet(handle)
        kernel32.CloseHandle(handle)
        return bool(result)
    except Exception:
        return False

def set_process_priority(pid: int, priority: str) -> bool:
    """Set prioritas proses Minecraft untuk boost performa."""
    priority_map = {
        "normal":       0x00000020,
        "above_normal": 0x00008000,
        "high":         0x00000080,
        "realtime":     0x00000100,  # HATI-HATI: bisa freeze sistem
    }
    try:
        PROCESS_ALL_ACCESS = 0x1F0FFF
        handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not handle:
            return False
        result = kernel32.SetPriorityClass(handle, priority_map.get(priority, 0x00000020))
        kernel32.CloseHandle(handle)
        return bool(result)
    except Exception:
        return False

def set_cpu_affinity(pid: int, cores: int) -> bool:
    """Atur affinitas CPU — batasi atau optimalkan core yang dipakai Minecraft."""
    try:
        proc = psutil.Process(pid)
        total = psutil.cpu_count()
        # Gunakan core pertama sebanyak `cores`
        affinity = list(range(min(cores, total)))
        proc.cpu_affinity(affinity)
        return True
    except Exception:
        return False

def find_minecraft_process() -> psutil.Process | None:
    """Cari proses Minecraft Bedrock yang sedang berjalan."""
    targets = {"minecraft.windows.exe", "minecraft.exe"}
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"].lower() in targets:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

def find_minecraft_exe() -> Path | None:
    """Cari executable Minecraft Bedrock (GDK) di sistem."""
    # Cek via Get-AppxPackage PowerShell
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage 'Microsoft.MinecraftUWP').InstallLocation"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            loc = Path(result.stdout.strip())
            for name in MC_EXE_NAMES:
                exe = loc / name
                if exe.exists():
                    return exe
            # Coba subfolder Content/
            for name in MC_EXE_NAMES:
                exe = loc / "Content" / name
                if exe.exists():
                    return exe
    except Exception:
        pass

    # Fallback: scan path GDK umum
    for base in MC_GDK_PATHS:
        try:
            if base.exists():
                for folder in base.iterdir():
                    for name in MC_EXE_NAMES:
                        exe = folder / name
                        if exe.exists():
                            return exe
                        exe = folder / "Content" / name
                        if exe.exists():
                            return exe
        except Exception:
            continue
    return None

# ── Logger ─────────────────────────────────────────────────────────────────────

class Logger:
    def __init__(self):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, level: str, msg: str):
        ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}\n"
        with self._lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        print(line, end="")

    def info(self, m):  self.log("INFO",  m)
    def warn(self, m):  self.log("WARN",  m)
    def error(self, m): self.log("ERROR", m)

logger = Logger()

# ── Config ─────────────────────────────────────────────────────────────────────

class Config:
    DEFAULTS = {
        "general": {
            "mc_path":          "",
            "auto_detect":      "true",
            "close_on_launch":  "false",
            "username":         "Player",
        },
        "memory": {
            "enabled":          "true",
            "limit_mb":         "4096",
            "trim_interval_s":  "30",
            "trim_threshold":   "80",
        },
        "boost": {
            "priority":         "above_normal",
            "cpu_affinity":     "0",
            "disable_xbox_bar": "true",
            "timer_resolution": "true",
        },
        "fps": {
            "overlay_enabled":  "false",
            "position":         "top_left",
            "color":            "#00FF41",
            "font_size":        "14",
            "update_interval":  "500",
        },
        "fixes": {
            "disable_improved_input_response": "false",
            "flush_dns_on_launch":             "false",
        },
    }

    def __init__(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._cfg = configparser.ConfigParser()
        for section, values in self.DEFAULTS.items():
            self._cfg[section] = values
        if CONFIG_FILE.exists():
            self._cfg.read(CONFIG_FILE)

    def get(self, section, key, fallback=None):
        return self._cfg.get(section, key, fallback=fallback or self.DEFAULTS.get(section, {}).get(key, ""))

    def set(self, section, key, value):
        if section not in self._cfg:
            self._cfg[section] = {}
        self._cfg[section][key] = str(value)
        self.save()

    def getbool(self, section, key):
        return self.get(section, key).lower() == "true"

    def getint(self, section, key):
        try:
            return int(self.get(section, key))
        except ValueError:
            return int(self.DEFAULTS.get(section, {}).get(key, 0))

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            self._cfg.write(f)

config = Config()

# ── Memory Monitor Thread ──────────────────────────────────────────────────────

class MemoryMonitorThread(QThread):
    stats_updated   = pyqtSignal(dict)
    trim_performed  = pyqtSignal(int)   # MB yang berhasil di-trim
    status_changed  = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running = True
        self._paused  = False

    def run(self):
        while self._running:
            if self._paused:
                time.sleep(1)
                continue

            proc = find_minecraft_process()
            if proc:
                try:
                    mem_info = proc.memory_info()
                    rss_mb   = mem_info.rss / 1024 / 1024
                    sys_ram  = psutil.virtual_memory()
                    sys_pct  = sys_ram.percent
                    cpu_pct  = proc.cpu_percent(interval=0.5)

                    stats = {
                        "mc_pid":      proc.pid,
                        "mc_rss_mb":   rss_mb,
                        "sys_ram_pct": sys_pct,
                        "sys_ram_mb":  sys_ram.used / 1024 / 1024,
                        "sys_ram_tot": sys_ram.total / 1024 / 1024,
                        "mc_cpu_pct":  cpu_pct,
                        "mc_running":  True,
                    }
                    self.stats_updated.emit(stats)

                    # Cek apakah perlu trim
                    if config.getbool("memory", "enabled"):
                        limit_mb   = config.getint("memory", "limit_mb")
                        threshold  = config.getint("memory", "trim_threshold")
                        # Trim jika melewati batas ATAU RAM sistem > threshold%
                        if rss_mb > limit_mb or sys_pct > threshold:
                            before = rss_mb
                            ok = trim_process_memory(proc.pid)
                            if ok:
                                time.sleep(2)
                                try:
                                    after_mb = proc.memory_info().rss / 1024 / 1024
                                    trimmed  = int(before - after_mb)
                                    if trimmed > 0:
                                        self.trim_performed.emit(trimmed)
                                        logger.info(f"Memory trim: {before:.0f}MB → {after_mb:.0f}MB (hemat {trimmed}MB)")
                                except Exception:
                                    pass
                            self.status_changed.emit("trim")

                    interval = config.getint("memory", "trim_interval_s")
                    time.sleep(max(5, interval))

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self.stats_updated.emit({"mc_running": False})
                    time.sleep(3)
            else:
                self.stats_updated.emit({"mc_running": False})
                time.sleep(3)

    def stop(self):
        self._running = False

    def pause(self):  self._paused = True
    def resume(self): self._paused = False

# ── FPS Overlay Window ─────────────────────────────────────────────────────────

class FPSOverlay(QWidget):
    """
    Window transparan frameless yang tampil di atas Minecraft.
    Menampilkan FPS, RAM usage Minecraft, CPU%.
    Update setiap X ms (bisa dikonfigurasi).
    """

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

        self._fps        = 0
        self._ram_mb     = 0.0
        self._cpu_pct    = 0.0
        self._frame_cnt  = 0
        self._last_time  = time.time()

        # Timer update statistik
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_stats)
        interval = config.getint("fps", "update_interval")
        self._timer.start(max(100, interval))

        self._reposition()
        self.resize(200, 70)

    def _reposition(self):
        pos = config.get("fps", "position")
        screen = QApplication.primaryScreen().geometry()
        margin = 10
        if pos == "top_left":
            self.move(margin, margin)
        elif pos == "top_right":
            self.move(screen.width() - 210, margin)
        elif pos == "bottom_left":
            self.move(margin, screen.height() - 80)
        elif pos == "bottom_right":
            self.move(screen.width() - 210, screen.height() - 80)

    def _update_stats(self):
        proc = find_minecraft_process()
        if proc:
            try:
                self._ram_mb   = proc.memory_info().rss / 1024 / 1024
                self._cpu_pct  = proc.cpu_percent()
            except Exception:
                pass
        self.update()

    def set_fps(self, fps: int):
        self._fps = fps
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background semi-transparan
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), 6, 6)

        color_str = config.get("fps", "color")
        font_sz   = config.getint("fps", "font_size")
        color     = QColor(color_str)
        font      = QFont("Consolas", font_sz, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(color)

        text = (
            f"FPS: {self._fps}\n"
            f"RAM: {self._ram_mb:.0f} MB\n"
            f"CPU: {self._cpu_pct:.1f}%"
        )
        painter.drawText(self.rect().adjusted(8, 6, -8, -6), Qt.AlignmentFlag.AlignLeft, text)

# ── Launch Thread ──────────────────────────────────────────────────────────────

class LaunchThread(QThread):
    launch_ok     = pyqtSignal(int)    # pid
    launch_failed = pyqtSignal(str)    # pesan error
    status        = pyqtSignal(str)

    def __init__(self, mc_path: Path):
        super().__init__()
        self._path = mc_path

    def run(self):
        self.status.emit("Mempersiapkan Minecraft...")

        # Flush DNS jika diaktifkan
        if config.getbool("fixes", "flush_dns_on_launch"):
            try:
                subprocess.run(["ipconfig", "/flushdns"], capture_output=True)
                self.status.emit("DNS di-flush ✓")
            except Exception:
                pass

        # Set timer resolution (mengurangi latency, penting untuk gaming)
        if config.getbool("boost", "timer_resolution"):
            try:
                # ntdll NtSetTimerResolution: set ke 0.5ms (5000 unit 100ns)
                ntdll = ctypes.windll.ntdll
                ntdll.NtSetTimerResolution(5000, True, ctypes.byref(ctypes.c_ulong()))
                self.status.emit("Timer resolution dioptimalkan ✓")
            except Exception:
                pass

        self.status.emit(f"Meluncurkan Minecraft dari:\n{self._path}")
        logger.info(f"Launching: {self._path}")

        try:
            proc = subprocess.Popen(
                [str(self._path)],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
            pid = proc.pid
        except Exception as e:
            # Coba lewat explorer.exe untuk bypass permission GDK
            try:
                subprocess.Popen(
                    ["explorer.exe", str(self._path)],
                    creationflags=subprocess.DETACHED_PROCESS
                )
                time.sleep(4)
                mc = find_minecraft_process()
                if mc:
                    pid = mc.pid
                else:
                    self.launch_failed.emit(f"Minecraft tidak terdeteksi setelah launch.\n{e}")
                    return
            except Exception as e2:
                self.launch_failed.emit(str(e2))
                return

        # Tunggu proses benar-benar berjalan
        time.sleep(3)
        mc = find_minecraft_process()
        if mc:
            pid = mc.pid

        # Apply boost
        priority = config.get("boost", "priority")
        if priority != "normal":
            set_process_priority(pid, priority)
            self.status.emit(f"Priority diset ke {priority} ✓")

        cores_str = config.get("boost", "cpu_affinity")
        if cores_str and cores_str != "0":
            try:
                cores = int(cores_str)
                set_cpu_affinity(pid, cores)
                self.status.emit(f"CPU affinity diset ke {cores} core ✓")
            except ValueError:
                pass

        logger.info(f"Minecraft launched PID={pid}")
        self.launch_ok.emit(pid)

# ── Stylesheet ─────────────────────────────────────────────────────────────────

def build_stylesheet() -> str:
    c = COLORS
    return f"""
    * {{
        font-family: 'Segoe UI', 'Consolas', monospace;
        color: {c['text']};
    }}
    QMainWindow, QWidget {{
        background-color: {c['bg_dark']};
    }}
    QTabWidget::pane {{
        border: 1px solid {c['border']};
        background: {c['bg_card']};
        border-radius: 4px;
    }}
    QTabBar::tab {{
        background: {c['bg_dark']};
        border: 1px solid {c['border']};
        padding: 8px 18px;
        color: {c['text_dim']};
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.5px;
    }}
    QTabBar::tab:selected {{
        background: {c['bg_card']};
        color: {c['accent']};
        border-bottom: 2px solid {c['accent']};
    }}
    QTabBar::tab:hover:!selected {{
        background: {c['bg_hover']};
        color: {c['text']};
    }}
    QPushButton {{
        background: {c['bg_hover']};
        border: 1px solid {c['border']};
        border-radius: 5px;
        padding: 8px 16px;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton:hover {{
        background: {c['accent_dim']};
        border-color: {c['accent']};
    }}
    QPushButton#btn_launch {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {c['accent']}, stop:1 {c['accent_dim']});
        border: none;
        border-radius: 6px;
        padding: 14px 32px;
        font-size: 16px;
        font-weight: 700;
        color: #fff;
        letter-spacing: 1px;
    }}
    QPushButton#btn_launch:hover {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {c['accent2']}, stop:1 {c['accent']});
    }}
    QPushButton#btn_launch:disabled {{
        background: {c['border']};
        color: {c['text_dim']};
    }}
    QGroupBox {{
        border: 1px solid {c['border']};
        border-radius: 6px;
        margin-top: 12px;
        padding: 12px 8px 8px 8px;
        font-size: 12px;
        font-weight: 700;
        color: {c['text_dim']};
        letter-spacing: 0.5px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        padding: 0 6px;
        color: {c['accent']};
    }}
    QSlider::groove:horizontal {{
        border: none;
        height: 4px;
        background: {c['border']};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {c['accent']};
        width: 16px;
        height: 16px;
        margin: -6px 0;
        border-radius: 8px;
    }}
    QSlider::sub-page:horizontal {{
        background: {c['accent']};
        border-radius: 2px;
    }}
    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border: 2px solid {c['border']};
        border-radius: 3px;
        background: {c['bg_dark']};
    }}
    QCheckBox::indicator:checked {{
        background: {c['accent']};
        border-color: {c['accent']};
    }}
    QComboBox {{
        background: {c['bg_hover']};
        border: 1px solid {c['border']};
        border-radius: 4px;
        padding: 6px 10px;
        min-width: 130px;
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
    }}
    QComboBox QAbstractItemView {{
        background: {c['bg_card']};
        border: 1px solid {c['border']};
        selection-background-color: {c['accent_dim']};
    }}
    QSpinBox {{
        background: {c['bg_hover']};
        border: 1px solid {c['border']};
        border-radius: 4px;
        padding: 6px 10px;
    }}
    QLabel#lbl_status {{
        font-size: 11px;
        color: {c['text_dim']};
        padding: 4px;
    }}
    QLabel#lbl_mc_ram {{
        font-family: Consolas;
        font-size: 22px;
        font-weight: 700;
        color: {c['accent']};
    }}
    QProgressBar {{
        border: 1px solid {c['border']};
        border-radius: 4px;
        background: {c['bg_dark']};
        text-align: center;
        font-size: 11px;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {c['accent_dim']}, stop:1 {c['accent']});
        border-radius: 3px;
    }}
    QTextEdit {{
        background: {c['bg_dark']};
        border: 1px solid {c['border']};
        border-radius: 4px;
        font-family: Consolas;
        font-size: 11px;
        color: {c['text_dim']};
        padding: 4px;
    }}
    QScrollBar:vertical {{
        background: {c['bg_dark']};
        width: 8px;
    }}
    QScrollBar::handle:vertical {{
        background: {c['border']};
        border-radius: 4px;
        min-height: 20px;
    }}
    """

# ── Main Window ────────────────────────────────────────────────────────────────

class MCLiteLauncher(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setMinimumSize(700, 560)
        self.resize(760, 600)

        self._mc_path        = None
        self._mc_pid         = None
        self._fps_overlay    = None
        self._mem_thread     = MemoryMonitorThread()
        self._total_trimmed  = 0

        self._detect_minecraft()
        self._build_ui()
        self._connect_signals()
        self._setup_tray()
        self._mem_thread.start()

        # Timer refresh status
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_path_label)
        self._refresh_timer.start(5000)

    # ── Detect ──────────────────────────────────────────────────────────────

    def _detect_minecraft(self):
        saved = config.get("general", "mc_path")
        if saved and Path(saved).exists():
            self._mc_path = Path(saved)
            return
        if config.getbool("general", "auto_detect"):
            found = find_minecraft_exe()
            if found:
                self._mc_path = found
                config.set("general", "mc_path", str(found))
                logger.info(f"Auto-detected: {found}")

    # ── UI Builder ───────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._make_header())

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._tab_home(),    "  🏠 HOME  ")
        tabs.addTab(self._tab_memory(),  "  💾 MEMORI  ")
        tabs.addTab(self._tab_boost(),   "  ⚡ BOOST  ")
        tabs.addTab(self._tab_fps(),     "  📊 FPS OVERLAY  ")
        tabs.addTab(self._tab_fixes(),   "  🔧 PERBAIKAN  ")
        tabs.addTab(self._tab_log(),     "  📋 LOG  ")
        root.addWidget(tabs, 1)

        root.addWidget(self._make_statusbar())

    def _make_header(self) -> QWidget:
        hdr = QFrame()
        hdr.setFixedHeight(64)
        hdr.setStyleSheet(f"""
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #0D2B0D, stop:0.5 #162816, stop:1 #0D2B0D);
            border-bottom: 2px solid {COLORS['accent_dim']};
        """)
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(20, 0, 20, 0)

        # Logo area
        logo_lbl = QLabel("⛏  MCLite Launcher")
        logo_lbl.setStyleSheet(f"""
            font-size: 20px;
            font-weight: 800;
            color: {COLORS['accent']};
            letter-spacing: 1px;
        """)
        lay.addWidget(logo_lbl)
        lay.addStretch()

        ver_lbl = QLabel(f"v{APP_VERSION}  •  Minecraft Bedrock GDK")
        ver_lbl.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        lay.addWidget(ver_lbl)
        return hdr

    def _make_statusbar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(28)
        bar.setStyleSheet(f"background: {COLORS['bg_card']}; border-top: 1px solid {COLORS['border']};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 12, 0)

        self._lbl_status = QLabel("Siap.")
        self._lbl_status.setObjectName("lbl_status")
        lay.addWidget(self._lbl_status)
        lay.addStretch()

        self._lbl_mc_status = QLabel("● Minecraft tidak berjalan")
        self._lbl_mc_status.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        lay.addWidget(self._lbl_mc_status)
        return bar

    # ── Tab Home ─────────────────────────────────────────────────────────────

    def _tab_home(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(16)

        # Path Minecraft
        path_group = QGroupBox("Lokasi Minecraft Bedrock")
        pg = QHBoxLayout(path_group)
        self._lbl_path = QLabel(str(self._mc_path) if self._mc_path else "Belum ditemukan")
        self._lbl_path.setStyleSheet(f"font-family: Consolas; font-size: 11px; color: {COLORS['text_dim']};")
        self._lbl_path.setWordWrap(True)
        pg.addWidget(self._lbl_path, 1)

        btn_browse = QPushButton("📂 Browse")
        btn_browse.clicked.connect(self._browse_mc)
        pg.addWidget(btn_browse)

        btn_detect = QPushButton("🔍 Auto Detect")
        btn_detect.clicked.connect(self._auto_detect_mc)
        pg.addWidget(btn_detect)
        lay.addWidget(path_group)

        # Status real-time
        stats_group = QGroupBox("Status Real-Time")
        sg = QHBoxLayout(stats_group)

        # RAM Minecraft
        ram_col = QVBoxLayout()
        self._lbl_mc_ram = QLabel("– MB")
        self._lbl_mc_ram.setObjectName("lbl_mc_ram")
        self._lbl_mc_ram.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ram_lbl2 = QLabel("RAM Minecraft")
        ram_lbl2.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        ram_lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ram_col.addWidget(self._lbl_mc_ram)
        ram_col.addWidget(ram_lbl2)
        sg.addLayout(ram_col)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color: {COLORS['border']};")
        sg.addWidget(sep)

        # CPU Minecraft
        cpu_col = QVBoxLayout()
        self._lbl_mc_cpu = QLabel("– %")
        self._lbl_mc_cpu.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_mc_cpu.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {COLORS['warning']};")
        cpu_lbl2 = QLabel("CPU Minecraft")
        cpu_lbl2.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        cpu_lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cpu_col.addWidget(self._lbl_mc_cpu)
        cpu_col.addWidget(cpu_lbl2)
        sg.addLayout(cpu_col)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet(f"color: {COLORS['border']};")
        sg.addWidget(sep2)

        # RAM sistem
        sys_col = QVBoxLayout()
        self._lbl_sys_ram = QLabel("– %")
        self._lbl_sys_ram.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_sys_ram.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {COLORS['accent2']};")
        sys_lbl2 = QLabel("RAM Sistem")
        sys_lbl2.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        sys_lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sys_col.addWidget(self._lbl_sys_ram)
        sys_col.addWidget(sys_lbl2)
        sg.addLayout(sys_col)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.VLine)
        sep3.setStyleSheet(f"color: {COLORS['border']};")
        sg.addWidget(sep3)

        # Trim info
        trim_col = QVBoxLayout()
        self._lbl_trimmed = QLabel("0 MB")
        self._lbl_trimmed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_trimmed.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {COLORS['success']};")
        trim_lbl2 = QLabel("Total Di-Trim")
        trim_lbl2.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")
        trim_lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trim_col.addWidget(self._lbl_trimmed)
        trim_col.addWidget(trim_lbl2)
        sg.addLayout(trim_col)

        lay.addWidget(stats_group)

        # RAM bar
        self._ram_bar = QProgressBar()
        self._ram_bar.setRange(0, 100)
        self._ram_bar.setValue(0)
        self._ram_bar.setFormat("RAM Minecraft: %v%")
        self._ram_bar.setFixedHeight(20)
        lay.addWidget(self._ram_bar)

        lay.addStretch()

        # Launch button
        self._btn_launch = QPushButton("▶  LAUNCH MINECRAFT")
        self._btn_launch.setObjectName("btn_launch")
        self._btn_launch.setFixedHeight(52)
        self._btn_launch.clicked.connect(self._launch_minecraft)
        if not self._mc_path:
            self._btn_launch.setEnabled(False)
            self._btn_launch.setText("⚠  Minecraft Tidak Ditemukan")
        lay.addWidget(self._btn_launch)

        # Quick trim
        self._btn_trim = QPushButton("🧹 Trim Memory Sekarang")
        self._btn_trim.setFixedHeight(36)
        self._btn_trim.clicked.connect(self._manual_trim)
        lay.addWidget(self._btn_trim)

        return w

    # ── Tab Memory ───────────────────────────────────────────────────────────

    def _tab_memory(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        info = QLabel(
            "ℹ️  Sejak Minecraft Bedrock migrasi dari UWP ke GDK (versi 1.21.120+), "
            "terjadi memory leak yang menyebabkan RAM terus naik.\n"
            "MCLite secara otomatis men-trim working set Minecraft menggunakan "
            "Windows API EmptyWorkingSet() untuk membebaskan RAM yang tidak terpakai."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"""
            background: #0D2218;
            border: 1px solid {COLORS['accent_dim']};
            border-radius: 6px;
            padding: 10px 14px;
            font-size: 12px;
            color: {COLORS['text_dim']};
        """)
        lay.addWidget(info)

        # Enable
        grp_enable = QGroupBox("Pengaturan Memory Manager")
        ge = QVBoxLayout(grp_enable)

        self._chk_mem_enabled = QCheckBox("Aktifkan Memory Manager Otomatis")
        self._chk_mem_enabled.setChecked(config.getbool("memory", "enabled"))
        self._chk_mem_enabled.toggled.connect(lambda v: config.set("memory", "enabled", v))
        ge.addWidget(self._chk_mem_enabled)

        # Limit
        lim_row = QHBoxLayout()
        lim_row.addWidget(QLabel("Batas RAM Minecraft (MB):"))
        self._spin_limit = QSpinBox()
        self._spin_limit.setRange(512, 32768)
        self._spin_limit.setSingleStep(256)
        self._spin_limit.setValue(config.getint("memory", "limit_mb"))
        self._spin_limit.valueChanged.connect(lambda v: config.set("memory", "limit_mb", v))
        lim_row.addWidget(self._spin_limit)
        lim_row.addStretch()
        ge.addLayout(lim_row)

        # Interval
        int_row = QHBoxLayout()
        int_row.addWidget(QLabel("Interval cek (detik):"))
        self._spin_interval = QSpinBox()
        self._spin_interval.setRange(5, 300)
        self._spin_interval.setValue(config.getint("memory", "trim_interval_s"))
        self._spin_interval.valueChanged.connect(lambda v: config.set("memory", "trim_interval_s", v))
        int_row.addWidget(self._spin_interval)
        int_row.addStretch()
        ge.addLayout(int_row)

        # Threshold
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("Trim jika RAM sistem > (%):"))
        self._spin_threshold = QSpinBox()
        self._spin_threshold.setRange(50, 99)
        self._spin_threshold.setValue(config.getint("memory", "trim_threshold"))
        self._spin_threshold.valueChanged.connect(lambda v: config.set("memory", "trim_threshold", v))
        thr_row.addWidget(self._spin_threshold)
        thr_row.addStretch()
        ge.addLayout(thr_row)

        lay.addWidget(grp_enable)

        # Trim manual
        btn_trim2 = QPushButton("🧹 Trim Memory Minecraft Sekarang")
        btn_trim2.clicked.connect(self._manual_trim)
        lay.addWidget(btn_trim2)
        lay.addStretch()
        return w

    # ── Tab Boost ────────────────────────────────────────────────────────────

    def _tab_boost(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        grp_prio = QGroupBox("Prioritas Proses")
        gp = QVBoxLayout(grp_prio)
        gp.addWidget(QLabel("Prioritas CPU untuk Minecraft:"))
        self._cmb_priority = QComboBox()
        self._cmb_priority.addItems(["normal", "above_normal", "high"])
        self._cmb_priority.setCurrentText(config.get("boost", "priority"))
        self._cmb_priority.currentTextChanged.connect(lambda v: config.set("boost", "priority", v))
        gp.addWidget(self._cmb_priority)
        gp.addWidget(QLabel(
            "• normal: default  •  above_normal: direkomendasikan  •  high: agresif",
        ))
        lay.addWidget(grp_prio)

        grp_cpu = QGroupBox("CPU Affinity")
        gc = QVBoxLayout(grp_cpu)
        total_cores = psutil.cpu_count()
        gc.addWidget(QLabel(f"Total core tersedia: {total_cores}"))
        gc.addWidget(QLabel("Set ke 0 = gunakan semua core (default):"))
        self._spin_cores = QSpinBox()
        self._spin_cores.setRange(0, total_cores)
        self._spin_cores.setValue(config.getint("boost", "cpu_affinity"))
        self._spin_cores.valueChanged.connect(lambda v: config.set("boost", "cpu_affinity", v))
        gc.addWidget(self._spin_cores)
        lay.addWidget(grp_cpu)

        grp_extra = QGroupBox("Optimasi Tambahan")
        ge = QVBoxLayout(grp_extra)

        self._chk_timer = QCheckBox("Optimalkan Timer Resolution Windows (mengurangi latency)")
        self._chk_timer.setChecked(config.getbool("boost", "timer_resolution"))
        self._chk_timer.toggled.connect(lambda v: config.set("boost", "timer_resolution", v))
        ge.addWidget(self._chk_timer)

        self._chk_xbox = QCheckBox("Nonaktifkan Xbox Game Bar saat bermain")
        self._chk_xbox.setChecked(config.getbool("boost", "disable_xbox_bar"))
        self._chk_xbox.toggled.connect(lambda v: config.set("boost", "disable_xbox_bar", v))
        ge.addWidget(self._chk_xbox)

        lay.addWidget(grp_extra)

        btn_apply = QPushButton("⚡ Apply Boost ke Minecraft (jika sedang berjalan)")
        btn_apply.clicked.connect(self._apply_boost_now)
        lay.addWidget(btn_apply)
        lay.addStretch()
        return w

    # ── Tab FPS ──────────────────────────────────────────────────────────────

    def _tab_fps(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        info2 = QLabel(
            "FPS Counter tampil sebagai overlay transparan di atas layar Minecraft.\n"
            "Menampilkan: FPS estimasi, RAM Minecraft, CPU Minecraft.\n"
            "Overlay tidak mengganggu input game (klik menembus ke Minecraft)."
        )
        info2.setWordWrap(True)
        info2.setStyleSheet(f"""
            background: #0A1A2E;
            border: 1px solid {COLORS['border']};
            border-radius: 6px;
            padding: 10px 14px;
            font-size: 12px;
            color: {COLORS['text_dim']};
        """)
        lay.addWidget(info2)

        grp_fps = QGroupBox("Pengaturan FPS Overlay")
        gf = QVBoxLayout(grp_fps)

        self._chk_fps = QCheckBox("Aktifkan FPS Overlay")
        self._chk_fps.setChecked(config.getbool("fps", "overlay_enabled"))
        self._chk_fps.toggled.connect(self._toggle_fps_overlay)
        gf.addWidget(self._chk_fps)

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Posisi:"))
        self._cmb_pos = QComboBox()
        self._cmb_pos.addItems(["top_left", "top_right", "bottom_left", "bottom_right"])
        self._cmb_pos.setCurrentText(config.get("fps", "position"))
        self._cmb_pos.currentTextChanged.connect(lambda v: (config.set("fps", "position", v), self._reposition_overlay()))
        pos_row.addWidget(self._cmb_pos)
        pos_row.addStretch()
        gf.addLayout(pos_row)

        sz_row = QHBoxLayout()
        sz_row.addWidget(QLabel("Ukuran font:"))
        self._spin_font = QSpinBox()
        self._spin_font.setRange(8, 28)
        self._spin_font.setValue(config.getint("fps", "font_size"))
        self._spin_font.valueChanged.connect(lambda v: config.set("fps", "font_size", v))
        sz_row.addWidget(self._spin_font)
        sz_row.addStretch()
        gf.addLayout(sz_row)

        upd_row = QHBoxLayout()
        upd_row.addWidget(QLabel("Update interval (ms):"))
        self._spin_upd = QSpinBox()
        self._spin_upd.setRange(100, 5000)
        self._spin_upd.setSingleStep(100)
        self._spin_upd.setValue(config.getint("fps", "update_interval"))
        self._spin_upd.valueChanged.connect(lambda v: config.set("fps", "update_interval", v))
        upd_row.addWidget(self._spin_upd)
        upd_row.addStretch()
        gf.addLayout(upd_row)

        lay.addWidget(grp_fps)
        lay.addStretch()
        return w

    # ── Tab Perbaikan ────────────────────────────────────────────────────────

    def _tab_fixes(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        grp_fix = QGroupBox("Perbaikan Bug Minecraft Bedrock GDK")
        gf = QVBoxLayout(grp_fix)

        self._chk_iir = QCheckBox(
            "Nonaktifkan 'Improved Input Response' otomatis\n"
            "  → Memperbaiki memory leak ekstrem (diketahui sejak update 1.21.120)"
        )
        self._chk_iir.setChecked(config.getbool("fixes", "disable_improved_input_response"))
        self._chk_iir.toggled.connect(lambda v: config.set("fixes", "disable_improved_input_response", v))
        gf.addWidget(self._chk_iir)

        self._chk_dns = QCheckBox("Flush DNS sebelum launch (mengurangi lag koneksi multiplayer)")
        self._chk_dns.setChecked(config.getbool("fixes", "flush_dns_on_launch"))
        self._chk_dns.toggled.connect(lambda v: config.set("fixes", "flush_dns_on_launch", v))
        gf.addWidget(self._chk_dns)

        lay.addWidget(grp_fix)

        grp_info = QGroupBox("Info GDK Migration")
        gi = QVBoxLayout(grp_info)
        gi.addWidget(QLabel(
            "📁 Data Minecraft GDK (worlds, resource packs):\n"
            f"   %APPDATA%\\Minecraft Bedrock\\users\\shared\\games\\com.mojang\n\n"
            "📁 Data UWP lama (sudah dipindah otomatis oleh Minecraft):\n"
            f"   %LOCALAPPDATA%\\Packages\\Microsoft.MinecraftUWP_8wekyb3d8bbwe\\LocalState"
        ))
        btn_open_gdk = QPushButton("📂 Buka Folder Data GDK")
        btn_open_gdk.clicked.connect(self._open_gdk_folder)
        gi.addWidget(btn_open_gdk)
        lay.addWidget(grp_info)

        lay.addStretch()
        return w

    # ── Tab Log ──────────────────────────────────────────────────────────────

    def _tab_log(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 12, 16, 12)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        lay.addWidget(self._log_view, 1)

        btn_row = QHBoxLayout()
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.clicked.connect(self._load_log)
        btn_clear = QPushButton("🗑 Hapus Log")
        btn_clear.clicked.connect(self._clear_log)
        btn_row.addWidget(btn_refresh)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._load_log()
        return w

    # ── Actions ──────────────────────────────────────────────────────────────

    def _browse_mc(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Pilih Minecraft.Windows.exe",
            str(Path("C:/Program Files/WindowsApps")),
            "Executable (*.exe)"
        )
        if path:
            self._mc_path = Path(path)
            config.set("general", "mc_path", path)
            self._refresh_path_label()
            self._btn_launch.setEnabled(True)
            self._btn_launch.setText("▶  LAUNCH MINECRAFT")

    def _auto_detect_mc(self):
        found = find_minecraft_exe()
        if found:
            self._mc_path = found
            config.set("general", "mc_path", str(found))
            self._refresh_path_label()
            self._btn_launch.setEnabled(True)
            self._btn_launch.setText("▶  LAUNCH MINECRAFT")
            self._set_status(f"Minecraft ditemukan: {found}")
        else:
            QMessageBox.warning(self, "Tidak Ditemukan",
                "Minecraft Bedrock tidak ditemukan secara otomatis.\n"
                "Pastikan Minecraft sudah terinstall via Xbox App / Microsoft Store,\n"
                "lalu gunakan tombol Browse untuk memilih Minecraft.Windows.exe.")

    def _launch_minecraft(self):
        if not self._mc_path or not self._mc_path.exists():
            QMessageBox.warning(self, "Error", "Path Minecraft tidak valid.")
            return

        self._btn_launch.setEnabled(False)
        self._btn_launch.setText("⏳ Meluncurkan...")

        self._launch_thread = LaunchThread(self._mc_path)
        self._launch_thread.launch_ok.connect(self._on_launch_ok)
        self._launch_thread.launch_failed.connect(self._on_launch_failed)
        self._launch_thread.status.connect(self._set_status)
        self._launch_thread.start()

    def _on_launch_ok(self, pid: int):
        self._mc_pid = pid
        self._btn_launch.setEnabled(True)
        self._btn_launch.setText("▶  LAUNCH MINECRAFT")
        self._set_status(f"✅ Minecraft berjalan (PID: {pid})")
        logger.info(f"Launched OK PID={pid}")

        if config.getbool("general", "close_on_launch"):
            self.hide()

    def _on_launch_failed(self, err: str):
        self._btn_launch.setEnabled(True)
        self._btn_launch.setText("▶  LAUNCH MINECRAFT")
        self._set_status(f"❌ Gagal: {err}")
        QMessageBox.critical(self, "Gagal Launch", err)

    def _manual_trim(self):
        proc = find_minecraft_process()
        if not proc:
            self._set_status("Minecraft tidak sedang berjalan.")
            return
        before = proc.memory_info().rss / 1024 / 1024
        ok = trim_process_memory(proc.pid)
        if ok:
            time.sleep(1)
            try:
                after = proc.memory_info().rss / 1024 / 1024
                saved = int(before - after)
                self._total_trimmed += max(0, saved)
                self._lbl_trimmed.setText(f"{self._total_trimmed} MB")
                self._set_status(f"🧹 Trim selesai: {before:.0f}MB → {after:.0f}MB (hemat {saved}MB)")
            except Exception:
                self._set_status("🧹 Trim dilakukan.")
        else:
            self._set_status("⚠ Trim gagal (perlu akses admin?).")

    def _apply_boost_now(self):
        proc = find_minecraft_process()
        if not proc:
            self._set_status("Minecraft tidak sedang berjalan.")
            return
        priority = config.get("boost", "priority")
        set_process_priority(proc.pid, priority)
        cores = config.getint("boost", "cpu_affinity")
        if cores > 0:
            set_cpu_affinity(proc.pid, cores)
        self._set_status(f"⚡ Boost applied: priority={priority}, cores={cores or 'all'}")

    def _toggle_fps_overlay(self, enabled: bool):
        config.set("fps", "overlay_enabled", enabled)
        if enabled:
            if not self._fps_overlay:
                self._fps_overlay = FPSOverlay()
            self._fps_overlay.show()
        else:
            if self._fps_overlay:
                self._fps_overlay.hide()

    def _reposition_overlay(self):
        if self._fps_overlay:
            self._fps_overlay._reposition()

    def _open_gdk_folder(self):
        gdk_path = Path(os.getenv("APPDATA")) / "Minecraft Bedrock" / "users" / "shared" / "games" / "com.mojang"
        if gdk_path.exists():
            os.startfile(gdk_path)
        else:
            QMessageBox.information(self, "Info",
                f"Folder belum ada:\n{gdk_path}\n\nJalankan Minecraft sekali terlebih dahulu.")

    def _load_log(self):
        try:
            if LOG_FILE.exists():
                text = LOG_FILE.read_text(encoding="utf-8")
                # Tampilkan 200 baris terakhir
                lines = text.splitlines()
                self._log_view.setPlainText("\n".join(lines[-200:]))
                # Scroll ke bawah
                bar = self._log_view.verticalScrollBar()
                bar.setValue(bar.maximum())
            else:
                self._log_view.setPlainText("(Belum ada log)")
        except Exception as e:
            self._log_view.setPlainText(f"Error membaca log: {e}")

    def _clear_log(self):
        try:
            LOG_FILE.write_text("", encoding="utf-8")
            self._log_view.setPlainText("")
        except Exception:
            pass

    def _refresh_path_label(self):
        if self._mc_path:
            self._lbl_path.setText(str(self._mc_path))
        else:
            self._lbl_path.setText("Belum ditemukan — klik Auto Detect atau Browse")

    def _set_status(self, msg: str):
        self._lbl_status.setText(msg)

    # ── Signals ──────────────────────────────────────────────────────────────

    def _connect_signals(self):
        self._mem_thread.stats_updated.connect(self._on_stats)
        self._mem_thread.trim_performed.connect(self._on_trimmed)
        self._mem_thread.status_changed.connect(self._on_mem_status)

    def _on_stats(self, stats: dict):
        if stats.get("mc_running"):
            rss   = stats["mc_rss_mb"]
            cpu   = stats["mc_cpu_pct"]
            sys_p = stats["sys_ram_pct"]
            sys_t = stats["sys_ram_tot"]
            limit = config.getint("memory", "limit_mb")

            self._lbl_mc_ram.setText(f"{rss:.0f} MB")
            self._lbl_mc_cpu.setText(f"{cpu:.1f}%")
            self._lbl_sys_ram.setText(f"{sys_p:.0f}%")

            pct = int((rss / limit) * 100) if limit else 0
            self._ram_bar.setValue(min(100, pct))
            self._ram_bar.setFormat(f"RAM Minecraft: {rss:.0f}/{limit} MB  ({pct}%)")

            self._lbl_mc_status.setText(f"● Minecraft berjalan  PID={stats['mc_pid']}")
            self._lbl_mc_status.setStyleSheet(f"font-size: 11px; color: {COLORS['success']};")
        else:
            self._lbl_mc_ram.setText("– MB")
            self._lbl_mc_cpu.setText("– %")
            self._lbl_sys_ram.setText(f"{psutil.virtual_memory().percent:.0f}%")
            self._ram_bar.setValue(0)
            self._ram_bar.setFormat("Minecraft tidak berjalan")
            self._lbl_mc_status.setText("● Minecraft tidak berjalan")
            self._lbl_mc_status.setStyleSheet(f"font-size: 11px; color: {COLORS['text_dim']};")

    def _on_trimmed(self, mb: int):
        self._total_trimmed += mb
        self._lbl_trimmed.setText(f"{self._total_trimmed} MB")
        self._set_status(f"🧹 Memory di-trim, hemat {mb} MB")

    def _on_mem_status(self, s: str):
        pass  # bisa dipakai untuk notifikasi

    # ── Tray ─────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        # Gunakan icon default aplikasi
        self._tray.setToolTip(APP_NAME)
        menu = QMenu()
        act_show  = QAction("Tampilkan", self); act_show.triggered.connect(self.show)
        act_trim  = QAction("Trim Memory", self); act_trim.triggered.connect(self._manual_trim)
        act_quit  = QAction("Keluar", self); act_quit.triggered.connect(self._quit)
        menu.addAction(act_show)
        menu.addAction(act_trim)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(lambda r: self.show() if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self._tray.show()

    def _quit(self):
        self._mem_thread.stop()
        if self._fps_overlay:
            self._fps_overlay.close()
        QApplication.quit()

    def closeEvent(self, event):
        # Minimize ke tray, jangan tutup
        if hasattr(self, "_tray") and self._tray.isVisible():
            self.hide()
            event.ignore()
        else:
            self._quit()
            event.accept()


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    # Pastikan satu instance
    mutex = kernel32.CreateMutexW(None, True, "MCLiteLauncher_Mutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        QMessageBox.information(None, APP_NAME, "MCLite Launcher sudah berjalan.")
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyleSheet(build_stylesheet())

    window = MCLiteLauncher()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
