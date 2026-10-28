import math
import os
import string
import sys
import tempfile
import threading
import time
from datetime import datetime

import requests
import urllib3
import pystray
from PIL import Image, ImageDraw, ImageFont

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

APP_NAME = "LoL Auto Accept Game"

LOCKFILE_RELATIVE = os.path.join("Riot Games", "League of Legends", "lockfile")
CHECK_INTERVAL = 1
LOCKFILE_RECHECK_INTERVAL = 5
REQUEST_TIMEOUT = 1.5
RECONNECT_FAIL_THRESHOLD = 5

LOG_FILE = os.path.join(tempfile.gettempdir(), "lol_auto_accept_game.log")
SINGLE_INSTANCE_PORT = 51837

worker = None
tray_icon = None
_instance_sock = None

_current_phase = None
_phase_lock = threading.Lock()
_last_icon_state = None
_icon_lock = threading.Lock()
_icon_cache = {}
_icon_cache_lock = threading.Lock()

_log_file_lock = threading.Lock()


def log(message):
    print(message)
    try:
        with _log_file_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def _lol_path_from_registry():
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    registry_entries = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Riot Games, Inc.\League of Legends"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Riot Games, Inc.\League of Legends"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Riot Games, Inc.\League of Legends"),
    ]
    for hive, path in registry_entries:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        try:
            for value_name in ("InstallLocation", "Path", "GamePath", "InstallDir"):
                try:
                    value, _ = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                if value and os.path.isdir(value):
                    return value
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
    return None


def _collect_lockfile_candidates():
    candidates = []
    seen = set()

    def add(p):
        if p and p not in seen:
            seen.add(p)
            candidates.append(p)

    add(os.environ.get("LOL_LOCKFILE_PATH"))

    reg_dir = _lol_path_from_registry()
    if reg_dir:
        add(os.path.join(reg_dir, "lockfile"))
        add(os.path.join(reg_dir, LOCKFILE_RELATIVE))

    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                if os.path.exists(root):
                    add(os.path.join(root, LOCKFILE_RELATIVE))
            except OSError:
                continue
    else:
        add(os.path.expanduser("~/Riot Games/League of Legends/lockfile"))

    return candidates


class LockfileLocator:
    def __init__(self):
        self._cached = None
        self._lock = threading.Lock()

    def invalidate(self):
        with self._lock:
            self._cached = None

    def find(self):
        with self._lock:
            if self._cached and os.path.isfile(self._cached):
                return self._cached
        for path in _collect_lockfile_candidates():
            if os.path.isfile(path):
                with self._lock:
                    self._cached = path
                return path
        return None


_ICON_STATES = {
    "green":  ((76, 175, 80),   "A"),
    "yellow": ((255, 193, 7),   "!"),
    "blue":   ((33, 150, 243),  "▶"),
    "red":    ((244, 67, 54),   "×"),
}


def _load_icon_font(size):
    for name in ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_state_icon(state):
    color, symbol = _ICON_STATES.get(state, _ICON_STATES["green"])
    S = 4
    size = 64 * S
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx = cy = size / 2
    r = size / 2 - 4 * S

    def hex_points(radius):
        pts = []
        for i in range(6):
            ang = math.pi / 3 * i - math.pi / 2
            pts.append((cx + radius * math.cos(ang), cy + radius * math.sin(ang)))
        return pts

    darker = tuple(int(c * 0.45) for c in color) + (255,)
    draw.polygon(hex_points(r), fill=darker)
    draw.polygon(hex_points(r - 5 * S), fill=color + (255,))

    font = _load_icon_font(52 * S)
    try:
        bbox = draw.textbbox((0, 0), symbol, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        draw.text(
            (cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]),
            symbol, fill="white", font=font,
        )
    except Exception as e:
        print(f"Не удалось нарисовать символ иконки: {e}")

    return img.resize((64, 64), Image.LANCZOS)


def get_state_icon(state):
    with _icon_cache_lock:
        cached = _icon_cache.get(state)
    if cached is not None:
        return cached
    img = _draw_state_icon(state)
    with _icon_cache_lock:
        _icon_cache[state] = img
    return img


def compute_icon_state():
    if worker is None or not worker.is_enabled():
        return "red"
    with _phase_lock:
        phase = _current_phase
    if phase is None:
        return "green"
    if phase == "ReadyCheck":
        return "yellow"
    if phase in ("ChampSelect", "GameStart", "InProgress", "Reconnect"):
        return "blue"
    return "green"


_TRAY_TITLES = {
    "green":  f"{APP_NAME} — Жду игру",
    "yellow": f"{APP_NAME} — Игра найдена!",
    "blue":   f"{APP_NAME} — В игре / Выбор",
    "red":    f"{APP_NAME} — Выключен",
}


def update_tray_icon(force=False):
    global _last_icon_state
    if tray_icon is None:
        return
    state = compute_icon_state()
    with _icon_lock:
        if not force and state == _last_icon_state:
            return
        _last_icon_state = state
    try:
        tray_icon.icon = get_state_icon(state)
        tray_icon.title = _TRAY_TITLES.get(state, APP_NAME)
    except Exception as e:
        print(f"Ошибка обновления иконки: {e}")


