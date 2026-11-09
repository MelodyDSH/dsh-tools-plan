"""hr_service.py - 心率守护服务：锁手环 + 本地 HTTP/SSE 给 OBS 和小窗供数

> **类型**：代码
> **是什么**：一个常驻后台的 Python 服务，连小米手环收 0x2A37 心率，用 127.0.0.1:8787 对外提供网页/JSON/SSE。
> **干什么**：OBS 加「浏览器源」指向 http://127.0.0.1:8787/overlay 就能显示实时心跳；小窗也读同一个地址。
> **怎么装**：不用装。双击 `启动心率服务.bat` 即可；依赖 bleak 在上级目录 _pylibs。
> **依赖/风险**：只读 BLE + 监听本机回环端口；不改系统设置、不写注册表、不联网外发。
> **回滚**：关掉窗口即可；删掉整个 ble 目录等于没发生过。

设备锁定策略（对抗干扰）见 pick_device()：
  1. 配置里的 MAC 精确命中 = 满分，直接锁；
  2. 找不到 MAC 时按「名字 + 0x180D 服务 + RSSI 阈值」打分，且必须全场唯一最高分；
  3. 有并存的其它心率设备时报 multiple 警告，绝不自动串台；
  4. 连上后 20 秒内没有心率通知 → 判为连错设备，断开重扫。
"""
import asyncio
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote

from bleak import BleakClient, BleakScanner

import game_watch

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.json"
OVERLAY_DIR = ROOT / "overlay"

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

DEFAULT_CFG = {
    "port": 8787,
    "device": {
        "mac": "",
        "name_match": "Smart Band",
        "require_service": HR_SERVICE,
        "min_rssi": -85,
        "scan_seconds": 6,
    },
    "theme": {
        "icon": "",
        "icon_size": 55,
        "beat_hz": 0.0,
        "gap": 14,
        "gap_right": 18,
        "icon_offx": 0,
        "icon_offy": 0,
        "num_offx": 0,
        "num_offy": 0,
        "bg": "",
        "bg_opacity": 0.9,
        "panel_alpha": 0.55,
        "show_panel": False,
        "show_border": False,
        "radius": 40,
        "num_size": 75,
        "num_auto": True,
        "font": "Segoe UI",
        "meta_gap": 4,
        "unit_show": False,
        "unit_size": 13,
        "unit_offx": 0,
        "unit_offy": 0,
        "show_zone": False,
        "zone_size": 14,
        "zone_offx": 0,
        "zone_offy": 0,
        "show_device": False,
        "dev_size": 11,
        "dev_offx": 0,
        "dev_offy": 0,
        "device_label": "",
        "uptime_show": False,
        "uptime_size": 11,
        "uptime_offx": 0,
        "uptime_offy": 0,
        "uptime_label": "时长",
        "batt": "off",
        "batt_size": 22,
        "batt_num": "in",
        "batt_offx": 0,
        "batt_offy": 0,
        "rev": 0,
    },
    "game": {
        "process": "",
        "ring_hours": 24.0,
    },
    "zones": [
        {"name": "很低", "min": 0, "max": 50, "color": "#4ea1ff", "icon": "", "gif": ""},
        {"name": "静息", "min": 50, "max": 70, "color": "#43d17c", "icon": "", "gif": ""},
        {"name": "日常", "min": 70, "max": 100, "color": "#ffd166", "icon": "", "gif": ""},
        {"name": "运动", "min": 100, "max": 130, "color": "#ff8a3d", "icon": "", "gif": ""},
        {"name": "冲刺", "min": 130, "max": 300, "color": "#ff3b5c", "icon": "", "gif": ""},
    ],
}


def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


def load_cfg():
    """读配置：跟默认值做深合并，这样新增的配置项老文件也能自动补齐。"""
    if CFG_PATH.exists():
        try:
            cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
            merged = json.loads(json.dumps(DEFAULT_CFG))
            deep_merge(merged, cfg)
            # 老配置兼容：1.1 里电量叫 battery，1.2 起改成 batt + batt_num
            th = merged.get("theme", {})
            if "battery" in th:
                if "batt" not in th or th.get("batt") == DEFAULT_CFG["theme"]["batt"]:
                    th["batt"] = th.pop("battery")
                else:
                    th.pop("battery", None)
            # 老配置兼容：1.3 之前图标和数字共用 offx/offy，现在各挪各的
            if "offx" in th:
                if "icon_offx" not in th:
                    th["icon_offx"] = th.get("offx") or 0
                th.pop("offx", None)
            if "offy" in th:
                if "icon_offy" not in th:
                    th["icon_offy"] = th.get("offy") or 0
                th.pop("offy", None)
            return merged
        except Exception as exc:
            log("配置读不了，用默认值:", exc)
    CFG_PATH.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(json.dumps(DEFAULT_CFG))


