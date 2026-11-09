"""hr_window.py - 桌面上悬浮的实时心率小窗（读 hr_service 的 HTTP 接口）

> **类型**：代码
> **是什么**：无边框、置顶、可拖动的小窗，显示 bpm + 状态色 + 心跳动画。
> **干什么**：直播时瞟一眼就知道心率；单独运行也能当心率表用。
> **怎么装**：不用装。双击 `实时心率小窗.bat`；需要先开 `启动心率服务.bat`。
> **依赖/风险**：只读本机 127.0.0.1:8787，不碰 BLE、不写文件。
> **回滚**：关窗即结束。

操作：左键拖动，右键菜单「置顶 / 关闭」，Esc 关闭。
调试：设环境变量 HR_API 可指向别的服务（例如 demo 的 8788）。
"""
import json
import os
import sys
import threading
import time
import tkinter as tk
import urllib.request

API = os.environ.get("HR_API", "http://127.0.0.1:8787/api/hr")
POLL_SECONDS = 1.0

state = {"bpm": None, "zone": None, "dev": "", "connected": False, "err": ""}


def poll_loop():
    while True:
        try:
            with urllib.request.urlopen(API, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            state["bpm"] = data.get("bpm")
            state["zone"] = data.get("zone")
            state["dev"] = data.get("device") or ""
            state["connected"] = bool(data.get("connected"))
            state["err"] = data.get("error") or ""
        except Exception:
            state["bpm"] = None
            state["zone"] = None
            state["err"] = "服务没开？"
        time.sleep(POLL_SECONDS)


class Win:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg="#0c0e14")
        self.root.geometry("252x116+40+40")

        self.frame = tk.Frame(self.root, bg="#0c0e14", highlightthickness=2,
                              highlightbackground="#43d17c")
        self.frame.pack(fill="both", expand=True)
        self.frame.pack_propagate(False)

        self.heart = tk.Label(self.frame, text="♥", fg="#43d17c", bg="#0c0e14",
                              font=("Segoe UI", 28, "bold"))
        self.heart.pack(side="left", padx=(10, 2))

        right = tk.Frame(self.frame, bg="#0c0e14")
        right.pack(side="left", fill="both", expand=True, pady=4)

        self.num = tk.Label(right, text="--", fg="#ffffff", bg="#0c0e14",
                            font=("Segoe UI", 32, "bold"))
        self.num.pack(anchor="w")

        bottom = tk.Frame(right, bg="#0c0e14")
        bottom.pack(anchor="w")
        self.zone = tk.Label(bottom, text="等待服务", fg="#43d17c", bg="#0c0e14",
                             font=("Microsoft YaHei", 10, "bold"))
        self.zone.pack(side="left")
        self.unit = tk.Label(bottom, text=" BPM", fg="#8b93a1", bg="#0c0e14",
                             font=("Segoe UI", 9))
        self.unit.pack(side="left")

        self.dev = tk.Label(right, text="", fg="#5c6472", bg="#0c0e14",
                            font=("Segoe UI", 7))
        self.dev.pack(anchor="w")

        self.offset = (0, 0)
        widgets = [self.root, self.frame, self.heart, self.num, self.unit,
                   self.zone, self.dev, right, bottom]
        for w in widgets:
            w.bind("<Button-1>", self.start_move)
            w.bind("<B1-Motion>", self.do_move)
            w.bind("<Button-3>", self.menu)

        self.menu_obj = tk.Menu(self.root, tearoff=0)
        self.topmost = tk.BooleanVar(value=True)
        self.menu_obj.add_checkbutton(label="置顶", variable=self.topmost, command=self.toggle_top)
        self.menu_obj.add_command(label="关闭", command=self.root.destroy)

        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.beat_phase = 0
        self.tick()
        self.anim()

    def start_move(self, event):
        self.offset = (event.x, event.y)

    def do_move(self, event):
        x = self.root.winfo_x() + event.x - self.offset[0]
        y = self.root.winfo_y() + event.y - self.offset[1]
        self.root.geometry(f"+{x}+{y}")

    def menu(self, event):
        try:
            self.menu_obj.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu_obj.grab_release()

    def toggle_top(self):
        self.root.attributes("-topmost", bool(self.topmost.get()))

    def tick(self):
        bpm = state["bpm"]
        zone = state["zone"] or {}
        color = zone.get("color") or "#43d17c"
        if bpm is None:
            self.num.config(text="--", fg="#7b8390")
            self.heart.config(fg="#6b7280")
            self.zone.config(text=(state["err"] or "无信号")[:10], fg="#ff6b6b")
            self.dev.config(text=state["dev"][:26])
            self.frame.config(highlightbackground="#3a3f48")
        else:
            self.num.config(text=str(int(round(bpm))), fg="#ffffff")
            self.heart.config(fg=color)
            self.zone.config(text=zone.get("name", ""), fg=color)
            self.dev.config(text=state["dev"][:26])
            self.frame.config(highlightbackground=color)
        self.root.after(500, self.tick)

    def anim(self):
        """心跳动画：按 bpm 算间隔，一大一小地跳。"""
        bpm = state["bpm"] or 0
        if bpm > 0:
            interval = max(220, int(60000 / max(bpm, 40)))
            self.beat_phase = 1 - self.beat_phase
            size = 33 if self.beat_phase else 25
        else:
            interval = 900
            size = 28
        self.heart.config(font=("Segoe UI", size, "bold"))
        self.root.after(max(120, interval // 2), self.anim)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    threading.Thread(target=poll_loop, daemon=True).start()
    Win().run()

