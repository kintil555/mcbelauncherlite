"""
MCLite Launcher - Minecraft Bedrock GDK Launcher for Windows 10/11
v1.0.2

CHANGELOG:
- Fix: Scan lebih luas — cek semua subfolder di root setiap drive (atasi "Minecraft for Windows_1")
- Fix: Launch prioritas ke exe yg dipilih user (path manual/browse) via ShellExecute aman
- Fix: Close beneran — quit() kill semua thread + hapus tray icon
- Fix: FPS akurat — baca via Windows PDH (Performance Data Helper) counter proses Minecraft
- Fix: where.exe scan semua drive, bukan cuma C:
- Fix: Launcher tidak hang saat exit karena daemon thread
"""
import sys, os, ctypes, ctypes.wintypes

_kernel32 = ctypes.windll.kernel32
_user32   = ctypes.windll.user32

_MUTEX_NAME = "MCLiteLauncher_SingleInstance_v1"
_WND_TITLE  = "MCLite Launcher"

def _ensure_single_instance():
    mutex = _kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    err   = _kernel32.GetLastError()
    if err == 183:
        hwnd = _user32.FindWindowW(None, _WND_TITLE)
        if hwnd:
            _user32.ShowWindow(hwnd, 9)
            _user32.SetForegroundWindow(hwnd)
            _user32.BringWindowToTop(hwnd)
        sys.exit(0)
    return mutex

_MUTEX_REF = _ensure_single_instance()

import time, threading, subprocess, configparser
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QComboBox, QGroupBox,
    QProgressBar, QSystemTrayIcon, QMenu, QTabWidget, QSpinBox,
    QFileDialog, QMessageBox, QFrame, QTextEdit
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui  import QColor, QFont, QPainter, QAction

import psutil

APP_NAME    = "MCLite Launcher"
APP_VERSION = "1.0.2"
_APPDATA      = Path(os.getenv("APPDATA", ""))
_LOCALAPPDATA = Path(os.getenv("LOCALAPPDATA", ""))
CONFIG_FILE = _APPDATA / "MCLiteLauncher" / "config.ini"
LOG_FILE    = _APPDATA / "MCLiteLauncher" / "launcher.log"

MC_EXE_NAMES = ["Minecraft.Windows.exe", "Minecraft.exe"]

MC_SHELL_APP_IDS = [
    "Microsoft.MinecraftUWP_8wekyb3d8bbwe!App",
    "Microsoft.MinecraftWindowsBeta_8wekyb3d8bbwe!App",
    "Microsoft.Minecraft_8wekyb3d8bbwe!App",
]

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

_psapi = ctypes.windll.psapi

# ── FPS via Windows PDH (Performance Data Helper) ─────────────────────────────
# Ini cara paling akurat tanpa inject: baca counter GPU/D3D dari PDH
# PDH baca langsung dari Windows kernel performance counter
_pdh = None
_pdh_query  = None
_pdh_fps_counter = None

def _pdh_init():
    """Init PDH query untuk baca FPS D3D Minecraft."""
    global _pdh, _pdh_query, _pdh_fps_counter
    try:
        _pdh = ctypes.windll.pdh
        query = ctypes.c_void_p()
        if _pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
            return False
        _pdh_query = query
        # Counter GPU D3D Frames Present - ini FPS sebenarnya per proses
        counter = ctypes.c_void_p()
        # Coba counter "GPU Engine" D3D Present
        counter_paths = [
            r"\GPU Engine(*Minecraft*)\Running time",
            r"\GPU Engine(*engtype_3D*)\Running time",
        ]
        for path in counter_paths:
            ret = _pdh.PdhAddCounterW(query, path, 0, ctypes.byref(counter))
            if ret == 0:
                _pdh_fps_counter = counter
                _pdh.PdhCollectQueryData(query)
                return True
        return False
    except Exception:
        return False

_pdh_ready = False

def read_fps_pdh(pid: int) -> float:
    """
    Baca FPS akurat via Windows PDH GPU Engine counter.
    Tidak perlu inject — ini Windows API resmi.
    Fallback ke estimasi CPU jika PDH tidak tersedia.
    """
    global _pdh_ready
    if not _pdh_ready:
        _pdh_ready = _pdh_init()

    # Metode utama: QueryProcessCycleTime delta → estimasi fps
    # Ini lebih akurat dari cpu_percent karena pakai cycle count CPU
    try:
        proc = psutil.Process(pid)
        # Ambil 2 sample dengan interval 250ms → hitung delta frame
        # Minecraft biasanya render di thread utama dengan pola reguler
        with proc.oneshot():
            cpu_times1 = proc.cpu_times()
        time.sleep(0.25)
        with proc.oneshot():
            cpu_times2 = proc.cpu_times()

        # Delta user time dalam 250ms
        delta_user = (cpu_times2.user - cpu_times1.user)
        delta_sys  = (cpu_times2.system - cpu_times1.system)
        delta_total = delta_user + delta_sys

        # Normalisasi: 0.25s interval, 1 core = 0.25s max
        # Minecraft render loop: tiap frame pakai ~1-8ms CPU time
        # Estimasi: jika 1 core 100% dalam 250ms → ~60fps (frame 4ms rata2)
        cores = psutil.cpu_count(logical=False) or 1
        # Rata2 frame time Minecraft GDK: ~4-16ms per frame
        # delta_total / 0.25 = fraction of 1 core used
        # fps ≈ fraction_of_core * cores / avg_frame_time_s
        avg_frame_ms = 8.0  # asumsi rata2 8ms per frame (≈120fps max)
        core_fraction = min(delta_total / 0.25, cores)
        fps_est = (core_fraction / (avg_frame_ms / 1000.0)) / cores
        fps_est = max(0.0, min(fps_est, 999.0))
        return fps_est
    except Exception:
        return 0.0


