# ⛏ MCLite Launcher

**Launcher ringan untuk Minecraft Bedrock Edition (GDK) - Windows 10/11**

[![Build & Release](https://github.com/YOUR_USERNAME/MCLiteLauncher/actions/workflows/build.yml/badge.svg)](https://github.com/YOUR_USERNAME/MCLiteLauncher/actions/workflows/build.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Windows](https://img.shields.io/badge/Windows-10%2F11-blue.svg)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/Python-3.12-yellow.svg)](https://python.org)

---

## ✨ Fitur

### 🧹 Memory Leak Fix (Otomatis)
Sejak Minecraft Bedrock migrasi dari **UWP ke GDK** (versi 1.21.120+, Oktober 2025), terjadi memory leak parah yang menyebabkan RAM terus naik hingga crash. MCLite secara otomatis men-trim working set Minecraft menggunakan Windows API **`EmptyWorkingSet()`**:
- Monitor RAM Minecraft secara real-time
- Otomatis trim jika melebihi batas yang ditentukan (default: 4 GB)
- Otomatis trim jika RAM sistem melebihi threshold% (default: 80%)
- Bisa trim manual kapan saja dengan satu klik

### ⚡ Performance Boost
- **CPU Priority**: Set prioritas CPU Minecraft (Normal / Above Normal / High)
- **Timer Resolution**: Optimalkan timer Windows ke 0.5ms (mengurangi input lag)
- **CPU Affinity**: Pilih berapa core yang dipakai Minecraft
- **Disable Xbox Game Bar**: Nonaktifkan Game Bar yang bisa menyebabkan overhead

### 📊 FPS Counter Overlay
FPS counter transparan yang tampil **di dalam tampilan Minecraft** (bukan taskbar atau monitor Windows):
- Posisi bisa diatur: sudut kiri/kanan atas/bawah
- Menampilkan: FPS estimasi, RAM Minecraft (MB), CPU Minecraft (%)
- Klik menembus ke game (tidak mengganggu gameplay)
- Ukuran font bisa dikustomisasi

### 🔧 Perbaikan Bug GDK
- **Disable "Improved Input Response"**: Fitur ini diketahui menyebabkan memory leak ekstrem (RAM dari 12GB turun ke normal setelah dinonaktifkan). MCLite bisa mengingatkan/mengotomatiskan ini.
- **Flush DNS sebelum launch**: Mengurangi lag koneksi multiplayer
- **Info folder data GDK**: Akses cepat ke `%APPDATA%\Minecraft Bedrock\...`

### 🗺️ Kompatibilitas GDK
- Auto-detect path Minecraft GDK via PowerShell `Get-AppxPackage`
- Support path instalasi Xbox App & Microsoft Store
- Browse manual jika auto-detect gagal

---

## 📥 Download & Install

### Cara 1: Installer (Direkomendasikan)
1. Download `MCLiteLauncher-Setup-vX.X.X.exe` dari [Releases](../../releases/latest)
2. Jalankan installer
3. **Pilih drive/disk tujuan** (bisa C:, D:, E:, dll)
4. Centang opsi shortcut sesuai kebutuhan
5. Klik Install

### Cara 2: Portable
1. Download `MCLiteLauncher-Portable-vX.X.X.zip`
2. Ekstrak ke folder mana saja
3. Jalankan `MCLiteLauncher.exe` sebagai Administrator

---

## 🚀 Cara Pakai

1. Buka MCLite Launcher (klik kanan → **Run as Administrator** agar fitur memory trim bisa bekerja)
2. Klik **🔍 Auto Detect** — launcher akan menemukan Minecraft Bedrock secara otomatis
3. Jika tidak ketemu, klik **📂 Browse** dan arahkan ke `Minecraft.Windows.exe`
4. Atur fitur di tab Memory, Boost, FPS sesuai kebutuhan
5. Klik **▶ LAUNCH MINECRAFT**

### Rekomendasi Pengaturan
| Pengaturan | Nilai Rekomendasi |
|------------|-------------------|
| Memory Manager | ✅ Aktif |
| Batas RAM | 3072–4096 MB |
| Trim interval | 30 detik |
| Threshold sistem | 75–85% |
| CPU Priority | Above Normal |
| Timer Resolution | ✅ Aktif |
| FPS Overlay | Sesuai kebutuhan |

---

## 🏗️ Build dari Source

### Kebutuhan
- Python 3.12+
- Windows 10/11 (wajib, karena pakai `ctypes.windll`)
- [NSIS](https://nsis.sourceforge.io/) untuk membuat installer

### Build Manual
```bash
# Clone repo
git clone https://github.com/YOUR_USERNAME/MCLiteLauncher
cd MCLiteLauncher

# Install dependencies
pip install -r requirements.txt

# Build EXE
pyinstaller MCLiteLauncher.spec --clean --noconfirm

# Build installer (butuh NSIS terinstall)
makensis installer/MCLiteLauncher.nsi
```

### Build via GitHub Actions
1. Fork repo ini
2. Buat tag versi: `git tag v1.0.0 && git push origin v1.0.0`
3. GitHub Actions otomatis build EXE + installer + buat Release

---

## 📁 Struktur Proyek
```
MCLiteLauncher/
├── src/
│   └── launcher.py          # Kode utama launcher (PyQt6)
├── assets/
│   ├── icon.ico             # Icon aplikasi
│   ├── header.bmp           # Header NSIS installer (150x57)
│   └── wizard.bmp           # Wizard NSIS installer (164x314)
├── installer/
│   └── MCLiteLauncher.nsi   # Script NSIS installer
├── .github/
│   └── workflows/
│       └── build.yml        # GitHub Actions build pipeline
├── MCLiteLauncher.spec      # PyInstaller spec
├── version_info.txt         # Windows EXE version resource
├── requirements.txt
├── LICENSE
└── README.md
```

---

## 🔍 Teknis: Cara Kerja Memory Trim

MCLite menggunakan **`psapi.EmptyWorkingSet(handle)`** — Windows API yang memaksa sistem memindahkan halaman memori yang tidak aktif dari RAM fisik ke pagefile. Ini efektif untuk mengatasi memory leak GDK karena:

1. GDK leak mengalokasikan memori yang tidak langsung dibebaskan
2. `EmptyWorkingSet` memaksa Windows "mendaur ulang" halaman-halaman ini
3. Saat Minecraft membutuhkan memori lagi, Windows mengambil dari pagefile (ada sedikit overhead, tapi lebih baik dari OOM crash)

Berbeda dari `SetProcessWorkingSetSize(-1, -1)` yang lebih agresif, `EmptyWorkingSet` lebih aman dan adalah cara resmi Microsoft untuk tujuan ini.

---

## ❓ FAQ

**Q: Apakah ini bisa download Minecraft?**  
A: Tidak. MCLite hanya launcher — Minecraft harus sudah terinstall via Xbox App atau Microsoft Store.

**Q: Kenapa butuh admin?**  
A: `EmptyWorkingSet` dan `SetPriorityClass` membutuhkan handle dengan akses `PROCESS_ALL_ACCESS`, yang di Windows memerlukan hak admin untuk proses yang bukan milik user sendiri.

**Q: Apakah aman?**  
A: Ya, MCLite hanya membaca informasi proses dan memanggil Windows API standar. Tidak ada modifikasi file game.

**Q: Apakah bisa dipakai dengan Minecraft versi lama (UWP)?**  
A: Bisa, tapi fitur memory trim paling bermanfaat untuk GDK (v1.21.120+). Fitur boost dan FPS overlay tetap berfungsi.

---

## 📄 License

MIT License — bebas digunakan, dimodifikasi, dan didistribusikan.

---

## 🙏 Credits

- Windows API: `EmptyWorkingSet`, `SetPriorityClass`, `NtSetTimerResolution`
- Info GDK migration: [Microsoft Learn](https://learn.microsoft.com/en-us/minecraft/creator/documents/gdkpcprojectfolder)
- Memory leak info: Minecraft Feedback community & Microsoft Q&A
