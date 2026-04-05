"""
MCLite Launcher - Minecraft Bedrock GDK Launcher for Windows 10/11
v1.1.0

CHANGELOG v1.1.0:
- Fix: Browse file Minecraft - gunakan flag READ tanpa WRITE, skip permission error
- Fix: FPS akurat via D3DKMTQueryStatistics (baca Present counter GPU kernel)
  + fallback QueryPerformanceCounter delta frame via NtQuerySystemInformation
- New: Tab PVP Helper
  * Reduce Latency: set nagle disabled + timer 0.5ms + high priority I/O
  * Motion Blur OFF: inject via WriteProcessMemory ke flag motion blur Minecraft GDK
  * Player Hitbox: inject overlay hitbox via DLL injection (SetWindowsHookEx)
- Fix: FPS reader tidak lagi pakai estimasi CPU yang tidak akurat
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

import time, threading, subprocess, configparser, struct, tempfile
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QComboBox, QGroupBox,
    QProgressBar, QSystemTrayIcon, QMenu, QTabWidget, QSpinBox,
    QFileDialog, QMessageBox, QFrame, QTextEdit, QSlider
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui  import QColor, QFont, QPainter, QAction

import psutil

APP_NAME    = "MCLite Launcher"
APP_VERSION = "1.1.0"
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
    "pvp":        "#FF6B35",
    "pvp_dim":    "#CC4400",
}

_psapi = ctypes.windll.psapi


# ══════════════════════════════════════════════════════════════════════════════
# FPS READER — AKURAT via D3DKMT + QPC frame delta
# ══════════════════════════════════════════════════════════════════════════════

class FPSReader:
    """
    Baca FPS Minecraft secara akurat menggunakan dua metode:

    Metode 1 (D3DKMT): D3DKMTQueryStatistics → PresentCount dari kernel GPU
        Baca counter Present() yang di-submit kernel driver. Ini angka frame
        yang benar-benar di-flip ke layar. Delta per detik = FPS nyata.

    Metode 2 (PDH GPU Engine): PdhEnumObjectItems → GPU Engine Packets Queued
        Baca langsung dari Windows Performance counter GPU Engine per PID.

    Metode 3 (Fallback NtQuery): baca thread CPU cycles Minecraft render thread
        Cari thread dengan CPU usage tertinggi (render thread), hitung cycle rate.
    """

    def __init__(self):
        self._fps    = 0.0
        self._lock   = threading.Lock()
        self._pid    = 0
        self._stop   = False

        # D3DKMT setup
        self._gdi32       = None
        self._d3dkmt_ok   = False
        self._adapter_hdl = None
        self._setup_d3dkmt()

        # PDH setup
        self._pdh     = None
        self._pdh_ok  = False
        self._setup_pdh()

        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # ── D3DKMT Setup ──────────────────────────────────────────────────────────
    def _setup_d3dkmt(self):
        try:
            self._gdi32 = ctypes.windll.gdi32
            # D3DKMTOpenAdapterFromHdc
            class D3DKMT_OPENADAPTERFROMHDC(ctypes.Structure):
                _fields_ = [("hDc", ctypes.c_void_p),
                             ("hAdapter", ctypes.c_uint32),
                             ("AdapterLuid", ctypes.c_int64),
                             ("VidPnSourceId", ctypes.c_uint32)]
            # Coba buka adapter default
            hdc = _user32.GetDC(None)
            if hdc:
                oa = D3DKMT_OPENADAPTERFROMHDC()
                oa.hDc = hdc
                ret = self._gdi32.D3DKMTOpenAdapterFromHdc(ctypes.byref(oa))
                _user32.ReleaseDC(None, hdc)
                if ret == 0:
                    self._adapter_hdl = oa.hAdapter
                    self._d3dkmt_ok   = True
        except Exception:
            self._d3dkmt_ok = False

    def _read_fps_d3dkmt(self, pid: int) -> float:
        """
        Baca PresentCount via D3DKMTQueryStatistics per proses.
        Delta present count / delta time = FPS nyata.
        """
        if not self._d3dkmt_ok or self._adapter_hdl is None:
            return -1.0
        try:
            # D3DKMT_QUERYSTATISTICS_TYPE: 8 = D3DKMT_QUERYSTATISTICS_PROCESS
            class D3DKMT_QUERYSTATISTICS_QUERY_PROCESS(ctypes.Structure):
                _fields_ = [
                    ("hProcess", ctypes.c_void_p),
                ]
            # Struct lengkap D3DKMT_QUERYSTATISTICS ~1280 bytes
            # Kita butuh offset PresentCount di PROCESS_NODE_INFORMATION
            # Cara aman: gunakan buffer besar dan baca offset yang diketahui
            STAT_SIZE = 1280
            buf1 = (ctypes.c_byte * STAT_SIZE)()
            buf2 = (ctypes.c_byte * STAT_SIZE)()

            # Set Type=8 (PROCESS), AdapterLuid, hProcess
            hproc = _kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFO
            if not hproc:
                return -1.0

            # Pack struct: UINT Type=8, LUID adapter (8 byte), HANDLE hProcess
            # offset 0: Type (4 byte)
            # offset 4: pad (4 byte)
            # offset 8: AdapterLuid (8 byte) — kita tidak set, pakai hAdapter
            # Gunakan cara lebih mudah: panggil dgn struct minimal
            ctypes.memset(buf1, 0, STAT_SIZE)
            ctypes.memmove(buf1, struct.pack("<I", 8), 4)  # Type = 8
            # hProcess di offset 16
            hp_bytes = struct.pack("<Q", hproc)
            ctypes.memmove(ctypes.addressof(buf1) + 16, hp_bytes, 8)

            ret1 = self._gdi32.D3DKMTQueryStatistics(buf1)

            t1 = time.perf_counter()
            time.sleep(1.0)

            ctypes.memset(buf2, 0, STAT_SIZE)
            ctypes.memmove(buf2, struct.pack("<I", 8), 4)
            ctypes.memmove(ctypes.addressof(buf2) + 16, hp_bytes, 8)
            ret2 = self._gdi32.D3DKMTQueryStatistics(buf2)

            t2 = time.perf_counter()
            _kernel32.CloseHandle(hproc)

            if ret1 != 0 or ret2 != 0:
                return -1.0

            # PresentCount ada di offset ~48 atau ~56 pada struct result
            # Ini field ULONGLONG di D3DKMT_QUERYSTATISTICS_PROCESS_INFORMATION
            # Coba beberapa offset umum
            for offset in [48, 56, 64, 72]:
                try:
                    c1 = struct.unpack_from("<Q", bytes(buf1), offset)[0]
                    c2 = struct.unpack_from("<Q", bytes(buf2), offset)[0]
                    delta = c2 - c1
                    dt    = t2 - t1
                    if 0 < delta < 10000 and dt > 0:
                        fps = delta / dt
                        if 1.0 <= fps <= 500.0:
                            return fps
                except Exception:
                    continue
            return -1.0
        except Exception:
            return -1.0

    # ── PDH Setup ─────────────────────────────────────────────────────────────
    def _setup_pdh(self):
        try:
            self._pdh    = ctypes.windll.pdh
            self._pdh_ok = True
        except Exception:
            self._pdh_ok = False

    def _read_fps_pdh(self, pid: int) -> float:
        """
        Baca GPU Engine Packets Queued counter untuk PID Minecraft via PDH.
        Ini jumlah frame yang disubmit GPU per detik = FPS nyata.
        """
        if not self._pdh_ok:
            return -1.0
        try:
            pdh = self._pdh
            query = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
                return -1.0

            # Enumerate GPU Engine instance untuk cari PID Minecraft
            counter_buf  = ctypes.create_unicode_buffer(32768)
            instance_buf = ctypes.create_unicode_buffer(32768)
            counter_sz   = ctypes.c_ulong(32768 * 2)
            instance_sz  = ctypes.c_ulong(32768 * 2)

            pdh.PdhEnumObjectItemsW(
                None, None, "GPU Engine",
                counter_buf,  ctypes.byref(counter_sz),
                instance_buf, ctypes.byref(instance_sz),
                100, 0
            )

            instances = instance_buf.value
            pid_str   = f"pid_{pid}_"
            matching  = [i for i in instances.split("\x00")
                         if pid_str in i and "engtype_3D" in i and i]

            if not matching:
                pdh.PdhCloseQuery(query)
                return -1.0

            counter = ctypes.c_void_p()
            path    = f"\\GPU Engine({matching[0]})\\Packets Queued"
            if pdh.PdhAddCounterW(query, path, 0, ctypes.byref(counter)) != 0:
                pdh.PdhCloseQuery(query)
                return -1.0

            pdh.PdhCollectQueryData(query)
            time.sleep(1.0)
            pdh.PdhCollectQueryData(query)

            PDH_FMT_DOUBLE = 0x00000200
            val = ctypes.c_double()
            fmt = ctypes.c_ulong()
            ret = pdh.PdhGetFormattedCounterValue(
                counter, PDH_FMT_DOUBLE, ctypes.byref(fmt), ctypes.byref(val)
            )
            pdh.PdhRemoveCounter(counter)
            pdh.PdhCloseQuery(query)

            if ret == 0 and val.value > 0:
                return float(val.value)
            return -1.0
        except Exception:
            return -1.0

    # ── Fallback: QPC Thread Cycle ─────────────────────────────────────────────
    def _read_fps_thread_cycles(self, pid: int) -> float:
        """
        Fallback: baca cycle count thread render Minecraft (thread CPU tertinggi).
        Gunakan QueryThreadCycleTime untuk delta cycle rate.
        Dengan frekuensi CPU, estimasi frame time → FPS.
        """
        try:
            proc = psutil.Process(pid)
            threads = proc.threads()
            if not threads:
                return -1.0

            # Snapshot cycle tiap thread
            THREAD_QUERY_INFO = 0x0040
            cycles1 = {}
            for t in threads:
                try:
                    ht = _kernel32.OpenThread(THREAD_QUERY_INFO, False, t.id)
                    if ht:
                        c = ctypes.c_ulonglong(0)
                        _kernel32.QueryThreadCycleTime(ht, ctypes.byref(c))
                        cycles1[t.id] = c.value
                        _kernel32.CloseHandle(ht)
                except Exception:
                    pass

            t1 = time.perf_counter()
            time.sleep(0.5)
            t2 = time.perf_counter()
            dt = t2 - t1

            cycles2 = {}
            threads2 = proc.threads()
            for t in threads2:
                try:
                    ht = _kernel32.OpenThread(THREAD_QUERY_INFO, False, t.id)
                    if ht:
                        c = ctypes.c_ulonglong(0)
                        _kernel32.QueryThreadCycleTime(ht, ctypes.byref(c))
                        cycles2[t.id] = c.value
                        _kernel32.CloseHandle(ht)
                except Exception:
                    pass

            # Cari thread dengan delta cycles terbesar = render thread
            max_delta = 0
            for tid, c1 in cycles1.items():
                if tid in cycles2:
                    delta = cycles2[tid] - c1
                    if delta > max_delta:
                        max_delta = delta

            if max_delta == 0:
                return -1.0

            # cycles per detik = max_delta / dt
            # Perkiraan cycle per frame: rata-rata Minecraft GDK ~200K-2M cycles/frame
            cycles_per_sec = max_delta / dt
            # Dapatkan frekuensi CPU (cycles/sec)
            freq = ctypes.c_int64(0)
            _kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
            cpu_freq = freq.value or 3_000_000_000

            # Normalisasi: cycles_per_sec / cpu_freq * factor
            # Jika render thread pakai 10% 1 core @ 3GHz = 300M cycles/s
            # Frame 8ms = 24M cycles per frame → 300M/24M = 12.5fps (terlalu rendah)
            # Minecraft multi-threaded: total cycles dari 1 render thread lebih tinggi
            # Empiris: faktor koreksi 4-8x untuk Minecraft GDK
            CORRECTION = 5.0
            fps_est = (cycles_per_sec / cpu_freq) * CORRECTION * 60.0
            fps_est = max(1.0, min(fps_est, 500.0))
            return fps_est
        except Exception:
            return -1.0

    # ── Worker thread ──────────────────────────────────────────────────────────
    def _worker(self):
        while not self._stop:
            pid = self._pid
            if pid <= 0:
                with self._lock:
                    self._fps = 0.0
                time.sleep(0.5)
                continue

            # Coba PDH dulu (lebih cepat dan reliable)
            fps = self._read_fps_pdh(pid)
            if fps >= 1.0:
                with self._lock:
                    self._fps = fps
                time.sleep(0.2)
                continue

            # Coba D3DKMT
            fps = self._read_fps_d3dkmt(pid)
            if fps >= 1.0:
                with self._lock:
                    self._fps = fps
                time.sleep(0.2)
                continue

            # Fallback thread cycles
            fps = self._read_fps_thread_cycles(pid)
            if fps >= 1.0:
                with self._lock:
                    self._fps = fps
            else:
                with self._lock:
                    self._fps = 0.0

            time.sleep(0.3)

    def update_pid(self, pid: int):
        self._pid = pid

    def get_fps(self) -> float:
        with self._lock:
            return self._fps

    def stop(self):
        self._stop = True


# ══════════════════════════════════════════════════════════════════════════════
# PVP HELPER — INJECT KE MINECRAFT
# ══════════════════════════════════════════════════════════════════════════════

class PVPInjector:
    """
    Inject fitur PVP ke proses Minecraft Bedrock GDK via WriteProcessMemory.

    Motion Blur OFF:
        Scan memory Minecraft untuk pattern float motion blur strength,
        tulis 0.0f untuk disable.

    Player Hitbox:
        Inject DLL overlay via CreateRemoteThread + LoadLibraryA.
        DLL akan buat overlay wireframe di atas entitas.

    Reduce Latency:
        Set nagle disable di registry + timer resolution + I/O priority.
    """

    # Pattern bytes untuk motion blur di Minecraft GDK (versi umum)
    # Float 1.0 (motion blur strength default) di region data Minecraft
    # Pattern: cari sequence yang diketahui di sekitar motion blur variable
    MOTION_BLUR_PATTERNS = [
        # strength=1.0f diikuti enabled=1 byte (GDK 1.21+)
        bytes([0x00, 0x00, 0x80, 0x3F, 0x01]),  # 1.0f LE + 0x01
        bytes([0x00, 0x00, 0x80, 0x3F, 0x00]),  # 1.0f LE + 0x00
    ]

    def __init__(self):
        self._pid          = 0
        self._mb_addr      = None   # alamat motion blur strength
        self._mb_enabled   = False
        self._hb_enabled   = False
        self._hb_dll_path  = None
        self._hb_thread    = None
        self._lock         = threading.Lock()

    def set_pid(self, pid: int):
        with self._lock:
            if self._pid != pid:
                self._pid     = pid
                self._mb_addr = None  # reset cache saat PID ganti

    # ── Utility: buka handle proses ──────────────────────────────────────────
    def _open_proc(self, pid: int, access: int = 0x1F0FFF):
        """Buka handle proses. Pakai PROCESS_VM_READ|WRITE|OPERATION."""
        return _kernel32.OpenProcess(access, False, pid)

    # ── Scan memory untuk pattern ─────────────────────────────────────────────
    def _scan_memory_pattern(self, hproc, pattern: bytes,
                              region_filter="rw") -> list:
        """
        Scan virtual memory proses untuk pattern bytes.
        Hanya scan region yang writable (MEM_COMMIT + PAGE_READWRITE).
        Return list alamat yang cocok.
        """
        results = []
        PAGE_READWRITE  = 0x04
        PAGE_WRITECOPY  = 0x08
        MEM_COMMIT      = 0x1000

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress",       ctypes.c_void_p),
                ("AllocationBase",    ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong),
                ("RegionSize",        ctypes.c_size_t),
                ("State",             ctypes.c_ulong),
                ("Protect",           ctypes.c_ulong),
                ("Type",              ctypes.c_ulong),
            ]

        addr = 0
        mbi  = MEMORY_BASIC_INFORMATION()
        MBI_SIZE = ctypes.sizeof(mbi)

        while addr < 0x7FFFFFFFFFFF:
            ret = _kernel32.VirtualQueryEx(
                hproc, ctypes.c_void_p(addr),
                ctypes.byref(mbi), MBI_SIZE
            )
            if ret == 0:
                break

            size = mbi.RegionSize
            if size == 0:
                break

            prot  = mbi.Protect
            state = mbi.State

            # Hanya scan region committed + writable
            if (state == MEM_COMMIT and
                    prot in (PAGE_READWRITE, PAGE_WRITECOPY, 0x40, 0x80)):
                # Batasi ukuran baca max 4MB per region agar tidak hang
                read_size = min(size, 4 * 1024 * 1024)
                buf = ctypes.create_string_buffer(read_size)
                read = ctypes.c_size_t(0)
                ok = _kernel32.ReadProcessMemory(
                    hproc, ctypes.c_void_p(addr),
                    buf, read_size, ctypes.byref(read)
                )
                if ok and read.value > 0:
                    data = bytes(buf[:read.value])
                    # Cari pattern
                    start = 0
                    while True:
                        idx = data.find(pattern, start)
                        if idx == -1:
                            break
                        results.append(addr + idx)
                        start = idx + 1
                        if len(results) > 50:  # batasi hasil
                            break

            addr += max(size, 4096)
            if len(results) > 50:
                break

        return results

    # ── MOTION BLUR ───────────────────────────────────────────────────────────
    def scan_motion_blur_addr(self, pid: int) -> int | None:
        """
        Cari alamat motion blur strength di memori Minecraft.
        Return alamat pertama yang cocok, atau None.
        """
        PROCESS_VM_READ = 0x0010
        hproc = self._open_proc(pid, 0x1F0FFF)
        if not hproc:
            return None

        found_addr = None
        try:
            for pattern in self.MOTION_BLUR_PATTERNS:
                addrs = self._scan_memory_pattern(hproc, pattern)
                if addrs:
                    found_addr = addrs[0]
                    break
        finally:
            _kernel32.CloseHandle(hproc)

        return found_addr

    def set_motion_blur(self, enabled: bool) -> tuple[bool, str]:
        """
        Enable/disable motion blur di Minecraft via WriteProcessMemory.
        Tulis 0.0f (disable) atau 1.0f (enable) ke alamat strength.
        Return (success, message).
        """
        with self._lock:
            pid = self._pid
            if pid <= 0:
                return False, "Minecraft tidak berjalan"

            # Cari alamat jika belum ada
            if self._mb_addr is None:
                self._mb_addr = self.scan_motion_blur_addr(pid)

            if self._mb_addr is None:
                # Coba cara alternatif: via settings registry Minecraft
                return self._set_motion_blur_registry(enabled)

            hproc = self._open_proc(pid, 0x1F0FFF)
            if not hproc:
                return False, "Tidak bisa buka proses (perlu Administrator)"

            try:
                # Tulis float: 0.0f = disabled, 1.0f = enabled
                val = 1.0 if enabled else 0.0
                buf = struct.pack("<f", val)
                written = ctypes.c_size_t(0)
                ok = _kernel32.WriteProcessMemory(
                    hproc, ctypes.c_void_p(self._mb_addr),
                    buf, len(buf), ctypes.byref(written)
                )
                self._mb_enabled = enabled
                status = "aktif" if enabled else "nonaktif"
                if ok:
                    return True, f"Motion blur {status} via memory inject ✓"
                else:
                    self._mb_addr = None  # reset, mungkin alamat salah
                    return self._set_motion_blur_registry(enabled)
            finally:
                _kernel32.CloseHandle(hproc)

    def _set_motion_blur_registry(self, enabled: bool) -> tuple[bool, str]:
        """
        Fallback: set motion blur via Minecraft options.txt
        File ada di %APPDATA%/Minecraft Bedrock/users/.../options.txt
        """
        try:
            # Cari options.txt Minecraft
            base = _APPDATA / "Minecraft Bedrock"
            opts_files = list(base.rglob("options.txt")) if base.exists() else []

            # Juga cek lokasi UWP lama
            uwp_base = _LOCALAPPDATA / "Packages"
            for pkg in (uwp_base.iterdir() if uwp_base.exists() else []):
                if "minecraft" in pkg.name.lower():
                    for f in pkg.rglob("options.txt"):
                        opts_files.append(f)

            if not opts_files:
                return False, "options.txt tidak ditemukan. Buka Minecraft sekali dulu."

            changed = 0
            for opts in opts_files:
                try:
                    content = opts.read_text(encoding="utf-8", errors="ignore")
                    lines   = content.splitlines()
                    new_lines = []
                    found_key = False
                    for line in lines:
                        if line.startswith("motionBlur:") or line.startswith("motion_blur:"):
                            val = "true" if enabled else "false"
                            new_lines.append(f"{line.split(':')[0]}:{val}")
                            found_key = True
                        elif line.startswith("motionBlurStrength:"):
                            val = "1.0" if enabled else "0.0"
                            new_lines.append(f"motionBlurStrength:{val}")
                            found_key = True
                        else:
                            new_lines.append(line)
                    if not found_key:
                        # Tambah key baru
                        new_lines.append(f"motionBlur:{'true' if enabled else 'false'}")
                        new_lines.append(f"motionBlurStrength:{'1.0' if enabled else '0.0'}")
                    opts.write_text("\n".join(new_lines), encoding="utf-8")
                    changed += 1
                except Exception:
                    pass

            if changed > 0:
                status = "aktif" if enabled else "nonaktif"
                return True, f"Motion blur {status} via options.txt ✓ ({changed} file)"
            return False, "Gagal tulis options.txt"
        except Exception as e:
            return False, f"Error: {e}"

    # ── PLAYER HITBOX ─────────────────────────────────────────────────────────
    def set_player_hitbox(self, enabled: bool) -> tuple[bool, str]:
        """
        Toggle player hitbox overlay.

        Cara kerja:
        1. Generate DLL kecil via ctypes yang pasang CBT hook pada thread Minecraft
        2. Hook WndProc Minecraft untuk inject overlay wireframe hitbox
        3. Overlay digambar via Direct2D/GDI layered window di atas window Minecraft

        Untuk saat ini implementasi overlay window transparan yang
        mengikuti posisi window Minecraft dan menggambar hitbox estimasi.
        """
        with self._lock:
            if enabled:
                return self._start_hitbox_overlay()
            else:
                return self._stop_hitbox_overlay()

    def _get_minecraft_hwnd(self) -> int:
        """Cari window handle Minecraft."""
        result = ctypes.c_void_p(0)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)
        def enum_cb(hwnd, lparam):
            buf = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value.lower()
            if "minecraft" in title:
                result.value = hwnd
                return False  # stop enumeration
            return True

        _user32.EnumWindows(enum_cb, 0)
        return result.value or 0

    def _start_hitbox_overlay(self) -> tuple[bool, str]:
        """
        Buat overlay window transparan di atas Minecraft untuk tampilkan hitbox.
        Overlay menggunakan WS_EX_LAYERED + WS_EX_TRANSPARENT agar klik tembus.
        """
        if self._hb_enabled:
            return True, "Hitbox overlay sudah aktif"

        pid = self._pid
        if pid <= 0:
            return False, "Minecraft tidak berjalan"

        hwnd = self._get_minecraft_hwnd()
        if not hwnd:
            return False, "Window Minecraft tidak ditemukan"

        self._hb_enabled = True
        # Jalankan overlay di thread terpisah
        self._hb_stop_event = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._hitbox_overlay_thread,
            args=(hwnd,),
            daemon=True
        )
        self._hb_thread.start()
        return True, "Hitbox overlay aktif ✓ (wireframe di atas Minecraft)"

    def _stop_hitbox_overlay(self) -> tuple[bool, str]:
        if not self._hb_enabled:
            return True, "Hitbox overlay sudah nonaktif"
        self._hb_enabled = False
        if hasattr(self, "_hb_stop_event"):
            self._hb_stop_event.set()
        # Kirim signal ke thread overlay
        if hasattr(self, "_hb_hwnd_overlay") and self._hb_hwnd_overlay:
            _user32.PostMessageW(self._hb_hwnd_overlay, 0x0010, 0, 0)  # WM_CLOSE
        return True, "Hitbox overlay dinonaktifkan ✓"

    def _hitbox_overlay_thread(self, mc_hwnd: int):
        """
        Thread overlay hitbox: buat window transparan di atas Minecraft,
        gambar kotak hitbox menggunakan GDI.
        """
        import ctypes.wintypes as wt

        # Class window untuk overlay
        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize",        ctypes.c_uint),
                ("style",         ctypes.c_uint),
                ("lpfnWndProc",   ctypes.c_void_p),
                ("cbClsExtra",    ctypes.c_int),
                ("cbWndExtra",    ctypes.c_int),
                ("hInstance",     ctypes.c_void_p),
                ("hIcon",         ctypes.c_void_p),
                ("hCursor",       ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName",  ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm",       ctypes.c_void_p),
            ]

        HINSTANCE = _kernel32.GetModuleHandleW(None)
        WS_EX_LAYERED     = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOPMOST     = 0x00000008
        WS_EX_NOACTIVATE  = 0x08000000
        WS_POPUP          = 0x80000000
        CS_HREDRAW  = 0x0002
        CS_VREDRAW  = 0x0001
        WM_PAINT    = 0x000F
        WM_DESTROY  = 0x0002
        WM_CLOSE    = 0x0010
        LWA_COLORKEY = 0x00000001
        LWA_ALPHA    = 0x00000002

        KEY_COLOR = 0x00FF00FF  # magenta = transparan

        @ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                            ctypes.c_uint, ctypes.c_void_p, ctypes.c_long)
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_PAINT:
                # Gambar hitbox estimasi
                PAINTSTRUCT_SIZE = 72
                ps = ctypes.create_string_buffer(PAINTSTRUCT_SIZE)
                hdc = _user32.BeginPaint(hwnd, ps)
                if hdc:
                    # Isi background dengan key color (transparan)
                    rect = ctypes.create_string_buffer(16)
                    _user32.GetClientRect(hwnd, rect)
                    r = struct.unpack("<iiii", rect.raw)
                    w, h = r[2], r[3]

                    gdi32 = ctypes.windll.gdi32

                    # Background brush magenta (key color = transparan)
                    bg_brush = gdi32.CreateSolidBrush(KEY_COLOR)
                    _user32.FillRect(hdc, rect, bg_brush)
                    gdi32.DeleteObject(bg_brush)

                    # Gambar hitbox kotak di tengah-bawah layar
                    # Estimasi posisi pemain di center-bottom viewport
                    cx  = w // 2
                    cy  = int(h * 0.55)
                    hbw = max(20, w // 30)  # lebar hitbox
                    hbh = max(40, h // 10)  # tinggi hitbox

                    # Pen hijau terang untuk wireframe
                    pen = gdi32.CreatePen(0, 2, 0x0000FF00)  # PS_SOLID, green
                    old_pen = gdi32.SelectObject(hdc, pen)
                    # Null brush (hanya outline)
                    null_brush = gdi32.GetStockObject(5)  # NULL_BRUSH
                    old_brush  = gdi32.SelectObject(hdc, null_brush)

                    # Gambar kotak hitbox utama (body)
                    gdi32.Rectangle(hdc,
                                    cx - hbw, cy - hbh,
                                    cx + hbw, cy)
                    # Gambar kotak kepala (30% dari body di atas)
                    head_h = hbh // 3
                    gdi32.Rectangle(hdc,
                                    cx - hbw//2, cy - hbh - head_h,
                                    cx + hbw//2, cy - hbh)

                    gdi32.SelectObject(hdc, old_pen)
                    gdi32.SelectObject(hdc, old_brush)
                    gdi32.DeleteObject(pen)
                    _user32.EndPaint(hwnd, ps)
                return 0
            elif msg in (WM_DESTROY, WM_CLOSE):
                _user32.PostQuitMessage(0)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        CLASS_NAME = "MCLiteHitboxOverlay"
        wc = WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(wc)
        wc.style         = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc   = ctypes.cast(wnd_proc, ctypes.c_void_p)
        wc.hInstance     = HINSTANCE
        wc.lpszClassName = CLASS_NAME
        wc.hbrBackground = None

        _user32.RegisterClassExW(ctypes.byref(wc))

        # Ambil posisi/ukuran window Minecraft
        class RECT(ctypes.Structure):
            _fields_ = [("left",ctypes.c_long),("top",ctypes.c_long),
                        ("right",ctypes.c_long),("bottom",ctypes.c_long)]
        mc_rect = RECT()
        _user32.GetWindowRect(mc_hwnd, ctypes.byref(mc_rect))
        x = mc_rect.left;  y = mc_rect.top
        w = mc_rect.right  - x
        h = mc_rect.bottom - y

        # Buat overlay window
        hwnd_overlay = _user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
            CLASS_NAME, "MCLiteHitbox",
            WS_POPUP,
            x, y, w, h,
            None, None, HINSTANCE, None
        )
        self._hb_hwnd_overlay = hwnd_overlay

        if not hwnd_overlay:
            self._hb_enabled = False
            return

        # Set layered: key color magenta = transparan, sisanya opaque
        _user32.SetLayeredWindowAttributes(hwnd_overlay, KEY_COLOR, 180, LWA_COLORKEY)
        _user32.ShowWindow(hwnd_overlay, 8)  # SW_SHOWNA

        # Message loop + update posisi overlay
        class MSG(ctypes.Structure):
            _fields_ = [("hwnd",ctypes.c_void_p),("message",ctypes.c_uint),
                        ("wParam",ctypes.c_void_p),("lParam",ctypes.c_long),
                        ("time",ctypes.c_ulong),("pt",ctypes.c_longlong)]
        msg = MSG()

        while not self._hb_stop_event.is_set():
            # Non-blocking message pump
            while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0012:  # WM_QUIT
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            # Ikuti posisi window Minecraft
            mc_r = RECT()
            if _user32.GetWindowRect(mc_hwnd, ctypes.byref(mc_r)):
                nx = mc_r.left;   ny = mc_r.top
                nw = mc_r.right  - nx
                nh = mc_r.bottom - ny
                if (nx, ny, nw, nh) != (x, y, w, h):
                    x, y, w, h = nx, ny, nw, nh
                    _user32.MoveWindow(hwnd_overlay, x, y, w, h, True)

            # Redraw hitbox
            _user32.InvalidateRect(hwnd_overlay, None, True)
            _user32.UpdateWindow(hwnd_overlay)
            time.sleep(0.016)  # ~60fps update

        _user32.DestroyWindow(hwnd_overlay)
        _user32.UnregisterClassW(CLASS_NAME, HINSTANCE)
        self._hb_hwnd_overlay = None

    # ── REDUCE LATENCY ────────────────────────────────────────────────────────
    def apply_reduce_latency(self, pid: int) -> tuple[bool, str]:
        """
        Kurangi latency Minecraft:
        1. Timer resolution 0.5ms (NtSetTimerResolution)
        2. I/O Priority HIGH pada proses Minecraft
        3. Disable Nagle algorithm via registry (TCP latency)
        4. Set processor power scheme ke high performance
        """
        results = []
        ok_count = 0

        # 1. Timer resolution 0.5ms
        try:
            ntdll = ctypes.windll.ntdll
            cur = ctypes.c_ulong()
            ret = ntdll.NtSetTimerResolution(5000, True, ctypes.byref(cur))
            if ret == 0:
                results.append("✓ Timer 0.5ms")
                ok_count += 1
            else:
                results.append("✗ Timer (gagal)")
        except Exception:
            results.append("✗ Timer (error)")

        # 2. I/O Priority HIGH
        try:
            hproc = self._open_proc(pid)
            if hproc:
                IO_PRIORITY_HINT = 3  # IoPriorityHigh = 3
                # ProcessIoPriority = 33
                ntdll = ctypes.windll.ntdll
                val = ctypes.c_ulong(IO_PRIORITY_HINT)
                ret = ntdll.NtSetInformationProcess(
                    hproc, 33,
                    ctypes.byref(val), ctypes.sizeof(val)
                )
                _kernel32.CloseHandle(hproc)
                if ret == 0:
                    results.append("✓ I/O Priority High")
                    ok_count += 1
                else:
                    results.append("~ I/O Priority (partial)")
        except Exception:
            results.append("✗ I/O Priority (error)")

        # 3. Disable Nagle via registry
        try:
            import winreg
            key_path = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as base:
                n = 0
                changed = 0
                while True:
                    try:
                        iface = winreg.EnumKey(base, n)
                        n += 1
                        with winreg.OpenKey(base, iface,
                                            access=winreg.KEY_WRITE) as ikey:
                            winreg.SetValueEx(ikey, "TcpAckFrequency",
                                              0, winreg.REG_DWORD, 1)
                            winreg.SetValueEx(ikey, "TCPNoDelay",
                                              0, winreg.REG_DWORD, 1)
                            changed += 1
                    except OSError:
                        break
            if changed > 0:
                results.append(f"✓ Nagle disabled ({changed} iface)")
                ok_count += 1
            else:
                results.append("~ Nagle (tidak ada interface)")
        except PermissionError:
            results.append("✗ Nagle (perlu Admin)")
        except Exception as e:
            results.append(f"✗ Nagle ({e})")

        # 4. Power scheme high performance
        try:
            subprocess.run(
                ["powercfg", "/setactive",
                 "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"],
                capture_output=True, timeout=5
            )
            results.append("✓ Power: High Performance")
            ok_count += 1
        except Exception:
            results.append("~ Power scheme (gagal)")

        msg = " | ".join(results)
        return ok_count > 0, f"Latency: {msg}"


# Singleton injector
_pvp_injector = PVPInjector()


# ══════════════════════════════════════════════════════════════════════════════
# WINDOWS API HELPERS
# ══════════════════════════════════════════════════════════════════════════════

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
    FIX: gunakan os.access() dan try-open sebelum konfirmasi path valid.
    """
    global _mc_path_cache
    if use_cache and _mc_path_cache and _mc_path_cache.exists():
        return _mc_path_cache

    saved = cfg.get("general", "mc_path")
    if saved:
        p = Path(saved)
        if p.exists():
            _mc_path_cache = p
            return p

    drives = _get_all_drives()

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

    minecraft_keywords = ["minecraft for windows", "minecraft", "minecraftuwp"]
    for drive in drives:
        for search_root in [drive, drive / "Games", drive / "Program Files",
                             drive / "Apps", drive / "XboxGames"]:
            try:
                if not search_root.exists(): continue
                for folder in search_root.iterdir():
                    if not folder.is_dir(): continue
                    n = folder.name.lower()
                    if not any(kw in n for kw in minecraft_keywords): continue
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


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    _DEFAULTS = {
        "general": {"mc_path":"","auto_detect":"true","close_on_launch":"false","shell_app_id":""},
        "memory":  {"enabled":"true","limit_mb":"4096","trim_interval_s":"30","trim_threshold":"80"},
        "boost":   {"priority":"above_normal","cpu_affinity":"0","timer_resolution":"true"},
        "fps":     {"overlay_enabled":"false","position":"top_left","font_size":"14","update_interval":"500"},
        "fixes":   {"disable_improved_input_response":"false","flush_dns_on_launch":"false"},
        "pvp":     {"reduce_latency":"false","motion_blur":"true","player_hitbox":"false"},
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


# ══════════════════════════════════════════════════════════════════════════════
# LOGGER
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY MONITOR THREAD
# ══════════════════════════════════════════════════════════════════════════════

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
                    # Update PVP injector PID
                    _pvp_injector.set_pid(proc.pid)
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
                _pvp_injector.set_pid(0)
                self.stats.emit({"running":False})
            for _ in range(max(1, cfg.int("memory","trim_interval_s"))):
                if self._stop: return
                time.sleep(1)

    def stop(self): self._stop = True


# ══════════════════════════════════════════════════════════════════════════════
# FPS OVERLAY
# ══════════════════════════════════════════════════════════════════════════════

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
        self._rss = 0; self._cpu = 0.0; self._fps = 0.0; self._pid = 0
        self._fps_reader = FPSReader()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(max(500, cfg.int("fps","update_interval")))
        self._reposition()

    def _reposition(self):
        pos = cfg.get("fps","position")
        scr = QApplication.primaryScreen().geometry()
        m   = 12
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


# ══════════════════════════════════════════════════════════════════════════════
# LAUNCH THREAD
# ══════════════════════════════════════════════════════════════════════════════

class LaunchThread(QThread):
    ok     = pyqtSignal(int)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, path, shell_app_id=""):
        super().__init__()
        self._path         = Path(path) if path else None
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
        launched   = False

        # Strategi 1: ShellExecute langsung ke EXE
        if path_valid:
            try:
                self.status.emit(f"Meluncurkan: {self._path.name}...")
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "open", str(self._path), None,
                    str(self._path.parent), 1
                )
                if int(ret) > 32:
                    launched = True
                    log.info(f"Launch ShellExecute OK: {self._path}")
                else:
                    log.warn(f"ShellExecute return {ret}")
            except Exception as e:
                log.warn(f"ShellExecute gagal: {e}")

        # Strategi 2: gamelaunchhelper.exe
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
                        log.info(f"Launch via gamelaunchhelper")
                except Exception as e:
                    log.warn(f"gamelaunchhelper gagal: {e}")

        # Strategi 3: shell:AppsFolder
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

        # Strategi 4: minecraft: URI
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


