"""game_watch.py — 用「进程启动时间」算游戏玩了多久（Windows，纯 ctypes，不依赖 psutil）

思路：
  ／：不自己累加计时，直接问系统「这个进程什么时候起来的」，now - 创建时间 = 已运行时长。
  ／：好处是服务重启、切到后台、双开都不会算错；坏处是进程一直在跑就一直算。

对外三个函数：
  list_processes()       → [{"pid":1,"name":"a.exe","title":"窗口标题","started":1699999999.0}]
  find_game(name)        → 同名进程里启动最早的那个（返回 dict 或 None）
  game_status(cfg)       → {"process","ring_hours","running","pid","elapsed","started"}

注意：进程名大小写不敏感；只认 exe 名，不认路径。
"""
import ctypes
import time
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 260
FILETIME_EPOCH = 11644473600.0  # 1601-01-01 → 1970-01-01 的秒数


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_to_unix(ft):
    ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    if ticks <= 0:
        return None
    return ticks / 1e7 - FILETIME_EPOCH


def proc_start_time(pid):
    """进程启动时刻（unix 秒）；拿不到返回 None。"""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        cre, exi, ker, usr = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        ok = _k32.GetProcessTimes(h, ctypes.byref(cre), ctypes.byref(exi),
                                  ctypes.byref(ker), ctypes.byref(usr))
        return _filetime_to_unix(cre) if ok else None
    finally:
        _k32.CloseHandle(h)


def _window_titles():
    """pid → 第一个可见窗口标题（用来在设置台里认出「这是游戏」）。"""
    out = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        try:
            if not _u32.IsWindowVisible(hwnd):
                return True
            n = _u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            _u32.GetWindowTextW(hwnd, buf, n + 1)
            pid = wintypes.DWORD()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value and pid.value not in out:
                out[pid.value] = buf.value.strip()
        except Exception:
            pass
        return True

    try:
        _u32.EnumWindows(cb, 0)
    except Exception:
        pass
    return out


_CACHE = {"t": 0.0, "items": []}
_TTL = 2.0


def list_processes(force=False):
    """列进程（带 2 秒缓存，避免每次请求都全量快照）。"""
    now = time.time()
    if not force and _CACHE["items"] and now - _CACHE["t"] < _TTL:
        return _CACHE["items"]

    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    items = []
    if snap and snap != -1:
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile
                pid = entry.th32ProcessID
                if pid and name and name.lower() not in ("system", "registry", "memory compression"):
                    items.append({
                        "pid": pid,
                        "name": name,
                        "started": proc_start_time(pid),
                    })
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _k32.CloseHandle(snap)

    titles = _window_titles()
    for it in items:
        it["title"] = titles.get(it["pid"], "")

    _CACHE["t"] = now
    _CACHE["items"] = items
    return items


def find_game(name):
    """按 exe 名找进程；同名多个时取启动最早的那个（双开时算总时长）。"""
    if not name:
        return None
    want = str(name).strip().lower()
    if not want:
        return None
    hits = [p for p in list_processes() if p["name"].lower() == want]
    if not hits:
        hits = [p for p in list_processes() if p["name"].lower().startswith(want)]
    if not hits:
        return None
    hits.sort(key=lambda p: p.get("started") or 9e18)
    return hits[0]


def game_status(cfg):
    game = (cfg or {}).get("game") or {}
    name = (game.get("process") or "").strip()
    try:
        ring = float(game.get("ring_hours") or 4)
    except Exception:
        ring = 4.0
    st = {
        "process": name,
        "ring_hours": ring,
        "running": False,
        "pid": None,
        "elapsed": 0,
        "started": None,
        "title": "",
    }
    hit = find_game(name)
    if hit:
        st["running"] = True
        st["pid"] = hit["pid"]
        st["name"] = hit["name"]
        st["title"] = hit.get("title") or ""
        st["started"] = hit.get("started")
        if hit.get("started"):
            st["elapsed"] = max(0, int(time.time() - hit["started"]))
    return st


if __name__ == "__main__":
    import json
    print(json.dumps(list_processes(), ensure_ascii=False, indent=1)[:1200])
    print(json.dumps(game_status({"game": {"process": "", "ring_hours": 4}}), ensure_ascii=False))
