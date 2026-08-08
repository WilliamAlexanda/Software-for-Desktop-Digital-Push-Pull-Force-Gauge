"""实时读取数显推拉力计（115200, 8N1）。"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import messagebox, ttk
from ctypes import wintypes


FORCE_BAUDRATE = 2400
START_COMMAND = b"e"
DEFAULT_NEWTONS_PER_COUNT = 0.001
READ_TIMEOUT_MS = 10
UI_UPDATE_INTERVAL_MS = 10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
CLRDTR = 6
SETRTS = 3


class CommTimeouts(ctypes.Structure):
    _fields_ = [
        ("read_interval_timeout", wintypes.DWORD),
        ("read_total_timeout_multiplier", wintypes.DWORD),
        ("read_total_timeout_constant", wintypes.DWORD),
        ("write_total_timeout_multiplier", wintypes.DWORD),
        ("write_total_timeout_constant", wintypes.DWORD),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.SetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(CommTimeouts)]
kernel32.SetCommTimeouts.restype = wintypes.BOOL
kernel32.EscapeCommFunction.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.EscapeCommFunction.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.ReadFile.restype = wintypes.BOOL


def raise_last_error(action: str) -> None:
    error_code = ctypes.get_last_error()
    raise OSError(error_code, f"{action} failed: {ctypes.FormatError(error_code)}")


def counts_to_newtons(raw_integer: int, newtons_per_count: float) -> float:
    return raw_integer * newtons_per_count


def configure_force_port(port_name: str) -> None:
    if not re.fullmatch(r"COM\d+", port_name, re.IGNORECASE):
        raise ValueError("串口格式必须为 COM 加数字，例如 COM14")

    mode_executable = os.path.join(os.environ["SystemRoot"], "System32", "mode.com")
    current = subprocess.run(
        [mode_executable, port_name],
        capture_output=True,
        text=True,
    )
    baud_match = re.search(r"(?:Baud|波特率)\s*:\s*(\d+)", current.stdout, re.IGNORECASE)
    if baud_match and int(baud_match.group(1)) == FORCE_BAUDRATE:
        return

    completed = subprocess.run(
        [
            mode_executable,
            f"{port_name}:",
            f"BAUD={FORCE_BAUDRATE}",
            "PARITY=n",
            "DATA=8",
            "STOP=1",
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "未知错误"
        raise OSError(f"无法配置 {port_name} 为 {FORCE_BAUDRATE} 波特率：{detail}")


class NativeSerialPort:
    def __init__(self, port_name: str) -> None:
        self.port_name = port_name
        self.handle: int | None = None

    def open(self) -> None:
        self.handle = kernel32.CreateFileW(
            rf"\\.\{self.port_name}",
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if self.handle == INVALID_HANDLE_VALUE:
            self.handle = None
            raise_last_error(f"无法打开 {self.port_name}")

        try:
            timeouts = CommTimeouts(0xFFFFFFFF, 0, READ_TIMEOUT_MS, 0, 1000)
            if not kernel32.SetCommTimeouts(self.handle, ctypes.byref(timeouts)):
                raise_last_error("无法设置串口超时")
            if not kernel32.EscapeCommFunction(self.handle, CLRDTR):
                raise_last_error("无法关闭 DTR")
            if not kernel32.EscapeCommFunction(self.handle, SETRTS):
                raise_last_error("无法开启 RTS")
        except OSError:
            self.close()
            raise

    def write(self, data: bytes) -> None:
        if self.handle is None:
            raise RuntimeError("串口尚未打开")
        buffer = ctypes.create_string_buffer(data)
        bytes_written = wintypes.DWORD()
        if not kernel32.WriteFile(self.handle, buffer, len(data), ctypes.byref(bytes_written), None):
            raise_last_error("串口写入失败")
        if bytes_written.value != len(data):
            raise OSError(f"串口写入不完整：{bytes_written.value}/{len(data)}")

    def read(self, size: int = 256) -> bytes:
        if self.handle is None:
            raise RuntimeError("串口尚未打开")
        buffer = ctypes.create_string_buffer(size)
        bytes_read = wintypes.DWORD()
        if not kernel32.ReadFile(self.handle, buffer, size, ctypes.byref(bytes_read), None):
            raise_last_error("串口读取失败")
        return buffer.raw[: bytes_read.value]

    def close(self) -> None:
        if self.handle is not None:
            kernel32.CloseHandle(self.handle)
            self.handle = None


class ForceFrameParser:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, int]]:
        self.buffer.extend(data)
        values: list[tuple[str, int]] = []

        while True:
            start_index = self.buffer.find(b"\x55\x01")
            if start_index < 0:
                self.buffer[:] = self.buffer[-1:]
                break

            if len(self.buffer) < start_index + 8:
                if start_index:
                    del self.buffer[:start_index]
                break

            payload = bytes(self.buffer[start_index + 2 : start_index + 8])
            del self.buffer[: start_index + 1]

            if payload[:1] not in (b"+", b"-"):
                continue

            raw_integer = 0
            for digit in payload[1:]:
                raw_integer = (raw_integer << 4) | (digit & 0x0F)
            if payload[0] == ord("-"):
                raw_integer = -raw_integer

            raw_value = payload.decode("latin-1")
            values.append((raw_value, raw_integer))

        return values


class ForceGaugeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("测力计实时读取")
        self.root.minsize(520, 350)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.sample_times: deque[float] = deque()

        self.port_name = tk.StringVar(value="COM14")
        self.newtons_per_count_text = tk.StringVar(value=f"{DEFAULT_NEWTONS_PER_COUNT:g}")
        self.newtons_per_count = DEFAULT_NEWTONS_PER_COUNT
        self.status_text = tk.StringVar(value="未连接")
        self.force_text = tk.StringVar(value="—")
        self.raw_text = tk.StringVar(value="等待数据")
        self.time_text = tk.StringVar(value="")
        self.rate_text = tk.StringVar(value="采样率：—")

        self.build_ui()
        self.root.after(UI_UPDATE_INTERVAL_MS, self.process_messages)

    def build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Force.TLabel", font=("Segoe UI", 42, "bold"))
        style.configure("Subtitle.TLabel", foreground="#555555")

        container = ttk.Frame(self.root, padding=24)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)

        ttk.Label(container, text="串口号").grid(row=0, column=0, sticky="w")
        self.port_entry = ttk.Entry(container, textvariable=self.port_name, width=20)
        self.port_entry.grid(row=0, column=1, sticky="ew", padx=(12, 12))
        self.connect_button = ttk.Button(container, text="连接", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=2)

        ttk.Label(container, text="每计数牛顿值").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.scale_entry = ttk.Entry(container, textvariable=self.newtons_per_count_text, width=20)
        self.scale_entry.grid(row=1, column=1, sticky="ew", padx=(12, 12), pady=(12, 0))
        ttk.Label(container, text="默认 0.001；已按原厂程序的倍率表提取", style="Subtitle.TLabel").grid(
            row=1, column=2, sticky="w", pady=(12, 0)
        )

        ttk.Separator(container).grid(row=2, column=0, columnspan=3, sticky="ew", pady=22)
        ttk.Label(container, textvariable=self.status_text).grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Label(container, textvariable=self.force_text, style="Force.TLabel", anchor="center").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(10, 4)
        )
        ttk.Label(container, textvariable=self.raw_text, style="Subtitle.TLabel", anchor="center").grid(
            row=5, column=0, columnspan=3, sticky="ew"
        )
        ttk.Label(container, textvariable=self.time_text, style="Subtitle.TLabel", anchor="center").grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=(4, 0)
        )
        ttk.Label(container, textvariable=self.rate_text, style="Subtitle.TLabel", anchor="center").grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=(18, 0)
        )

        ttk.Label(
            container,
            text="提示：请先关闭原厂软件，避免它占用同一个串口。",
            style="Subtitle.TLabel",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(28, 0))

    def toggle_connection(self) -> None:
        if self.reader_thread and self.reader_thread.is_alive():
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        port_name = self.port_name.get().strip().upper()
        if not port_name:
            messagebox.showwarning("缺少串口号", "请输入串口号，例如 COM14。")
            return

        try:
            newtons_per_count = float(self.newtons_per_count_text.get())
            if newtons_per_count <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("校准值无效", "每计数牛顿值必须是大于零的数字。")
            return

        self.newtons_per_count = newtons_per_count
        self.stop_event.clear()
        self.port_entry.configure(state="disabled")
        self.scale_entry.configure(state="disabled")
        self.connect_button.configure(text="断开")
        self.status_text.set(f"正在连接 {port_name}…")
        self.force_text.set("—")
        self.raw_text.set("等待设备数据")
        self.sample_times.clear()
        self.reader_thread = threading.Thread(target=self.read_serial, args=(port_name,), daemon=True)
        self.reader_thread.start()

    def disconnect(self) -> None:
        self.stop_event.set()
        self.status_text.set("正在断开…")
        self.connect_button.configure(state="disabled")

    def read_serial(self, port_name: str) -> None:
        serial_port = NativeSerialPort(port_name)

        try:
            configure_force_port(port_name)
            serial_port.open()
            self.messages.put(("connected", port_name))
            parser = ForceFrameParser()
            received_force_value = False
            next_start_command = 0.0

            while not self.stop_event.is_set():
                if not received_force_value and time.monotonic() >= next_start_command:
                    serial_port.write(START_COMMAND)
                    next_start_command = time.monotonic() + 0.5

                incoming = serial_port.read()
                for raw_value, raw_integer in parser.feed(incoming):
                    received_force_value = True
                    self.messages.put(("value", (raw_value, raw_integer, time.time(), time.monotonic())))
        except OSError as error:
            self.messages.put(("error", str(error)))
        finally:
            serial_port.close()
            self.messages.put(("disconnected", None))

    def process_messages(self) -> None:
        latest_value: tuple[str, int, float, float] | None = None

        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break

            if kind == "connected":
                self.status_text.set(f"已连接 {payload}，正在接收数据")
            elif kind == "value":
                latest_value = payload  # type: ignore[assignment]
                self.sample_times.append(latest_value[3])
            elif kind == "error":
                self.status_text.set("连接失败")
                messagebox.showerror("串口错误", str(payload))
            elif kind == "disconnected":
                self.port_entry.configure(state="normal")
                self.scale_entry.configure(state="normal")
                self.connect_button.configure(state="normal", text="连接")
                if not self.status_text.get().startswith("连接失败"):
                    self.status_text.set("已断开")

        if latest_value:
            raw_value, raw_integer, timestamp, _ = latest_value
            now = time.monotonic()
            while self.sample_times and now - self.sample_times[0] > 5:
                self.sample_times.popleft()

            force_newtons = counts_to_newtons(raw_integer, self.newtons_per_count)
            self.force_text.set(f"{force_newtons:.3f} N")
            self.raw_text.set(f"原始编码：{raw_value}（{raw_integer} counts）")
            self.time_text.set(f"更新时间：{datetime.fromtimestamp(timestamp):%H:%M:%S.%f}"[:-3])
            if len(self.sample_times) >= 2:
                duration = self.sample_times[-1] - self.sample_times[0]
                if duration > 0:
                    self.rate_text.set(f"采样率：{(len(self.sample_times) - 1) / duration:.1f} Hz")

        self.root.after(UI_UPDATE_INTERVAL_MS, self.process_messages)

    def close(self) -> None:
        self.stop_event.set()
        self.root.destroy()


if __name__ == "__main__":
    application_root = tk.Tk()
    ForceGaugeApp(application_root)
    application_root.mainloop()
