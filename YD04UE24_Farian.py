import time
import datetime
import os
import sys
import requests
import psutil
import pygetwindow as gw
import win32process
import win32api  # Deteksi input keyboard/mouse
import win32file # Deteksi tipe drive fisik (C:, D:, F:, Cloud, dsb)
import winreg
import getpass
import subprocess
import json
import ctypes
import ctypes.wintypes as wintypes

# ==========================================
# KONFIGURASI SUPABASE & GITHUB AUTO-UPDATE
# ==========================================
SUPABASE_URL = "https://ffsuhupsxjhryczgnjqj.supabase.co/rest/v1"
SUPABASE_KEY = "sb_publishable_lcAannkP02FbrpmynMC3ag_psQrfP3j"

# Identitas Laptop/Karyawan, Divisi, dan Area
EMPLOYEE_NAME = "YD04UE24_Farian"
DIVISION = "Presales"        # Sesuaikan Divisi (Presales/Sales/IT/dll)
AREA = "Jabodetabek"        # Sesuaikan Area (Jabodetabek/Bali/Papua/dll)

# Versi & Repositori GitHub Releases (ganti sesuai repo/release Anda)
AGENT_VERSION = "1.0.0"
GITHUB_OWNER = "farianpsd"
GITHUB_REPO = "atlas-agent-releases"
GITHUB_ASSET_NAME = "YD04UE24_Farian.exe"  # Nama file aset .exe di GitHub Releases
WINDOWS_SERVICE_NAME = "ATLASAgent"         # Nama Windows Service di NSSM
UPDATE_CHECK_INTERVAL = 3600                # Cek update otomatis setiap 1 jam (detik)

# Header Koneksi API Supabase
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal"
}

INTERVAL_SECONDS = 5
IDLE_THRESHOLD_SECONDS = 60  # Durasi tanpa input sebelum dianggap IDLE

# Variabel Global simpan lokasi & koordinat
CURRENT_LOCATION = "Unknown"
CURRENT_LAT = None
CURRENT_LON = None

# Variabel Global untuk mengingat aplikasi aktif terakhir (Cegah 'Unknown' selang-seling)
LAST_APP_NAME = "System / Desktop"
LAST_WINDOW_TITLE = "Desktop"

# ==========================================
# FUNGSI DETEKSI LOKASI (HYBRID: WIN API + IP FALLBACK)
# ==========================================
def fetch_ip_location():
    """Fallback: Mengambil lokasi & koordinat berdasarkan IP Geolocation"""
    global CURRENT_LOCATION, CURRENT_LAT, CURRENT_LON
    try:
        res = requests.get("http://ip-api.com/json/", timeout=5)
        if res.status_code == 200:
            data = res.json()
            city = data.get("city", "")
            region = data.get("regionName", "")
            country = data.get("country", "")
            CURRENT_LOCATION = f"{city}, {region}, {country}".strip(", ")
            CURRENT_LAT = data.get("lat")
            CURRENT_LON = data.get("lon")
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [LOKASI IP]: {CURRENT_LOCATION} ({CURRENT_LAT}, {CURRENT_LON})")
    except Exception as e:
        print(f"[ERROR LOKASI IP]: {e}")

def fetch_location():
    """Mengambil lokasi & koordinat presisi dari Windows Geolocation API"""
    global CURRENT_LOCATION, CURRENT_LAT, CURRENT_LON
    
    ps_script = """
    Add-Type -AssemblyName System.Device
    $GeoWatcher = New-Object System.Device.Location.GeoCoordinateWatcher
    $GeoWatcher.Start()
    
    $count = 0
    while (($GeoWatcher.Status -ne 'Ready') -and ($count -lt 25)) {
        Start-Sleep -Milliseconds 100
        $count++
    }
    
    if ($GeoWatcher.Status -eq 'Ready') {
        $loc = $GeoWatcher.Position.Location
        if (-not $loc.IsUnknown) {
            Write-Output "$($loc.Latitude),$($loc.Longitude)"
        } else {
            Write-Output "UNKNOWN"
        }
    } else {
        Write-Output "UNKNOWN"
    }
    """
    
    try:
        res = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW
        ).decode('utf-8').strip()
        
        if res and res != "UNKNOWN" and "," in res:
            lat_str, lon_str = res.split(',')
            CURRENT_LAT = float(lat_str)
            CURRENT_LON = float(lon_str)
            
            rev_url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={CURRENT_LAT}&lon={CURRENT_LON}"
            headers_osm = {'User-Agent': 'EmployeeMonitoringAgent/1.0'}
            r = requests.get(rev_url, headers=headers_osm, timeout=5)
            
            if r.status_code == 200:
                geo_data = r.json().get('address', {})
                kel = geo_data.get('village') or geo_data.get('suburb') or geo_data.get('neighbourhood') or ''
                kec = geo_data.get('city_district') or geo_data.get('district') or geo_data.get('subdistrict') or ''
                kota = geo_data.get('city') or geo_data.get('town') or geo_data.get('regency') or geo_data.get('county') or ''
                
                parts = [p for p in [kel, kec, kota] if p]
                CURRENT_LOCATION = ", ".join(parts) if parts else f"{CURRENT_LAT}, {CURRENT_LON}"
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [LOKASI WIN API]: {CURRENT_LOCATION} ({CURRENT_LAT}, {CURRENT_LON})")
                return
    except Exception:
        pass

    fetch_ip_location()