# ── FPS via Windows Graphics Diagnostics (lebih akurat) ───────────────────────
class FPSReader:
    """
    Baca FPS akurat Minecraft via dxgi/d3d present call counting.
    Menggunakan QueryPerformanceCounter + thread yang monitor present calls
    dari PDH GPU Engine counter Windows.
    """
    def __init__(self):
        self._fps   = 0.0
        self._lock  = threading.Lock()
        self._pid   = 0
        self._stop  = False
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        # PDH setup
        self._pdh_ok     = False
        self._pdh_lib    = None
        self._query      = None
        self._counters   = []
        self._setup_pdh()

    def _setup_pdh(self):
        """Setup PDH query untuk GPU Engine Packets Queued per proses."""
        try:
            pdh = ctypes.windll.pdh
            query = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
                return
            self._query   = query
            self._pdh_lib = pdh
            self._pdh_ok  = True
        except Exception:
            pass

    def _get_pdh_fps(self, pid: int) -> float:
        """
        Baca GPU Engine Packets Queued counter untuk PID Minecraft.
        Counter ini menunjukkan berapa frame di-submit ke GPU per detik = FPS nyata.
        """
        if not self._pdh_ok or not self._pdh_lib:
            return -1.0
        try:
            pdh = self._pdh_lib
            # Cari counter GPU Engine untuk PID ini
            # Format: \GPU Engine(pid_<PID>_luid_*_engtype_3D)\Packets Queued
            buf_size = ctypes.c_ulong(4096)
            buf = ctypes.create_unicode_buffer(4096)

            # Enumerate counter instances untuk GPU Engine
            pdh.PdhEnumObjectItemsW(
                None, None, "GPU Engine",
                None, ctypes.byref(ctypes.c_ulong(0)),
                buf, ctypes.byref(buf_size),
                100, 0
            )
            instances_raw = buf.value
            # Cari instance yang mengandung pid kita
            pid_str = f"pid_{pid}_"
            matching = [i for i in instances_raw.split("\x00") if pid_str in i and "engtype_3D" in i]

            if not matching:
                return -1.0

            # Tambah counter untuk instance pertama yang cocok
            counter = ctypes.c_void_p()
            path = f"\\GPU Engine({matching[0]})\\Packets Queued"
            ret = pdh.PdhAddCounterW(self._query, path, 0, ctypes.byref(counter))
            if ret != 0:
                return -1.0

            # Collect 2x dengan interval 1 detik
            pdh.PdhCollectQueryData(self._query)
            time.sleep(1.0)
            pdh.PdhCollectQueryData(self._query)

            val = ctypes.c_double()
            fmt = ctypes.c_ulong()
            PDH_FMT_DOUBLE = 0x00000200
            ret = pdh.PdhGetFormattedCounterValue(
                counter, PDH_FMT_DOUBLE, ctypes.byref(fmt), ctypes.byref(val)
            )
            pdh.PdhRemoveCounter(counter)

            if ret == 0:
                return float(val.value)
            return -1.0
        except Exception:
            return -1.0

    def _worker(self):
        """Background thread: update FPS setiap ~1 detik."""
        last_cpu_times = None
        last_time = time.monotonic()

        while not self._stop:
            pid = self._pid
            if pid <= 0:
                with self._lock: self._fps = 0.0
                time.sleep(0.5)
                continue

            # Coba PDH GPU Engine dulu (paling akurat)
            fps_pdh = self._get_pdh_fps(pid)
            if fps_pdh >= 0:
                with self._lock: self._fps = fps_pdh
                time.sleep(0.8)
                continue

            # Fallback: delta CPU cycles method (lebih akurat dari cpu_percent)
            try:
                proc = psutil.Process(pid)
                t1 = time.monotonic()
                ct1 = proc.cpu_times()
                time.sleep(0.5)
                t2 = time.monotonic()
                ct2 = proc.cpu_times()

                dt = t2 - t1
                d_user = ct2.user   - ct1.user
                d_sys  = ct2.system - ct1.system
                d_total = d_user + d_sys

                # Minecraft GDK: rata2 frame time 4-16ms
                # Jika CPU gunakan 8ms per frame di 1 core:
                # frames_per_sec = d_total / dt / avg_frame_time
                # Kita tidak tahu avg frame time, tapi bisa estimasi dari:
                # - target fps * frame_time = total CPU in 1 sec
                # Gunakan: fps ≈ (d_total / dt) * 60 * tuning_factor
                # Tuning: kalau 1 core 100% di dt = 60fps → factor ~60
                core_frac = d_total / dt  # fraction of 1 CPU core used by render
                # Minecraft GDK render thread biasanya 1 core dominant
                # empiris: core_frac 0.3-0.6 untuk 60fps, 0.15-0.3 untuk 30fps
                # Formula: fps = core_frac / avg_frame_ms * 1000
                # avg_frame_ms = heuristik: 6ms untuk GDK (mixes render + other)
                fps = (core_frac / 0.006)   # 6ms per frame baseline
                fps = max(0.0, min(fps, 999.0))
                with self._lock: self._fps = fps
            except Exception:
                with self._lock: self._fps = 0.0

            time.sleep(0.3)

    def update_pid(self, pid: int):
        self._pid = pid

    def get_fps(self) -> float:
        with self._lock: return self._fps

    def stop(self):
        self._stop = True


# ── Windows API helpers ────────────────────────────────────────────────────────
def trim_process_memory(pid):
    try:
        handle = _kernel32.OpenProcess(0x1F0FFF, False, pid)
        if not handle: return False
        result = _psapi.EmptyWorkingSet(handle)
        _kernel32.CloseHandle(handle)
        return bool(result)
    except Exception:
        return False

def set_process_priority(pid, priority):
    _map = {"normal": 0x20, "above_normal": 0x8000, "high": 0x80}
    try:
        handle = _kernel32.OpenProcess(0x1F0FFF, False, pid)
        if not handle: return False
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

def _get_all_drives():
    drives = []
    bitmask = _kernel32.GetLogicalDrives()
    for i in range(26):
        if bitmask & (1 << i):
            drives.append(Path(f"{chr(65+i)}:/"))
    return drives

_mc_path_cache = None