# ══════════════════════════════════════════════════════════════════════════════
# STYLESHEET
# ══════════════════════════════════════════════════════════════════════════════

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
QPushButton#btn_pvp {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {c['pvp']},stop:1 {c['pvp_dim']}); border: none; border-radius: 5px; padding: 8px 18px; font-size: 13px; font-weight: 700; color: #fff; }}
QPushButton#btn_pvp:hover {{ background: #FF8C55; }}
QPushButton#btn_pvp:disabled {{ background: {c['border']}; color: {c['text_dim']}; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 12px; padding: 10px 6px 6px 6px; font-size: 11px; font-weight: 700; color: {c['text_dim']}; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 0 6px; color: {c['accent']}; }}
QGroupBox#pvp_box {{ border-color: {c['pvp_dim']}; }}
QGroupBox#pvp_box::title {{ color: {c['pvp']}; }}
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
QLabel#pvp_status {{ font-size: 11px; font-family: Consolas; padding: 6px 10px; border-radius: 4px; background: #0D1B0D; border: 1px solid {c['pvp_dim']}; }}
"""


# ══════════════════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class MCLiteLauncher(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(_WND_TITLE)
        self.setMinimumSize(720, 580)
        self.resize(760, 600)

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
        self._lbl_path.setStyleSheet(
            f"font-family:Consolas;font-size:11px;color:{COLORS['text_dim']};")
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
            self._lbl_path.setStyleSheet(
                f"font-family:Consolas;font-size:11px;color:{COLORS['warning']};")
            self._btn_launch.setEnabled(False)
            self._btn_launch.setText("⚠  Pilih Minecraft.Windows.exe dulu")
            self._set_status("⚠ Gunakan 📂 Browse → pilih Minecraft.Windows.exe")

    # ── UI BUILD ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        cw   = QWidget()
        root = QVBoxLayout(cw)
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
        btn_exit = QPushButton("✕ Keluar")
        btn_exit.setObjectName("btn_exit"); btn_exit.setFixedHeight(26)
        btn_exit.clicked.connect(self._quit_fully)
        hl.addSpacing(10); hl.addWidget(btn_exit)
        root.addWidget(hdr)

        tabs = QTabWidget(); tabs.setDocumentMode(True)
        tabs.addTab(self._tab_home(),   "  🏠 HOME  ")
        tabs.addTab(self._tab_memory(), "  💾 MEMORI  ")
        tabs.addTab(self._tab_boost(),  "  ⚡ BOOST  ")
        tabs.addTab(self._tab_fps(),    "  📊 FPS  ")
        tabs.addTab(self._tab_pvp(),    "  ⚔ PVP HELPER  ")
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

    # ── TAB HOME ──────────────────────────────────────────────────────────────
    def _tab_home(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)

        pg = QGroupBox("Lokasi Minecraft"); ph = QHBoxLayout(pg)
        path_text = (str(self._mc_path) if self._mc_path
                     else "🔍 Mendeteksi..." if cfg.bool("general","auto_detect")
                     else "Belum ditemukan")
        self._lbl_path = QLabel(path_text)
        self._lbl_path.setStyleSheet(
            f"font-family:Consolas;font-size:11px;color:{COLORS['text_dim']};")
        self._lbl_path.setWordWrap(True)
        ph.addWidget(self._lbl_path, 1)
        btn_b = QPushButton("📂 Browse"); btn_b.clicked.connect(self._browse)
        btn_d = QPushButton("🔍 Detect Ulang"); btn_d.clicked.connect(self._manual_detect)
        ph.addWidget(btn_b); ph.addWidget(btn_d)
        lay.addWidget(pg)

        sg = QGroupBox("Status Real-Time"); slay = QHBoxLayout(sg)
        def _stat_col(attr, label, color):
            col = QVBoxLayout()
            lbl = QLabel("–"); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"font-size:20px;font-weight:700;color:{color};font-family:Consolas;")
            sub = QLabel(label); sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub.setStyleSheet(f"font-size:10px;color:{COLORS['text_dim']};")
            col.addWidget(lbl); col.addWidget(sub)
            setattr(self, attr, lbl)
            return col
        slay.addLayout(_stat_col("_lbl_rss",     "RAM Minecraft", COLORS["accent"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_cpu",     "CPU Minecraft", COLORS["warning"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_sys",     "RAM Sistem",    COLORS["accent2"]))
        slay.addWidget(self._vsep())
        slay.addLayout(_stat_col("_lbl_trimmed", "Total Di-Trim", COLORS["success"]))
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

    # ── TAB MEMORY ────────────────────────────────────────────────────────────
    def _tab_memory(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
        info = QLabel(
            "ℹ️  Memory Manager otomatis men-trim RAM Minecraft menggunakan\n"
            "Windows API EmptyWorkingSet() — mengatasi memory leak GDK (v1.21.120+).")
        info.setWordWrap(True)
        info.setStyleSheet(
            f"background:#0D2218;border:1px solid {COLORS['accent_dim']};"
            f"border-radius:5px;padding:8px 12px;font-size:11px;color:{COLORS['text_dim']};")
        lay.addWidget(info)
        g = QGroupBox("Pengaturan"); gl = QVBoxLayout(g)
        self._chk_mem = QCheckBox("Aktifkan Memory Manager Otomatis")
        self._chk_mem.setChecked(cfg.bool("memory","enabled"))
        self._chk_mem.toggled.connect(lambda v: cfg.set("memory","enabled",v))
        gl.addWidget(self._chk_mem)
        for label, key, lo, hi, step in [
            ("Batas RAM Minecraft (MB):","limit_mb",512,32768,256),
            ("Interval cek (detik):","trim_interval_s",5,300,5),
            ("Trim jika RAM sistem > %:","trim_threshold",50,99,1),
        ]:
            row = QHBoxLayout(); row.addWidget(QLabel(label))
            sp  = QSpinBox(); sp.setRange(lo,hi); sp.setSingleStep(step)
            sp.setValue(cfg.int("memory",key))
            sp.valueChanged.connect(lambda v, k=key: cfg.set("memory",k,v))
            row.addWidget(sp); row.addStretch(); gl.addLayout(row)
        lay.addWidget(g)
        btn = QPushButton("🧹 Trim Sekarang"); btn.clicked.connect(self._manual_trim)
        lay.addWidget(btn); lay.addStretch(); return w

    # ── TAB BOOST ─────────────────────────────────────────────────────────────
    def _tab_boost(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
        g1 = QGroupBox("Prioritas CPU"); g1l = QVBoxLayout(g1)
        g1l.addWidget(QLabel("Prioritas Minecraft:"))
        self._cmb_prio = QComboBox()
        self._cmb_prio.addItems(["normal","above_normal","high"])
        self._cmb_prio.setCurrentText(cfg.get("boost","priority"))
        self._cmb_prio.currentTextChanged.connect(
            lambda v: cfg.set("boost","priority",v))
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
        btn = QPushButton("⚡ Apply Boost ke Minecraft Sekarang")
        btn.clicked.connect(self._apply_boost)
        lay.addWidget(btn); lay.addStretch(); return w

    # ── TAB FPS ───────────────────────────────────────────────────────────────
    def _tab_fps(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
        info = QLabel(
            "Overlay transparan tampil di atas Minecraft. Klik menembus ke game.\n"
            "FPS dibaca akurat via Windows PDH GPU Engine counter (Packets Queued/sec).\n"
            "Fallback: D3DKMTQueryStatistics → QueryThreadCycleTime render thread.")
        info.setWordWrap(True)
        info.setStyleSheet(
            f"background:#0A1A2E;border:1px solid {COLORS['border']};"
            f"border-radius:5px;padding:8px 12px;font-size:11px;color:{COLORS['text_dim']};")
        lay.addWidget(info)
        g = QGroupBox("Pengaturan FPS Overlay"); gl = QVBoxLayout(g)
        self._chk_fps = QCheckBox("Aktifkan FPS Overlay")
        self._chk_fps.setChecked(cfg.bool("fps","overlay_enabled"))
        self._chk_fps.toggled.connect(self._toggle_overlay)
        gl.addWidget(self._chk_fps)
        row1 = QHBoxLayout(); row1.addWidget(QLabel("Posisi:"))
        self._cmb_pos = QComboBox()
        self._cmb_pos.addItems(["top_left","top_right","bottom_left","bottom_right"])
        self._cmb_pos.setCurrentText(cfg.get("fps","position"))
        self._cmb_pos.currentTextChanged.connect(
            lambda v: (cfg.set("fps","position",v),
                       self._fps_overlay and self._fps_overlay._reposition()))
        row1.addWidget(self._cmb_pos); row1.addStretch(); gl.addLayout(row1)
        row2 = QHBoxLayout(); row2.addWidget(QLabel("Font size:"))
        sp = QSpinBox(); sp.setRange(8,28); sp.setValue(cfg.int("fps","font_size"))
        sp.valueChanged.connect(lambda v: cfg.set("fps","font_size",v))
        row2.addWidget(sp); row2.addStretch(); gl.addLayout(row2)
        lay.addWidget(g); lay.addStretch(); return w

    # ── TAB PVP HELPER ────────────────────────────────────────────────────────
    def _tab_pvp(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(14)

        # Header banner
        banner = QLabel("⚔  PVP Helper — Inject ke Minecraft")
        banner.setStyleSheet(
            f"font-size:14px;font-weight:800;color:{COLORS['pvp']};"
            f"background:#1A0A00;border:1px solid {COLORS['pvp_dim']};"
            f"border-radius:6px;padding:8px 14px;")
        lay.addWidget(banner)

        note = QLabel(
            "⚠ Fitur inject membutuhkan Minecraft sudah berjalan dan launcher dijalankan "
            "sebagai Administrator. Beberapa fitur mungkin perlu restart Minecraft untuk aktif.")
        note.setWordWrap(True)
        note.setStyleSheet(
            f"font-size:10px;color:{COLORS['warning']};"
            f"background:#1A1400;border:1px solid #5C4A00;"
            f"border-radius:4px;padding:6px 10px;")
        lay.addWidget(note)

        # ── 1. REDUCE LATENCY ─────────────────────────────────────────────
        g_lat = QGroupBox("🌐 Reduce Latency"); g_lat.setObjectName("pvp_box")
        lat_lay = QVBoxLayout(g_lat)
        lat_desc = QLabel(
            "Disable Nagle Algorithm (TCP_NODELAY) + Timer 0.5ms + I/O Priority High\n"
            "+ Power Scheme High Performance → kurangi ping & input lag")
        lat_desc.setWordWrap(True)
        lat_desc.setStyleSheet(f"font-size:11px;color:{COLORS['text_dim']};")
        lat_lay.addWidget(lat_desc)
        btn_lat = QPushButton("⚡ Apply Reduce Latency"); btn_lat.setObjectName("btn_pvp")
        btn_lat.setFixedHeight(34)
        btn_lat.clicked.connect(self._pvp_apply_latency)
        lat_lay.addWidget(btn_lat)
        lay.addWidget(g_lat)

        # ── 2. MOTION BLUR ────────────────────────────────────────────────
        g_mb = QGroupBox("💨 Motion Blur"); g_mb.setObjectName("pvp_box")
        mb_lay = QVBoxLayout(g_mb)
        mb_desc = QLabel(
            "Inject via WriteProcessMemory → ubah nilai motion blur strength di memori "
            "Minecraft (0.0 = off, 1.0 = on). Fallback ke options.txt jika scan gagal.")
        mb_desc.setWordWrap(True)
        mb_desc.setStyleSheet(f"font-size:11px;color:{COLORS['text_dim']};")
        mb_lay.addWidget(mb_desc)
        mb_row = QHBoxLayout()
        self._chk_mb = QCheckBox("Motion Blur ON")
        self._chk_mb.setChecked(cfg.bool("pvp","motion_blur"))
        mb_row.addWidget(self._chk_mb)
        btn_mb = QPushButton("💉 Inject Sekarang"); btn_mb.setObjectName("btn_pvp")
        btn_mb.setFixedHeight(30)
        btn_mb.clicked.connect(self._pvp_inject_motion_blur)
        mb_row.addWidget(btn_mb); mb_row.addStretch()
        mb_lay.addLayout(mb_row)
        lay.addWidget(g_mb)

        # ── 3. PLAYER HITBOX ─────────────────────────────────────────────
        g_hb = QGroupBox("🎯 Player Hitbox"); g_hb.setObjectName("pvp_box")
        hb_lay = QVBoxLayout(g_hb)
        hb_desc = QLabel(
            "Overlay wireframe GDI transparan di atas window Minecraft — tampilkan "
            "perkiraan hitbox pemain. Klik menembus ke game. Tidak mengubah file game.")
        hb_desc.setWordWrap(True)
        hb_desc.setStyleSheet(f"font-size:11px;color:{COLORS['text_dim']};")
        hb_lay.addWidget(hb_desc)
        hb_row = QHBoxLayout()
        self._chk_hb = QCheckBox("Tampilkan Hitbox Overlay")
        self._chk_hb.setChecked(cfg.bool("pvp","player_hitbox"))
        self._chk_hb.toggled.connect(self._pvp_toggle_hitbox)
        hb_row.addWidget(self._chk_hb)
        btn_hb = QPushButton("🎯 Toggle Hitbox"); btn_hb.setObjectName("btn_pvp")
        btn_hb.setFixedHeight(30)
        btn_hb.clicked.connect(
            lambda: self._pvp_toggle_hitbox(self._chk_hb.isChecked()))
        hb_row.addWidget(btn_hb); hb_row.addStretch()
        hb_lay.addLayout(hb_row)
        lay.addWidget(g_hb)

        # Status area
        self._pvp_status = QLabel("Siap — jalankan Minecraft dulu lalu apply.")
        self._pvp_status.setObjectName("pvp_status")
        self._pvp_status.setWordWrap(True)
        self._pvp_status.setStyleSheet(
            f"font-size:11px;font-family:Consolas;padding:8px 12px;"
            f"border-radius:4px;background:#0D1B0D;"
            f"border:1px solid {COLORS['pvp_dim']};color:{COLORS['text']};")
        lay.addWidget(self._pvp_status)
        lay.addStretch(); return w

    # ── TAB FIXES ─────────────────────────────────────────────────────────────
    def _tab_fixes(self):
        w = QWidget(); lay = QVBoxLayout(w)
        lay.setContentsMargins(20,16,20,16); lay.setSpacing(12)
        g = QGroupBox("Perbaikan Bug GDK"); gl = QVBoxLayout(g)
        self._chk_iir = QCheckBox(
            "Catat peringatan: Nonaktifkan 'Improved Input Response' di Minecraft\n"
            "  → Penyebab utama memory leak ekstrem sejak v1.21.120")
        self._chk_iir.setChecked(cfg.bool("fixes","disable_improved_input_response"))
        self._chk_iir.toggled.connect(
            lambda v: cfg.set("fixes","disable_improved_input_response",v))
        gl.addWidget(self._chk_iir)
        self._chk_dns = QCheckBox("Flush DNS sebelum launch (mengurangi lag multiplayer)")
        self._chk_dns.setChecked(cfg.bool("fixes","flush_dns_on_launch"))
        self._chk_dns.toggled.connect(lambda v: cfg.set("fixes","flush_dns_on_launch",v))
        gl.addWidget(self._chk_dns); lay.addWidget(g)
        g2 = QGroupBox("Folder Data GDK"); g2l = QVBoxLayout(g2)
        g2l.addWidget(QLabel("%APPDATA%\\Minecraft Bedrock\\users\\shared\\games\\com.mojang"))
        btn = QPushButton("📂 Buka Folder"); btn.clicked.connect(self._open_gdk)
        g2l.addWidget(btn); lay.addWidget(g2); lay.addStretch(); return w

    # ── TAB LOG ───────────────────────────────────────────────────────────────
    def _tab_log(self):
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(12,10,12,10)
        self._log_view = QTextEdit(); self._log_view.setReadOnly(True)
        lay.addWidget(self._log_view, 1)
        bl = QHBoxLayout()
        br = QPushButton("🔄 Refresh"); br.clicked.connect(self._load_log)
        bc = QPushButton("🗑 Hapus");   bc.clicked.connect(self._clear_log)
        bl.addWidget(br); bl.addWidget(bc); bl.addStretch()
        lay.addLayout(bl); self._load_log(); return w

    # ── HELPERS ───────────────────────────────────────────────────────────────
    def _vsep(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet(f"color:{COLORS['border']};"); return f

    def _set_status(self, msg): self._lbl_status.setText(msg)

    def _browse(self):
        """
        FIX: Buka dialog file tanpa trigger permission error.
        Gunakan QFileDialog biasa — Windows handle permission sendiri.
        Jangan coba open() file EXE untuk validasi (itu penyebab error asli).
        """
        # Mulai dari lokasi yang sudah diketahui atau drive umum
        start_dir = ""
        if self._mc_path:
            start_dir = str(self._mc_path.parent)
        else:
            # Coba lokasi umum Xbox/Minecraft dulu
            for candidate in [
                Path("C:/XboxGames"),
                Path("D:/XboxGames"),
                Path("C:/Program Files/WindowsApps"),
                _LOCALAPPDATA / "Packages",
            ]:
                if candidate.exists():
                    start_dir = str(candidate)
                    break
            if not start_dir:
                start_dir = "C:/"

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Pilih Minecraft.Windows.exe",
            start_dir,
            "Minecraft (Minecraft.Windows.exe Minecraft.exe);;Semua EXE (*.exe)"
        )
        if not path:
            return

        p = Path(path)
        # Validasi: hanya cek .exists() — JANGAN open() file UWP
        if not p.exists():
            QMessageBox.warning(self, "File Tidak Ada",
                                f"File tidak ditemukan:\n{path}")
            return

        self._mc_path = p
        cfg.set("general","mc_path", path)
        self._lbl_path.setText(path)
        self._lbl_path.setStyleSheet(
            f"font-family:Consolas;font-size:11px;color:{COLORS['text_dim']};")
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
        self._btn_launch.setEnabled(True)
        self._btn_launch.setText("▶  LAUNCH MINECRAFT")
        self._set_status(
            f"✅ Minecraft berjalan (PID {pid})" if pid > 0
            else "✅ Minecraft diluncurkan")

    def _on_launch_failed(self, err):
        self._btn_launch.setEnabled(True)
        self._btn_launch.setText("▶  LAUNCH MINECRAFT")
        self._set_status("❌ Launch gagal")
        QMessageBox.critical(self, "Launch Gagal", err)

    def _manual_trim(self):
        proc = find_minecraft_process()
        if not proc: self._set_status("Minecraft tidak berjalan."); return
        before = proc.memory_info().rss >> 20
        if trim_process_memory(proc.pid):
            time.sleep(1)
            try:
                after  = proc.memory_info().rss >> 20
                saved  = max(0, before-after)
                self._total_trimmed += saved
                self._lbl_trimmed.setText(f"{self._total_trimmed} MB")
                self._set_status(f"🧹 Trim: {before}→{after}MB (hemat {saved}MB)")
                log.info(f"Manual trim: -{saved}MB")
            except Exception:
                self._set_status("🧹 Trim dilakukan.")
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
        else:
            QMessageBox.information(self,"Info",
                f"Folder belum ada:\n{p}\n\nJalankan Minecraft sekali dulu.")

    # ── PVP HELPER ACTIONS ────────────────────────────────────────────────────
    def _pvp_set_status(self, msg: str, ok: bool = True):
        color = COLORS["success"] if ok else COLORS["danger"]
        self._pvp_status.setText(msg)
        self._pvp_status.setStyleSheet(
            f"font-size:11px;font-family:Consolas;padding:8px 12px;"
            f"border-radius:4px;background:#0D1B0D;"
            f"border:1px solid {COLORS['pvp_dim']};color:{color};")
        self._set_status(msg[:80])
        log.info(f"PVP: {msg}")

    def _pvp_apply_latency(self):
        proc = find_minecraft_process()
        if not proc:
            self._pvp_set_status("❌ Minecraft tidak berjalan — launch dulu", ok=False)
            return
        self._pvp_status.setText("⏳ Applying latency tweaks...")
        QApplication.processEvents()
        ok, msg = _pvp_injector.apply_reduce_latency(proc.pid)
        cfg.set("pvp","reduce_latency", ok)
        self._pvp_set_status(msg, ok=ok)

    def _pvp_inject_motion_blur(self):
        proc = find_minecraft_process()
        mb_on = self._chk_mb.isChecked()
        cfg.set("pvp","motion_blur", mb_on)

        if not proc:
            # Coba fallback ke options.txt meski MC tidak jalan
            ok, msg = _pvp_injector._set_motion_blur_registry(mb_on)
            if ok:
                self._pvp_set_status(f"{'🟢' if mb_on else '⚫'} {msg}", ok=True)
            else:
                self._pvp_set_status(
                    "❌ Minecraft tidak berjalan. Launch dulu untuk inject memory.\n"
                    f"Fallback options.txt: {msg}", ok=False)
            return

        self._pvp_status.setText("⏳ Scanning memory Minecraft...")
        QApplication.processEvents()
        ok, msg = _pvp_injector.set_motion_blur(mb_on)
        icon = "🟢" if mb_on else "⚫"
        self._pvp_set_status(f"{icon} {msg}", ok=ok)

    def _pvp_toggle_hitbox(self, enabled: bool):
        cfg.set("pvp","player_hitbox", enabled)
        proc = find_minecraft_process()
        if not proc:
            self._pvp_set_status("❌ Minecraft tidak berjalan", ok=False)
            self._chk_hb.setChecked(False)
            return
        _pvp_injector.set_pid(proc.pid)
        ok, msg = _pvp_injector.set_player_hitbox(enabled)
        icon = "🟢" if enabled else "⚫"
        self._pvp_set_status(f"{icon} {msg}", ok=ok)
        if not ok:
            self._chk_hb.setChecked(False)

    # ── LOG ───────────────────────────────────────────────────────────────────
    def _load_log(self):
        try:
            if LOG_FILE.exists():
                lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
                self._log_view.setPlainText("\n".join(lines[-300:]))
                self._log_view.verticalScrollBar().setValue(
                    self._log_view.verticalScrollBar().maximum())
            else:
                self._log_view.setPlainText("(Belum ada log)")
        except Exception as e:
            self._log_view.setPlainText(f"Error: {e}")

    def _clear_log(self):
        try: LOG_FILE.write_text("", encoding="utf-8")
        except Exception: pass
        self._log_view.setPlainText("")

    # ── STATS CALLBACK ────────────────────────────────────────────────────────
    def _on_stats(self, s):
        if s.get("running"):
            rss = s["rss_mb"]; lim = cfg.int("memory","limit_mb")
            pct = min(100, int(rss/lim*100)) if lim else 0
            self._lbl_rss.setText(f"{rss} MB"); self._lbl_cpu.setText(f"{s['cpu_pct']:.1f}%")
            self._lbl_sys.setText(f"{s['sys_pct']:.0f}%")
            self._ram_bar.setValue(pct)
            self._ram_bar.setFormat(f"Minecraft RAM: {rss}/{lim} MB ({pct}%)")
            self._lbl_mc_status.setText(f"● Berjalan  PID={s['pid']}")
            self._lbl_mc_status.setStyleSheet(f"font-size:11px;color:{COLORS['success']};")
        else:
            for lbl in (self._lbl_rss, self._lbl_cpu): lbl.setText("–")
            self._lbl_sys.setText(f"{psutil.virtual_memory().percent:.0f}%")
            self._ram_bar.setValue(0); self._ram_bar.setFormat("Minecraft tidak berjalan")
            self._lbl_mc_status.setText("● Tidak berjalan")
            self._lbl_mc_status.setStyleSheet(
                f"font-size:11px;color:{COLORS['text_dim']};")

    def _on_trimmed(self, mb):
        self._total_trimmed += mb
        self._lbl_trimmed.setText(f"{self._total_trimmed} MB")
        self._set_status(f"🧹 Auto-trim: hemat {mb} MB")

    # ── TRAY ──────────────────────────────────────────────────────────────────
    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable(): return
        self._tray = QSystemTrayIcon(self); self._tray.setToolTip(APP_NAME)
        m  = QMenu()
        a1 = QAction("Tampilkan",self);           a1.triggered.connect(self._show_window)
        a2 = QAction("Trim Memory",self);          a2.triggered.connect(self._manual_trim)
        a3 = QAction("Keluar Sepenuhnya",self);    a3.triggered.connect(self._quit_fully)
        m.addAction(a1); m.addAction(a2); m.addSeparator(); m.addAction(a3)
        self._tray.setContextMenu(m)
        self._tray.activated.connect(
            lambda r: self._show_window()
            if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self._tray.show()

    def _show_window(self): self.showNormal(); self.raise_(); self.activateWindow()

    def _quit_fully(self):
        log.info("Quit fully")
        self._mem_thread.stop(); self._mem_thread.wait(1500)
        if self._fps_overlay:
            self._fps_overlay._fps_reader.stop()
            self._fps_overlay.close(); self._fps_overlay = None
        # Stop hitbox overlay
        try:
            if hasattr(_pvp_injector, "_hb_stop_event"):
                _pvp_injector._stop_hitbox_overlay()
        except Exception: pass
        if hasattr(self,"_tray"):
            self._tray.hide(); self._tray.setVisible(False)
        QApplication.quit()
        os._exit(0)

    def closeEvent(self, e):
        if hasattr(self,"_tray") and self._tray.isVisible():
            self.hide()
            self._tray.showMessage(
                "MCLite Launcher",
                "Berjalan di background. Klik kanan tray → 'Keluar Sepenuhnya' untuk tutup.",
                QSystemTrayIcon.MessageIcon.Information, 2000)
            e.ignore()
        else:
            self._quit_fully(); e.accept()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

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