# ==========================================
# FUNGSI DETEKSI JENDELA AKTIF, USER AKTIF & MONITORING
# ==========================================
def get_active_windows_user() -> str:
    """Mengambil username Windows yang sedang aktif login (aman saat di layar logon)."""
    try:
        users = psutil.users()
        if users and len(users) > 0:
            names = [u.name for u in users if getattr(u, "name", None)]
            if names:
                return names[-1]
    except Exception:
        pass
    try:
        user = getpass.getuser()
        return user if user else "Logon / SYSTEM"
    except Exception:
        return "Logon / SYSTEM"

def get_idle_time():
    """Mengembalikan durasi diam pengguna (dalam detik) berdasarkan input mouse/keyboard"""
    try:
        last_input_time = win32api.GetLastInputInfo()
        current_time = win32api.GetTickCount()
        idle_seconds = (current_time - last_input_time) / 1000.0
        return idle_seconds
    except Exception:
        return 0

def get_active_window_details():
    """Mendeteksi aplikasi dan window title aktif, mengembalikan aplikasi terakhir jika gagal/sekilas kosong"""
    global LAST_APP_NAME, LAST_WINDOW_TITLE
    try:
        window = gw.getActiveWindow()
        if window and window.title and window.title.strip() != "":
            title = window.title.strip()
            hwnd = window._hWnd
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid)
            app_name = process.name()
            
            # Update variabel memori aplikasi aktif terakhir jika berhasil dibaca
            LAST_APP_NAME = app_name
            LAST_WINDOW_TITLE = title
            return app_name, title
    except Exception:
        pass
    
    # Jika sekilas terdeteksi 'Unknown' (saat perpindahan fokus window), pakai aplikasi aktif terakhir
    return LAST_APP_NAME, LAST_WINDOW_TITLE

def check_remote_switch():
    try:
        url = f"{SUPABASE_URL}/agent_switches?employee_name=eq.{EMPLOYEE_NAME}&select=is_active"
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data and len(data) > 0:
                return data[0].get("is_active", True)
    except Exception:
        pass
    return True

# ==========================================
# FUNGSI REMOTE PASSWORD RESET (AKUN LOKAL)
# ==========================================
class USER_INFO_1003(ctypes.Structure):
    _fields_ = [("usri1003_password", wintypes.LPWSTR)]

def reset_windows_local_password(username: str, new_password: str) -> tuple[bool, str]:
    """Mengganti password akun lokal Windows via NetUserSetInfo (Level 1003)."""
    netapi32 = ctypes.WinDLL("netapi32.dll")
    user_info = USER_INFO_1003(usri1003_password=new_password)
    parm_err = wintypes.DWORD(0)

    status = netapi32.NetUserSetInfo(
        None,
        ctypes.c_wchar_p(username),
        1003,
        ctypes.byref(user_info),
        ctypes.byref(parm_err)
    )

    if status == 0:
        return True, f"Password akun '{username}' berhasil direset."
    elif status == 5:
        return False, "Akses ditolak (Error 5). Jalankan skrip sebagai Administrator/SYSTEM."
    elif status == 2221:
        return False, f"Akun lokal '{username}' tidak ditemukan (Error 2221)."
    elif status == 2245:
        return False, "Password baru melanggar kebijakan kompleksitas Windows (Error 2245)."
    else:
        return False, f"Gagal mereset password. Windows Error Code: {status}"