def pretty_name(name, fallback="手环"):
    """BLE 广播里的名字偶尔是坏字节（GBK / 半截 UTF-8），显示前把替换符剔掉。"""
    if not name:
        return fallback
    cleaned = "".join(ch for ch in name if ch != "\ufffd").strip()
    return cleaned or fallback


FONT_CACHE = []


def list_fonts():
    """列出本机已安装的字体家族名（读注册表，去掉 (TrueType)/Bold 这类后缀）。"""
    global FONT_CACHE
    if FONT_CACHE:
        return FONT_CACHE
    import re
    names = set()
    try:
        import winreg
        subs = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        ]
        for root, sub in subs:
            try:
                with winreg.OpenKey(root, sub) as key:
                    i = 0
                    while True:
                        try:
                            name, _val, _t = winreg.EnumValue(key, i)
                        except OSError:
                            break
                        i += 1
                        base = re.sub(r"\s*\((TrueType|OpenType|All res)\)\s*$", "", name, flags=re.I)
                        base = re.sub(r"\s+(Bold|Italic|Oblique|Light|Semibold|SemiBold|"
                                      r"Black|Medium|Regular|Thin|ExtraLight|Heavy)\b.*$",
                                      "", base, flags=re.I).strip()
                        if base and not base.startswith("@") and not base.isdigit() and len(base) >= 2:
                            names.add(base)
            except OSError:
                pass
    except Exception as exc:
        log("字体列表读不到:", exc)
    prefer = ["Segoe UI", "Microsoft YaHei", "Microsoft YaHei UI", "等线", "DengXian",
              "SimHei", "SimSun", "KaiTi", "Consolas", "Arial", "Impact", "Bahnschrift"]
    out = [p for p in prefer if p in names]
    out += sorted(n for n in names if n not in out)
    FONT_CACHE = out
    return out


ASSET_VER = {"at": 0.0, "map": {}}


def asset_versions():
    """素材的「版本号」= 文件修改时间。用它破缓存，而不是用 theme.rev，
    否则每拖一下滑块 rev 就变，1MB 底图会被反复重载（这就是之前卡顿的来源）。"""
    now = time.time()
    if now - ASSET_VER["at"] < 1.0:
        return ASSET_VER["map"]
    out = {}
    adir = ROOT / "assets"
    if adir.exists():
        for p in adir.iterdir():
            if p.is_file():
                try:
                    out[p.name] = int(p.stat().st_mtime_ns)
                except OSError:
                    pass
    ASSET_VER["at"] = now
    ASSET_VER["map"] = out
    return out


def deep_merge(dst, src):
    """把 src 合并进 dst，dict 递归，其它类型直接覆盖。"""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v


def parse_hr(payload):
    """Heart Rate Measurement 解析，见 Bluetooth SIG 规范。"""
    flags = payload[0]
    i = 1
    if flags & 0x01:
        hr = int.from_bytes(payload[i:i + 2], "little")
        i += 2
    else:
        hr = payload[i]
        i += 1
    contact = (flags >> 1) & 0x03
    if flags & 0x08:
        i += 2
    rr = []
    while (flags & 0x10) and len(payload) >= i + 2:
        rr.append(int.from_bytes(payload[i:i + 2], "little") / 1024.0)
        i += 2
    return hr, contact, rr


def pick_zone(bpm, zones):
    for z in zones:
        if z["min"] <= bpm < z["max"]:
            return z
    return zones[-1] if bpm >= zones[-1]["max"] else zones[0]


# ---------- 预览假数据（设置台右上角的「预览」开关）----------
# 只在内存里造数：不写 config.json、不改主题、不影响 OBS 里正式显示。
# 纯开关，不设时长：点开一直有效，点关才停。
PREVIEW = {
    "on": False,
    "mode": "sweep",   # sweep=每 6 秒跳一档 / fixed=固定 / auto=慢慢往下掉
    "batt": 42,
    "started": 0.0,
}
PREVIEW_SWEEP = [100, 80, 60, 40, 20, 8]