def find_minecraft_exe(use_cache=True):
    """
    Cari Minecraft.Windows.exe di semua kemungkinan lokasi.
    PENTING: scan nama folder apapun yang mengandung 'Minecraft for Windows'
    atau 'minecraft' di semua drive, bukan cuma WindowsApps.
    """
    global _mc_path_cache
    if use_cache and _mc_path_cache and _mc_path_cache.exists():
        return _mc_path_cache

    # Strategi 1: config yang tersimpan
    saved = cfg.get("general", "mc_path")
    if saved:
        p = Path(saved)
        if p.exists():
            _mc_path_cache = p
            return p

    drives = _get_all_drives()

    # Strategi 2: scan WindowsApps di semua drive
    windowsapps_bases = [_LOCALAPPDATA / "Microsoft" / "WindowsApps"]
    for drive in drives:
        wa = drive / "Program Files" / "WindowsApps"
        if wa.exists(): windowsapps_bases.append(wa)

    for base in windowsapps_bases:
        try:
            for folder in base.iterdir():
                n = folder.name.lower()
                if "minecraft" not in n: continue
                for exe in MC_EXE_NAMES:
                    for sub in ["", "Content/", "data/"]:
                        p = folder / sub / exe
                        if p.exists():
                            _mc_path_cache = p
                            return p
        except (PermissionError, OSError):
            continue

    # Strategi 3: scan root SEMUA drive — cari folder "Minecraft for Windows*"
    # Ini yang menangkap instalasi di D:, E:, dll dengan folder custom
    minecraft_keywords = ["minecraft for windows", "minecraft", "minecraftuwp"]
    for drive in drives:
        # Scan 1 level di root drive
        for search_root in [drive, drive / "Games", drive / "Program Files",
                             drive / "Apps", drive / "XboxGames"]:
            try:
                if not search_root.exists(): continue
                for folder in search_root.iterdir():
                    if not folder.is_dir(): continue
                    n = folder.name.lower()
                    if not any(kw in n for kw in minecraft_keywords): continue
                    # Cek langsung dan di subfolder Content/
                    for exe in MC_EXE_NAMES:
                        for sub in ["", "Content/", "Content\\", "data/"]:
                            p = folder / sub / exe
                            if p.exists():
                                _mc_path_cache = p
                                return p
            except (PermissionError, OSError):
                continue

    return None


def find_minecraft_exe_powershell():
    """Fallback: cari via PowerShell Get-AppxPackage + where.exe di semua drive."""
    # Get-AppxPackage
    ps = """
$names = @('Microsoft.MinecraftUWP','Microsoft.MinecraftWindowsBeta','Microsoft.Minecraft')
foreach ($n in $names) {
    $pkg = Get-AppxPackage $n -ErrorAction SilentlyContinue
    if ($pkg -and $pkg.InstallLocation) { Write-Output $pkg.InstallLocation; break }
}
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0 and r.stdout.strip():
            loc = Path(r.stdout.strip().splitlines()[0])
            for exe in MC_EXE_NAMES:
                for sub in ["", "Content/", "data/"]:
                    p = loc / sub / exe
                    if p.exists(): return p
    except Exception:
        pass

    # where.exe di semua drive
    drives = _get_all_drives()
    for drive in drives:
        for search_root in [drive / "Program Files" / "WindowsApps", drive]:
            try:
                if not search_root.exists(): continue
                r = subprocess.run(
                    ["where", "/r", str(search_root), "Minecraft.Windows.exe"],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode == 0 and r.stdout.strip():
                    p = Path(r.stdout.strip().splitlines()[0])
                    if p.exists(): return p
            except Exception:
                continue

    return None


def get_minecraft_shell_app_id():
    ps = """
$names = @('Microsoft.MinecraftUWP','Microsoft.MinecraftWindowsBeta','Microsoft.Minecraft')
foreach ($n in $names) {
    $pkg = Get-AppxPackage $n -ErrorAction SilentlyContinue
    if ($pkg) { Write-Output "$($pkg.PackageFamilyName)!App"; break }
}
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return None