def check_and_execute_password_reset():
    """Mengecek perintah pending reset password untuk EMPLOYEE_NAME ini."""
    try:
        url = (
            f"{SUPABASE_URL}/password_reset_commands"
            f"?employee=eq.{EMPLOYEE_NAME}&status=eq.PENDING&select=id,target_username,new_password"
        )
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code != 200:
            return

        commands = res.json()
        for cmd in commands:
            cmd_id = cmd.get("id")
            target_user = cmd.get("target_username")
            new_pass = cmd.get("new_password")

            if not target_user or not new_pass:
                continue

            success, message = reset_windows_local_password(target_user, new_pass)
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [RESET PASS] {message}")

            patch_url = f"{SUPABASE_URL}/password_reset_commands?id=eq.{cmd_id}"
            patch_payload = {
                "status": "DONE" if success else "FAILED",
                "result_message": message,
                "new_password": None,
                "executed_at": datetime.datetime.now().astimezone().isoformat()
            }
            requests.patch(patch_url, headers=HEADERS, json=patch_payload, timeout=5)
    except Exception as e:
        print(f"[ERROR PASSWORD RESET]: {e}")

# ==========================================
# FUNGSI REMOTE DEVICE COMMANDS (RESTART)
# ==========================================
def check_and_execute_device_commands():
    """Mengecek perintah perangkat jarak jauh (misalnya RESTART) dari Supabase."""
    try:
        url = (
            f"{SUPABASE_URL}/device_commands"
            f"?employee=eq.{EMPLOYEE_NAME}&status=eq.PENDING&select=id,command_type"
        )
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code != 200:
            return

        for cmd in res.json():
            cmd_id = cmd.get("id")
            cmd_type = (cmd.get("command_type") or "").strip().upper()

            if cmd_type == "RESTART":
                patch_url = f"{SUPABASE_URL}/device_commands?id=eq.{cmd_id}"
                requests.patch(
                    patch_url,
                    headers=HEADERS,
                    json={
                        "status": "DONE",
                        "result_message": "Perintah restart Windows dijalankan.",
                        "executed_at": datetime.datetime.now().astimezone().isoformat()
                    },
                    timeout=5
                )
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [RESTART] Menjalankan restart Windows...")
                subprocess.Popen(
                    ["shutdown", "/r", "/t", "5", "/f"],
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                return
    except Exception as e:
        print(f"[ERROR DEVICE COMMAND]: {e}")

# ==========================================
# FUNGSI AUTO-UPDATE DARI GITHUB RELEASES (.EXE)
# ==========================================
def _parse_version_tuple(ver_str: str) -> tuple:
    cleaned = str(ver_str or "").strip().lstrip("vV")
    parts = []
    for p in cleaned.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) if parts else (0,)

def _trigger_exe_updater(new_exe_path: str, target_exe_path: str):
    """Menjalankan skrip batch sementara untuk mengganti .exe & me-restart service."""
    updater_bat = os.path.join(os.environ.get("TEMP", "C:\\Windows\\Temp"), "atlas_updater.bat")
    bat_content = f"""@echo off
timeout /t 2 /nobreak >nul
net stop "{WINDOWS_SERVICE_NAME}" >nul 2>&1
timeout /t 2 /nobreak >nul
move /y "{new_exe_path}" "{target_exe_path}" >nul 2>&1
net start "{WINDOWS_SERVICE_NAME}" >nul 2>&1
del "%~f0" >nul 2>&1
"""
    with open(updater_bat, "w", encoding="utf-8") as f:
        f.write(bat_content)

    subprocess.Popen(
        ["cmd.exe", "/c", updater_bat],
        creationflags=subprocess.CREATE_NO_WINDOW
    )

