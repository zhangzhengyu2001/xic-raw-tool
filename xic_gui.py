#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
图形界面版：选择 .raw 文件 → 填目标 m/z →（可选）限定时间范围 → 一键出曲线。

双击同目录的「一键提取曲线.cmd」即可启动，无需命令行。
核心逻辑复用 xic_from_raw.py，因此两边结果完全一致。
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
    "mz": "58.83",
    "tol": "0.01",
    "tol_type": "da",
    "whole_file": True,
    "rt_start": "",
    "rt_end": "",
    "out": os.path.join(HERE, "xic_out"),
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
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
def run_extraction(files, args, out_dir, log):
    """跑完全部文件。返回 (png路径或None, 成功数, 失败列表)"""
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

        csv_path = os.path.join(out_dir, f"{os.path.splitext(os.path.basename(p))[0]}_xic_{tag}.csv")
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
            core.plot_overlay(traces, args.mz, args.tol, args.tol_type, png)
        except Exception as e:
            log(f"  画图失败：{e}")
            png = None

    return png, len(traces), failed


# ---------------------------------------------------------------- 界面
def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = load_config()

    root = tk.Tk()
    root.title("m/z 强度-时间曲线提取")
    root.geometry("880x720")
    root.minsize(760, 620)

    try:
        core.load_pyopenms()
        pyopenms_ok = True
    except Exception as e:
        pyopenms_ok = False
        pyopenms_err = str(e)

    files = []
    log_q = queue.Queue()
    state = {"png": None, "photo": None}

    # ---------------- 文件选择区
    frm_files = ttk.LabelFrame(root, text="1. 选择 .raw 文件")
    frm_files.pack(fill="x", padx=10, pady=(10, 6))

    listbox = tk.Listbox(frm_files, height=4, selectmode="extended")
    listbox.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=8)

    sb = ttk.Scrollbar(frm_files, orient="vertical", command=listbox.yview)
    sb.pack(side="left", fill="y", pady=8)
    listbox.config(yscrollcommand=sb.set)

    frm_btns = ttk.Frame(frm_files)
    frm_btns.pack(side="right", padx=(0, 10), pady=8)

    def add_paths(paths):
        added = 0
        for p in paths:
            if p.lower().endswith(".raw") and os.path.abspath(p) not in files:
                files.append(os.path.abspath(p))
                listbox.insert("end", p)
                added += 1
        if added:
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
        for i in reversed(listbox.curselection()):
            listbox.delete(i)
            files.pop(i)

    def clear_all():
        files.clear()
        listbox.delete(0, "end")

    ttk.Button(frm_btns, text="选择文件…", width=12, command=pick_files).pack(pady=2)
    ttk.Button(frm_btns, text="选择文件夹…", width=12, command=pick_folder).pack(pady=2)
    ttk.Button(frm_btns, text="移除选中", width=12, command=remove_sel).pack(pady=2)
    ttk.Button(frm_btns, text="清空", width=12, command=clear_all).pack(pady=2)

    # ---------------- 参数区
    frm_par = ttk.LabelFrame(root, text="2. 参数")
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

    # ---------------- 操作区
    frm_act = ttk.Frame(root)
    frm_act.pack(fill="x", padx=10, pady=4)

    run_btn = ttk.Button(frm_act, text="开始提取", width=16)
    run_btn.pack(side="left")
    open_btn = ttk.Button(frm_act, text="打开输出目录", width=14,
                          command=lambda: os.startfile(out_var.get().strip() or HERE))
    open_btn.pack(side="left", padx=8)

    status_var = tk.StringVar(value="就绪" if pyopenms_ok else "依赖缺失")
    ttk.Label(frm_act, textvariable=status_var, foreground="#0a5").pack(side="left", padx=10)

    # ---------------- 日志 + 预览
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=10, pady=(6, 10))

    tab_log = ttk.Frame(nb)
    nb.add(tab_log, text="运行日志")

    log_text = tk.Text(tab_log, wrap="word", height=12, state="disabled")
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

    # ---------------- 运行
    def show_preview(png):
        try:
            img = tk.PhotoImage(file=png)
            w, h = img.width(), img.height()
            factor = max(1, int(max(w / 820, h / 400) + 0.999))
            if factor > 1:
                img = img.subsample(factor, factor)
            state["photo"] = img
            img_label.configure(image=img, text="")
        except Exception as e:
            img_label.configure(image="", text=f"预览加载失败：{e}\n图片位置：{png}")

    def on_finish(png, ok, failed):
        run_btn.configure(state="normal")
        status_var.set(f"完成：成功 {ok}，失败 {len(failed)}")
        if png:
            show_preview(png)
            nb.select(tab_img)
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
            if all(b is None for b in bounds):
                rt_range = None
            else:
                rt_range = (bounds[0] if bounds[0] is not None else 0.0,
                            bounds[1] if bounds[1] is not None else 1e9)

        out_dir = out_var.get().strip()
        if not out_dir:
            messagebox.showerror("参数错误", "请填写输出目录。")
            return

        save_config({
            "mz": mz_var.get(), "tol": tol_var.get(), "tol_type": unit_var.get(),
            "whole_file": whole_var.get(),
            "rt_start": rt_start_var.get(), "rt_end": rt_end_var.get(),
            "out": out_dir,
        })

        args = types.SimpleNamespace(mz=mz, tol=tol, tol_type=unit_var.get(),
                                     mode="sum", ms_level="auto", centroid=False,
                                     rt_range=rt_range)

        run_btn.configure(state="disabled")
        status_var.set("正在处理…")
        nb.select(tab_log)

        def worker():
            try:
                png, ok, failed = run_extraction(list(files), args, out_dir, log)
            except Exception:
                log("未预期的错误：\n" + traceback.format_exc())
                png, ok, failed = None, 0, []
            root.after(0, lambda: on_finish(png, ok, failed))

        threading.Thread(target=worker, daemon=True).start()

    run_btn.configure(command=start)

    log(f"就绪。脚本目录：{HERE}")
    if not pyopenms_ok:
        log("依赖加载失败：" + pyopenms_err)
        log("请确认 pkgs 文件夹与本脚本在同一目录。")

    root.mainloop()


# ---------------------------------------------------------------- 自检
def selftest():
    """命令行自检：不开窗口跑一遍，验证整条链路。"""
    files = core.collect_inputs([sys.argv[2]])
    args = types.SimpleNamespace(mz=58.83, tol=0.01, tol_type="da", mode="sum",
                                 ms_level="auto", centroid=False, rt_range=None)
    png, ok, failed = run_extraction(files, args, os.path.join(HERE, "selftest_out"), print)
    print(f"\n成功 {ok}，失败 {len(failed)}，图片 {png}")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    main()