class AutoAcceptWorker:
    def __init__(self):
        self._enabled = True
        self._stop_event = threading.Event()
        self._thread = None
        self._port = None
        self._password = None
        self._locator = LockfileLocator()
        self._session = requests.Session()
        self._session.verify = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def toggle(self):
        self._enabled = not self._enabled
        return self._enabled

    def is_enabled(self):
        return self._enabled

    def _read_credentials(self):
        path = self._locator.find()
        if not path:
            return None, None
        try:
            with open(path, "r", encoding="utf-8") as f:
                parts = f.read().strip().split(":")
            if len(parts) < 4:
                self._locator.invalidate()
                return None, None
            return parts[2], parts[3]
        except (OSError, ValueError):
            self._locator.invalidate()
            return None, None

    def _get_phase(self):
        try:
            r = self._session.get(
                f"https://127.0.0.1:{self._port}/lol-gameflow/v1/gameflow-phase",
                auth=("riot", self._password),
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            return None
        return None

    def _accept_game(self):
        url = f"https://127.0.0.1:{self._port}/lol-matchmaking/v1/ready-check/accept"
        try:
            r = self._session.post(
                url, auth=("riot", self._password), timeout=REQUEST_TIMEOUT,
            )
            if r.status_code in (200, 204):
                log("✅ Игра принята!")
                return True
            log(f"⚠️ Статус accept: {r.status_code}")
            return False
        except requests.RequestException as e:
            log(f"❌ Ошибка accept: {e}")
            return False

    def _reconnect_if_needed(self):
        new_port, new_password = self._read_credentials()
        if new_port is not None and new_port != self._port:
            log(f"🔄 Переподключение к порту {new_port}")
            self._port = new_port
            self._password = new_password
            return True
        return False

    def _run(self):
        try:
            self._run_inner()
        finally:
            try:
                self._session.close()
            except Exception:
                pass

    def _run_inner(self):
        global _current_phase
        log("Жду клиент LoL...")

        while not self._stop_event.is_set():
            port, password = self._read_credentials()
            if port is not None:
                self._port = port
                self._password = password
                break
            self._stop_event.wait(2)

        if self._stop_event.is_set():
            return

        log(f"Подключён к порту {self._port}. Жду игру...")

        already_accepted = False
        last_logged_phase = None
        last_lockfile_check = time.time()
        consecutive_failures = 0

        while not self._stop_event.is_set():
            if not self._enabled:
                if _current_phase is not None:
                    with _phase_lock:
                        _current_phase = None
                    update_tray_icon()
                already_accepted = False
                last_logged_phase = None
                self._stop_event.wait(0.5)
                continue

            now = time.time()

            if now - last_lockfile_check >= LOCKFILE_RECHECK_INTERVAL:
                last_lockfile_check = now
                if self._reconnect_if_needed():
                    with _phase_lock:
                        _current_phase = None
                    already_accepted = False
                    last_logged_phase = None
                    consecutive_failures = 0

            phase = self._get_phase()

            if phase is None:
                consecutive_failures += 1
                if consecutive_failures >= RECONNECT_FAIL_THRESHOLD:
                    log("⚠️ Нет ответа от клиента, проверяю порт...")
                    self._locator.invalidate()
                    if self._reconnect_if_needed():
                        with _phase_lock:
                            _current_phase = None
                        already_accepted = False
                        last_logged_phase = None
                        consecutive_failures = 0
                    else:
                        consecutive_failures = RECONNECT_FAIL_THRESHOLD
            else:
                consecutive_failures = 0

            with _phase_lock:
                _current_phase = phase

            if phase != last_logged_phase:
                if phase:
                    log(f"📡 Фаза: {phase}")
                last_logged_phase = phase
                update_tray_icon()

            if phase == "ReadyCheck":
                if not already_accepted:
                    if self._accept_game():
                        already_accepted = True
            else:
                already_accepted = False

            self._stop_event.wait(CHECK_INTERVAL)


def _toggle_label(item):
    return "Выкл" if worker.is_enabled() else "Вкл"


def on_toggle(icon, item):
    enabled = worker.toggle()
    log("▶ Автоприём включён" if enabled else "⏸ Автоприём выключен")
    update_tray_icon(force=True)
    try:
        icon.update_menu()
    except Exception as e:
        print(f"Ошибка обновления меню: {e}")


def on_exit(icon, item):
    worker.stop()
    worker.join(timeout=3)
    icon.stop()


def run_tray():
    global tray_icon
    menu = pystray.Menu(
        pystray.MenuItem(_toggle_label, on_toggle),
        pystray.MenuItem("Выход", on_exit),
    )
    tray_icon = pystray.Icon(
        "LoL_Auto_Accept_Game",
        get_state_icon("green"),
        _TRAY_TITLES["green"],
        menu,
    )
    tray_icon.run()


def _acquire_single_instance():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(1)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        return None


def _show_already_running_message():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "Программа уже запущена.", APP_NAME, 0x40,
            )
            return
        except Exception:
            pass
    print("Программа уже запущена.")


if __name__ == "__main__":
    _instance_sock = _acquire_single_instance()
    if _instance_sock is None:
        _show_already_running_message()
        sys.exit(0)

    try:
        worker = AutoAcceptWorker()
        worker.start()

        log(f"Лог-файл: {LOG_FILE}")

        run_tray()
    finally:
        if _instance_sock is not None:
            try:
                _instance_sock.close()
            except OSError:
                pass