def check_and_run_github_update():
    """Mengecek GitHub Releases terbaru dan memperbarui file .exe jika versi lebih baru."""
    if not getattr(sys, "frozen", False):
        return  # Hanya mengganti binary saat berjalan sebagai file .exe terkompilasi

    if "NAMA_ORG_ATAU_USER_GITHUB" in GITHUB_OWNER or "NAMA_REPO_ATLAS" in GITHUB_REPO:
        return

    try:
        api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
        resp = requests.get(
            api_url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "ATLAS-Agent"},
            timeout=10
        )
        if resp.status_code != 200:
            return

        rel_data = resp.json()
        latest_tag = rel_data.get("tag_name") or ""
        if not latest_tag:
            return

        if _parse_version_tuple(latest_tag) <= _parse_version_tuple(AGENT_VERSION):
            return

        download_url = None
        for asset in rel_data.get("assets", []):
            if asset.get("name") == GITHUB_ASSET_NAME:
                download_url = asset.get("browser_download_url")
                break

        if not download_url:
            return

        current_exe = sys.executable
        staged_exe = current_exe + ".new"

        dl_resp = requests.get(download_url, headers={"User-Agent": "ATLAS-Agent"}, stream=True, timeout=60)
        if dl_resp.status_code != 200:
            return

        with open(staged_exe, "wb") as out_f:
            for chunk in dl_resp.iter_content(chunk_size=65536):
                if chunk:
                    out_f.write(chunk)

        if os.path.getsize(staged_exe) < 100000:
            os.remove(staged_exe)
            return

        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [AUTO-UPDATE] Mengupdate ke versi {latest_tag}...")
        _trigger_exe_updater(staged_exe, current_exe)
    except Exception as e:
        print(f"[ERROR AUTO-UPDATE]: {e}")

# ==========================================
# FUNGSI PENGIRIMAN DATA TO SUPABASE
# ==========================================
def send_activity_log(app_name, title):
    try:
        url = f"{SUPABASE_URL}/laptop_logs"
        
        idle_time = get_idle_time()
        status_user = "IDLE" if idle_time >= IDLE_THRESHOLD_SECONDS else "ACTIVE"
        
        # MENENTUKAN TIMESTAMP LOKAL MULTI-TIMEZONE (WIB/WITA/WIT) BERDASARKAN SYSTEM LAPTOP
        local_iso_timestamp = datetime.datetime.now().astimezone().isoformat()
        
        payload = {
            "employee": EMPLOYEE_NAME,
            "division": DIVISION,
            "area": AREA,
            "location": CURRENT_LOCATION,
            "latitude": CURRENT_LAT,
            "longitude": CURRENT_LON,
            "app_name": app_name,
            "window_title": title,
            "status": status_user,
            "laptop_timestamp": local_iso_timestamp,
            "duration_seconds": INTERVAL_SECONDS
        }
        
        res = requests.post(url, headers=HEADERS, json=payload, timeout=5)
        if res.status_code in [200, 201]:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Log aktivitas terkirim ({status_user}).")
        else:
            print(f"[ERROR {res.status_code}] Supabase Menolak Log: {res.text}")
    except Exception as e:
        print(f"[ERROR KONEKSI LOG]: {e}")

# ==========================================
# UPDATE: KHUSUS MENG-GREP DISK SYSTEM C:\ + USER AKTIF
# ==========================================
def send_resource_usage():
    """Mengukur CPU, RAM, KHUSUS DISK C SYSTEM saja + user Windows aktif"""
    try:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()

        # Terkunci langsung dan hanya meng-grep Partisi System Drive C:\
        disk_c = psutil.disk_usage('C:\\')
        active_user = get_active_windows_user()

        url = f"{SUPABASE_URL}/laptop_resources"
        payload = {
            "employee": EMPLOYEE_NAME,
            "division": DIVISION,
            "area": AREA,
            "active_user": active_user,
            "cpu_usage_percent": cpu,
            "ram_usage_percent": ram.percent,
            "ram_used_gb": round(ram.used / (1024**3), 2),
            "ram_total_gb": round(ram.total / (1024**3), 2),
            "disk_usage_percent": disk_c.percent,
            "disk_free_gb": round(disk_c.free / (1024**3), 2),
            "disk_total_gb": round(disk_c.total / (1024**3), 2)
        }
        res = requests.post(url, headers=HEADERS, json=payload, timeout=10)
        if res.status_code in [200, 201]:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [RESOURCE] User Aktif: {active_user} | Disk C Total: {payload['disk_total_gb']} GB, Free: {payload['disk_free_gb']} GB.")
        elif res.status_code == 400 and "active_user" in res.text:
            # Fallback otomatis jika kolom active_user belum ditambahkan di tabel laptop_resources
            payload.pop("active_user", None)
            res_fb = requests.post(url, headers=HEADERS, json=payload, timeout=10)
            if res_fb.status_code in [200, 201]:
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [RESOURCE] User Aktif: {active_user} | Disk C Total: {payload['disk_total_gb']} GB, Free: {payload['disk_free_gb']} GB.")
        else:
            print(f"[ERROR {res.status_code}] Resource Menolak: {res.text}")
    except Exception as e:
        print(f"[ERROR RESOURCE]: {e}")

