"""hr_window.py - 桌面无边框实时心率小窗（按 DIY 设置自己绘制）

> **类型**：代码
> **是什么**：无边框、置顶、可拖动、真正的逐像素透明小窗；外观照抄设置页里的 DIY 配置。
> **干什么**：直播/打游戏时看心率。图标（含 GIF 帧）、字号、颜色、各项偏移、跳动频率都跟着设置走。
> **怎么装**：不用装。双击 `实时心率小窗.bat`（先开 `启动心率服务.bat`）。
> **依赖/风险**：只读本机 127.0.0.1:8787；需要 Pillow；不写任何文件。
> **回滚**：右键 → 关闭，或 Esc。

为什么不用浏览器窗口：要无边框 + 置顶 + 透明，浏览器 app 窗口做不到（标题栏去不掉、置顶改不了）。
这里用 Win32 分层窗口（WS_EX_LAYERED + UpdateLayeredWindow）逐像素贴图，所以边缘是真的透明。

和网页版的差别（必须知道）：
  * 文字用 GDI 之外的 PIL 渲染，字形与浏览器有细微差别（同一字体文件，位置一致）
  * 电量做成简单圆环/电池条，不做 SVG 渐变
  * 复杂 CSS 动效（扫描线、glitch）不复刻

用法：
    python hr_window.py
    python hr_window.py --pos 40,40
    python hr_window.py --base http://127.0.0.1:8788
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import io
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------------- Win32 基础
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
WM_DESTROY, WM_TIMER, WM_LBUTTONDOWN, WM_RBUTTONUP, WM_KEYDOWN = 0x0002, 0x0113, 0x0201, 0x0205, 0x0100
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
WS_EX_NOACTIVATE = 0x08000000

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = wt.LPARAM


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


WNDPROC = ctypes.WINFUNCTYPE(wt.LPARAM, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


# 结构都定义完再挂签名，否则 POINTER(WNDCLASSEX) 还不存在
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
user32.RegisterClassExW.restype = wt.WORD
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                       ctypes.POINTER(wt.SIZE), wt.HDC, ctypes.POINTER(wt.POINT),
                                       wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


# ---------------------------------------------------------------- 主题与数据
API_BASE = os.environ.get("HR_API_BASE", "http://127.0.0.1:8787")
POLL = 0.5

FONT_MAP = {
    "segoe ui": r"C:\Windows\Fonts\segoeuib.ttf",
    "microsoft yahei": r"C:\Windows\Fonts\msyhbd.ttc",
    "微软雅黑": r"C:\Windows\Fonts\msyhbd.ttc",
    "consolas": r"C:\Windows\Fonts\consolab.ttf",
    "simhei": r"C:\Windows\Fonts\simhei.ttf",
    "黑体": r"C:\Windows\Fonts\simhei.ttf",
    "simsun": r"C:\Windows\Fonts\simsun.ttc",
    "宋体": r"C:\Windows\Fonts\simsun.ttc",
    "dengxian": r"C:\Windows\Fonts\Dengb.ttf",
    "等线": r"C:\Windows\Fonts\Dengb.ttf",
}


def _get(path, timeout=3):
    with urllib.request.urlopen(API_BASE.rstrip("/") + path, timeout=timeout) as r:
        return r.read()


def fetch_json(path):
    return json.loads(_get(path).decode("utf-8"))


def pick_font(family):
    fam = (family or "Segoe UI").split("&")[0].strip().lower()
    if fam in FONT_MAP and os.path.exists(FONT_MAP[fam]):
        return FONT_MAP[fam]
    for k, v in FONT_MAP.items():
        if k in fam and os.path.exists(v):
            return v
    return FONT_MAP["segoe ui"]


def parse_color(s, fallback=(255, 209, 102)):
    s = (s or "").strip()
    if s.startswith("#") and len(s) == 7:
        try:
            return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            pass
    return fallback


class State:
    def __init__(self):
        self.bpm = None
        self.zone = {"color": "#ffd60a"}
        self.theme = {}
        self.battery = None
        self.uptime = None
        self.icon_names = ("", "")
        self.assets = {}
        self.asset_ver = {}
        self.err = ""

    def poll(self):
        try:
            d = fetch_json("/api/hr")
            self.bpm = d.get("bpm")
            self.zone = d.get("zone") or {"color": "#ffd60a"}
            self.theme = d.get("theme") or {}
            self.battery = d.get("battery")
            self.asset_ver = d.get("assetver") or {}
            self.err = ""
        except Exception:
            self.err = "服务没开？"
        try:
            g = fetch_json("/api/game")
            self.uptime = g
        except Exception:
            self.uptime = None

    def icon_image(self):
        name = self.theme.get("icon") or ""
        if not name:
            return None, ""
        ver = self.asset_ver.get(name)
        key = (name, ver)
        if key in self.assets:
            return self.assets[key], name
        try:
            raw = _get("/asset/" + urllib.parse.quote(name) + ("?v=%s" % ver if ver else ""), timeout=5)
            im = Image.open(io.BytesIO(raw))
            frames, durations = [], []
            try:
                n = getattr(im, "n_frames", 1)
            except Exception:
                n = 1
            for i in range(n):
                im.seek(i)
                frames.append(im.convert("RGBA"))
                durations.append(im.info.get("duration", 100) / 1000.0)
            self.assets[key] = (frames, durations)
            return self.assets[key], name
        except Exception:
            self.assets[key] = None
            return None, name


# ---------------------------------------------------------------- 绘制
PANEL_BG = (12, 16, 24)
PAD = 2


def draw_round_panel(size, radius, alpha, border=None):
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius,
                        fill=PANEL_BG + (int(255 * alpha),),
                        outline=(border + (110,) if border else None), width=1)
    return img


def text_size(draw, s, font):
    box = draw.textbbox((0, 0), s, font=font)
    return box[2] - box[0], box[3] - box[1], box


def ink_text(text, font, color):
    """把文字画成"只保留墨迹"的 RGBA 图层，这样按底边对齐才是真的对齐。"""
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    box = probe.textbbox((0, 0), text, font=font)
    pad = 6
    w = max(1, box[2] - box[0]) + pad * 2
    h = max(1, box[3] - box[1]) + pad * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - box[0], pad - box[1]), text, font=font,
                             fill=tuple(color) + (255,))
    bb = img.getbbox()
    return img.crop(bb) if bb else img


def render(st, frame_idx, scale):
    t = st.theme
    icon_size = float(t.get("icon_size") or 55)
    num_size = float(t.get("num_size") or 75)
    gap = float(t.get("gap") or 14)
    gap_right = 14.0
    font_path = pick_font(t.get("font"))
    color = parse_color(st.zone.get("color"))

    # 图标
    icon_frames, _ = st.icon_image()
    icon = None
    if icon_frames and icon_frames[0]:
        frames, durs = icon_frames
        f = frames[frame_idx % len(frames)] if len(frames) > 1 else frames[0]
        side = int(round(icon_size * scale))
        icon = f.resize((side, side), Image.LANCZOS)

    # 数字
    bpm = st.bpm
    num_text = "--" if bpm is None else str(int(round(bpm)))
    if t.get("num_auto") and icon is not None:
        num_size = icon_size * 1.15
    num_size = num_size * scale
    font_big = ImageFont.truetype(font_path, max(8, int(round(num_size))))

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    nw, nh, nbox = text_size(probe, num_text, font_big)

    # 右侧信息列
    meta = []
    for key, size_key, flag_key, src in (
        ("unit", "unit_size", "unit_show", "BPM"),
        ("zone", "zone_size", "show_zone", st.zone.get("name") if isinstance(st.zone, dict) else ""),
        ("dev", "dev_size", "show_device", (st.theme.get("device_label") or "")),
        ("uptime", "uptime_size", "uptime_show", ""),
    ):
        if not t.get(flag_key):
            continue
        if key == "uptime":
            g = st.uptime or {}
            if g.get("process") and g.get("running"):
                label = t.get("uptime_label") or "时长"
                secs = int(g.get("elapsed") or 0)
                txt = "%s %d:%02d:%02d" % (label, secs // 3600, secs % 3600 // 60, secs % 60)
            else:
                continue
        elif key == "dev":
            txt = src or ""
        else:
            txt = src or ""
        if not txt:
            continue
        fs = float(t.get(size_key) or 12) * scale
        font = ImageFont.truetype(font_path, max(7, int(round(fs))))
        col = (210, 225, 240) if key in ("dev", "uptime") else tuple(color)
        layer = ink_text(txt, font, col)
        meta.append((key, layer))

    meta_w = max([m[1].width for m in meta], default=0)
    meta_h = sum([m[1].height for m in meta]) + max(0, len(meta) - 1) * 3

    left_w = (icon.width if icon else 0) + (gap * scale if icon else 0) + nw
    content_w = left_w + ((gap_right * scale + meta_w) if meta else 0)
    content_h = max(icon.height if icon else 0, nh, meta_h, 12)

    pad = 6 * scale
    W = int(math.ceil(content_w + pad * 2 + 4))
    H = int(math.ceil(content_h + pad * 2 + 4))

    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if t.get("show_panel"):
        radius = int(min(t.get("radius") or 16, H / 2))
        panel = draw_round_panel((W, H), radius, float(t.get("panel_alpha") or 0.55))
        base.alpha_composite(panel)

    bottom = H - pad
    x = pad

    # 图标（底部对齐）
    if icon is not None:
        ox = float(t.get("icon_offx") or 0) * scale
        oy = float(t.get("icon_offy") or 0) * scale
        base.alpha_composite(icon, (int(x + ox), int(bottom - icon.height + oy)))
        x += icon.width + gap * scale

    # 数字：先画到独立图层再按"墨迹底"贴，避免字体上下留白把对齐算歪
    nx = x + float(t.get("num_offx") or 0) * scale
    num_layer = ink_text(num_text, font_big, color)
    ny = int(bottom - num_layer.height + float(t.get("num_offy") or 0) * scale)
    glow = num_layer.filter(ImageFilter.GaussianBlur(max(1.6, 2.6 * scale)))
    a = glow.getchannel("A").point(lambda v: int(v * 0.55))
    glow.putalpha(a)
    base.alpha_composite(glow, (int(nx), ny))
    base.alpha_composite(num_layer, (int(nx), ny))

    # 右侧信息列（每项独立偏移，整列底部对齐）
    if meta:
        mx = pad + left_w + gap_right * scale
        my = bottom - meta_h
        d = ImageDraw.Draw(base)
        for key, layer in meta:
            ox = float(t.get("%s_offx" % key) or 0) * scale
            oy = float(t.get("%s_offy" % key) or 0) * scale
            base.alpha_composite(layer, (int(mx + ox), int(my + oy)))
            my += layer.height + 3
    return base


# ---------------------------------------------------------------- 窗口
class MiniWin:
    def __init__(self, st, pos):
        self.st = st
        self.frames = 0
        self.t0 = time.time()
        self.gif_t = 0.0
        self.gif_i = 0
        self.hwnd = None
        self._wndproc = WNDPROC(self._proc)

        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "DSHHRMini"
        if not user32.RegisterClassExW(ctypes.byref(cls)):
            err = ctypes.get_last_error()
            if err != 1410:  # 类已注册
                raise OSError("RegisterClassExW failed: %d" % err)

        x, y = pos
        self.pos = (x, y)
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        self.hwnd = user32.CreateWindowExW(
            ex, "DSHHRMini", "HR Mini", WS_POPUP,
            x, y, 240, 90, None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed: %d" % ctypes.get_last_error())
        self.w, self.h = 0, 0
        self.last = None
        self.paint()
        user32.SetTimer(self.hwnd, 1, 60, None)
        user32.ShowWindow(self.hwnd, 5)
        # 网络轮询放后台线程：UI 线程只负责画，别被 3 秒超时卡住
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    def _poll_loop(self):
        while True:
            try:
                self.st.poll()
            except Exception:
                pass
            time.sleep(0.7)

    def paint(self):
        st = self.st
        beat = 1.0
        hz = float(st.theme.get("beat_hz") or 0)
        if hz > 0:
            phase = (time.time() * hz) % 1.0
            beat = 1.0 + 0.06 * math.sin(phase * math.pi * 2)
        img = render(st, self.gif_i, beat)
        img = img.convert("RGBA")
        self.blit(img)

    def blit(self, img):
        w, h = img.size
        # UpdateLayeredWindow 会按 pptDst 摆放窗口，所以每次都读当前实际位置，别写死 0,0
        rect = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        if rect.left == 0 and rect.top == 0 and rect.right <= 0:
            rect.left, rect.top = self.pos
        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(hdc_mem, hbmp)
        data = img.tobytes("raw", "BGRA")
        ctypes.memmove(bits, data, len(data))
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        pt_src = wt.POINT(0, 0)
        pt_dst = wt.POINT(rect.left, rect.top)
        size = wt.SIZE(w, h)
        ok = user32.UpdateLayeredWindow(self.hwnd, hdc_screen, ctypes.byref(pt_dst),
                                        ctypes.byref(size), hdc_mem, ctypes.byref(pt_src),
                                        0, ctypes.byref(blend), ULW_ALPHA)
        self.w, self.h = w, h
        self.last_ok = bool(ok)
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

    def _proc(self, hwnd, msg, wp, lp):
        try:
            return self._handle(hwnd, msg, wp, lp)
        except Exception:
            import traceback
            traceback.print_exc()
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _handle(self, hwnd, msg, wp, lp):
        if msg == WM_TIMER:
            self.frames += 1
            self.gif_t += 0.06
            self.gif_i = int(self.gif_t * 10)
            self.paint()
            return 0
        if msg == WM_LBUTTONDOWN:
            user32.ReleaseCapture()
            user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
            return 0
        if msg == WM_RBUTTONUP:
            user32.PostMessageW(hwnd, WM_KEYDOWN, 0x1B, 0)   # Esc 也能关
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_KEYDOWN and wp == 0x1B:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self):
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("HR_API_BASE", "http://127.0.0.1:8787"))
    ap.add_argument("--pos", default="40,40")
    args = ap.parse_args()
    global API_BASE
    API_BASE = args.base
    try:
        x, y = [int(v) for v in args.pos.split(",")]
    except Exception:
        x, y = 40, 40
    st = State()
    st.poll()
    MiniWin(st, (x, y)).run()


if __name__ == "__main__":
    main()
