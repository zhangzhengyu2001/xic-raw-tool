#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
图形界面版：选择 .raw 文件 → 填目标 m/z →（可选）限定时间范围 → 一键出曲线。

支持自定义图片标题、X/Y 轴名称与单位、每条曲线的名称，并可单独导出 PNG。
核心逻辑复用 xic_from_raw.py，两边结果完全一致。
"""

import json
import os
import queue
import sys
import threading
import traceback
import types

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import xic_from_raw as core  # noqa: E402

try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

CONFIG_PATH = os.path.join(HERE, "xic_gui_config.json")

DEFAULTS = {
    # 提取参数
    "extract_mode": "xic",     # xic = 按 m/z 提取；tic = 总离子流
    "mz": "58.83",
    "tol": "0.01",
    "tol_type": "da",
    "whole_file": True,
    "rt_start": "",
    "rt_end": "",
    "out": os.path.join(HERE, "xic_out"),
    # 图片外观
    "title_auto": True,
    "title": "",
    "x_name": "保留时间",
    "x_unit": "min",
    "y_name": "强度",
    "y_unit": "",
    "dpi": "130",
    # 时间轴单位：min 或 s
    "x_time_unit": "min",
    # 曲线名称（文件名 -> 显示名）
    "curve_names": {},
    # 每个文件单独的时间范围（文件名 -> [起, 止]，None 表示该端不限）
    "file_ranges": {},
    # 绘图时是否把各曲线对齐到各自起点
    "align_start": False,
    # 峰值标记：none / max / min / both / mean
    "mark": "none",
    # 每条曲线单独的标记方式（文件名 -> 代码），没有条目则跟随 mark
    "curve_marks": {},
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 核心执行
def run_extraction(files, args, out_dir, log, style=None, ranges=None, mz_list=None):
    """跑完全部文件。ranges 为 {文件名: (起,止)}，覆盖 args.rt_range。
    mz_list 为要提取的 m/z 列表（TIC 模式忽略）。"""
    os.makedirs(out_dir, exist_ok=True)
    tic = bool(getattr(args, "tic", False))
    if tic:
        targets = [None]
    else:
        targets = list(mz_list) if mz_list else [getattr(args, "mz", None)]
        targets = [m for m in targets if m is not None]

    if tic:
        tag, prefix = "tic", "TIC"
    elif len(targets) > 1:
        tag, prefix = "multi", "XIC"
    else:
        tag = f"{targets[0]:g}".replace(".", "p") if targets else "xic"
        prefix = "XIC"

    traces, failed = [], []

    for p in files:
        limit = core._range_for(p, ranges) or getattr(args, "rt_range", None)
        for mz in targets:
            head = os.path.basename(p) + (f"  m/z {mz:g}" if mz is not None else "")
            log(f"=== {head} ===")
            if mz is not None:
                args.mz = mz
            try:
                rows, info = core.process_one(p, args, rt_range=limit)
            except Exception as e:
                failed.append((p, f"{type(e).__name__}: {e}"))
                log(f"  失败：{type(e).__name__}: {e}")
                continue

            if info.get("empty"):
                failed.append((p, f"没有符合 ms level 的谱（分布 {info['hist']}）"))
                log(f"  跳过：该文件没有符合 ms level 的谱，分布 {info['hist']}")
                continue

            stem = os.path.splitext(os.path.basename(p))[0]
            if tic:
                fname = f"{stem}_tic.csv"
            else:
                fname = f"{stem}_xic_{mz:g}".replace(".", "p") + ".csv"
            csv_path = os.path.join(out_dir, fname)
            core.write_csv(rows, csv_path)
            traces.append((p, mz, rows))

            used = info.get("rt_limit")
            rng_txt = f"{used[0]:g}~{used[1]:g} min" if used else "整个文件"
            log(f"  谱图数 {info['n_used']}，取用 {rng_txt}")
            if info.get("apex_rt") is not None:
                log(f"  最强处 RT={info['apex_rt']:.3f} min，强度={info['apex_int']:.4g}")
            elif not tic:
                log("  警告：该 m/z 窗口内没有信号")
                for m, i in (info.get("diagnostic") or [])[:5]:
                    log(f"      附近有峰 m/z={m:.4f} 强度={i:.4g}")
            log(f"  → {os.path.basename(csv_path)}")

    png = None
    if traces:
        base = core.safe_filename((style or {}).get("title"))
        png = os.path.join(out_dir, f"{base}.png" if base
                           else f"{prefix}_{tag}_overlay.png")
        try:
            only_mz = targets[0] if (not tic and len(targets) == 1) else 0.0
            core.plot_overlay(traces, only_mz, args.tol, args.tol_type, png,
                              style=style)
        except Exception as e:
            log(f"  画图失败：{e}")
            png = None

    return png, len(traces), failed, traces


# ---------------------------------------------------------------- 输出目录
def resolve_out_dir(field_value, last_out=None, default=None, create=True):
    """
    解析该用哪个输出目录。

    优先级：界面输入框 > 上次运行实际用的目录 > 默认目录。
    之所以要"输入框优先"，是因为用户可能刚改了输出目录、还没重新提取就要导出，
    此时 last_out 还是旧的，不能拿它覆盖用户的选择。

    返回规范化后的绝对路径；目录无法创建时返回 None。
    """
    cand = (field_value or "").strip()
    if not cand:
        cand = last_out or default or os.getcwd()
    cand = os.path.normpath(os.path.expanduser(cand))

    if os.path.isdir(cand):
        return cand
    if not create:
        return cand
    try:
        os.makedirs(cand, exist_ok=True)
    except Exception:
        return None
    return cand


# ---------------------------------------------------------------- 界面
def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    cfg = load_config()

    # 标记方式：下拉框显示文本 <-> 内部代码
    MARK_LABELS = {
        "不标记": "none",
        "标记最大值（峰值）": "max",
        "标记最小值": "min",
        "最大值和最小值都标": "both",
        "标记均值线": "mean",
    }
    MARK_BY_CODE = {v: k for k, v in MARK_LABELS.items()}
    # 文件列表里那一列显示的简短文字
    MARK_TEXT = {"none": "", "max": "峰值", "min": "谷值",
                 "both": "峰+谷", "mean": "均值"}

    root = tk.Tk()
    root.title("m/z 强度-时间曲线提取")
    root.minsize(780, 640)

    try:
        core.load_pyopenms()
        pyopenms_ok, pyopenms_err = True, ""
    except Exception as e:
        pyopenms_ok, pyopenms_err = False, str(e)

    files = []                                      # 绝对路径列表
    name_map = dict(cfg.get("curve_names") or {})   # 文件名 -> 自定义名称
    # 文件名 -> (起, 止) 分钟；None 表示该端不限。没有条目 = 用全局设置
    range_map = {}
    for k, v in (cfg.get("file_ranges") or {}).items():
        try:
            range_map[k] = (None if v[0] is None else float(v[0]),
                            None if v[1] is None else float(v[1]))
        except Exception:
            continue
    # 文件名 -> 标记方式（none/max/min/both/mean），没有条目 = 跟随全局
    mark_map = dict(cfg.get("curve_marks") or {})
    log_q = queue.Queue()
    state = {"png": None, "photo": None, "traces": None, "args": None, "out": None}

    # ---------------- 可滚动主体
    # 窗口高度不够时（小屏幕、系统缩放较大），整块内容可以在右侧滚动条里上下拖动
    outer = ttk.Frame(root)
    outer.pack(fill="both", expand=True)

    canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
    vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)
    vbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    body = ttk.Frame(canvas)
    body_window = canvas.create_window((0, 0), window=body, anchor="nw")

    def sync_scrollregion(_=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def fit_body_width(event):
        canvas.itemconfigure(body_window, width=event.width)

    body.bind("<Configure>", sync_scrollregion)
    canvas.bind("<Configure>", fit_body_width)

    def on_mousewheel(event):
        # 指针在能自己滚动的控件上时交给控件处理，避免抢走它的滚轮
        under = root.winfo_containing(event.x_root, event.y_root)
        for w in (under, getattr(under, "master", None)):
            if isinstance(w, (tk.Text, tk.Listbox, ttk.Treeview)):
                return
        canvas.yview_scroll(int(-event.delta / 120), "units")

    root.bind_all("<MouseWheel>", on_mousewheel)

    # ---------------- 1. 文件
    frm_files = ttk.LabelFrame(
        body, text="1. 选择 .raw 文件（双击各列可改：曲线名称 / 起始·结束 / 标记方式）")
    frm_files.pack(fill="x", padx=10, pady=(10, 6))

    tree = ttk.Treeview(frm_files, columns=("file", "curve", "t0", "t1", "mark"),
                        show="headings", height=4, selectmode="extended")
    tree.heading("file", text="文件")
    tree.heading("curve", text="曲线名称")
    tree.heading("t0", text="起始(min)")
    tree.heading("t1", text="结束(min)")
    tree.heading("mark", text="标记")
    tree.column("file", width=250, anchor="w")
    tree.column("curve", width=140, anchor="w")
    tree.column("t0", width=80, anchor="center")
    tree.column("t1", width=80, anchor="center")
    tree.column("mark", width=90, anchor="center")
    tree.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=8)

    tsb = ttk.Scrollbar(frm_files, orient="vertical", command=tree.yview)
    tsb.pack(side="left", fill="y", pady=8)
    tree.configure(yscrollcommand=tsb.set)

    frm_btns = ttk.Frame(frm_files)
    frm_btns.pack(side="right", padx=(0, 10), pady=8)

    def display_name(path):
        base = os.path.basename(path)
        return name_map.get(base) or os.path.splitext(base)[0]

    def display_range(path):
        rng = range_map.get(os.path.basename(path))
        if not rng:
            return ("", "")
        a, b = rng
        return ("" if a is None else f"{a:g}", "" if b is None else f"{b:g}")

    def display_mark(path):
        return MARK_TEXT.get(mark_map.get(os.path.basename(path), ""), "")

    def refresh_tree():
        tree.delete(*tree.get_children())
        for p in files:
            t0, t1 = display_range(p)
            tree.insert("", "end", iid=p,
                        values=(os.path.basename(p), display_name(p), t0, t1,
                                display_mark(p)))

    def add_paths(paths):
        added = 0
        for p in paths:
            ap = os.path.abspath(p)
            if ap not in files:
                files.append(ap)
                added += 1
        if added:
            refresh_tree()
            log(f"已添加 {added} 个文件，共 {len(files)} 个")

    def pick_files():
        ps = filedialog.askopenfilenames(
            title="选择 .raw 文件",
            filetypes=[("Thermo 原始文件", "*.raw"), ("所有文件", "*.*")])
        if ps:
            add_paths(ps)

    def pick_folder():
        d = filedialog.askdirectory(title="选择包含 .raw 的文件夹")
        if not d:
            return
        found = core.collect_inputs([d])
        if not found:
            messagebox.showwarning("没找到", f"该文件夹下没有 .raw 文件：\n{d}")
            return
        add_paths(found)
        if not out_var.get().strip():
            out_var.set(os.path.join(d, "xic_out"))

    def remove_sel():
        for p in tree.selection():
            if p in files:
                files.remove(p)
        refresh_tree()

    def clear_all():
        files.clear()
        refresh_tree()

    def rename_selected(event=None):
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一行。")
            return
        base = os.path.basename(sel[0])
        new = simpledialog.askstring(
            "曲线名称", f"给「{base}」设置显示名称（留空恢复默认）：",
            initialvalue=display_name(sel[0]), parent=root)
        if new is None:
            return
        new = new.strip()
        if new:
            name_map[base] = new
        else:
            name_map.pop(base, None)
        refresh_tree()

    # ---------------- 每个文件单独设时间范围
    def ask_range_dialog(initial_start="", initial_end=""):
        """弹出小窗口，填起止分钟。返回 (起, 止) 或 None 表示取消。"""
        dlg = tk.Toplevel(root)
        dlg.title("设置时间范围")
        dlg.transient(root)
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="起始(min)：").grid(row=0, column=0, sticky="e", pady=4)
        v0 = tk.StringVar(value=initial_start)
        ttk.Entry(frm, textvariable=v0, width=12).grid(row=0, column=1, sticky="w")
        ttk.Label(frm, text="结束(min)：").grid(row=1, column=0, sticky="e", pady=4)
        v1 = tk.StringVar(value=initial_end)
        ttk.Entry(frm, textvariable=v1, width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(frm, text="留空表示该端不限；两个都留空 = 取整个文件",
                  foreground="#666").grid(row=2, column=0, columnspan=2, pady=(6, 0))

        result = {"value": None}

        def ok(_=None):
            vals = []
            for var in (v0, v1):
                s = var.get().strip()
                if not s:
                    vals.append(None)
                    continue
                try:
                    vals.append(float(s))
                except ValueError:
                    messagebox.showerror("格式不对", f"「{s}」不是数字，请填分钟数，例如 1.5")
                    return
            if vals[0] is not None and vals[1] is not None and vals[0] >= vals[1]:
                messagebox.showerror("范围不对", "起始时间必须小于结束时间。")
                return
            result["value"] = tuple(vals)
            dlg.destroy()

        def cancel():
            dlg.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="确定", width=10, command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="清除", width=10,
                   command=lambda: (v0.set(""), v1.set(""))).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", width=10, command=cancel).pack(side="left", padx=4)

        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda e: cancel())
        dlg.grab_set()
        root.wait_window(dlg)
        return result["value"]

    def set_range_for_selected():
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一行。")
            return
        path = sel[0]
        base = os.path.basename(path)
        cur = range_map.get(base)
        got = ask_range_dialog(
            "" if not cur or cur[0] is None else f"{cur[0]:g}",
            "" if not cur or cur[1] is None else f"{cur[1]:g}")
        if got is None:
            return
        if got[0] is None and got[1] is None:
            range_map.pop(base, None)
            log(f"{base}：恢复为使用全局时间设置")
        else:
            range_map[base] = got
            a = "-∞" if got[0] is None else f"{got[0]:g}"
            b = "+∞" if got[1] is None else f"{got[1]:g}"
            log(f"{base}：时间范围 {a} ~ {b} min")
            # 一旦用了单独的时间范围，通常就是想把不同时段的曲线叠在一起比
            try:
                align_var.set(True)
            except Exception:
                pass
        refresh_tree()

    # ---------------- 每条曲线单独设标记方式
    def set_mark_for_selected():
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一行。")
            return
        base = os.path.basename(sel[0])
        cur = mark_map.get(base, "")

        dlg = tk.Toplevel(root)
        dlg.title("标记方式")
        dlg.transient(root)
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"「{base}」显示哪种标记？",
                  font=("", 9, "bold")).pack(anchor="w", pady=(0, 8))

        choice = tk.StringVar(value=cur if cur else "__default__")
        options = [("__default__", "跟随上面的「默认标记」设置")]
        for text, code in MARK_LABELS.items():
            options.append((code, text))
        for code, text in options:
            ttk.Radiobutton(frm, text=text, value=code,
                            variable=choice).pack(anchor="w", pady=1)

        btns = ttk.Frame(frm)
        btns.pack(pady=(12, 0))

        def ok(_=None):
            v = choice.get()
            if v == "__default__":
                mark_map.pop(base, None)
                log(f"{base}：标记方式跟随全局设置")
            else:
                mark_map[base] = v
                log(f"{base}：标记方式 = {MARK_TEXT.get(v) or v}")
            dlg.destroy()
            refresh_tree()

        ttk.Button(btns, text="确定", width=10, command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", width=10,
                   command=dlg.destroy).pack(side="left", padx=4)
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()
        root.wait_window(dlg)

    def on_tree_double_click(event):
        col = tree.identify_column(event.x)
        row = tree.identify_row(event.y)
        if not row:
            return
        tree.selection_set(row)
        tree.focus(row)
        if col == "#2":          # 曲线名称列
            rename_selected()
        elif col == "#5":        # 标记列
            set_mark_for_selected()
        else:                    # 文件 / 起始 / 结束 列 → 时间范围
            set_range_for_selected()

    tree.bind("<Double-1>", on_tree_double_click)

    ttk.Button(frm_btns, text="选择文件…", width=12, command=pick_files).pack(pady=2)
    ttk.Button(frm_btns, text="选择文件夹…", width=12, command=pick_folder).pack(pady=2)
    ttk.Button(frm_btns, text="设时间范围…", width=12,
               command=set_range_for_selected).pack(pady=2)
    ttk.Button(frm_btns, text="设标记方式…", width=12,
               command=set_mark_for_selected).pack(pady=2)
    ttk.Button(frm_btns, text="重命名曲线", width=12, command=rename_selected).pack(pady=2)
    ttk.Button(frm_btns, text="移除选中", width=12, command=remove_sel).pack(pady=2)
    ttk.Button(frm_btns, text="清空", width=12, command=clear_all).pack(pady=2)

    # ---------------- 2. 提取参数
    frm_par = ttk.LabelFrame(body, text="2. 提取参数")
    frm_par.pack(fill="x", padx=10, pady=6)

    row0 = ttk.Frame(frm_par)
    row0.pack(fill="x", padx=10, pady=(8, 2))
    ttk.Label(row0, text="提取类型：").pack(side="left")
    mode_var = tk.StringVar(value=cfg.get("extract_mode", "xic"))
    ttk.Radiobutton(row0, text="指定 m/z（XIC）", variable=mode_var,
                    value="xic").pack(side="left")
    ttk.Radiobutton(row0, text="总离子流（TIC，不需要填 m/z）", variable=mode_var,
                    value="tic").pack(side="left", padx=(12, 0))

    row1 = ttk.Frame(frm_par)
    row1.pack(fill="x", padx=10, pady=(2, 4))

    left = ttk.Frame(row1)
    left.pack(side="left", anchor="n")
    ttk.Label(left, text="目标 m/z（每行一个）：").pack(anchor="w")

    mz_box = ttk.Frame(left)
    mz_box.pack(anchor="w", pady=(2, 0))
    mz_text = tk.Text(mz_box, width=16, height=4, wrap="none",
                      font=("Consolas", 9))
    mz_text.pack(side="left", fill="y")
    mz_text.insert("1.0", str(cfg["mz"]))
    mz_sb = ttk.Scrollbar(mz_box, orient="vertical", command=mz_text.yview)
    mz_sb.pack(side="left", fill="y")
    mz_text.configure(yscrollcommand=mz_sb.set)

    right = ttk.Frame(row1)
    right.pack(side="left", anchor="n", padx=(14, 0))

    tol_row = ttk.Frame(right)
    tol_row.pack(anchor="w")
    tol_label = ttk.Label(tol_row, text="容差：")
    tol_label.pack(side="left")
    tol_var = tk.StringVar(value=cfg["tol"])
    tol_entry = ttk.Entry(tol_row, textvariable=tol_var, width=8)
    tol_entry.pack(side="left")
    unit_var = tk.StringVar(value=cfg["tol_type"])
    unit_box = ttk.Combobox(tol_row, textvariable=unit_var, values=["da", "ppm"],
                            width=5, state="readonly")
    unit_box.pack(side="left", padx=(4, 0))

    ttk.Label(right, text="一行一个 m/z，也可用逗号分隔",
              foreground="#666").pack(anchor="w", pady=(6, 0))

    def get_mz_text():
        return mz_text.get("1.0", "end-1c")

    def toggle_mode(*_):
        st = "disabled" if mode_var.get() == "tic" else "normal"
        for w in (mz_text, tol_entry):
            w.configure(state=st)
        unit_box.configure(state="disabled" if st == "disabled" else "readonly")

    mode_var.trace_add("write", toggle_mode)
    toggle_mode()

    row2 = ttk.Frame(frm_par)
    row2.pack(fill="x", padx=10, pady=4)
    whole_var = tk.BooleanVar(value=cfg["whole_file"])
    ttk.Checkbutton(row2, text="取整个文件的时间", variable=whole_var).pack(side="left")
    ttk.Label(row2, text="    从").pack(side="left")
    rt_start_var = tk.StringVar(value=cfg["rt_start"])
    rt_start_entry = ttk.Entry(row2, textvariable=rt_start_var, width=8)
    rt_start_entry.pack(side="left")
    ttk.Label(row2, text="到").pack(side="left")
    rt_end_var = tk.StringVar(value=cfg["rt_end"])
    rt_end_entry = ttk.Entry(row2, textvariable=rt_end_var, width=8)
    rt_end_entry.pack(side="left")
    ttk.Label(row2, text="分钟（留空表示不限）", foreground="#666").pack(side="left")

    def toggle_rt(*_):
        st = "disabled" if whole_var.get() else "normal"
        rt_start_entry.configure(state=st)
        rt_end_entry.configure(state=st)

    whole_var.trace_add("write", toggle_rt)
    toggle_rt()

    row3 = ttk.Frame(frm_par)
    row3.pack(fill="x", padx=10, pady=(4, 10))
    ttk.Label(row3, text="输出目录：").pack(side="left")
    out_var = tk.StringVar(value=cfg["out"])
    ttk.Entry(row3, textvariable=out_var).pack(side="left", fill="x", expand=True)

    def pick_out():
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            out_var.set(d)

    ttk.Button(row3, text="浏览…", width=8, command=pick_out).pack(side="left", padx=4)

    # ---------------- 3. 图片设置
    frm_img = ttk.LabelFrame(body, text="3. 图片设置")
    frm_img.pack(fill="x", padx=10, pady=6)

    g = ttk.Frame(frm_img)
    g.pack(fill="x", padx=10, pady=8)
    g.columnconfigure(1, weight=1)
    g.columnconfigure(4, weight=1)

    title_auto_var = tk.BooleanVar(value=cfg["title_auto"])
    title_var = tk.StringVar(value=cfg["title"])
    ttk.Checkbutton(g, text="自动生成标题", variable=title_auto_var).grid(
        row=0, column=0, sticky="w", pady=3)
    title_entry = ttk.Entry(g, textvariable=title_var)
    title_entry.grid(row=0, column=1, columnspan=4, sticky="ew", padx=(6, 0), pady=3)

    def toggle_title(*_):
        title_entry.configure(state="disabled" if title_auto_var.get() else "normal")

    title_auto_var.trace_add("write", toggle_title)

    ttk.Label(g, text="X 轴名称：").grid(row=1, column=0, sticky="e", pady=3)
    x_name_var = tk.StringVar(value=cfg["x_name"])
    ttk.Entry(g, textvariable=x_name_var).grid(
        row=1, column=1, sticky="ew", padx=(6, 12), pady=3)
    ttk.Label(g, text="单位：").grid(row=1, column=2, sticky="e", pady=3)
    x_unit_var = tk.StringVar(value=cfg["x_unit"])
    ttk.Entry(g, textvariable=x_unit_var, width=10).grid(
        row=1, column=3, sticky="w", padx=(6, 0), pady=3)

    ttk.Label(g, text="Y 轴名称：").grid(row=2, column=0, sticky="e", pady=3)
    y_name_var = tk.StringVar(value=cfg["y_name"])
    ttk.Entry(g, textvariable=y_name_var).grid(
        row=2, column=1, sticky="ew", padx=(6, 12), pady=3)
    ttk.Label(g, text="单位：").grid(row=2, column=2, sticky="e", pady=3)
    y_unit_var = tk.StringVar(value=cfg["y_unit"])
    ttk.Entry(g, textvariable=y_unit_var, width=10).grid(
        row=2, column=3, sticky="w", padx=(6, 0), pady=3)

    ttk.Label(g, text="分辨率 DPI：").grid(row=3, column=0, sticky="e", pady=3)
    dpi_var = tk.StringVar(value=cfg["dpi"])
    ttk.Combobox(g, textvariable=dpi_var, values=["100", "130", "200", "300", "600"],
                 width=8).grid(row=3, column=1, sticky="w", padx=(6, 0), pady=3)
    ttk.Label(g, text="数值越大图片越清晰、文件越大",
              foreground="#666").grid(row=3, column=2, columnspan=3, sticky="w", pady=3)

    align_var = tk.BooleanVar(value=cfg.get("align_start", False))
    ttk.Checkbutton(g, text="各曲线对齐到各自起点（都从 0 开始画）",
                    variable=align_var).grid(row=4, column=0, columnspan=2,
                                             sticky="w", pady=(3, 1))
    ttk.Label(g, text="配合各文件不同的时间范围使用",
              foreground="#666").grid(row=4, column=2, columnspan=3, sticky="w", pady=(3, 1))

    # 峰值/均值标记（可选，全局默认；单条曲线可在文件列表里单独覆盖）
    ttk.Label(g, text="默认标记：").grid(row=5, column=0, sticky="e", pady=3)
    mark_var = tk.StringVar(value=MARK_BY_CODE.get(cfg.get("mark", "none"), "不标记"))
    ttk.Combobox(g, textvariable=mark_var, values=list(MARK_LABELS.keys()),
                 width=18, state="readonly").grid(
        row=5, column=1, sticky="w", padx=(6, 0), pady=3)
    ttk.Label(g, text="单条曲线可在文件列表「标记」列里单独设置",
              foreground="#666").grid(row=5, column=2, columnspan=3,
                                      sticky="w", pady=3)

    # 时间轴单位：分钟 / 秒
    ttk.Label(g, text="时间轴单位：").grid(row=6, column=0, sticky="e", pady=3)
    time_unit_var = tk.StringVar(
        value="秒" if str(cfg.get("x_time_unit", "min")).lower() in ("s", "秒") else "分钟")

    def on_time_unit_change(*_):
        want = "s" if time_unit_var.get() == "秒" else "min"
        cur = x_unit_var.get().strip().lower()
        # 只在用户没手动改成别的单位时跟着切换
        if cur in ("min", "s", ""):
            x_unit_var.set(want)

    ttk.Combobox(g, textvariable=time_unit_var, values=["分钟", "秒"],
                 width=8, state="readonly").grid(
        row=6, column=1, sticky="w", padx=(6, 0), pady=3)
    time_unit_var.trace_add("write", on_time_unit_change)
    ttk.Label(g, text="选「秒」时刻度保留一位小数，并补 0.5 秒次刻度",
              foreground="#666").grid(row=6, column=2, columnspan=3,
                                      sticky="w", pady=3)

    toggle_title()

    # ---------------- 操作区
    frm_act = ttk.Frame(body)
    frm_act.pack(fill="x", padx=10, pady=4)

    run_btn = ttk.Button(frm_act, text="开始提取", width=14)
    run_btn.pack(side="left")
    replot_btn = ttk.Button(frm_act, text="重新生成图", width=12, state="disabled")
    replot_btn.pack(side="left", padx=6)
    export_btn = ttk.Button(frm_act, text="导出图片…", width=12, state="disabled")
    export_btn.pack(side="left")
    def open_out_dir():
        folder = current_out_dir()
        if folder:
            os.startfile(folder)

    ttk.Button(frm_act, text="打开输出目录", width=13,
               command=open_out_dir).pack(side="left", padx=6)

    status_var = tk.StringVar(value="就绪" if pyopenms_ok else "依赖缺失")
    ttk.Label(frm_act, textvariable=status_var, foreground="#0a5").pack(side="left", padx=10)

    # ---------------- 日志 / 预览
    nb = ttk.Notebook(body)
    # 不随窗口拉伸：窗口变矮时让整页滚动，而不是把日志/预览区压扁
    nb.pack(fill="x", expand=False, padx=10, pady=(6, 10))

    tab_log = ttk.Frame(nb)
    nb.add(tab_log, text="运行日志")
    log_text = tk.Text(tab_log, wrap="word", height=8, state="disabled")
    log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
    log_sb = ttk.Scrollbar(tab_log, orient="vertical", command=log_text.yview)
    log_sb.pack(side="right", fill="y", pady=8)
    log_text.config(yscrollcommand=log_sb.set)

    tab_img = ttk.Frame(nb)
    nb.add(tab_img, text="曲线预览")

    # 预览缩放工具条
    bar = ttk.Frame(tab_img)
    bar.pack(fill="x", padx=8, pady=(8, 2))

    zoom_text = tk.StringVar(value="适应窗口")

    ttk.Button(bar, text="－ 缩小", width=8,
               command=lambda: zoom_by(1 / 1.25)).pack(side="left")
    ttk.Button(bar, text="＋ 放大", width=8,
               command=lambda: zoom_by(1.25)).pack(side="left", padx=4)
    ttk.Button(bar, text="适应窗口", width=9,
               command=lambda: set_zoom(None)).pack(side="left", padx=(8, 4))
    ttk.Button(bar, text="原始大小", width=9,
               command=lambda: set_zoom(1.0)).pack(side="left")
    ttk.Label(bar, textvariable=zoom_text, foreground="#666").pack(side="left", padx=10)
    ttk.Label(bar, text="（也可在图上滚动鼠标滚轮缩放）",
              foreground="#999").pack(side="left")

    img_label = ttk.Label(tab_img, text="运行完成后在这里显示曲线", anchor="center")
    img_label.pack(fill="both", expand=True)

    def log(msg):
        log_q.put(str(msg))

    def drain_log():
        try:
            while True:
                msg = log_q.get_nowait()
                log_text.configure(state="normal")
                log_text.insert("end", msg + "\n")
                log_text.see("end")
                log_text.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(120, drain_log)

    root.after(120, drain_log)

    # ---------------- 设置汇总
    def collect_style():
        try:
            dpi = int(float(dpi_var.get()))
            dpi = max(50, min(1200, dpi))
        except Exception:
            dpi = 130
        return {
            "title": None if title_auto_var.get() else title_var.get(),
            "x_name": x_name_var.get(),
            "x_unit": x_unit_var.get(),
            "y_name": y_name_var.get(),
            "y_unit": y_unit_var.get(),
            "dpi": dpi,
            "names": {k: v for k, v in name_map.items() if v},
            "kind": "tic" if mode_var.get() == "tic" else "xic",
            "align_start": align_var.get(),
            "mark": MARK_LABELS.get(mark_var.get(), "none"),
            "marks": dict(mark_map),
            "x_time_unit": "s" if time_unit_var.get() == "秒" else "min",
        }

    def persist():
        save_config({
            "extract_mode": mode_var.get(),
            "mz": get_mz_text(), "tol": tol_var.get(), "tol_type": unit_var.get(),
            "whole_file": whole_var.get(),
            "rt_start": rt_start_var.get(), "rt_end": rt_end_var.get(),
            "out": out_var.get().strip(),
            "title_auto": title_auto_var.get(), "title": title_var.get(),
            "x_name": x_name_var.get(), "x_unit": x_unit_var.get(),
            "y_name": y_name_var.get(), "y_unit": y_unit_var.get(),
            "dpi": dpi_var.get(),
            "x_time_unit": "s" if time_unit_var.get() == "秒" else "min",
            "align_start": align_var.get(),
            "mark": MARK_LABELS.get(mark_var.get(), "none"),
            "curve_marks": dict(mark_map),
            "curve_names": {k: v for k, v in name_map.items() if v},
            "file_ranges": {k: list(v) for k, v in range_map.items()},
        })

    # ---------------- 预览缩放
    def fit_scale():
        im = state.get("pil")
        if im is None:
            return 1.0
        lw = max(img_label.winfo_width() - 10, 120)
        lh = max(img_label.winfo_height() - 10, 90)
        return min(lw / im.width, lh / im.height, 1.0)

    def render_preview():
        im = state.get("pil")
        if im is None:
            return
        if not _HAS_PIL:
            img_label.configure(image="", text="当前环境缺少 Pillow，无法显示预览")
            return
        scale = fit_scale() if state.get("zoom") is None else state["zoom"]
        scale = max(0.02, min(8.0, scale))
        state["scale"] = scale
        w = max(1, int(round(im.width * scale)))
        h = max(1, int(round(im.height * scale)))
        try:
            photo = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))
        except Exception as e:
            img_label.configure(image="", text=f"预览渲染失败：{e}")
            return
        state["photo"] = photo      # 必须保留引用，否则图片会被回收成空白
        img_label.configure(image=photo, text="")
        zoom_text.set("适应窗口" if state.get("zoom") is None
                      else f"{int(scale * 100)}%")

    def set_zoom(z):
        state["zoom"] = z
        render_preview()

    def zoom_by(factor):
        base = state.get("scale") or 1.0
        set_zoom(max(0.05, min(8.0, base * factor)))

    def show_preview(png):
        state["path"] = png
        if not _HAS_PIL:
            img_label.configure(
                image="", text=f"当前环境缺少 Pillow，无法显示预览。\n图片位置：{png}")
            return
        try:
            im = Image.open(png)
            im.load()
            state["pil"] = im
        except Exception as e:
            img_label.configure(image="", text=f"预览加载失败：{e}\n图片位置：{png}")
            return
        state["zoom"] = None
        render_preview()

    _resize_job = {"id": None}

    def on_preview_resize(event=None):
        # 窗口尺寸变了：只在"适应窗口"模式下重新计算，且做防抖
        if state.get("zoom") is not None:
            return
        if _resize_job["id"]:
            root.after_cancel(_resize_job["id"])
        _resize_job["id"] = root.after(150, render_preview)

    img_label.bind("<Configure>", on_preview_resize)

    def on_preview_wheel(event):
        zoom_by(1.1 if event.delta > 0 else 1 / 1.1)
        return "break"          # 阻止整页跟着滚动

    img_label.bind("<MouseWheel>", on_preview_wheel)

    # ---------------- 重新生成图 / 导出
    def current_out_dir(create=True):
        """当前该使用的输出目录；目录不可用时弹错误并返回 None。"""
        folder = resolve_out_dir(out_var.get(), state["out"], HERE, create=create)
        if folder is None:
            messagebox.showerror(
                "输出目录不可用",
                f"无法使用这个目录：\n{out_var.get().strip()}\n\n"
                "请检查路径是否有拼写错误、是否打到了只读位置。")
        return folder

    def title_based_name():
        """按当前标题推导 PNG 文件名；没有自定义标题时返回空串。"""
        text = "" if title_auto_var.get() else title_var.get()
        return core.safe_filename(text)

    def replot(save_path=None, silent=False):
        traces, args = state["traces"], state["args"]
        if not traces or not args:
            messagebox.showinfo("提示", "还没有提取结果，请先点「开始提取」。")
            return
        target = save_path
        if not target:
            # 重新生成时也跟随标题改名，改完标题点一下就换名字
            base = title_based_name()
            folder = current_out_dir()
            if folder is None:
                return
            if base:
                target = os.path.join(folder, base + ".png")
            else:
                target = state["png"]
        if not target:
            return
        try:
            core.plot_overlay(traces, args.mz, args.tol, args.tol_type,
                              target, style=collect_style())
        except Exception as e:
            messagebox.showerror("画图失败", f"{type(e).__name__}: {e}")
            return
        state["png"] = target
        show_preview(target)
        nb.select(tab_img)
        if not silent:
            log(f"图片已保存 → {target}")

    def do_replot():
        persist()
        replot()

    def do_export():
        if not state["traces"]:
            messagebox.showinfo("提示", "还没有提取结果，请先点「开始提取」。")
            return
        persist()
        # 目标目录以输入框为准（用户可能刚改过还没重新提取）
        folder = current_out_dir()
        if folder is None:
            return
        # 默认文件名优先用图片标题；标题为空或自动生成时退回按 m/z 命名
        title_text = "" if title_auto_var.get() else title_var.get()
        default_name = core.safe_filename(title_text)
        if not default_name:
            if getattr(state["args"], "tic", False):
                default_name = "TIC"
            else:
                tag = f"{state['args'].mz:g}".replace(".", "p")
                default_name = f"XIC_{tag}"
        default_name += ".png"
        path = filedialog.asksaveasfilename(
            title=f"导出图片（当前目录：{folder}）",
            defaultextension=".png",
            initialfile=default_name,
            initialdir=folder,
            filetypes=[("PNG 图片", "*.png")])
        if path:
            replot(save_path=path)

    replot_btn.configure(command=do_replot)
    export_btn.configure(command=do_export)

    # ---------------- 开始提取
    def on_finish(png, ok, failed):
        run_btn.configure(state="normal")
        status_var.set(f"完成：成功 {ok}，失败 {len(failed)}")
        if png:
            state["png"] = png
            show_preview(png)
            nb.select(tab_img)
        if ok:
            replot_btn.configure(state="normal")
            export_btn.configure(state="normal")
        if failed:
            lines = "\n".join(f"· {os.path.basename(p)}：{r}" for p, r in failed[:8])
            messagebox.showwarning("部分失败", f"成功 {ok} 个，失败 {len(failed)} 个：\n\n{lines}")
        elif ok:
            messagebox.showinfo(
                "完成",
                f"已完成 {ok} 个文件。\n\n实际写入目录：\n"
                f"{state['out'] or out_var.get().strip()}")

    def start():
        if not pyopenms_ok:
            messagebox.showerror("依赖缺失", pyopenms_err)
            return
        if not files:
            messagebox.showwarning("还没选文件", "请先选择 .raw 文件或文件夹。")
            return
        is_tic = mode_var.get() == "tic"

        mz_list = []
        if not is_tic:
            try:
                mz_list = core.parse_mz_list(get_mz_text())
            except ValueError as e:
                messagebox.showerror(
                    "参数错误",
                    f"{e}\n\n目标 m/z 请一行填一个，例如：\n58.83\n60.02\n72.5")
                return
            if not mz_list:
                messagebox.showerror("参数错误", "请填写目标 m/z，例如 58.83")
                return
        mz = mz_list[0] if mz_list else None
        try:
            tol = float(tol_var.get() or 0.01)
        except ValueError:
            messagebox.showerror("参数错误", "容差必须是数字，例如 0.01")
            return

        rt_range = None
        if not whole_var.get():
            bounds = []
            for name, var in (("开始", rt_start_var), ("结束", rt_end_var)):
                s = var.get().strip()
                if not s:
                    bounds.append(None)
                else:
                    try:
                        bounds.append(float(s))
                    except ValueError:
                        messagebox.showerror("参数错误", f"时间范围的{name}值必须是数字（分钟）。")
                        return
            if not all(b is None for b in bounds):
                rt_range = (bounds[0] if bounds[0] is not None else 0.0,
                            bounds[1] if bounds[1] is not None else 1e9)

        out_dir = out_var.get().strip()
        if not out_dir:
            messagebox.showerror("参数错误", "请填写输出目录。")
            return

        persist()

        args = types.SimpleNamespace(mz=mz, tol=tol, tol_type=unit_var.get(),
                                     mode="sum", ms_level="auto", centroid=False,
                                     rt_range=rt_range, tic=is_tic)
        style = collect_style()

        run_btn.configure(state="disabled")
        replot_btn.configure(state="disabled")
        export_btn.configure(state="disabled")
        status_var.set("正在处理…")
        nb.select(tab_log)

        def worker():
            try:
                png, ok, failed, traces = run_extraction(
                    list(files), args, out_dir, log, style=style,
                    ranges=dict(range_map), mz_list=mz_list)
            except Exception:
                log("未预期的错误：\n" + traceback.format_exc())
                png, ok, failed, traces = None, 0, [], []
            state["traces"] = traces
            state["args"] = args
            state["out"] = out_dir
            root.after(0, lambda: on_finish(png, ok, failed))

        threading.Thread(target=worker, daemon=True).start()

    run_btn.configure(command=start)

    log(f"就绪。脚本目录：{HERE}")
    if not pyopenms_ok:
        log("依赖加载失败：" + pyopenms_err)
        log("请确认 pkgs 文件夹与本脚本在同一目录。")

    # 按内容自动定尺寸；超出屏幕可用高度时靠右侧滚动条上下拖动
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = min(max(body.winfo_reqwidth() + 46, 880), sw - 60)
    need_h = body.winfo_reqheight() + 46
    h = min(max(need_h, 660), min(940, int(sh * 0.92)))
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(20, (sh - h) // 4)}")
    root.update_idletasks()
    sync_scrollregion()

    root.mainloop()


# ---------------------------------------------------------------- 自检
def selftest():
    """命令行自检：不开窗口跑一遍，验证整条链路。"""
    target = sys.argv[2] if len(sys.argv) > 2 else "."
    files = core.collect_inputs([target])
    args = types.SimpleNamespace(mz=58.83, tol=0.01, tol_type="da", mode="sum",
                                 ms_level="auto", centroid=False, rt_range=None)
    style = {"title": "自检标题", "x_name": "Time", "x_unit": "min",
             "y_name": "Intensity", "y_unit": "a.u.", "dpi": 150, "names": {}}
    png, ok, failed, traces = run_extraction(
        files, args, os.path.join(HERE, "selftest_out"), print, style=style,
        mz_list=[58.83, 60.02])
    print(f"\n成功 {ok}，失败 {len(failed)}，图片 {png}")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    main()