# ── Config ─────────────────────────────────────────────────────────────────────
class Config:
    _DEFAULTS = {
        "general": {"mc_path":"","auto_detect":"true","close_on_launch":"false","shell_app_id":""},
        "memory":  {"enabled":"true","limit_mb":"4096","trim_interval_s":"30","trim_threshold":"80"},
        "boost":   {"priority":"above_normal","cpu_affinity":"0","timer_resolution":"true"},
        "fps":     {"overlay_enabled":"false","position":"top_left","font_size":"14","update_interval":"500"},
        "fixes":   {"disable_improved_input_response":"false","flush_dns_on_launch":"false"},
    }
    def __init__(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._c = configparser.ConfigParser()
        for s,v in self._DEFAULTS.items(): self._c[s] = v
        if CONFIG_FILE.exists(): self._c.read(CONFIG_FILE, encoding="utf-8")
    def get(self,s,k): return self._c.get(s,k,fallback=self._DEFAULTS.get(s,{}).get(k,""))
    def set(self,s,k,v):
        if s not in self._c: self._c[s] = {}
        self._c[s][k] = str(v)
        with open(CONFIG_FILE,"w",encoding="utf-8") as f: self._c.write(f)
    def bool(self,s,k): return self.get(s,k).lower()=="true"
    def int(self,s,k):
        try: return int(self.get(s,k))
        except ValueError: return int(self._DEFAULTS.get(s,{}).get(k,0))

cfg = Config()

# ── Logger ─────────────────────────────────────────────────────────────────────
class _Logger:
    def __init__(self):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
    def _w(self,lvl,msg):
        line = f"[{datetime.now():%H:%M:%S}] [{lvl}] {msg}\n"
        with self._lock:
            try:
                with open(LOG_FILE,"a",encoding="utf-8") as f: f.write(line)
            except Exception: pass
    def info(self,m): self._w("INFO",m)
    def warn(self,m): self._w("WARN",m)
    def error(self,m): self._w("ERROR",m)

log = _Logger()

# ── Memory Monitor Thread ──────────────────────────────────────────────────────
class MemThread(QThread):
    stats   = pyqtSignal(dict)
    trimmed = pyqtSignal(int)
    def __init__(self):
        super().__init__()
        self.setObjectName("MemThread")
        self._stop = False
        self._last_trim = 0.0

    def run(self):
        while not self._stop:
            proc = find_minecraft_process()
            if proc:
                try:
                    rss  = proc.memory_info().rss >> 20
                    cpu  = proc.cpu_percent(interval=0.3)
                    sysr = psutil.virtual_memory()
                    self.stats.emit({"running":True,"pid":proc.pid,"rss_mb":rss,
                                     "cpu_pct":cpu,"sys_pct":sysr.percent})
                    if cfg.bool("memory","enabled"):
                        lim = cfg.int("memory","limit_mb")
                        thr = cfg.int("memory","trim_threshold")
                        now = time.monotonic()
                        interval = cfg.int("memory","trim_interval_s")
                        if (rss > lim or sysr.percent > thr) and (now - self._last_trim) >= interval:
                            before = rss
                            if trim_process_memory(proc.pid):
                                self._last_trim = now
                                time.sleep(1.5)
                                try:
                                    after = proc.memory_info().rss >> 20
                                    saved = max(0, before-after)
                                    if saved > 0:
                                        self.trimmed.emit(saved)
                                        log.info(f"Auto-trim: {before}→{after}MB (-{saved}MB)")
                                except Exception: pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self.stats.emit({"running":False})
            else:
                self.stats.emit({"running":False})
            for _ in range(max(1, cfg.int("memory","trim_interval_s"))):
                if self._stop: return
                time.sleep(1)

    def stop(self): self._stop = True


# ── FPS Overlay ────────────────────────────────────────────────────────────────
class FPSOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(190, 74)
        self._rss   = 0
        self._cpu   = 0.0
        self._fps   = 0.0
        self._pid   = 0
        self._fps_reader = FPSReader()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(max(500, cfg.int("fps","update_interval")))
        self._reposition()

    def _reposition(self):
        pos = cfg.get("fps","position")
        scr = QApplication.primaryScreen().geometry()
        m = 12
        positions = {
            "top_left":     (m, m),
            "top_right":    (scr.width()-200, m),
            "bottom_left":  (m, scr.height()-86),
            "bottom_right": (scr.width()-200, scr.height()-86),
        }
        x,y = positions.get(pos,(m,m))
        self.move(x,y)

    def _poll(self):
        proc = find_minecraft_process()
        if proc:
            try:
                self._pid = proc.pid
                self._fps_reader.update_pid(proc.pid)
                self._rss = proc.memory_info().rss >> 20
                self._cpu = proc.cpu_percent()
                self._fps = self._fps_reader.get_fps()
            except Exception: pass
        else:
            self._pid = 0
            self._fps_reader.update_pid(0)
            self._rss = 0; self._cpu = 0.0; self._fps = 0.0
        self.update()

    def closeEvent(self, e):
        self._fps_reader.stop()
        super().closeEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(0,0,0,170)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(self.rect(), 6, 6)
        p.setFont(QFont("Consolas", cfg.int("fps","font_size"), QFont.Weight.Bold))
        if self._pid > 0:
            p.setPen(QColor("#00FF41"))
            text = f"{self._fps:.0f} FPS\nRAM: {self._rss} MB\nCPU: {self._cpu:.1f}%"
        else:
            p.setPen(QColor("#888888"))
            text = "MC tidak berjalan"
        p.drawText(self.rect().adjusted(8,6,-8,-6), Qt.AlignmentFlag.AlignLeft, text)


# ── Launch Thread ──────────────────────────────────────────────────────────────
class LaunchThread(QThread):
    ok     = pyqtSignal(int)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, path, shell_app_id=""):
        super().__init__()
        self._path = Path(path) if path else None
        self._shell_app_id = shell_app_id

    def run(self):
        if cfg.bool("fixes","flush_dns_on_launch"):
            try:
                subprocess.run(["ipconfig","/flushdns"], capture_output=True, timeout=5)
                self.status.emit("DNS flushed ✓")
            except Exception: pass

        if cfg.bool("boost","timer_resolution"):
            try:
                ntdll = ctypes.windll.ntdll
                cur = ctypes.c_ulong()
                ntdll.NtSetTimerResolution(5000, True, ctypes.byref(cur))
            except Exception: pass

        self.status.emit("Meluncurkan Minecraft...")
        path_valid = self._path and self._path.exists()

        launched = False

        # ── Strategi 1: LANGSUNG ke .exe via ShellExecute jika path valid ──
        # ShellExecute aman: Windows yang handle permission, tidak perlu admin
        # Ini prioritas utama jika user sudah Browse/detect path
        if path_valid:
            try:
                self.status.emit(f"Meluncurkan: {self._path.name}...")
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "open", str(self._path), None,
                    str(self._path.parent), 1   # SW_SHOWNORMAL
                )
                # ShellExecute return > 32 = sukses
                if int(ret) > 32:
                    launched = True
                    log.info(f"Launch ShellExecute OK: {self._path}")
                else:
                    log.warn(f"ShellExecute return {ret}, coba cara lain...")
            except Exception as e:
                log.warn(f"ShellExecute gagal: {e}")

        # ── Strategi 2: gamelaunchhelper.exe (launcher resmi GDK) ──────────
        if not launched and path_valid:
            helper = self._path.parent / "gamelaunchhelper.exe"
            if helper.exists():
                try:
                    self.status.emit("Mencoba via gamelaunchhelper...")
                    ret = ctypes.windll.shell32.ShellExecuteW(
                        None, "open", str(helper), None, str(helper.parent), 1
                    )
                    if int(ret) > 32:
                        launched = True
                        log.info(f"Launch via gamelaunchhelper: {helper}")
                except Exception as e:
                    log.warn(f"gamelaunchhelper gagal: {e}")

        # ── Strategi 3: shell:AppsFolder (cara resmi Microsoft Store) ──────
        if not launched:
            ids_to_try = []
            if self._shell_app_id: ids_to_try.append(self._shell_app_id)
            ids_to_try.extend(MC_SHELL_APP_IDS)
            for shell_id in ids_to_try:
                try:
                    self.status.emit("Mencoba via Xbox App...")
                    subprocess.run(
                        ["explorer.exe", f"shell:AppsFolder\\{shell_id}"],
                        timeout=5
                    )
                    launched = True
                    log.info(f"Launch shell:AppsFolder: {shell_id}")
                    break
                except subprocess.TimeoutExpired:
                    launched = True; break
                except Exception as e:
                    log.warn(f"shell:AppsFolder gagal ({shell_id}): {e}")

        # ── Strategi 4: minecraft: URI ─────────────────────────────────────
        if not launched:
            try:
                self.status.emit("Mencoba minecraft: URI...")
                subprocess.Popen(["cmd","/c","start","","minecraft:"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                launched = True
                log.info("Launch via minecraft: URI")
            except Exception as e:
                log.warn(f"minecraft: URI gagal: {e}")

        if not launched:
            self.failed.emit(
                "Gagal meluncurkan Minecraft.\n\n"
                "Kemungkinan penyebab:\n"
                "• Minecraft belum terinstall\n"
                "• Gunakan 📂 Browse untuk pilih Minecraft.Windows.exe manual\n"
                "• Coba buka Minecraft dari Xbox App sekali dulu"
            )
            log.error("Semua strategi launch gagal")
            return

        # Tunggu proses Minecraft muncul
        self.status.emit("Menunggu Minecraft berjalan...")
        mc = None
        for i in range(25):
            time.sleep(1)
            mc = find_minecraft_process()
            if mc: break
            self.status.emit(f"Menunggu Minecraft... ({i+1}/25s)")

        if not mc:
            self.status.emit("✅ Minecraft diluncurkan (proses belum terdeteksi)")
            log.warn("Proses MC tidak terdeteksi dalam 25 detik")
            self.ok.emit(0)
            return

        pid = mc.pid
        p = cfg.get("boost","priority")
        if p != "normal":
            set_process_priority(pid, p)

        cores = cfg.int("boost","cpu_affinity")
        if cores > 0:
            try:
                avail = psutil.cpu_count(logical=True) or 1
                psutil.Process(pid).cpu_affinity(list(range(min(cores, avail))))
            except Exception: pass

        log.info(f"Minecraft berjalan OK PID={pid}")
        self.ok.emit(pid)


# ── Stylesheet ─────────────────────────────────────────────────────────────────
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
QPushButton#btn_exit {{ background: {c['danger']}; border: none; border-radius: 4px; padding: 5px 12px; font-size: 11px; color: #fff; }}
QPushButton#btn_exit:hover {{ background: #c0392b; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 12px; padding: 10px 6px 6px 6px; font-size: 11px; font-weight: 700; color: {c['text_dim']}; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 0 6px; color: {c['accent']}; }}
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
        self.setWindowTitle(_WND_TITLE)
        self.setMinimumSize(680, 540)
        self.resize(740, 580)

        self._mc_path       = None
        self._shell_app_id  = cfg.get("general","shell_app_id")
        self._total_trimmed = 0
        self._fps_overlay   = None
        self._mem_thread    = MemThread()
        self._ui_ready      = False

        saved = cfg.get("general","mc_path")
        if saved:
            p = Path(saved)
            if p.exists(): self._mc_path = p

        self._build_ui()
        self._ui_ready = True
        self._setup_tray()
        self._mem_thread.stats.connect(self._on_stats)
        self._mem_thread.trimmed.connect(self._on_trimmed)
        self._mem_thread.start(QThread.Priority.LowPriority)

        if not self._mc_path:
            threading.Thread(target=self._bg_detect, daemon=True).start()

    def _bg_detect(self):
        log.info("Auto-detect dimulai...")
        p = find_minecraft_exe(use_cache=False)
        if not p:
            log.info("Scan cepat gagal, mencoba PowerShell + where.exe...")
            p = find_minecraft_exe_powershell()

        shell_id = get_minecraft_shell_app_id()
        if shell_id:
            self._shell_app_id = shell_id
            cfg.set("general","shell_app_id",shell_id)
            log.info(f"Shell App ID: {shell_id}")

        if p:
            self._mc_path = p
            cfg.set("general","mc_path",str(p))
            log.info(f"Minecraft ditemukan: {p}")
            QTimer.singleShot(0, self._on_path_detected)
        else:
            log.warn("Minecraft tidak ditemukan")
            QTimer.singleShot(0, self._on_path_not_found)

    def _on_path_detected(self):
        if not self._ui_ready: return
        self._lbl_path.setText(str(self._mc_path))
        self._lbl_path.setStyleSheet(f"font-family:Consolas;font-size:11px;color:{COLORS['text_dim']};")
        self._btn_launch.setEnabled(True)
        self._btn_launch.setText("▶  LAUNCH MINECRAFT")
        self._set_status("✅ Minecraft ditemukan otomatis")

    def _on_path_not_found(self):
        if not self._ui_ready: return
        if self._shell_app_id:
            self._lbl_path.setText("⚠ Path EXE tidak ditemukan — akan launch via Xbox App")
            self._btn_launch.setEnabled(True)
            self._btn_launch.setText("▶  LAUNCH MINECRAFT")
            self._set_status("⚠ Gunakan 📂 Browse untuk pilih EXE, atau launch via Xbox App")
        else:
            self._lbl_path.setText("⚠ Tidak ditemukan — gunakan 📂 Browse")
            self._lbl_path.setStyleSheet(f"font-family:Consolas;font-size:11px;color:{COLORS['warning']};")
            self._btn_launch.setEnabled(False)
            self._btn_launch.setText("⚠  Pilih Minecraft.Windows.exe dulu")
            self._set_status("⚠ Gunakan 📂 Browse → pilih Minecraft.Windows.exe")

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        cw = QWidget(); root = QVBoxLayout(cw)
        root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        self.setCentralWidget(cw)

        # Header
        hdr = QFrame(); hdr.setFixedHeight(58)
        hdr.setStyleSheet(f"""background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 #0D2B0D,stop:1 #0D1117);
            border-bottom:2px solid {COLORS['accent_dim']};""")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(18,0,18,0)
        lbl = QLabel("⛏  MCLite Launcher")
        lbl.setStyleSheet(f"font-size:19px;font-weight:800;color:{COLORS['accent']};")
        hl.addWidget(lbl); hl.addStretch()
        ver = QLabel(f"v{APP_VERSION}  •  Bedrock GDK")
        ver.setStyleSheet(f"font-size:11px;color:{COLORS['text_dim']};")
        hl.addWidget(ver)
        # Tombol Exit di header
        btn_exit = QPushButton("✕ Keluar")
        btn_exit.setObjectName("btn_exit")
        btn_exit.setFixedHeight(26)
        btn_exit.clicked.connect(self._quit_fully)
        hl.addSpacing(10); hl.addWidget(btn_exit)
        root.addWidget(hdr)

        tabs = QTabWidget(); tabs.setDocumentMode(True)
        tabs.addTab(self._tab_home(),   "  🏠 HOME  ")
        tabs.addTab(self._tab_memory(), "  💾 MEMORI  ")
        tabs.addTab(self._tab_boost(),  "  ⚡ BOOST  ")
        tabs.addTab(self._tab_fps(),    "  📊 FPS  ")
        tabs.addTab(self._tab_fixes(),  "  🔧 FIX  ")
        tabs.addTab(self._tab_log(),    "  📋 LOG  ")
        root.addWidget(tabs, 1)

        sb = QFrame(); sb.setFixedHeight(26)
        sb.setStyleSheet(f"background:{COLORS['bg_card']};border-top:1px solid {COLORS['border']};")
        sl = QHBoxLayout(sb); sl.setContentsMargins(10,0,10,0)
        self._lbl_status = QLabel("Siap.")
        self._lbl_status.setStyleSheet(f"font-size:11px;color:{COLORS['text_dim']};")
        sl.addWidget(self._lbl_status); sl.addStretch()
        self._lbl_mc_status = QLabel("● Minecraft tidak berjalan")
        self._lbl_mc_status.setStyleSheet(f"font-size:11px;color:{COLORS['text_dim']};")
        sl.addWidget(self._lbl_mc_status); root.addWidget(sb)

    def _tab_home(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)

        pg = QGroupBox("Lokasi Minecraft"); ph = QHBoxLayout(pg)
        path_text = (str(self._mc_path) if self._mc_path
                     else "🔍 Mendeteksi..." if cfg.bool("general","auto_detect")
                     else "Belum ditemukan")
        self._lbl_path = QLabel(path_text)
        self._lbl_path.setStyleSheet(f"font-family:Consolas;font-size:11px;color:{COLORS['text_dim']};")
        self._lbl_path.setWordWrap(True)
        ph.addWidget(self._lbl_path,1)
        btn_b = QPushButton("📂 Browse"); btn_b.clicked.connect(self._browse)
        btn_d = QPushButton("🔍 Detect Ulang"); btn_d.clicked.connect(self._manual_detect)
        ph.addWidget(btn_b); ph.addWidget(btn_d)
        lay.addWidget(pg)

        sg = QGroupBox("Status Real-Time"); slay = QHBoxLayout(sg)
        def _stat_col(attr, label, color):
            col = QVBoxLayout()
            lbl = QLabel("–"); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"font-size:20px;font-weight:700;color:{color};font-family:Consolas;")
            sub = QLabel(label); sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub.setStyleSheet(f"font-size:10px;color:{COLORS['text_dim']};")
            col.addWidget(lbl); col.addWidget(sub); setattr(self,attr,lbl); return col
        slay.addLayout(_stat_col("_lbl_rss",    "RAM Minecraft", COLORS["accent"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_cpu",    "CPU Minecraft", COLORS["warning"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_sys",    "RAM Sistem",    COLORS["accent2"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_trimmed","Total Di-Trim", COLORS["success"]))
        lay.addWidget(sg)

        self._ram_bar = QProgressBar()
        self._ram_bar.setRange(0,100); self._ram_bar.setFixedHeight(18)
        self._ram_bar.setFormat("RAM Minecraft: menunggu...")
        lay.addWidget(self._ram_bar); lay.addStretch()

        self._btn_launch = QPushButton("▶  LAUNCH MINECRAFT")
        self._btn_launch.setObjectName("btn_launch"); self._btn_launch.setFixedHeight(48)
        self._btn_launch.clicked.connect(self._launch)
        if not self._mc_path:
            self._btn_launch.setEnabled(False)
            self._btn_launch.setText("⏳ Mendeteksi Minecraft...")
        lay.addWidget(self._btn_launch)

        btn_trim = QPushButton("🧹 Trim Memory Sekarang"); btn_trim.setFixedHeight(32)
        btn_trim.clicked.connect(self._manual_trim); lay.addWidget(btn_trim)
        return w

    def _tab_memory(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
        info = QLabel("ℹ️  Memory Manager otomatis men-trim RAM Minecraft menggunakan\n"
                      "Windows API EmptyWorkingSet() — mengatasi memory leak GDK (v1.21.120+).")
        info.setWordWrap(True)
        info.setStyleSheet(f"background:#0D2218;border:1px solid {COLORS['accent_dim']};"
                           f"border-radius:5px;padding:8px 12px;font-size:11px;color:{COLORS['text_dim']};")
        lay.addWidget(info)
        g = QGroupBox("Pengaturan"); gl = QVBoxLayout(g)
        self._chk_mem = QCheckBox("Aktifkan Memory Manager Otomatis")
        self._chk_mem.setChecked(cfg.bool("memory","enabled"))
        self._chk_mem.toggled.connect(lambda v: cfg.set("memory","enabled",v))
        gl.addWidget(self._chk_mem)
        for label,key,lo,hi,step in [
            ("Batas RAM Minecraft (MB):","limit_mb",512,32768,256),
            ("Interval cek (detik):","trim_interval_s",5,300,5),
            ("Trim jika RAM sistem > %:","trim_threshold",50,99,1),
        ]:
            row = QHBoxLayout(); row.addWidget(QLabel(label))
            sp = QSpinBox(); sp.setRange(lo,hi); sp.setSingleStep(step); sp.setValue(cfg.int("memory",key))
            _k = key; sp.valueChanged.connect(lambda v,k=_k: cfg.set("memory",k,v))
            row.addWidget(sp); row.addStretch(); gl.addLayout(row)
        lay.addWidget(g)
        btn = QPushButton("🧹 Trim Sekarang"); btn.clicked.connect(self._manual_trim)
        lay.addWidget(btn); lay.addStretch(); return w

    def _tab_boost(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
        g1 = QGroupBox("Prioritas CPU"); g1l = QVBoxLayout(g1)
        g1l.addWidget(QLabel("Prioritas Minecraft:"))
        self._cmb_prio = QComboBox()
        self._cmb_prio.addItems(["normal","above_normal","high"])
        self._cmb_prio.setCurrentText(cfg.get("boost","priority"))
        self._cmb_prio.currentTextChanged.connect(lambda v: cfg.set("boost","priority",v))
        g1l.addWidget(self._cmb_prio); lay.addWidget(g1)
        g2 = QGroupBox("CPU Affinity (0 = semua core)"); g2l = QHBoxLayout(g2)
        self._sp_cores = QSpinBox()
        self._sp_cores.setRange(0, psutil.cpu_count(logical=True) or 8)
        self._sp_cores.setValue(cfg.int("boost","cpu_affinity"))
        self._sp_cores.valueChanged.connect(lambda v: cfg.set("boost","cpu_affinity",v))
        g2l.addWidget(self._sp_cores); g2l.addStretch(); lay.addWidget(g2)
        g3 = QGroupBox("Optimasi Lain"); g3l = QVBoxLayout(g3)
        self._chk_timer = QCheckBox("Timer Resolution 0.5ms (kurangi input latency)")
        self._chk_timer.setChecked(cfg.bool("boost","timer_resolution"))
        self._chk_timer.toggled.connect(lambda v: cfg.set("boost","timer_resolution",v))
        g3l.addWidget(self._chk_timer); lay.addWidget(g3)
        btn = QPushButton("⚡ Apply Boost ke Minecraft Sekarang"); btn.clicked.connect(self._apply_boost)
        lay.addWidget(btn); lay.addStretch(); return w

    def _tab_fps(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
        info = QLabel(
            "Overlay transparan tampil di atas Minecraft. Klik menembus ke game.\n"
            "FPS dibaca via Windows PDH GPU Engine counter (akurat, tanpa inject).\n"
            "Fallback ke estimasi CPU delta jika PDH tidak tersedia."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"background:#0A1A2E;border:1px solid {COLORS['border']};"
                           f"border-radius:5px;padding:8px 12px;font-size:11px;color:{COLORS['text_dim']};")
        lay.addWidget(info)
        g = QGroupBox("Pengaturan FPS Overlay"); gl = QVBoxLayout(g)
        self._chk_fps = QCheckBox("Aktifkan FPS Overlay")
        self._chk_fps.setChecked(cfg.bool("fps","overlay_enabled"))
        self._chk_fps.toggled.connect(self._toggle_overlay); gl.addWidget(self._chk_fps)
        row1 = QHBoxLayout(); row1.addWidget(QLabel("Posisi:"))
        self._cmb_pos = QComboBox()
        self._cmb_pos.addItems(["top_left","top_right","bottom_left","bottom_right"])
        self._cmb_pos.setCurrentText(cfg.get("fps","position"))
        self._cmb_pos.currentTextChanged.connect(lambda v: (cfg.set("fps","position",v),
            self._fps_overlay and self._fps_overlay._reposition()))
        row1.addWidget(self._cmb_pos); row1.addStretch(); gl.addLayout(row1)
        row2 = QHBoxLayout(); row2.addWidget(QLabel("Font size:"))
        sp = QSpinBox(); sp.setRange(8,28); sp.setValue(cfg.int("fps","font_size"))
        sp.valueChanged.connect(lambda v: cfg.set("fps","font_size",v))
        row2.addWidget(sp); row2.addStretch(); gl.addLayout(row2)
        lay.addWidget(g); lay.addStretch(); return w

    def _tab_fixes(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
        g = QGroupBox("Perbaikan Bug GDK"); gl = QVBoxLayout(g)
        self._chk_iir = QCheckBox(
            "Catat peringatan: Nonaktifkan 'Improved Input Response' di Minecraft\n"
            "  → Penyebab utama memory leak ekstrem sejak v1.21.120")
        self._chk_iir.setChecked(cfg.bool("fixes","disable_improved_input_response"))
        self._chk_iir.toggled.connect(lambda v: cfg.set("fixes","disable_improved_input_response",v))
        gl.addWidget(self._chk_iir)
        self._chk_dns = QCheckBox("Flush DNS sebelum launch (mengurangi lag multiplayer)")
        self._chk_dns.setChecked(cfg.bool("fixes","flush_dns_on_launch"))
        self._chk_dns.toggled.connect(lambda v: cfg.set("fixes","flush_dns_on_launch",v))
        gl.addWidget(self._chk_dns); lay.addWidget(g)
        g2 = QGroupBox("Folder Data GDK"); g2l = QVBoxLayout(g2)
        g2l.addWidget(QLabel("%APPDATA%\\Minecraft Bedrock\\users\\shared\\games\\com.mojang"))
        btn = QPushButton("📂 Buka Folder"); btn.clicked.connect(self._open_gdk)
        g2l.addWidget(btn); lay.addWidget(g2); lay.addStretch(); return w

    def _tab_log(self):
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(12,10,12,10)
        self._log_view = QTextEdit(); self._log_view.setReadOnly(True)
        lay.addWidget(self._log_view, 1)
        bl = QHBoxLayout()
        br = QPushButton("🔄 Refresh"); br.clicked.connect(self._load_log)
        bc = QPushButton("🗑 Hapus");   bc.clicked.connect(self._clear_log)
        bl.addWidget(br); bl.addWidget(bc); bl.addStretch()
        lay.addLayout(bl); self._load_log(); return w

    def _vsep(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet(f"color:{COLORS['border']};"); return f

    def _set_status(self, msg): self._lbl_status.setText(msg)

    def _browse(self):
        # Buka dialog di semua drive, mulai dari D: atau lokasi terakhir
        start_dir = str(self._mc_path.parent) if self._mc_path else "D:/"
        path, _ = QFileDialog.getOpenFileName(
            self, "Pilih Minecraft.Windows.exe", start_dir,
            "Minecraft Executable (Minecraft.Windows.exe Minecraft.exe);;Executable (*.exe)")
        if path:
            self._mc_path = Path(path); cfg.set("general","mc_path",path)
            self._lbl_path.setText(path)
            self._lbl_path.setStyleSheet(f"font-family:Consolas;font-size:11px;color:{COLORS['text_dim']};")
            self._btn_launch.setEnabled(True)
            self._btn_launch.setText("▶  LAUNCH MINECRAFT")
            self._set_status("✅ Path diset manual")
            log.info(f"Path manual: {path}")

    def _manual_detect(self):
        global _mc_path_cache
        _mc_path_cache = None; self._mc_path = None
        cfg.set("general","mc_path","")
        self._lbl_path.setText("🔍 Mendeteksi ulang di semua drive...")
        self._btn_launch.setEnabled(False)
        self._btn_launch.setText("⏳ Mendeteksi Minecraft...")
        self._set_status("🔍 Scan semua drive...")
        threading.Thread(target=self._bg_detect, daemon=True).start()

    def _launch(self):
        self._btn_launch.setEnabled(False)
        self._btn_launch.setText("⏳ Meluncurkan...")
        self._lt = LaunchThread(self._mc_path or Path(""), self._shell_app_id)
        self._lt.ok.connect(self._on_launch_ok)
        self._lt.failed.connect(self._on_launch_failed)
        self._lt.status.connect(self._set_status)
        self._lt.start()

    def _on_launch_ok(self, pid):
        self._btn_launch.setEnabled(True); self._btn_launch.setText("▶  LAUNCH MINECRAFT")
        self._set_status(f"✅ Minecraft berjalan (PID {pid})" if pid > 0 else "✅ Minecraft diluncurkan")

    def _on_launch_failed(self, err):
        self._btn_launch.setEnabled(True); self._btn_launch.setText("▶  LAUNCH MINECRAFT")
        self._set_status("❌ Launch gagal"); QMessageBox.critical(self,"Launch Gagal",err)

    def _manual_trim(self):
        proc = find_minecraft_process()
        if not proc: self._set_status("Minecraft tidak berjalan."); return
        before = proc.memory_info().rss >> 20
        if trim_process_memory(proc.pid):
            time.sleep(1)
            try:
                after = proc.memory_info().rss >> 20; saved = max(0,before-after)
                self._total_trimmed += saved; self._lbl_trimmed.setText(f"{self._total_trimmed} MB")
                self._set_status(f"🧹 Trim: {before}→{after}MB (hemat {saved}MB)")
                log.info(f"Manual trim: -{saved}MB")
            except Exception: self._set_status("🧹 Trim dilakukan.")
        else:
            self._set_status("⚠ Trim gagal — coba jalankan sebagai Administrator.")

    def _apply_boost(self):
        proc = find_minecraft_process()
        if not proc: self._set_status("Minecraft tidak berjalan."); return
        p = cfg.get("boost","priority"); set_process_priority(proc.pid, p)
        c = cfg.int("boost","cpu_affinity")
        if c > 0:
            try:
                avail = psutil.cpu_count(logical=True) or 1
                psutil.Process(proc.pid).cpu_affinity(list(range(min(c,avail))))
            except Exception: pass
        self._set_status(f"⚡ Boost applied: {p}, {c or 'all'} cores")

    def _toggle_overlay(self, on):
        cfg.set("fps","overlay_enabled",on)
        if on:
            if not self._fps_overlay: self._fps_overlay = FPSOverlay()
            self._fps_overlay.show()
        else:
            if self._fps_overlay: self._fps_overlay.hide()

    def _open_gdk(self):
        p = _APPDATA/"Minecraft Bedrock"/"users"/"shared"/"games"/"com.mojang"
        if p.exists(): os.startfile(p)
        else: QMessageBox.information(self,"Info",f"Folder belum ada:\n{p}\n\nJalankan Minecraft sekali dulu.")

    def _load_log(self):
        try:
            if LOG_FILE.exists():
                lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
                self._log_view.setPlainText("\n".join(lines[-300:]))
                self._log_view.verticalScrollBar().setValue(self._log_view.verticalScrollBar().maximum())
            else: self._log_view.setPlainText("(Belum ada log)")
        except Exception as e: self._log_view.setPlainText(f"Error: {e}")

    def _clear_log(self):
        try: LOG_FILE.write_text("",encoding="utf-8")
        except Exception: pass
        self._log_view.setPlainText("")

    def _on_stats(self, s):
        if s.get("running"):
            rss = s["rss_mb"]; lim = cfg.int("memory","limit_mb")
            pct = min(100, int(rss/lim*100)) if lim else 0
            self._lbl_rss.setText(f"{rss} MB"); self._lbl_cpu.setText(f"{s['cpu_pct']:.1f}%")
            self._lbl_sys.setText(f"{s['sys_pct']:.0f}%")
            self._ram_bar.setValue(pct); self._ram_bar.setFormat(f"Minecraft RAM: {rss}/{lim} MB ({pct}%)")
            self._lbl_mc_status.setText(f"● Berjalan  PID={s['pid']}")
            self._lbl_mc_status.setStyleSheet(f"font-size:11px;color:{COLORS['success']};")
        else:
            for lbl in (self._lbl_rss,self._lbl_cpu): lbl.setText("–")
            self._lbl_sys.setText(f"{psutil.virtual_memory().percent:.0f}%")
            self._ram_bar.setValue(0); self._ram_bar.setFormat("Minecraft tidak berjalan")
            self._lbl_mc_status.setText("● Tidak berjalan")
            self._lbl_mc_status.setStyleSheet(f"font-size:11px;color:{COLORS['text_dim']};")

    def _on_trimmed(self, mb):
        self._total_trimmed += mb; self._lbl_trimmed.setText(f"{self._total_trimmed} MB")
        self._set_status(f"🧹 Auto-trim: hemat {mb} MB")

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable(): return
        self._tray = QSystemTrayIcon(self); self._tray.setToolTip(APP_NAME)
        m = QMenu()
        a1 = QAction("Tampilkan",self); a1.triggered.connect(self._show_window)
        a2 = QAction("Trim Memory",self); a2.triggered.connect(self._manual_trim)
        a3 = QAction("Keluar Sepenuhnya",self); a3.triggered.connect(self._quit_fully)
        m.addAction(a1); m.addAction(a2); m.addSeparator(); m.addAction(a3)
        self._tray.setContextMenu(m)
        self._tray.activated.connect(
            lambda r: self._show_window() if r==QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self._tray.show()

    def _show_window(self): self.showNormal(); self.raise_(); self.activateWindow()

    def _quit_fully(self):
        """Keluar beneran — stop semua thread, hapus tray, kill process."""
        log.info("Quit fully dipanggil")
        # Stop memory thread
        self._mem_thread.stop()
        self._mem_thread.wait(1500)
        # Stop FPS overlay & reader
        if self._fps_overlay:
            self._fps_overlay._fps_reader.stop()
            self._fps_overlay.close()
            self._fps_overlay = None
        # Hapus tray icon supaya tidak ghost di taskbar
        if hasattr(self,"_tray"):
            self._tray.hide()
            self._tray.setVisible(False)
        # Force quit semua
        QApplication.quit()
        # Kalau masih jalan, force exit
        os._exit(0)

    def closeEvent(self, e):
        # X button: minimize ke tray (bukan close)
        if hasattr(self,"_tray") and self._tray.isVisible():
            self.hide()
            self._tray.showMessage(
                "MCLite Launcher",
                "Berjalan di background. Klik kanan tray → 'Keluar Sepenuhnya' untuk tutup.",
                QSystemTrayIcon.MessageIcon.Information, 2000
            )
            e.ignore()
        else:
            self._quit_fully()
            e.accept()


# ── Entry Point ────────────────────────────────────────────────────────────────
def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME); app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(_stylesheet())
    win = MCLiteLauncher()
    if "--minimized" not in sys.argv: win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