def get_installed_apps_from_reg(hive, flag):
    apps = []
    try:
        key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | flag)
        for i in range(0, winreg.QueryInfoKey(key)[0]):
            try:
                skey_name = winreg.EnumKey(key, i)
                skey = winreg.OpenKey(key, skey_name)
                name, _ = winreg.QueryValueEx(skey, "DisplayName")
                version = "Unknown"
                try:
                    version, _ = winreg.QueryValueEx(skey, "DisplayVersion")
                except EnvironmentError:
                    pass
                if name:
                    apps.append({
                        "employee": EMPLOYEE_NAME,
                        "app_name": str(name),
                        "app_version": str(version)
                    })
            except EnvironmentError:
                pass
    except Exception:
        pass
    return apps

def send_installed_apps():
    try:
        apps_64 = get_installed_apps_from_reg(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY)
        apps_32 = get_installed_apps_from_reg(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)
        apps_user = get_installed_apps_from_reg(winreg.HKEY_CURRENT_USER, 0)
        
        all_apps = apps_64 + apps_32 + apps_user
        
        if all_apps:
            url = f"{SUPABASE_URL}/laptop_installed_apps"
            chunk_size = 20
            for i in range(0, len(all_apps), chunk_size):
                chunk = all_apps[i:i + chunk_size]
                requests.post(url, headers=HEADERS, json=chunk, timeout=15)
                time.sleep(0.2)
            
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [INSTALLED APPS] Total {len(all_apps)} aplikasi terinstal terkirim.")
    except Exception as e:
        print(f"[ERROR INSTALLED APPS]: {e}")

# ==========================================
# UTAMA (MAIN LOOP PRESISI PER JAM DIJAMIN MASUK)
# ==========================================
if __name__ == "__main__":
    print(f"=== AGEN MONITORING LAPTOP BERJALAN v{AGENT_VERSION} ({EMPLOYEE_NAME} - {DIVISION} - {AREA}) ===")
    
    psutil.cpu_percent(interval=None)
    
    fetch_location()
    send_installed_apps()
    send_resource_usage() # Pengiriman awal saat skrip di-start
    check_and_run_github_update()
    
    # Mengunci jam pengiriman awal (misal: 15)
    last_resource_hour = datetime.datetime.now().hour
    last_app_check = time.time()
    last_location_check = time.time()
    last_update_check = time.time()

    while True:
        loop_start_time = time.time()
        now_dt = datetime.datetime.now()
        
        try:
            # 1. Update Lokasi Setiap 1 Jam (3600 detik)
            if loop_start_time - last_location_check >= 3600:
                fetch_location()
                last_location_check = loop_start_time

            # 2. Cek Perintah Reset Password & Restart Setiap 5 Detik
            check_and_execute_password_reset()
            check_and_execute_device_commands()

            is_active = check_remote_switch()
            if is_active:
                app_name, title = get_active_window_details()
                send_activity_log(app_name, title)
            else:
                print(f"[{now_dt.strftime('%H:%M:%S')}] Monitoring STATUS: OFF (Remote Switch Active)")

            # 3. PENGIRIMAN RESOURCE PRESISI SETIAP PERGANTIAN JAM BARU (15:00 -> 16:00 -> 17:00, dst)
            if now_dt.hour != last_resource_hour:
                send_resource_usage()
                last_resource_hour = now_dt.hour

            # 4. Cek Auto-Update GitHub Setiap 1 Jam (3600 detik)
            if loop_start_time - last_update_check >= UPDATE_CHECK_INTERVAL:
                check_and_run_github_update()
                last_update_check = loop_start_time

            # 5. Update Aplikasi Terinstal 24 Jam Sekali
            if loop_start_time - last_app_check >= 86400:
                send_installed_apps()
                last_app_check = loop_start_time

        except Exception as e:
            print(f"[ERROR MAIN LOOP]: {e}")
            
        elapsed_time = time.time() - loop_start_time
        sleep_duration = max(0.0, INTERVAL_SECONDS - elapsed_time)
        time.sleep(sleep_duration)