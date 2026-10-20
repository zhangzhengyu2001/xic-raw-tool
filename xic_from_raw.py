#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从 Thermo .raw 文件直接提取某个 m/z 的"强度 - 保留时间"曲线（XIC）。

不需要先转 mzML：pyopenms 内置 Thermo 的 RawFileReader，直接读 .raw。
运行前提：本机有 .NET 8+ 运行时（已确认你的机器是 8.0.11）。

用法
----
单个文件：
    python xic_from_raw.py "D:\\data\\100.raw" --mz 58.83

整个目录：
    python xic_from_raw.py "D:\\data\\实验数据" --mz 58.83

多个 / 通配符：
    python xic_from_raw.py "D:\\data\\*.raw" --mz 58.83 --tol 0.01

输出
----
每个 .raw 一个 CSV（默认写到 ./xic_out/），列：
    RT_min, RT_sec, intensity_sum, intensity_max, n_points, nearest_mz, nearest_intensity
外加一张叠加曲线图 XIC_58p83_overlay.png
"""

import argparse
import csv
import glob
import importlib
import os
import shutil
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 依赖加载
def load_pyopenms():
    """导入 pyopenms；环境里没有时，自动使用脚本同目录的 ./pkgs。"""
    try:
        return importlib.import_module("pyopenms")
    except Exception:
        pass

    local = os.path.join(_HERE, "pkgs")
    if os.path.isdir(os.path.join(local, "pyopenms")):
        if local not in sys.path:
            sys.path.insert(0, local)
        for d in (os.path.join(local, "pyopenms"),
                  os.path.join(local, "pyopenms.libs")):
            if os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
        return importlib.import_module("pyopenms")

    raise ImportError(
        "找不到 pyopenms。请安装它，或把 pyopenms 放到本脚本同目录的 pkgs/ 下。"
    )


# ------------------------------------------------------- 非 ASCII 路径处理
def make_readable_path(path):
    """
    OpenMS 的 C++ 层读不了中文路径（实测报 Could not open RAW file），
    所以遇到非 ASCII 路径时转成本地纯 ASCII 副本。
    返回 (可读路径, 临时目录或 None)
    """
    p = os.path.abspath(path)
    if p.isascii():
        return p, None

    # 注意：不用 tempfile.mkdtemp，它在 Windows 上会建带限制性 ACL 的目录
    tmpdir = os.path.join(_HERE, f"xic_tmp_{os.getpid()}_{int(time.time() * 1000)}")
    os.makedirs(tmpdir, exist_ok=True)
    dst = os.path.join(tmpdir, "input.raw")
    # 必须真实复制：实测硬链接会让 Thermo RawFileReader 抛
    # "string query: managed exception"；同一文件真实复制后即可正常读取。
    shutil.copy2(p, dst)
    return dst, tmpdir


def cleanup(tmpdir):
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------ 读取
def load_experiment(path, centroid):
    oms = load_pyopenms()
    reader = oms.ThermoRawFile()
    opts = reader.getOptions()
    opts.centroid = bool(centroid)
    reader.setOptions(opts)

    exp = oms.MSExperiment()
    reader.load(path, exp)
    if len(exp) == 0:
        raise RuntimeError("文件里没有读到任何谱图")
    return exp


def level_histogram(exp):
    hist = {}
    for spec in exp:
        lv = spec.getMSLevel()
        hist[lv] = hist.get(lv, 0) + 1
    return hist


def resolve_ms_level(hist, want):
    """want: 'auto' | 'all' | '1' | '2' ...  auto = 有 MS1 就用 MS1，否则用全部"""
    if want == "all":
        return None, "全部谱图"
    if want == "auto":
        if 1 in hist:
            return 1, "MS1"
        return None, f"全部谱图（文件里没有 MS1，实际 ms level 为 {sorted(hist)}）"
    lv = int(want)
    return lv, f"MS level {lv}"


# ------------------------------------------------------------------ 提取
def extract_trace(exp, target_mz, tol, tol_type, mode, ms_level, rt_limit):
    rows = []
    for spec in exp:
        if ms_level is not None and spec.getMSLevel() != ms_level:
            continue

        rt_sec = spec.getRT()
        rt_min = rt_sec / 60.0
        if rt_limit is not None and not (rt_limit[0] <= rt_min <= rt_limit[1]):
            continue

        mz, inten = spec.get_peaks()
        if mz is None or len(mz) == 0:
            continue

        half = target_mz * tol * 1e-6 if tol_type == "ppm" else tol
        mask = (mz >= target_mz - half) & (mz <= target_mz + half)
        n_hit = int(mask.sum())

        if n_hit == 0:
            rows.append(dict(RT_min=rt_min, RT_sec=rt_sec, intensity_sum=0.0,
                             intensity_max=0.0, n_points=0,
                             nearest_mz=float("nan"), nearest_intensity=0.0))
            continue

        sel_mz, sel_it = mz[mask], inten[mask]
        k = int(np.argmax(sel_it))
        value = float(sel_it.max()) if mode == "max" else float(sel_it.sum())

        rows.append(dict(RT_min=rt_min, RT_sec=rt_sec,
                         intensity_sum=float(sel_it.sum()),
                         intensity_max=float(sel_it.max()),
                         n_points=n_hit,
                         nearest_mz=float(sel_mz[k]),
                         nearest_intensity=float(sel_it[k])))
    return rows


def diagnose(exp, target_mz, span=0.5, n_spectra=40, topn=8):
    """窗口内一无所获时，列出附近真实存在的强峰，便于校准 m/z。"""
    lo, hi = target_mz - span, target_mz + span
    mz_list, it_list = [], []

    for i, spec in enumerate(exp):
        if i >= n_spectra:
            break
        mz, inten = spec.get_peaks()
        if mz is None or len(mz) == 0:
            continue
        m = (mz >= lo) & (mz <= hi)
        if m.any():
            mz_list.append(mz[m])
            it_list.append(inten[m])

    if not mz_list:
        return None

    mz_cat = np.concatenate(mz_list)
    it_cat = np.concatenate(it_list)
    order = np.argsort(it_cat)[::-1][:topn]
    return [(float(mz_cat[i]), float(it_cat[i])) for i in order]


# ------------------------------------------------------------------ 输出
def write_csv(rows, out_path):
    fields = ["RT_min", "RT_sec", "intensity_sum", "intensity_max",
              "n_points", "nearest_mz", "nearest_intensity"]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def cjk_font():
    try:
        from matplotlib import font_manager
        for name in ("Microsoft YaHei", "SimHei", "DengXian", "SimSun"):
            try:
                font_manager.findfont(name, fallback_to_default=False)
                return name
            except Exception:
                continue
    except Exception:
        pass
    return None


def plot_overlay(traces, target_mz, tol, tol_type, out_png, label_en=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font]
    plt.rcParams["axes.unicode_minus"] = False
    cn = bool(font) and not label_en

    fig, ax = plt.subplots(figsize=(11, 5), dpi=130)
    for name, rows in traces:
        if not rows:
            continue
        x = np.array([r["RT_min"] for r in rows])
        y = np.array([r["intensity_sum"] for r in rows])
        ax.plot(x, y, linewidth=1.0, label=os.path.basename(name))

    unit = "ppm" if tol_type == "ppm" else "Da"
    if cn:
        ax.set_xlabel("保留时间 (min)")
        ax.set_ylabel("强度（窗口内累加）")
        ax.set_title(f"m/z {target_mz} ±{tol} {unit} 的提取离子色谱")
    else:
        ax.set_xlabel("Retention time (min)")
        ax.set_ylabel("Intensity (sum in window)")
        ax.set_title(f"XIC of m/z {target_mz} +/- {tol} {unit}")

    ax.grid(alpha=0.3)
    if len(traces) > 1:
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


# ------------------------------------------------------------------ 主流程
def collect_inputs(items):
    found = []
    for a in items:
        if os.path.isdir(a):
            found += sorted(glob.glob(os.path.join(a, "*.raw")))
            found += sorted(glob.glob(os.path.join(a, "*.RAW")))
        elif any(ch in a for ch in "*?[]"):
            found += sorted(glob.glob(a))
        else:
            found.append(a)

    seen, uniq = set(), []
    for p in found:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            uniq.append(p)
    return [p for p in uniq if os.path.exists(p)]


def process_one(path, args):
    readable, tmpdir = make_readable_path(path)
    try:
        exp = load_experiment(readable, centroid=args.centroid)
    finally:
        cleanup(tmpdir)

    hist = level_histogram(exp)
    ms_level, level_desc = resolve_ms_level(hist, args.ms_level)

    rows = extract_trace(exp, args.mz, args.tol, args.tol_type,
                         args.mode, ms_level, args.rt_range)

    n = len(exp)
    info = dict(
        file=path, n_spectra=n, hist=hist, level_desc=level_desc,
        rt_first=exp[0].getRT(), rt_last=exp[n - 1].getRT(),
    )

    if not rows:
        info["empty"] = True
        return rows, info

    nonzero = [r for r in rows if r["intensity_sum"] > 0]
    info["n_used"] = len(rows)
    info["n_signal"] = len(nonzero)

    if nonzero:
        apex = max(nonzero, key=lambda r: r["intensity_sum"])
        info.update(apex_rt=apex["RT_min"], apex_int=apex["intensity_sum"],
                    apex_mz=apex["nearest_mz"], apex_n=apex["n_points"])
    else:
        info["apex_rt"] = None
        info["diagnostic"] = diagnose(exp, args.mz)
    return rows, info


def main():
    ap = argparse.ArgumentParser(
        description="从 Thermo .raw 提取指定 m/z 的强度-时间曲线")
    ap.add_argument("inputs", nargs="+", help=".raw 文件、目录或通配符")
    ap.add_argument("--mz", type=float, required=True, help="目标 m/z，如 58.83")
    ap.add_argument("--tol", type=float, default=0.01,
                    help="容差，默认 0.01（--tol-type ppm 时为 ppm 数）")
    ap.add_argument("--tol-type", choices=["da", "ppm"], default="da")
    ap.add_argument("--mode", choices=["sum", "max"], default="sum",
                    help="窗口内取和(sum, 默认) 或取最大(max)")
    ap.add_argument("--ms-level", default="auto",
                    help="用哪一级谱：auto(默认)/all/1/2。auto=有MS1用MS1，否则用全部")
    ap.add_argument("--centroid", action="store_true",
                    help="让仪器端先做峰检测再取数（默认用采集到的原始点）")
    ap.add_argument("--rt-range", nargs=2, type=float, metavar=("MIN", "MIN"),
                    help="只处理该保留时间区间（分钟）")
    ap.add_argument("--out", default="xic_out", help="输出目录（默认 ./xic_out）")
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--label-en", action="store_true", help="图片标签用英文")
    args = ap.parse_args()

    try:
        load_pyopenms()
    except ImportError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2

    files = collect_inputs(args.inputs)
    if not files:
        print(f"[错误] 没找到 .raw 文件：{args.inputs}", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    traces, ok = [], 0

    for path in files:
        print(f"\n=== {os.path.basename(path)} ===")
        try:
            rows, info = process_one(path, args)
        except Exception as e:
            print(f"  [失败] {type(e).__name__}: {e}")
            continue

        if info.get("empty"):
            print(f"  谱图数 {info['n_spectra']}，但没有符合 --ms-level {args.ms_level} 的谱")
            print(f"  该文件的 ms level 分布：{info['hist']}")
            continue

        stem = os.path.splitext(os.path.basename(path))[0]
        tag = f"{args.mz:g}".replace(".", "p")
        csv_path = os.path.join(args.out, f"{stem}_xic_{tag}.csv")
        write_csv(rows, csv_path)
        traces.append((path, rows))
        ok += 1

        print(f"  谱图数 / 使用   : {info['n_spectra']} / {info['n_used']}  ({info['level_desc']})")
        print(f"  时间范围        : {info['rt_first']/60:.2f} ~ {info['rt_last']/60:.2f} min")
        print(f"  窗口内有信号的谱: {info['n_signal']}")

        if info.get("apex_rt") is not None:
            print(f"  信号最强处      : RT={info['apex_rt']:.3f} min, 强度={info['apex_int']:.4g}")
            print(f"                    该处峰 m/z={info['apex_mz']:.4f}，命中 {info['apex_n']} 个点")
        else:
            print("  [警告] 该 m/z 窗口内整个色谱没有信号")
            diag = info.get("diagnostic")
            if diag:
                print(f"  {args.mz}±0.5 附近实际存在的强峰：")
                for m, i in diag:
                    print(f"      m/z = {m:10.4f}   强度 = {i:.4g}")
        print(f"  → {csv_path}")

    if ok and not args.no_plot:
        tag = f"{args.mz:g}".replace(".", "p")
        png = os.path.join(args.out, f"XIC_{tag}_overlay.png")
        try:
            plot_overlay(traces, args.mz, args.tol, args.tol_type, png,
                         label_en=args.label_en)
            print(f"\n曲线图 → {png}")
        except Exception as e:
            print(f"\n[警告] 画图失败（CSV 已正常输出）：{e}")

    print(f"\n完成：{ok}/{len(files)} 个文件，输出目录 {os.path.abspath(args.out)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
