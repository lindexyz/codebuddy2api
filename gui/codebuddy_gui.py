#!/usr/bin/env python3
"""CodeBuddy2API 桌面 GUI 工具。

单窗口集成全部功能：
  - 服务控制：配置监听地址/端口/API Key/脱敏，启动/停止内置转换器服务
  - 模型列表：一键刷新 /v1/models
  - 对话测试：直接在 UI 里向任意模型发送真实聊天请求
  - 日志查看：实时查看 converter.log
  - 健康状态：/health 检查、登录态与 token 过期展示

运行方式：
  1) 源码模式：在仓库根目录  python gui/codebuddy_gui.py
  2) 打包模式：双击 CodeBuddy2API-UI.exe（PyInstaller 单文件）
  3) 自检模式：python gui/codebuddy_gui.py --selftest  （无界面验证服务全链路）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import httpx
import socket
import uvicorn
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 仓库定位：源码模式从仓库根导入 core；打包模式 PyInstaller 已内置
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_ROOT = _repo_root()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.converter import (  # noqa: E402
    CONFIG,
    CredentialManager,
    app as fastapi_app,
    find_auth_file,
)

CONFIG_FILE = _ROOT / "gui_config.json"
DEFAULT_WB_PATHS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
    Path("G:/AI/WorkBuddy/WorkBuddy.exe"),
    Path("C:/Program Files/WorkBuddy/WorkBuddy.exe"),
]


# ---------------------------------------------------------------------------
# 服务控制器（与 UI 解耦，便于 --selftest 无界面验证）
# ---------------------------------------------------------------------------

class ServerController:
    """以编程方式启动/停止内嵌的 codebuddy2api 服务。"""

    def __init__(self):
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.host = "127.0.0.1"
        self.port = 8787

    @property
    def running(self) -> bool:
        return self.server is not None and not self.server.should_exit

    def start(self, host: str, port: int, api_key: str, desensitize: bool,
              no_compact: bool, log_enabled: bool, workbuddy_path: str) -> str:
        if self.running:
            return f"服务已在运行：http://{self.host}:{self.port}"
        if _port_in_use(host, port):
            raise RuntimeError(
                f"端口 {host}:{port} 已被占用（可能已有 codebuddy2api 或其他服务在运行）。\n"
                "请在下方修改端口后重试，或先停止占用该端口的进程。"
            )
        if workbuddy_path:
            os.environ["WORKBUDDY_ELECTRON_PATH"] = workbuddy_path
        af = find_auth_file()
        if af is None:
            raise RuntimeError("未找到 CodeBuddy/WorkBuddy 登录态文件，请先打开桌面端完成登录")
        CONFIG["api_key"] = api_key
        CONFIG["desensitize"] = desensitize
        CONFIG["no_compact"] = no_compact
        CONFIG["log_path"] = str(_ROOT / "converter.log") if log_enabled else None
        CONFIG["cred"] = CredentialManager(af)
        self.host, self.port = host, port
        # log_config=None：跳过 uvicorn 的 dictConfig（其 'default' formatter 在
        # 打包/同进程二次配置场景下会抛 "Unable to configure formatter 'default'"）。
        # 服务请求日志由 converter.log 独立记录，不受影响。
        config = uvicorn.Config(
            fastapi_app, host=host, port=port, log_level="warning", log_config=None
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        return f"服务启动中：http://{host}:{port}"

    def stop(self) -> str:
        if not self.running:
            return "服务未在运行"
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.server = None
        self.thread = None
        return "服务已停止"

    @property
    def base_url(self) -> str:
        # 对外监听 0.0.0.0 时，本机内部请求用 127.0.0.1（Windows 无法连接 0.0.0.0）
        return f"http://{client_host(self.host)}:{self.port}"


def load_config() -> dict:
    defaults = {
        "workbuddy_path": "",
        "host": "127.0.0.1",
        "port": 8787,
        "api_key": "",
        "desensitize": True,
        "no_compact": False,
        "log_enabled": True,
    }
    if CONFIG_FILE.exists():
        try:
            defaults.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return defaults


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _port_in_use(host: str, port: int) -> bool:
    """检测端口是否已被占用。"""
    import contextlib

    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(1)
        return s.connect_ex((client_host(host), port)) == 0


def client_host(bind_host: str) -> str:
    """返回本机可连接的主机地址：0.0.0.0/:: 在 Windows 上不可作为连接目标。"""
    return "127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host


# ---------------------------------------------------------------------------
# 剩余额度查询（复用 admin/pool.py 的积分汇总逻辑，仅支持国内账号）
# ---------------------------------------------------------------------------

BILLING = "https://www.codebuddy.cn/v2/billing/meter/"


def query_credits(headers: dict) -> dict:
    """查询当前账号当前周期的剩余积分（分页汇总所有未过期积分包）。"""
    from admin.pool import summarize_packages

    now = datetime.now(timezone(timedelta(hours=8)))
    packages: list = []
    with httpx.Client(timeout=20, follow_redirects=False) as c:
        for page in range(1, 101):
            r = c.post(
                BILLING + "get-user-resource",
                headers=headers,
                json={
                    "PageNumber": page,
                    "PageSize": 100,
                    "ProductCode": "p_tcaca",
                    "Status": [0, 3],
                    "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "PackageEndTimeRangeEnd": "2126-01-01 00:00:00",
                },
            )
            if r.status_code != 200:
                raise RuntimeError(f"积分服务请求失败（HTTP {r.status_code}）")
            d = r.json()
            if not isinstance(d, dict) or d.get("code") != 0:
                raise RuntimeError(f"积分查询被上游拒绝（code={d.get('code')}），请检查账号或稍后重试")
            data = (d.get("data") or {}).get("Response", {}).get("Data", {})
            current = data.get("Accounts")
            total = int(data.get("TotalCount", 0))
            if not isinstance(current, list):
                raise RuntimeError("积分数据格式异常")
            packages.extend(current)
            if len(packages) >= total or not current:
                return summarize_packages(packages)
    raise RuntimeError("积分包分页不完整")


def detect_workbuddy() -> str:
    """自动检测 WorkBuddy.exe：默认路径 → 正在运行的进程。"""
    for p in DEFAULT_WB_PATHS:
        if p.is_file():
            return str(p)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | ? { $_.Name -match 'WorkBuddy' } | Select -First 1 -Expand Path"],
            capture_output=True, text=True, timeout=15,
        )
        path = (r.stdout or "").strip()
        if path and Path(path).is_file():
            return path
    except Exception:
        pass
    return ""


def http_get(url: str, api_key: str = "", timeout: float = 15) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.get(url, headers=headers, timeout=timeout)
    return r.status_code, r.text


def http_chat(base_url: str, model: str, content: str, api_key: str = "",
              temperature: float = 0.0, timeout: float = 120) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "stream": False,
    }
    r = httpx.post(f"{base_url}/v1/chat/completions", headers=headers, json=body, timeout=timeout)
    if r.status_code == 200:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        return 200, content
    return r.status_code, r.text[:800]


# ---------------------------------------------------------------------------
# 无界面自检（--selftest）：验证 启动 → health → models → 真实对话 → 停止
# ---------------------------------------------------------------------------

def selftest() -> int:
    print("==== CodeBuddy2API GUI 自检 ====")
    wb = detect_workbuddy()
    print("WorkBuddy:", wb or "未检测到（将使用默认探测路径）")
    ctrl = ServerController()
    try:
        msg = ctrl.start("127.0.0.1", 18787, "", True, False, False, wb)
        print("[1/5]", msg)
        for _ in range(30):
            time.sleep(1)
            try:
                code, body = http_get(f"{ctrl.base_url}/health")
                if code == 200:
                    break
            except Exception:
                continue
        print("[2/5] health:", code, "token_expired:", json.loads(body)["credential"]["token_expired"])
        code, body = http_get(f"{ctrl.base_url}/v1/models")
        models = [m["id"] for m in json.loads(body)["data"]]
        print(f"[3/5] models: {code}, 共 {len(models)} 个，DeepSeek:", [m for m in models if "deepseek" in m])
        code, body = http_chat(ctrl.base_url, "deepseek-v4-flash", "请只回复：GUI_SELFTEST_OK")
        print(f"[4/5] chat: {code}, 回复: {body[:80]}")
        ok = code == 200 and "GUI_SELFTEST_OK" in body
        print("[5/5] 自检结果:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        print("stop:", ctrl.stop())


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("CodeBuddy2API 控制台")
        root.geometry("760x560")
        self.ctrl = ServerController()
        self.cfg = load_config()
        self._build_ui()
        self._poll_status()

    # ---- UI 构建 ----
    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        f_srv = ttk.Frame(nb); nb.add(f_srv, text=" 服务控制 ")
        f_mdl = ttk.Frame(nb); nb.add(f_mdl, text=" 模型列表 ")
        f_chat = ttk.Frame(nb); nb.add(f_chat, text=" 对话测试 ")
        f_credit = ttk.Frame(nb); nb.add(f_credit, text=" 额度查询 ")
        f_log = ttk.Frame(nb); nb.add(f_log, text=" 日志 ")

        # --- 服务控制 ---
        row = 0
        ttk.Label(f_srv, text="WorkBuddy 可执行文件：").grid(row=row, column=0, sticky="e", padx=4, pady=4)
        self.e_wb = ttk.Entry(f_srv, width=52)
        self.e_wb.insert(0, self.cfg.get("workbuddy_path", ""))
        self.e_wb.grid(row=row, column=1, columnspan=2, sticky="we", padx=4)
        ttk.Button(f_srv, text="自动检测", command=self.on_detect).grid(row=row, column=3, padx=2)
        ttk.Button(f_srv, text="浏览...", command=self.on_browse).grid(row=row, column=4, padx=2)

        row += 1
        ttk.Label(f_srv, text="监听地址：").grid(row=row, column=0, sticky="e", padx=4)
        self.e_host = ttk.Entry(f_srv, width=16)
        self.e_host.insert(0, self.cfg.get("host", "127.0.0.1"))
        self.e_host.grid(row=row, column=1, sticky="w", padx=4)
        ttk.Label(f_srv, text="端口：").grid(row=row, column=1, sticky="e", padx=(120, 0))
        self.e_port = ttk.Entry(f_srv, width=8)
        self.e_port.insert(0, str(self.cfg.get("port", 8787)))
        self.e_port.grid(row=row, column=2, sticky="w", padx=4)
        ttk.Label(f_srv, text="API Key（可选）：").grid(row=row, column=2, sticky="e", padx=(80, 0))
        self.e_key = ttk.Entry(f_srv, width=18, show="*")
        self.e_key.insert(0, self.cfg.get("api_key", ""))
        self.e_key.grid(row=row, column=3, columnspan=2, sticky="w", padx=4)

        row += 1
        self.v_des = tk.BooleanVar(value=self.cfg.get("desensitize", True))
        self.v_noc = tk.BooleanVar(value=self.cfg.get("no_compact", False))
        self.v_log = tk.BooleanVar(value=self.cfg.get("log_enabled", True))
        ttk.Checkbutton(f_srv, text="启用脱敏（--desensitize，建议开启）", variable=self.v_des).grid(
            row=row, column=1, sticky="w", padx=4)
        ttk.Checkbutton(f_srv, text="保留全文（--no-compact）", variable=self.v_noc).grid(
            row=row, column=2, sticky="w", padx=4)
        ttk.Checkbutton(f_srv, text="写日志 converter.log", variable=self.v_log).grid(
            row=row, column=3, sticky="w", padx=4)

        row += 1
        self.btn_start = ttk.Button(f_srv, text="启动服务", command=self.on_start)
        self.btn_start.grid(row=row, column=1, sticky="we", padx=4, pady=8)
        self.btn_stop = ttk.Button(f_srv, text="停止服务", command=self.on_stop, state="disabled")
        self.btn_stop.grid(row=row, column=2, sticky="we", padx=4, pady=8)
        ttk.Button(f_srv, text="保存配置", command=self.on_save).grid(row=row, column=3, sticky="we", padx=4, pady=8)

        row += 1
        self.lbl_status = ttk.Label(f_srv, text="状态：未启动", font=("", 11, "bold"))
        self.lbl_status.grid(row=row, column=0, columnspan=5, sticky="w", padx=8)

        row += 1
        self.txt_info = tk.Text(f_srv, height=12, wrap="word", state="disabled")
        self.txt_info.grid(row=row, column=0, columnspan=5, sticky="nsew", padx=8, pady=6)
        f_srv.columnconfigure(1, weight=1)
        f_srv.rowconfigure(row, weight=1)
        self._info(
            "使用说明：\n"
            "1. 确保 WorkBuddy/CodeBuddy 桌面端已在本机登录。\n"
            "2. WorkBuddy 路径用于读取本机登录态密钥，点“自动检测”填写。\n"
            "3. 点“启动服务”，状态变为运行中后即可在模型列表 / 对话测试页使用。\n"
            "4. 外部客户端接入：Base URL = http://127.0.0.1:8787/v1，API Key 留空"
            "（除非启动时设置了鉴权 Key）。支持 /v1/chat/completions、/v1/messages、/v1/responses。\n"
        )

        # --- 模型列表 ---
        ttk.Button(f_mdl, text="刷新模型列表", command=self.on_models).pack(pady=6)
        self.lbl_models = ttk.Label(f_mdl, text="尚未获取")
        self.lbl_models.pack()
        self.lb_models = tk.Listbox(f_mdl)
        self.lb_models.pack(fill="both", expand=True, padx=8, pady=6)

        # --- 对话测试 ---
        crow = 0
        ttk.Label(f_chat, text="模型：").grid(row=crow, column=0, sticky="e", padx=4, pady=4)
        self.cb_model = ttk.Combobox(f_chat, width=32, values=["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2", "auto"])
        self.cb_model.set("deepseek-v4-flash")
        self.cb_model.grid(row=crow, column=1, sticky="w")
        ttk.Label(f_chat, text="temperature：").grid(row=crow, column=2, sticky="e")
        self.sp_temp = ttk.Spinbox(f_chat, from_=0, to=2, increment=0.1, width=6)
        self.sp_temp.set("0")
        self.sp_temp.grid(row=crow, column=3, sticky="w")

        crow += 1
        ttk.Label(f_chat, text="消息：").grid(row=crow, column=0, sticky="ne", padx=4)
        self.txt_prompt = tk.Text(f_chat, height=6, wrap="word")
        self.txt_prompt.insert("1.0", "请只回复：CODEBUDDY_GUI_OK")
        self.txt_prompt.grid(row=crow, column=1, columnspan=3, sticky="we", padx=4)

        crow += 1
        self.btn_send = ttk.Button(f_chat, text="发送请求", command=self.on_send)
        self.btn_send.grid(row=crow, column=1, sticky="w", padx=4, pady=6)

        crow += 1
        ttk.Label(f_chat, text="回复：").grid(row=crow, column=0, sticky="ne", padx=4)
        self.txt_reply = tk.Text(f_chat, wrap="word", state="disabled")
        self.txt_reply.grid(row=crow, column=1, columnspan=3, sticky="nsew", padx=4, pady=4)
        f_chat.columnconfigure(1, weight=1)
        f_chat.rowconfigure(crow, weight=1)

        # --- 额度查询 ---
        qrow = 0
        ttk.Button(f_credit, text="查询剩余额度", command=self.on_credits).grid(
            row=qrow, column=0, sticky="w", padx=8, pady=8)
        self.lbl_credit = ttk.Label(f_credit, text="尚未查询（需先在“服务控制”页启动服务）")
        self.lbl_credit.grid(row=qrow, column=1, sticky="w")
        self.txt_credit = tk.Text(f_credit, height=8, wrap="word", state="disabled")
        self.txt_credit.grid(row=qrow + 1, column=0, columnspan=2, sticky="nsew", padx=8, pady=4)
        f_credit.columnconfigure(0, weight=1)
        f_credit.rowconfigure(qrow + 1, weight=1)

        # --- 日志 ---
        lrow = 0
        self.v_follow = tk.BooleanVar(value=True)
        ttk.Button(f_log, text="刷新", command=self.on_log).grid(row=lrow, column=0, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(f_log, text="自动跟随", variable=self.v_follow).grid(row=lrow, column=1, sticky="w")
        self.txt_log = tk.Text(f_log, wrap="none", state="disabled")
        self.txt_log.grid(row=lrow + 1, column=0, columnspan=2, sticky="nsew", padx=6, pady=4)
        f_log.columnconfigure(0, weight=1)
        f_log.rowconfigure(lrow + 1, weight=1)

    def _info(self, text: str):
        self.txt_info.configure(state="normal")
        self.txt_info.delete("1.0", "end")
        self.txt_info.insert("1.0", text)
        self.txt_info.configure(state="disabled")

    # ---- 事件 ----
    def on_detect(self):
        path = detect_workbuddy()
        if path:
            self.e_wb.delete(0, "end")
            self.e_wb.insert(0, path)
            self.on_save()
        else:
            messagebox.showwarning("未检测到", "未自动检测到 WorkBuddy.exe，请点“浏览”手动选择。")

    def on_browse(self):
        path = filedialog.askopenfilename(title="选择 WorkBuddy.exe",
                                          filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.e_wb.delete(0, "end")
            self.e_wb.insert(0, path)
            self.on_save()

    def _collect(self) -> dict:
        return {
            "workbuddy_path": self.e_wb.get().strip(),
            "host": self.e_host.get().strip() or "127.0.0.1",
            "port": int(self.e_port.get() or 8787),
            "api_key": self.e_key.get().strip(),
            "desensitize": self.v_des.get(),
            "no_compact": self.v_noc.get(),
            "log_enabled": self.v_log.get(),
        }

    def on_save(self):
        try:
            self.cfg = self._collect()
        except ValueError:
            messagebox.showerror("错误", "端口必须是数字")
            return
        save_config(self.cfg)
        self._set_status(f"配置已保存 → {CONFIG_FILE.name}（服务未启动）" if not self.ctrl.running
                         else f"配置已保存 → {CONFIG_FILE.name}（重启服务后生效）")

    def on_start(self):
        try:
            self.cfg = self._collect()
            save_config(self.cfg)
            msg = self.ctrl.start(
                self.cfg["host"], self.cfg["port"], self.cfg["api_key"],
                self.cfg["desensitize"], self.cfg["no_compact"],
                self.cfg["log_enabled"], self.cfg["workbuddy_path"],
            )
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self._set_status(msg)
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def on_stop(self):
        self._set_status(self.ctrl.stop())
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def on_models(self):
        def work():
            try:
                code, body = http_get(f"{self.ctrl.base_url}/v1/models", self.cfg.get("api_key", ""))
                models = [m["id"] for m in json.loads(body)["data"]] if code == 200 else []
                self.root.after(0, self._show_models, code, models)
            except Exception as e:
                self.root.after(0, self._show_models, 0, [f"请求失败：{e}"])
        threading.Thread(target=work, daemon=True).start()

    def _show_models(self, code: int, models: list):
        self.lb_models.delete(0, "end")
        for m in models:
            self.lb_models.insert("end", m)
        self.lbl_models.configure(text=f"HTTP {code}，共 {len(models)} 个模型")
        if models and not str(models[0]).startswith("请求失败"):
            self.cb_model.configure(values=models)

    def on_send(self):
        model = self.cb_model.get().strip()
        content = self.txt_prompt.get("1.0", "end").strip()
        if not model or not content:
            messagebox.showwarning("提示", "请填写模型和消息内容")
            return
        self.btn_send.configure(state="disabled")
        self._reply("请求中，请稍候...")

        def work():
            try:
                code, body = http_chat(self.ctrl.base_url, model, content,
                                       self.cfg.get("api_key", ""),
                                       float(self.sp_temp.get() or 0))
                self.root.after(0, self._reply, f"HTTP {code}\n{body}")
            except Exception as e:
                self.root.after(0, self._reply, f"请求失败：{e}")
            finally:
                self.root.after(0, lambda: self.btn_send.configure(state="normal"))
        threading.Thread(target=work, daemon=True).start()

    def on_credits(self):
        cred = CONFIG.get("cred")
        if cred is None:
            messagebox.showwarning("提示", "请先在“服务控制”页启动服务（启动时加载登录态）")
            return
        self.lbl_credit.configure(text="查询中...")
        self._credit_text("查询中，请稍候...")

        def work():
            try:
                headers = cred.get_headers()
                domain = str(headers.get("X-Domain", "")).lower()
                if "workbuddy.ai" in domain:
                    self.root.after(0, self._credit_text, "当前为国际版（workbuddy.ai）账号，仅支持国内账号积分查询")
                    return
                info = query_credits(headers)
                text = (
                    f"剩余额度：{info['remaining']:g}\n"
                    f"总额度：  {info['capacity']:g}\n"
                    f"已使用：  {info['used']:g}\n"
                    f"积分包数：{info['packages']}\n\n"
                    f"（查询时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}，"
                    f"数据来自 www.codebuddy.cn 积分服务，仅统计当前周期内未过期的积分包）"
                )
                self.root.after(0, self._credit_text, text)
                self.root.after(0, lambda: self.lbl_credit.configure(
                    text=f"剩余 {info['remaining']:g} / {info['capacity']:g}"))
            except Exception as e:
                self.root.after(0, self._credit_text, f"查询失败：{e}")
                self.root.after(0, lambda: self.lbl_credit.configure(text="查询失败"))
        threading.Thread(target=work, daemon=True).start()

    def _credit_text(self, text: str):
        self.txt_credit.configure(state="normal")
        self.txt_credit.delete("1.0", "end")
        self.txt_credit.insert("1.0", text)
        self.txt_credit.configure(state="disabled")

    def _reply(self, text: str):
        self.txt_reply.configure(state="normal")
        self.txt_reply.delete("1.0", "end")
        self.txt_reply.insert("1.0", text)
        self.txt_reply.configure(state="disabled")

    def on_log(self):
        log = _ROOT / "converter.log"
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            self.txt_log.insert("1.0", "\n".join(lines[-400:]))
        else:
            self.txt_log.insert("1.0", "暂无日志（启动服务时勾选“写日志”后生成）")
        self.txt_log.configure(state="disabled")
        if self.v_follow.get():
            self.txt_log.see("end")

    # ---- 状态轮询 ----
    def _set_status(self, text: str, running: bool | None = None):
        if running is None:
            running = self.ctrl.running
        color = "green" if running else "gray"
        self.lbl_status.configure(text=f"状态：{text}", foreground=color)

    def _poll_status(self):
        if self.ctrl.running:
            def work():
                try:
                    code, body = http_get(f"{self.ctrl.base_url}/health", self.cfg.get("api_key", ""))
                    if code == 200:
                        cred = json.loads(body).get("credential", {})
                        expired = cred.get("token_expired")
                        hint = ""
                        if self.ctrl.host in ("0.0.0.0", "::"):
                            hint = " ｜ 局域网客户端请用本机IP访问（如 http://192.168.x.x:%d）" % self.ctrl.port
                        self.root.after(0, self._set_status,
                                        f"运行中 {self.ctrl.base_url} ｜ 登录态有效: {not expired}{hint}", True)
                    else:
                        self.root.after(0, self._set_status, f"运行中（health={code}）", True)
                except Exception:
                    self.root.after(0, self._set_status, "运行中（health 无响应）", True)
            threading.Thread(target=work, daemon=True).start()
        self.root.after(5000, self._poll_status)


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    root = tk.Tk()
    App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: _on_close(root))
    root.mainloop()


def _on_close(root: tk.Tk):
    root.destroy()
    os._exit(0)  # 确保 uvicorn 守护线程随进程退出


if __name__ == "__main__":
    main()
