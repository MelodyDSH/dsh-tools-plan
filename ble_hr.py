"""ble_hr.py - 收小米手环「心率广播 / Share HR」的标准 BLE 心率

类型：代码
是什么：用 bleak 扫描 BLE、找带 0x180D 的设备，连上订阅 0x2A37 心率通知并解析。
干什么：手环开 Share HR 后，能在 PC 上实时看到心率；不开就搜不到 0x180D。
怎么装：不用装。依赖 bleak，已装在上级目录的 _pylibs；跑法见文件末尾 usage。
依赖/风险：只读 BLE，不改手环设置；会短暂占用手环连接（手环同时只能连少量设备）。
回滚：删掉本文件即可，不动系统任何配置。

用法：
  python ble_hr.py scan [秒数]          扫一遍，列出附近 BLE 设备
  python ble_hr.py watch <地址或名字片段> [秒数]   连上并打印实时心率
"""
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BODY_SENSOR_LOCATION = "00002a38-0000-1000-8000-00805f9b34fb"


def parse_hr(payload: bytearray):
    """按 Bluetooth SIG Heart Rate Measurement 解析。

    byte0 flags:
      bit0 = 心率值宽度 (0: uint8, 1: uint16 LE)
      bit1-2 = 传感器接触状态
      bit3 = 是否有能量消耗 (uint16 kJ)
      bit4 = 是否有 RR 间期 (uint16, 单位 1/1024 秒)
    """
    flags = payload[0]
    wide = bool(flags & 0x01)
    contact = (flags >> 1) & 0x03
    has_energy = bool(flags & 0x08)
    has_rr = bool(flags & 0x10)
    i = 1
    if wide:
        hr = int.from_bytes(payload[i:i + 2], "little")
        i += 2
    else:
        hr = payload[i]
        i += 1
    energy = None
    if has_energy and len(payload) >= i + 2:
        energy = int.from_bytes(payload[i:i + 2], "little")
        i += 2
    rr = []
    while has_rr and len(payload) >= i + 2:
        raw = int.from_bytes(payload[i:i + 2], "little")
        rr.append(raw / 1024.0)
        i += 2
    return hr, contact, energy, rr


async def cmd_scan(seconds=12.0):
    print(f"扫描 {seconds:.0f} 秒 ...", flush=True)
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for addr, (dev, adv) in found.items():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        rows.append((
            adv.rssi if adv.rssi is not None else dev.rssi,
            addr,
            adv.local_name or dev.name or "",
            uuids,
        ))
    rows.sort(reverse=True)
    print(f"共 {len(rows)} 台设备：")
    print(f"{'RSSI':>5}  {'地址':<20} {'名字':<24} 服务")
    for rssi, addr, name, uuids in rows:
        mark = ""
        if HR_SERVICE in uuids:
            mark = "  <== 心率服务 0x180D"
        elif any("fe95" in u or "fee0" in u for u in uuids):
            mark = "  <- 小米私有服务"
        print(f"{rssi:>5}  {addr:<20} {name:<24} {','.join(uuids) or '-'}{mark}")
    hr_devs = [r for r in rows if HR_SERVICE in r[3]]
    print()
    if hr_devs:
        print("带心率广播的设备：")
        for rssi, addr, name, _ in hr_devs:
            print(f"  {name or '(无名)'}  {addr}  RSSI={rssi}")
    else:
        print("没找到带 0x180D 的设备 —— 手环 Share HR 是否真的在广播？")
    return hr_devs


async def cmd_watch(target, seconds=30.0):
    print(f"查找 {target} ...", flush=True)
    found = await BleakScanner.discover(timeout=10.0, return_adv=True)
    hit = None
    for addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name or ""
        if target.lower() in addr.lower() or target.lower() in name.lower():
            hit = (addr, dev, adv)
            break
    if hit is None:
        print("没找到该设备，先跑 scan 看看。")
        return
    addr, dev, adv = hit
    print(f"连接 {adv.local_name or dev.name} ({addr}) ...", flush=True)
    last = {"t": 0.0}
    beats = []

    def on_hr(_sender, data: bytearray):
        hr, contact, energy, rr = parse_hr(data)
        now = time.time()
        beats.append(now)
        gap = f"  间隔 {now - last['t']:.2f}s" if last["t"] else ""
        last["t"] = now
        extra = f"  RR={[round(x, 3) for x in rr]}" if rr else ""
        print(f"[{time.strftime('%H:%M:%S')}] 心率 {hr} bpm"
              f"  接触={contact}  flags=0x{data[0]:02x}{gap}{extra}", flush=True)

    async with BleakClient(addr, timeout=20.0) as client:
        print("已连接:", client.is_connected)
        try:
            loc = await client.read_gatt_char(BODY_SENSOR_LOCATION)
            print("Body Sensor Location:", loc[0])
        except Exception as exc:  # 很多手环没有这个特征
            print("Body Sensor Location 读不到:", exc)
        await client.start_notify(HR_MEASUREMENT, on_hr)
        print(f"已订阅 0x2A37，收 {seconds:.0f} 秒 ...", flush=True)
        await asyncio.sleep(seconds)
        await client.stop_notify(HR_MEASUREMENT)
    if len(beats) >= 2:
        span = beats[-1] - beats[0]
        print(f"共 {len(beats)} 条通知，跨度 {span:.1f}s，平均 {span / (len(beats) - 1):.2f}s 一条")
    else:
        print(f"共收到 {len(beats)} 条通知")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2 or sys.argv[1] not in ("scan", "watch"):
        print(__doc__)
        return 1
    if sys.argv[1] == "scan":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
        asyncio.run(cmd_scan(secs))
        return 0
    if len(sys.argv) < 3:
        print("用法: python ble_hr.py watch <地址或名字片段> [秒数]")
        return 1
    target = sys.argv[2]
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    asyncio.run(cmd_watch(target, secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
