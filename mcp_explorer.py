#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# MCP Explorer - a GUI for browsing and calling inZOI MCP tools
# Copyright (C) 2026 GnatGoSplat
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
MCP Explorer - a small Swagger-style GUI for an MCP server over HTTP.

Left   : searchable list of tools (from tools/list)
Center : input form generated from the selected tool's schema + Submit
Right  : readable output (toggle "Raw JSON" for the full response)

Requires: Python 3 with tkinter (included on Windows) and `pip install requests`
Usage   : python mcp_explorer.py            (game must be running)
"""
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import requests
import re

DEFAULT_URL = "http://localhost:11212/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

READ_HINTS = {"get", "list", "scan", "find", "search", "help", "reflect",
              "components", "lookup", "is", "can", "should", "info", "paths",
              "describe", "query"}
WRITE_HINTS = {"set", "add", "remove", "create", "destroy", "delete", "spawn",
               "kill", "fire", "hire", "settle", "apply", "reset", "write",
               "save", "load", "start", "freeze", "teleport", "execute",
               "apocalypse", "patch", "upload", "import", "place", "commit"}

_BAD_ESCAPE = re.compile(r'\\(["\\/bfnrt]|u[0-9a-fA-F]{4})|\\')

# For illegal json \ character returned from world_list_characters in inZOI 0.10.4 (bug?)
def loads_lenient(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # double any backslash that isn't part of a valid escape
        fixed = _BAD_ESCAPE.sub(
            lambda m: m.group(0) if m.group(1) else "\\\\", text)
        return json.loads(fixed)

def looks_read_only(name):
    tokens = set(name.lower().split("_"))
    return bool(tokens & READ_HINTS) and not (tokens & WRITE_HINTS)

# --------------------------------------------------------------------------
# MCP client (plain JSON-RPC over HTTP, no Node needed)
# --------------------------------------------------------------------------
class McpClient:
    def __init__(self, url):
        self.url = url
        self.session = None
        self._id = 0
        self.http = requests.Session()
        self.lock = threading.Lock()

    def _post(self, body, timeout=60):
        headers = dict(HEADERS)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        r = self.http.post(self.url, json=body, headers=headers, timeout=timeout)
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session = sid
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        return r

    @staticmethod
    def _parse(r, want_id):
        text = r.text.strip()
        if not text:
            return None
        ctype = r.headers.get("Content-Type", "")
        msgs = []
        if "text/event-stream" in ctype or text.startswith(("data:", "event:", "id:")):
            for line in text.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload:
                        try:
                            msgs.append(loads_lenient(payload))
                        except json.JSONDecodeError:
                            pass
        else:
            data = loads_lenient(text)
            msgs = data if isinstance(data, list) else [data]
        for m in msgs:
            if isinstance(m, dict) and m.get("id") == want_id:
                return m
        return msgs[-1] if msgs else None

    def request(self, method, params=None):
        with self.lock:
            self._id += 1
            rid = self._id
            body = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                body["params"] = params
            r = self._post(body)
            return self._parse(r, rid)

    def notify(self, method, params=None):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        self._post(body, timeout=10)

    def connect(self):
        self.session = None
        resp = self.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp-explorer", "version": "1.0"},
        })
        if resp is None or "error" in resp:
            raise RuntimeError(f"initialize failed: {resp}")
        try:
            self.notify("notifications/initialized")
        except Exception:
            pass
        tools, cursor = [], None
        while True:
            resp = self.request("tools/list", {"cursor": cursor} if cursor else {})
            if resp is None or "error" in resp:
                raise RuntimeError(f"tools/list failed: {resp}")
            result = resp.get("result", {})
            tools += result.get("tools", [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return sorted(tools, key=lambda t: t.get("name", ""))


# --------------------------------------------------------------------------
# Output formatting
# --------------------------------------------------------------------------
def _is_scalar(v):
    return not isinstance(v, (dict, list))


def _scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def fmt_value(v, indent=0):
    pad = "  " * indent
    lines = []
    if isinstance(v, dict):
        if not v:
            return [pad + "(empty)"]
        for k, val in v.items():
            if _is_scalar(val):
                lines.append(f"{pad}{k}: {_scalar(val)}")
            elif isinstance(val, list) and all(_is_scalar(x) for x in val):
                lines.append(f"{pad}{k}: " + (", ".join(_scalar(x) for x in val) if val else "(none)"))
            else:
                lines.append(f"{pad}{k}:")
                lines += fmt_value(val, indent + 1)
    elif isinstance(v, list):
        if not v:
            return [pad + "(none)"]
        for item in v:
            if isinstance(item, dict) and all(_is_scalar(x) for x in item.values()):
                lines.append(pad + "- " + ", ".join(f"{k}={_scalar(x)}" for k, x in item.items()))
            elif _is_scalar(item):
                lines.append(f"{pad}- {_scalar(item)}")
            else:
                lines.append(pad + "-")
                lines += fmt_value(item, indent + 1)
    else:
        lines.append(pad + _scalar(v))
    return lines


def _try_json(text):
    try:
        return loads_lenient(text)
    except (ValueError, TypeError):
        return None


def render_response(resp, raw=False):
    if resp is None:
        return "(empty response)"
    if raw:
        copy = json.loads(json.dumps(resp))
        for c in copy.get("result", {}).get("content", []) if isinstance(copy.get("result"), dict) else []:
            if c.get("type") == "text":
                parsed = _try_json(c.get("text", ""))
                if isinstance(parsed, (dict, list)):
                    c["text"] = parsed
        return json.dumps(copy, indent=2, ensure_ascii=False)
    if "error" in resp:
        err = resp["error"]
        return f"ERROR {err.get('code')}: {err.get('message')}\n\n" + json.dumps(err, indent=2)
    result = resp.get("result", {})
    out = []
    if isinstance(result, dict) and result.get("isError"):
        out.append("*** The tool reported an error ***\n")
    content = result.get("content", []) if isinstance(result, dict) else []
    for c in content:
        if c.get("type") == "text":
            text = c.get("text", "")
            parsed = _try_json(text)
            if isinstance(parsed, (dict, list)):
                out += fmt_value(parsed)
            else:
                out.append(text)
        else:
            out.append(f"[{c.get('type')} content omitted - see Raw JSON]")
    if not out:
        out = fmt_value(result)
    return "\n".join(out)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MCP Explorer v1.0")
        self.geometry("1400x760")
        self.minsize(1000, 500)

        self.client = None
        self.tools = []
        self.visible = []
        self.current = None
        self.fields = {}
        self.last_resp = None
        self.q = queue.Queue()

        self._build()
        self.after(100, self._poll)
        self.after(200, self.connect)

    # ---- layout ----
    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Server URL:").pack(side="left")
        self.url_var = tk.StringVar(value=DEFAULT_URL)
        ttk.Entry(top, textvariable=self.url_var, width=45).pack(side="left", padx=6)
        self.connect_btn = ttk.Button(top, text="Connect / Refresh", command=self.connect)
        self.connect_btn.pack(side="left")
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=12)

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # left: tool list
        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        frow = ttk.Frame(left)
        frow.pack(fill="x")
        ttk.Label(frow, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.apply_filter())
        ttk.Entry(frow, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=4)
        self.count_var = tk.StringVar()
        ttk.Label(left, textvariable=self.count_var, foreground="#666").pack(anchor="w")
        lbox_frame = ttk.Frame(left)
        lbox_frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(lbox_frame, exportselection=False, activestyle="none", width=40)
        sb = ttk.Scrollbar(lbox_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", self.on_select)

        # center: form
        center = ttk.Frame(panes, padding=(8, 0))
        panes.add(center, weight=1)
        self.tool_title = ttk.Label(center, text="Select a tool", font=("Segoe UI", 12, "bold"))
        self.tool_title.pack(anchor="w")
        self.tool_desc = ttk.Label(center, text="", wraplength=420, justify="left")
        self.tool_desc.pack(anchor="w", pady=(0, 6))

        bottom = ttk.Frame(center)
        bottom.pack(side="bottom", fill="x", pady=(6, 0))
        self.submit_btn = ttk.Button(bottom, text="Submit (Ctrl+Enter)", command=self.run, state="disabled")
        self.submit_btn.pack(side="left")
        self.confirm_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="Confirm before running tools that may change the game",
                        variable=self.confirm_var).pack(side="left", padx=10)

        canvas_frame = ttk.Frame(center)
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(canvas_frame, highlightthickness=0, width=40)
        csb = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=csb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        csb.pack(side="right", fill="y")
        self.form = ttk.Frame(self.canvas)
        self.form_id = self.canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.form_id, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.bind("<Control-Return>", lambda e: self.run())

        # right: output
        right = ttk.Frame(panes)
        panes.add(right, weight=1)
        orow = ttk.Frame(right)
        orow.pack(fill="x")
        ttk.Label(orow, text="Output", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.raw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(orow, text="Raw JSON", variable=self.raw_var,
                        command=self.refresh_output).pack(side="right")
        tframe = ttk.Frame(right)
        tframe.pack(fill="both", expand=True)
        self.output = tk.Text(tframe, wrap="word", font=("Consolas", 10), undo=False)
        osb = ttk.Scrollbar(tframe, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=osb.set)
        self.output.pack(side="left", fill="both", expand=True)
        osb.pack(side="right", fill="y")

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # ---- connection ----
    def connect(self):
        self.connect_btn.config(state="disabled")
        self.status_var.set("Connecting...")
        self.client = McpClient(self.url_var.get().strip())
        client = self.client

        def work():
            try:
                tools = client.connect()
                self.q.put(("connected", tools))
            except Exception as e:
                self.q.put(("connect_error", e))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "connected":
                    self.tools = payload
                    self.connect_btn.config(state="normal")
                    self.status_var.set(f"Connected - {len(self.tools)} tools")
                    self.apply_filter()
                elif kind == "connect_error":
                    self.connect_btn.config(state="normal")
                    self.status_var.set("Connection failed")
                    self.set_output(f"Could not connect to {self.url_var.get()}\n\n{payload}\n\n"
                                    "Is the game running? Is the URL/port right?")
                elif kind == "done":
                    self.submit_btn.config(state="normal")
                    self.last_resp = payload
                    self.refresh_output()
                elif kind == "run_error":
                    self.submit_btn.config(state="normal")
                    self.last_resp = None
                    self.set_output(f"Request failed:\n{payload}\n\n"
                                    "If the game was restarted, click Connect / Refresh.")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---- tool list ----
    def apply_filter(self):
        term = self.filter_var.get().strip().lower()
        self.visible = [t for t in self.tools
                        if term in t.get("name", "").lower()
                        or term in (t.get("description") or "").lower()]
        self.listbox.delete(0, "end")
        for t in self.visible:
            self.listbox.insert("end", t["name"])
        self.count_var.set(f"{len(self.visible)} of {len(self.tools)} tools")

    def on_select(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.current = self.visible[sel[0]]
        self.build_form(self.current)

    # ---- form ----
    @staticmethod
    def _type_of(prop):
        t = prop.get("type")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), None)
        if t is None and "enum" in prop:
            t = "string"
        if t is None and prop.get("anyOf"):
            for sub in prop["anyOf"]:
                st = sub.get("type")
                if st and st != "null":
                    t = st
                    break
        return t or "any"

    def build_form(self, tool):
        for w in self.form.winfo_children():
            w.destroy()
        self.fields = {}
        self.tool_title.config(text=tool["name"])
        self.tool_desc.config(text=tool.get("description") or "")
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        if not props:
            ttk.Label(self.form, text="This tool takes no inputs.").pack(anchor="w", pady=8)
        for name, p in props.items():
            typ = self._type_of(p)
            is_req = name in required
            frame = ttk.Frame(self.form)
            frame.pack(fill="x", pady=(0, 8), padx=2)
            title = name + (" *" if is_req else "") + f"   ({typ})"
            ttk.Label(frame, text=title, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            if p.get("description"):
                ttk.Label(frame, text=p["description"], wraplength=400,
                          foreground="#555", justify="left").pack(anchor="w")
            default = p.get("default")
            dtext = "" if default is None else (
                json.dumps(default) if isinstance(default, (dict, list)) else str(default))
            if "enum" in p:
                var = tk.StringVar(value=dtext)
                w = ttk.Combobox(frame, textvariable=var, state="readonly",
                                 values=[""] + [str(e) for e in p["enum"]])
                getter = var.get
            elif typ == "boolean":
                var = tk.StringVar(value=dtext.lower() if dtext else "")
                w = ttk.Combobox(frame, textvariable=var, state="readonly",
                                 values=["", "true", "false"])
                getter = var.get
            elif typ in ("array", "object"):
                w = tk.Text(frame, height=3, width=48, font=("Consolas", 9))
                w.insert("1.0", dtext)
                getter = (lambda w=w: w.get("1.0", "end"))
            else:
                var = tk.StringVar(value=dtext)
                w = ttk.Entry(frame, textvariable=var)
                getter = var.get
            w.pack(fill="x")
            self.fields[name] = (typ, getter, is_req)
        self.canvas.yview_moveto(0)
        self.submit_btn.config(state="normal" if self.client else "disabled")

    @staticmethod
    def _convert(raw, typ, name):
        try:
            if typ == "integer":
                return int(raw)
            if typ == "number":
                return float(raw)
            if typ == "boolean":
                return raw.lower() in ("true", "1", "yes")
            if typ in ("array", "object"):
                return json.loads(raw)
            if typ == "string":
                return raw
            if raw[:1] in "[{":
                return json.loads(raw)
            return raw
        except ValueError as e:
            raise ValueError(f"'{name}' should be {typ}: {e}")

    def collect_args(self):
        args = {}
        for name, (typ, getter, req) in self.fields.items():
            raw = getter().strip()
            if raw == "":
                if req:
                    raise ValueError(f"'{name}' is required")
                continue
            args[name] = self._convert(raw, typ, name)
        return args

    # ---- run ----
    def run(self):
        if not self.current or not self.client:
            return
        if str(self.submit_btn["state"]) == "disabled":
            return
        try:
            args = self.collect_args()
        except Exception as e:
            messagebox.showerror("Input error", str(e))
            return
        name = self.current["name"]
        if self.confirm_var.get() and not looks_read_only(name):
            ok = messagebox.askyesno(
                "Confirm",
                f"'{name}' may change your game or save.\n\n"
                f"Arguments:\n{json.dumps(args, indent=2)}\n\nRun it?")
            if not ok:
                return
        self.set_output(f"Running {name} ...")
        self.submit_btn.config(state="disabled")
        client = self.client

        def work():
            try:
                resp = client.request("tools/call", {"name": name, "arguments": args})
                self.q.put(("done", resp))
            except Exception as e:
                self.q.put(("run_error", e))
        threading.Thread(target=work, daemon=True).start()

    # ---- output ----
    def set_output(self, text):
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)

    def refresh_output(self):
        if self.last_resp is not None:
            self.set_output(render_response(self.last_resp, self.raw_var.get()))


if __name__ == "__main__":
    App().mainloop()
