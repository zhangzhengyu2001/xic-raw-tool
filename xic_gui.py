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

CONFIG_PATH = os.path.join(HERE, "xic_gui_config.json")

DEFAULTS = {
    # 提取参数
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
    # 曲线名称（文件名 -> 显示名）
    "curve_names": {},
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
def run_extraction(files, args, out_dir, log, style=None):
    """跑完全部文件。返回 (png路径, 成功数, 失败列表, traces)"""
    os.makedirs(out_dir, exist_ok=True)
    tag = f"{args.mz:g}".replace(".", "p")
    traces, failed = [], []

    for p in files:
        log(f"=== {os.path.basename(p)} ===")
        try:
            rows, info = core.process_one(p, args)
        except Exception as e:
            failed.append((p, f"{type(e).__name__}: {e}"))
            log(f"  失败：{type(e).__name__}: {e}")
            continue

        if info.get("empty"):
            failed.append((p, f"没有符合 ms level 的谱（分布 {info['hist']}）"))
            log(f"  跳过：该文件没有符合 ms level 的谱，分布 {info['hist']}")
            continue

        csv_path = os.path.join(
            out_dir, f"{os.path.splitext(os.path.basename(p))[0]}_xic_{tag}.csv")
        core.write_csv(rows, csv_path)
        traces.append((p, rows))

        log(f"  谱图数 {info['n_used']}，时间 {info['rt_first']/60:.2f}~{info['rt_last']/60:.2f} min")
        if info.get("apex_rt") is not None:
            log(f"  最强处 RT={info['apex_rt']:.3f} min，强度={info['apex_int']:.4g}")
        else:
            log("  警告：该 m/z 窗口内没有信号")
            for m, i in (info.get("diagnostic") or [])[:5]:
                log(f"      附近有峰 m/z={m:.4f} 强度={i:.4g}")
        log(f"  → {os.path.basename(csv_path)}")

    png = None
    if traces:
        png = os.path.join(out_dir, f"XIC_{tag}_overlay.png")
        try:
            core.plot_overlay(traces, args.mz, args.tol, args.tol_type, png,
                              style=style)
        except Exception as e:
            log(f"  画图失败：{e}")
            png = None

    return png, len(traces), failed, traces


# ---------------------------------------------------------------- 界面
def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    cfg = load_config()

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
    log_q = queue.Queue()
    state = {"png": None, "photo": None, "traces": None, "args": None, "out": None}

    # ---------------- 1. 文件
    frm_files = ttk.LabelFrame(root, text="1. 选择 .raw 文件（双击「曲线名称」可重命名）")
    frm_files.pack(fill="x", padx=10, pady=(10, 6))

    tree = ttk.Treeview(frm_files, columns=("file", "curve"),
                        show="headings", height=4, selectmode="extended")
    tree.heading("file", text="文件")
    tree.heading("curve", text="曲线名称")
    tree.column("file", width=420, anchor="w")
    tree.column("curve", width=200, anchor="w")
    tree.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=8)

    tsb = ttk.Scrollbar(frm_files, orient="vertical", command=tree.yview)
    tsb.pack(side="left", fill="y", pady=8)
    tree.configure(yscrollcommand=tsb.set)

    frm_btns = ttk.Frame(frm_files)
    frm_btns.pack(side="right", padx=(0, 10), pady=8)

    def display_name(path):
        base = os.path.basename(path)
        return name_map.get(base) or os.path.splitext(base)[0]

    def refresh_tree():
        tree.delete(*tree.get_children())
        for p in files:
            tree.insert("", "end", iid=p, values=(os.path.basename(p), display_name(p)))

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

    tree.bind("<Double-1>", rename_selected)

    ttk.Button(frm_btns, text="选择文件…", width=12, command=pick_files).pack(pady=2)
    ttk.Button(frm_btns, text="选择文件夹…", width=12, command=pick_folder).pack(pady=2)
    ttk.Button(frm_btns, text="重命名曲线", width=12, command=rename_selected).pack(pady=2)
    ttk.Button(frm_btns, text="移除选中", width=12, command=remove_sel).pack(pady=2)
    ttk.Button(frm_btns, text="清空", width=12, command=clear_all).pack(pady=2)

    # ---------------- 2. 提取参数
    frm_par = ttk.LabelFrame(root, text="2. 提取参数")
    frm_par.pack(fill="x", padx=10, pady=6)

    row1 = ttk.Frame(frm_par)
    row1.pack(fill="x", padx=10, pady=(8, 4))
    ttk.Label(row1, text="目标 m/z：").pack(side="left")
    mz_var = tk.StringVar(value=cfg["mz"])
    ttk.Entry(row1, textvariable=mz_var, width=12).pack(side="left")
    ttk.Label(row1, text="    容差：").pack(side="left")
    tol_var = tk.StringVar(value=cfg["tol"])
    ttk.Entry(row1, textvariable=tol_var, width=8).pack(side="left")
    unit_var = tk.StringVar(value=cfg["tol_type"])
    ttk.Combobox(row1, textvariable=unit_var, values=["da", "ppm"],
                 width=5, state="readonly").pack(side="left", padx=(4, 0))

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
    frm_img = ttk.LabelFrame(root, text="3. 图片设置")
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

    toggle_title()

    # ---------------- 操作区
    frm_act = ttk.Frame(root)
    frm_act.pack(fill="x", padx=10, pady=4)

    run_btn = ttk.Button(frm_act, text="开始提取", width=14)
    run_btn.pack(side="left")
    replot_btn = ttk.Button(frm_act, text="重新生成图", width=12, state="disabled")
    replot_btn.pack(side="left", padx=6)
    export_btn = ttk.Button(frm_act, text="导出图片…", width=12, state="disabled")
    export_btn.pack(side="left")
    ttk.Button(frm_act, text="打开输出目录", width=13,
               command=lambda: os.startfile(out_var.get().strip() or HERE)).pack(side="left", padx=6)

    status_var = tk.StringVar(value="就绪" if pyopenms_ok else "依赖缺失")
    ttk.Label(frm_act, textvariable=status_var, foreground="#0a5").pack(side="left", padx=10)

    # ---------------- 日志 / 预览
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=10, pady=(6, 10))

    tab_log = ttk.Frame(nb)
    nb.add(tab_log, text="运行日志")
    log_text = tk.Text(tab_log, wrap="word", height=8, state="disabled")
    log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
    log_sb = ttk.Scrollbar(tab_log, orient="vertical", command=log_text.yview)
    log_sb.pack(side="right", fill="y", pady=8)
    log_text.config(yscrollcommand=log_sb.set)

    tab_img = ttk.Frame(nb)
    nb.add(tab_img, text="曲线预览")
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
        }

    def persist():
        save_config({
            "mz": mz_var.get(), "tol": tol_var.get(), "tol_type": unit_var.get(),
            "whole_file": whole_var.get(),
            "rt_start": rt_start_var.get(), "rt_end": rt_end_var.get(),
            "out": out_var.get().strip(),
            "title_auto": title_auto_var.get(), "title": title_var.get(),
            "x_name": x_name_var.get(), "x_unit": x_unit_var.get(),
            "y_name": y_name_var.get(), "y_unit": y_unit_var.get(),
            "dpi": dpi_var.get(),
            "curve_names": {k: v for k, v in name_map.items() if v},
        })

    def show_preview(png):
        try:
            img = tk.PhotoImage(file=png)
            w, h = img.width(), img.height()
            factor = max(1, int(max(w / 840, h / 420) + 0.999))
            if factor > 1:
                img = img.subsample(factor, factor)
            state["photo"] = img
            img_label.configure(image=img, text="")
        except Exception as e:
            img_label.configure(image="", text=f"预览加载失败：{e}\n图片位置：{png}")

    # ---------------- 重新生成图 / 导出
    def replot(save_path=None, silent=False):
        traces, args = state["traces"], state["args"]
        if not traces or not args:
            messagebox.showinfo("提示", "还没有提取结果，请先点「开始提取」。")
            return
        target = save_path or state["png"]
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
            log(f"图片已更新 → {target}")

    def do_replot():
        persist()
        replot()

    def do_export():
        if not state["traces"]:
            messagebox.showinfo("提示", "还没有提取结果，请先点「开始提取」。")
            return
        persist()
        tag = f"{state['args'].mz:g}".replace(".", "p")
        path = filedialog.asksaveasfilename(
            title="导出图片",
            defaultextension=".png",
            initialfile=f"XIC_{tag}.png",
            initialdir=state["out"] or out_var.get().strip() or HERE,
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
            messagebox.showinfo("完成", f"已完成 {ok} 个文件。\n\n输出目录：\n{out_var.get().strip()}")

    def start():
        if not pyopenms_ok:
            messagebox.showerror("依赖缺失", pyopenms_err)
            return
        if not files:
            messagebox.showwarning("还没选文件", "请先选择 .raw 文件或文件夹。")
            return
        try:
            mz = float(mz_var.get())
        except ValueError:
            messagebox.showerror("参数错误", "目标 m/z 必须是数字，例如 58.83")
            return
        try:
            tol = float(tol_var.get())
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
                                     rt_range=rt_range)
        style = collect_style()

        run_btn.configure(state="disabled")
        replot_btn.configure(state="disabled")
        export_btn.configure(state="disabled")
        status_var.set("正在处理…")
        nb.select(tab_log)

        def worker():
            try:
                png, ok, failed, traces = run_extraction(
                    list(files), args, out_dir, log, style=style)
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

    # 按内容自动定尺寸，并限制在屏幕可用范围内，避免按钮被裁掉
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = min(max(root.winfo_reqwidth() + 16, 860), sw - 60)
    h = min(max(root.winfo_reqheight() + 170, 860), 940)
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(20, (sh - h) // 4)}")

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
        files, args, os.path.join(HERE, "selftest_out"), print, style=style)
    print(f"\n成功 {ok}，失败 {len(failed)}，图片 {png}")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    main()