def preview_state():
    """给设置台看的预览状态（纯开关，没有倒计时）。"""
    return {"on": PREVIEW["on"], "mode": PREVIEW["mode"],
            "batt": PREVIEW["batt"]}


def preview_values():
    """预览开着就返回 (bpm, battery)，否则 None。"""
    if not PREVIEW["on"]:
        return None
    now = time.time()
    t = now - PREVIEW["started"]
    bpm = int(round(78 + 26 * math.sin(t / 12.0)))
    mode = PREVIEW["mode"]
    if mode == "sweep":
        batt = PREVIEW_SWEEP[int(t // 6) % len(PREVIEW_SWEEP)]
    elif mode == "fixed":
        batt = int(PREVIEW["batt"])
    else:
        batt = 76 - (int(t) // 20) % 70
        if batt < 8:
            batt = 76
    return bpm, batt


def preview_game(cfg):
    """预览开着时给第三个环造一段假时长（从 0 走到满格，循环）。"""
    if not PREVIEW["on"]:
        return None
    g = cfg.get("game") or {}
    ring_h = float(g.get("ring_hours") or 24.0)
    span = max(60.0, ring_h * 3600.0)
    t = time.time() - PREVIEW["started"]
    elapsed = int((t * 60) % span)        # 1 秒真时间走 1 分钟假时长，10 分钟走满一圈
    return {
        "process": g.get("process") or "预览数据（假）",
        "ring_hours": ring_h,
        "running": True,
        "pid": 0,
        "elapsed": elapsed,
        "started": time.time() - elapsed,
        "name": "预览数据（假）",
        "title": "预览数据（假）",
        "preview": True,
    }


class State:
    def __init__(self):
        self.bpm = None
        self.contact = 0
        self.ts = 0.0
        self.connected = False
        self.device_name = ""
        self.device_addr = ""
        self.rssi = None
        self.zone = ""
        self.last_error = ""
        self.samples = 0
        self.battery = None
        self.battery_ts = 0.0

    def snapshot(self, cfg):
        zone = pick_zone(self.bpm, cfg["zones"]) if self.bpm else None
        d = {
            "bpm": self.bpm,
            "contact": self.contact,
            "ts": self.ts,
            "age": round(time.time() - self.ts, 1) if self.ts else None,
            "connected": self.connected,
            "device": self.device_name,
            "address": self.device_addr,
            "rssi": self.rssi,
            "samples": self.samples,
            "zone": zone,
            "battery": self.battery,
            "theme": cfg.get("theme", {}),
            "error": self.last_error,
        }
        pv = preview_values()
        if pv:
            d["bpm"], d["battery"] = pv
            d["zone"] = pick_zone(pv[0], cfg["zones"])
            d["device"] = "预览数据（假）"
            d["connected"] = True
            d["ts"] = time.time()
            d["age"] = 0.0
            d["preview"] = True
        return d


class HRService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State()
        self.subscribers = set()

    # ---------- BLE ----------
    async def pick_device(self):
        d = self.cfg["device"]
        log(f"扫描 {d['scan_seconds']}s 找手环 ...")
        found = await BleakScanner.discover(timeout=float(d["scan_seconds"]), return_adv=True)
        want_mac = (d.get("mac") or "").upper()
        name_match = (d.get("name_match") or "").lower()
        need_svc = (d.get("require_service") or "").lower()
        min_rssi = float(d.get("min_rssi", -85))

        cands = []
        for addr, (dev, adv) in found.items():
            raw_name = adv.local_name or ""
            # 广播里的名字偶尔是坏字节，优先用系统给的设备名兜底
            if not raw_name or "\ufffd" in raw_name:
                dev_name = dev.name or ""
                if dev_name and "\ufffd" not in dev_name:
                    raw_name = dev_name
            name = pretty_name(raw_name, name_match.title() or "手环")
            if "\ufffd" in (adv.local_name or "") and not (dev.name or ""):
                # 名字坏且系统也没名字：带上 MAC 尾巴，方便认设备
                name = f"{name} {addr[-5:].replace(':', '')}"
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            rssi = adv.rssi if adv.rssi is not None else (dev.rssi or -999)
            score = 0
            if want_mac and addr.upper() == want_mac:
                score += 100
            if name_match and name_match in raw_name.lower():
                score += 40
            if need_svc and need_svc in uuids:
                score += 50
            if rssi < min_rssi:
                score -= 60
            if score > 0:
                cands.append({"addr": addr, "name": name, "rssi": rssi,
                              "uuids": uuids, "score": score})

        hr_devs = [c for c in cands if HR_SERVICE in c["uuids"]]
        if len(hr_devs) > 1:
            log(f"注意：场上同时有 {len(hr_devs)} 台心率设备 "
                + ", ".join(f"{c['name'] or '无名'}({c['addr']},{c['rssi']})" for c in hr_devs))
        cands.sort(key=lambda c: (c["score"], c["rssi"]), reverse=True)
        if not cands:
            self.state.last_error = "没扫到目标手环（开了 Share HR 吗？）"
            log(self.state.last_error)
            return None
        best = cands[0]
        second = cands[1] if len(cands) > 1 else None
        if best["score"] < 50:
            self.state.last_error = f"候选分数太低({best['score']})，不敢连：{best['name']}"
            log(self.state.last_error)
            return None
        if second and second["score"] == best["score"] and second["rssi"] - best["rssi"] > -6:
            self.state.last_error = "两台设备分数打平，拒绝乱连"
            log(self.state.last_error, best["addr"], second["addr"])
            return None
        log(f"锁定 {best['name'] or '(无名)'} {best['addr']} RSSI={best['rssi']} score={best['score']}")
        return best

    def on_hr(self, _sender, data):
        try:
            hr, contact, _rr = parse_hr(data)
        except Exception as exc:
            log("心率解析失败:", exc, bytes(data).hex())
            return
        self.state.bpm = hr
        self.state.contact = contact
        self.state.ts = time.time()
        self.state.samples += 1
        self.broadcast()

    async def read_battery(self, client):
        """读标准电量特征 0x2A19。手环不暴露就返回 None，界面自动隐藏。"""
        try:
            raw = await client.read_gatt_char(BATTERY_LEVEL)
            self.state.battery = int(raw[0])
            self.state.battery_ts = time.time()
            log(f"电量 {self.state.battery}%")
        except Exception as exc:
            self.state.battery = None
            log("电量读不到（手环没暴露 0x2A19）:", type(exc).__name__)

    async def ble_loop(self):
        backoff = 1.0
        while True:
            dev = await self.pick_device()
            if dev is None:
                await asyncio.sleep(5)
                continue
            self.state.device_name = dev["name"]
            self.state.device_addr = dev["addr"]
            self.state.rssi = dev["rssi"]
            started = time.monotonic()
            try:
                async with BleakClient(dev["addr"], timeout=20.0) as client:
                    self.state.connected = True
                    self.state.last_error = ""
                    self.broadcast()
                    log("已连接，订阅 0x2A37")
                    await client.start_notify(HR_MEASUREMENT, self.on_hr)
                    await self.read_battery(client)
                    batt_at = time.monotonic()
                    while client.is_connected:
                        await asyncio.sleep(1.0)
                        if time.monotonic() - batt_at > 300:
                            batt_at = time.monotonic()
                            await self.read_battery(client)
                        if self.state.ts and time.time() - self.state.ts > 25:
                            self.state.last_error = "25 秒没心率，疑似连错设备，重扫"
                            log(self.state.last_error)
                            break
                    try:
                        await client.stop_notify(HR_MEASUREMENT)
                    except Exception:
                        pass
            except Exception as exc:
                self.state.last_error = f"{type(exc).__name__}: {exc}"
                log("连接失败:", self.state.last_error)
            finally:
                self.state.connected = False
                self.state.bpm = None
                self.broadcast()
            lived = time.monotonic() - started
            if lived > 60:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def snapshot(self):
        snap = self.state.snapshot(self.cfg)
        snap["assetver"] = asset_versions()
        return snap

    # ---------- 事件广播 ----------
    def broadcast(self):
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait("data: " + json.dumps(self.snapshot(), ensure_ascii=False) + "\n\n")
            except asyncio.QueueFull:
                dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            self.subscribers.discard(q)

    # ---------- HTTP ----------
    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            return
        try:
            head_lines = head.decode("latin-1").split("\r\n")
            method, raw_path, _ = (head_lines[0].split(" ") + ["", "", ""])[:3]
            query = ""
            if "?" in raw_path:
                raw_path, query = raw_path.split("?", 1)
            path = raw_path
            headers = {}
            for ln in head_lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            body = b""
            if method == "POST":
                n = int(headers.get("content-length") or 0)
                if n > 20 * 1024 * 1024:
                    await self.respond(writer, 413, "text/plain", b"too large")
                    return
                body = await reader.readexactly(n) if n > 0 else b""

            if method == "GET":
                await self.route_get(writer, path)
            elif method == "POST":
                await self.route_post(writer, path, query, body)
            elif method == "DELETE":
                await self.route_delete(writer, path, query)
            else:
                await self.respond(writer, 405, "text/plain", b"method not allowed")
        except Exception as exc:
            log("HTTP 出错:", exc)
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass

    async def route_get(self, writer, path):
        if path in ("/", "/overlay", "/index.html"):
            await self.respond(writer, 200, "text/html; charset=utf-8",
                               (OVERLAY_DIR / "index.html").read_bytes(), no_cache=True)
        elif path in ("/editor", "/editor.html"):
            await self.respond(writer, 200, "text/html; charset=utf-8",
                               (OVERLAY_DIR / "editor.html").read_bytes(), no_cache=True)
        elif path == "/api/game":
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(preview_game(self.cfg) or game_watch.game_status(self.cfg), ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/processes":
            items = game_watch.list_processes(force=True)
            slim = [{"pid": p["pid"], "name": p["name"], "title": p.get("title") or "",
                     "started": p.get("started")} for p in items
                    if (p.get("title") or p["name"].lower().endswith(".exe"))]
            slim.sort(key=lambda p: (not p["title"], -(p.get("started") or 0)))
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps({"items": slim[:400]}, ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/preview":
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(preview_state(), ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/hr":
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(self.snapshot(), ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/config":
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(self.cfg, ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/assets":
            items = []
            adir = ROOT / "assets"
            if adir.exists():
                for p in sorted(adir.iterdir()):
                    if p.is_file():
                        items.append({"name": p.name, "size": p.stat().st_size})
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(items, ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/fonts":
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(list_fonts(), ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/default-theme":
            # 设置页的「恢复默认」用这份，和代码里的出厂值保持同一处定义
            body = {
                "theme": json.loads(json.dumps(DEFAULT_CFG["theme"])),
                "zones": json.loads(json.dumps(DEFAULT_CFG["zones"])),
            }
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(body, ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/health":
            await self.respond(writer, 200, "text/plain", b"ok")
        elif path in ("/skins", "/api/skins"):
            items = []
            sdir = OVERLAY_DIR / "skins"
            if sdir.exists():
                for p in sorted(sdir.glob("*.html")):
                    title = p.stem
                    try:
                        head = p.read_text(encoding="utf-8")[:2000]
                        m = re.search(r"<title>(.*?)</title>", head, re.S | re.I)
                        if m:
                            title = m.group(1).strip()
                    except Exception:
                        pass
                    items.append({"name": p.stem, "title": title,
                                  "url": "/skin/" + p.stem})
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(items, ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path.startswith("/skin/"):
            name = os.path.basename(path[len("/skin/"):])
            if not name.endswith(".html"):
                name += ".html"
            fp = OVERLAY_DIR / "skins" / name
            if fp.is_file():
                await self.respond(writer, 200, "text/html; charset=utf-8",
                                   fp.read_bytes(), no_cache=True)
            else:
                await self.respond(writer, 404, "text/plain", b"no such skin")
        elif path == "/events":
            await self.sse(writer)
        elif path.startswith("/asset/"):
            await self.asset(writer, path[len("/asset/"):])
        else:
            await self.respond(writer, 404, "text/plain", b"not found")

    async def route_post(self, writer, path, query, body):
        if path == "/api/config":
            try:
                patch = json.loads(body.decode("utf-8"))
            except Exception as exc:
                await self.respond(writer, 400, "text/plain", f"bad json: {exc}".encode("utf-8"))
                return
            deep_merge(self.cfg, patch)
            self.cfg["theme"]["rev"] = int(self.cfg["theme"].get("rev", 0)) + 1
            CFG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self.broadcast()
            log("配置已保存（theme.rev=%s）" % self.cfg["theme"]["rev"])
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps({"ok": True, "config": self.cfg},
                                          ensure_ascii=False).encode("utf-8"), no_cache=True)
        elif path == "/api/game":
            try:
                patch = json.loads(body.decode("utf-8") or "{}")
            except Exception as exc:
                await self.respond(writer, 400, "text/plain", f"bad json: {exc}".encode("utf-8"))
                return
            game = self.cfg.setdefault("game", {"process": "", "ring_hours": 24.0})
            if "process" in patch:
                game["process"] = str(patch.get("process") or "").strip()
            if "ring_hours" in patch:
                try:
                    game["ring_hours"] = max(0.5, min(999.0, float(patch.get("ring_hours"))))
                except Exception:
                    pass
            CFG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self.broadcast()
            log("程序计时：%s（环 %.1f 小时）" % (game.get("process") or "未选", game.get("ring_hours") or 24))
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(preview_game(self.cfg) or game_watch.game_status(self.cfg), ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/preview":
            try:
                patch = json.loads(body.decode("utf-8") or "{}")
            except Exception as exc:
                await self.respond(writer, 400, "text/plain", f"bad json: {exc}".encode("utf-8"))
                return
            if patch.get("on"):
                PREVIEW["on"] = True
                PREVIEW["mode"] = patch.get("mode") or "sweep"
                PREVIEW["batt"] = int(patch.get("batt", 42) or 42)
                PREVIEW["started"] = time.time()
                log("预览效果：开")
            else:
                PREVIEW["on"] = False
                log("预览效果：关")
            self.broadcast()
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps(preview_state(), ensure_ascii=False).encode("utf-8"),
                               no_cache=True)
        elif path == "/api/asset":
            name = os.path.basename(parse_qs(query).get("name", [""])[0])
            ext = Path(name).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                await self.respond(writer, 400, "text/plain", b"only png/jpg/gif/webp")
                return
            if not name or not body:
                await self.respond(writer, 400, "text/plain", b"empty")
                return
            (ROOT / "assets").mkdir(parents=True, exist_ok=True)
            (ROOT / "assets" / name).write_bytes(body)
            log(f"素材已保存: {name} ({len(body)} 字节)")
            await self.respond(writer, 200, "application/json; charset=utf-8",
                               json.dumps({"ok": True, "name": name}).encode("utf-8"), no_cache=True)
        else:
            await self.respond(writer, 404, "text/plain", b"not found")

    async def route_delete(self, writer, path, query):
        """删素材。只删 ble/assets 下的单文件，名字先解码再 basename。"""
        if path != "/api/asset":
            await self.respond(writer, 404, "text/plain", b"not found")
            return
        name = os.path.basename(unquote(parse_qs(query).get("name", [""])[0]))
        if not name:
            await self.respond(writer, 400, "text/plain", b"missing name")
            return
        target = (ROOT / "assets" / name).resolve()
        base = (ROOT / "assets").resolve()
        if base not in target.parents or not target.exists():
            await self.respond(writer, 404, "text/plain", b"no asset")
            return
        try:
            size = target.stat().st_size
            target.unlink()
        except Exception as exc:
            await self.respond(writer, 500, "text/plain", f"{type(exc).__name__}: {exc}".encode("utf-8"))
            return
        # 删掉的图如果还被 theme/zones 引用，顺手清成空，免得页面一直 404
        t = self.cfg.get("theme", {})
        cleaned = []
        for key in ("icon", "bg"):
            if t.get(key) == name:
                t[key] = ""
                cleaned.append("theme." + key)
        for z in self.cfg.get("zones", []):
            for key in ("icon", "gif"):
                if z.get(key) == name:
                    z[key] = ""
                    cleaned.append(f"zone[{z.get('name')}].{key}")
        if cleaned:
            self.cfg["theme"]["rev"] = int(t.get("rev", 0)) + 1
            CFG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self.broadcast()
        log(f"素材已删除: {name} ({size} 字节)", ("清了引用: " + ", ".join(cleaned)) if cleaned else "")
        await self.respond(writer, 200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "name": name, "size": size,
                                       "cleaned": cleaned, "config": self.cfg},
                                      ensure_ascii=False).encode("utf-8"), no_cache=True)

    async def respond(self, writer, code, ctype, body, no_cache=False):
        head = (f"HTTP/1.1 {code} OK\r\n"
                f"Content-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"{'Cache-Control: no-store\r\n' if no_cache else ''}"
                f"Connection: close\r\n\r\n")
        writer.write(head.encode("latin-1") + body)

    async def asset(self, writer, name):
        """只允许读 ble/assets 下的文件，防目录穿越。名字要先做 URL 解码（中文文件名）。"""
        safe = Path(os.path.basename(unquote(name)))
        target = (ROOT / "assets" / safe).resolve()
        base = (ROOT / "assets").resolve()
        if base not in target.parents or not target.exists():
            await self.respond(writer, 404, "text/plain", b"no asset")
            return
        ctype = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(target.suffix.lower(),
                                                                  "application/octet-stream")
        await self.respond(writer, 200, ctype, target.read_bytes())

    async def sse(self, writer):
        q = asyncio.Queue(maxsize=8)
        self.subscribers.add(q)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Cache-Control: no-store\r\nAccess-Control-Allow-Origin: *\r\n"
                     b"Connection: keep-alive\r\n\r\n")
        try:
            first = "data: " + json.dumps(self.snapshot(), ensure_ascii=False) + "\n\n"
            writer.write(first.encode("utf-8"))
            await writer.drain()
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    writer.write(msg.encode("utf-8"))
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")
                await writer.drain()
        except Exception:
            pass
        finally:
            self.subscribers.discard(q)

    async def serve(self):
        srv = await asyncio.start_server(self.handle, "127.0.0.1", int(self.cfg["port"]))
        log(f"HTTP 已启动: http://127.0.0.1:{self.cfg['port']}/  (OBS 浏览器源用 /overlay)")
        async with srv:
            await srv.serve_forever()


async def demo_loop(svc):
    """--demo：不连手环，造一条正弦心率，专门用来调 OBS 布局/换图。

    2026-11-08 加电量模拟：手环不在家也能预览「有心率 + 有电量」的完整效果。
      --demo                 心率正弦 + 电量从 76% 慢慢往下掉（每 20 秒 -1，掉到 6 回到 100 循环）
      --demo --batt 42       电量固定 42%（想看某一档颜色就固定）
      --demo --batt-sweep    电量每 6 秒跳一档：100/80/60/40/20/8 轮着来（一次看完 5 段颜色）
    """
    import math
    import random
    log("DEMO 模式：假数据，不连手环（含电量模拟）")
    argv = sys.argv[1:]

    def _arg_int(name: str, default: int) -> int:
        try:
            return int(argv[argv.index(name) + 1])
        except (ValueError, IndexError):
            return default

    fixed_batt = _arg_int("--batt", -1)
    sweep = "--batt-sweep" in argv
    sweep_levels = [100, 80, 60, 40, 20, 8]
    batt = max(0, min(100, fixed_batt)) if fixed_batt >= 0 else 76

    t0 = time.time()
    last_batt_step = 0.0
    log(f"电量模拟：{'固定 ' + str(batt) + '%' if fixed_batt >= 0 else ('循环跳档' if sweep else '缓慢下降，从 76% 开始')}")
    while True:
        t = time.time() - t0
        bpm = 78 + 26 * math.sin(t / 12.0) + random.uniform(-2, 2)
        svc.state.bpm = max(40, int(round(bpm)))
        svc.state.contact = 3
        svc.state.ts = time.time()
        svc.state.connected = True
        svc.state.device_name = "DEMO BAND"
        svc.state.device_addr = "DE:MO:00:00:00:01"
        svc.state.rssi = -55
        svc.state.samples += 1
        svc.state.last_error = ""

        if fixed_batt < 0 and t - last_batt_step >= (6.0 if sweep else 20.0):
            last_batt_step = t
            if sweep:
                batt = sweep_levels[(sweep_levels.index(batt) + 1) % len(sweep_levels)] if batt in sweep_levels else sweep_levels[0]
            else:
                batt = 100 if batt <= 6 else batt - 1
        if fixed_batt < 0:
            svc.state.battery = batt
            svc.state.battery_ts = time.time()

        svc.broadcast()
        await asyncio.sleep(1.0)


async def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    cfg = load_cfg()
    if "--port" in args:
        cfg["port"] = int(args[args.index("--port") + 1])
    svc = HRService(cfg)
    demo = "--demo" in args
    log("心率服务启动" + ("（DEMO）" if demo else ""),
        "手环:", cfg["device"].get("name_match") or cfg["device"].get("mac"),
        "端口:", cfg["port"])
    if demo:
        await asyncio.gather(svc.serve(), demo_loop(svc))
    else:
        await asyncio.gather(svc.serve(), svc.ble_loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
