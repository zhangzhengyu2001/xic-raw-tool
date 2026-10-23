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
import re
import shutil
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))

_FILENAME_BAD = '<>:"/\\|?*'


def safe_filename(name, fallback="figure", max_len=120):
    """
    把任意文本转成可安全用作文件名的字符串（Windows 规则）。

    非法字符 <>:"/\\|?* 和控制字符替换为下划线，去掉结尾的空格与点，
    超长部分截断。返回空串表示"没有可用名字"，由调用方决定回退。
    """
    s = (name or "").strip()
    if not s:
        return ""
    s = "".join("_" if (ch in _FILENAME_BAD or ord(ch) < 32) else ch for ch in s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or fallback


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
def extract_trace(exp, target_mz, tol, tol_type, mode, ms_level, rt_limit,
                  tic=False):
    """
    逐张谱提取强度。

    tic=True 时不看 m/z，直接把该谱所有峰的强度加和（总离子流），
    此时 intensity_max 列即为该时刻的基峰强度（可选作 BPC 用）。
    """
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

        if tic:
            sel_mz, sel_it = mz, inten
            n_hit = int(len(mz))
        else:
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


def _label_for(path, names):
    """找出这条曲线该显示的名字：支持按完整路径 / 文件名 / 不含扩展名的名字匹配。"""
    if not names:
        return os.path.basename(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    for key in (path, os.path.abspath(path), os.path.basename(path), stem):
        v = names.get(key)
        if v:
            return v
    return os.path.basename(path)


def _range_for(path, ranges):
    """找出这个文件单独指定的时间范围；没有则返回 None（表示用全局设置）。"""
    if not ranges:
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    for key in (path, os.path.abspath(path), os.path.basename(path), stem):
        v = ranges.get(key)
        if v:
            return v
    return None


def parse_time_range(text):
    """
    把 "1,2" / "1-2" / "1~2" / "1" 解析成 (起, 止) 分钟。
    只给一个数表示"从该时刻到结束"。解析失败返回 None。
    """
    s = (text or "").strip().replace("，", ",").replace("－", "-")
    if not s:
        return None
    for sep in (",", "~", "—"):
        if sep in s:
            a, b = s.split(sep, 1)
            try:
                return (float(a), float(b))
            except ValueError:
                return None
    m = re.match(r"^\s*([0-9.]+)\s*-\s*([0-9.]+)\s*$", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    try:
        return (float(s), 1e9)
    except ValueError:
        return None


def _axis_label(name, unit):
    name = (name or "").strip()
    unit = (unit or "").strip()
    if unit:
        return f"{name} ({unit})" if name else unit
    return name


def plot_overlay(traces, target_mz, tol, tol_type, out_png,
                 label_en=False, style=None):
    """
    把多条曲线画在一张图上并存成 PNG。

    style 支持的键（不传就用默认值，传空字符串则表示"留白不要"）：
        title    图标题
        x_name   X 轴名称      x_unit  X 轴单位
        y_name   Y 轴名称      y_unit  Y 轴单位
        dpi      图片分辨率（默认 130）
        figsize  画布尺寸（默认 11×5 英寸）
        names    {路径或文件名: 曲线显示名}
        legend   是否显示图例（默认：多条曲线或有自定义名称时显示）
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    style = style or {}
    font = cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font]
    plt.rcParams["axes.unicode_minus"] = False
    cn = bool(font) and not label_en

    tol_unit = "ppm" if tol_type == "ppm" else "Da"
    names = style.get("names") or {}

    dpi = int(style.get("dpi") or 130)
    figsize = style.get("figsize") or (11, 5)
    align_start = bool(style.get("align_start"))

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    for path, rows in traces:
        if not rows:
            continue
        x = np.array([r["RT_min"] for r in rows])
        y = np.array([r["intensity_sum"] for r in rows])
        if align_start and len(x):
            # 每条曲线各自减去自己的首个时间点，使它们都从 0 开始
            x = x - x[0]
        ax.plot(x, y, linewidth=1.0, label=_label_for(path, names))

    # 名称/单位：None 表示用默认，"" 表示留空
    x_name = style.get("x_name")
    x_name = ("保留时间" if cn else "Retention time") if x_name is None else x_name
    x_unit = "min" if style.get("x_unit") is None else style.get("x_unit")

    y_name = style.get("y_name")
    if y_name is None:
        if style.get("kind") == "tic":
            y_name = "总离子流强度" if cn else "Total ion current"
        else:
            y_name = "强度（窗口内累加）" if cn else "Intensity (sum in window)"
    y_unit = "" if style.get("y_unit") is None else style.get("y_unit")

    title = style.get("title")
    if title is None:
        if style.get("kind") == "tic":
            title = "总离子流色谱图 (TIC)" if cn else "Total Ion Chromatogram (TIC)"
        else:
            title = (f"m/z {target_mz} ±{tol} {tol_unit} 的提取离子色谱" if cn
                     else f"XIC of m/z {target_mz} +/- {tol} {tol_unit}")

    ax.set_xlabel(_axis_label(x_name, x_unit))
    ax.set_ylabel(_axis_label(y_name, y_unit))
    ax.set_title(title)

    ax.grid(alpha=0.3)
    show_legend = style.get("legend")
    if show_legend is None:
        show_legend = len(traces) > 1 or bool(names)
    if show_legend and any(rows for _, rows in traces):
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


def process_one(path, args, rt_range=None):
    """rt_range 为 (起,止) 分钟；不给则用 args.rt_range（None 表示整条色谱）。"""
    readable, tmpdir = make_readable_path(path)
    try:
        exp = load_experiment(readable, centroid=args.centroid)
    finally:
        cleanup(tmpdir)

    hist = level_histogram(exp)
    ms_level, level_desc = resolve_ms_level(hist, args.ms_level)

    limit = rt_range if rt_range is not None else getattr(args, "rt_range", None)
    tic = bool(getattr(args, "tic", False))
    rows = extract_trace(exp, args.mz, args.tol, args.tol_type,
                         args.mode, ms_level, limit, tic=tic)

    n = len(exp)
    info = dict(
        file=path, n_spectra=n, hist=hist, level_desc=level_desc,
        rt_first=exp[0].getRT(), rt_last=exp[n - 1].getRT(), tic=tic,
        rt_limit=limit,
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
        if not tic:
            info["diagnostic"] = diagnose(exp, args.mz)
    return rows, info


def main():
    ap = argparse.ArgumentParser(
        description="从 Thermo .raw 提取指定 m/z 的强度-时间曲线")
    ap.add_argument("inputs", nargs="+", help=".raw 文件、目录或通配符")
    ap.add_argument("--mz", type=float, default=None,
                    help="目标 m/z，如 58.83（用 --tic 时不需要）")
    ap.add_argument("--tic", action="store_true",
                    help="提取总离子流色谱图 TIC（每个时刻所有离子强度之和），不需要 --mz")
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
                    help="所有文件共用同一段保留时间区间（分钟）")
    ap.add_argument("--file-range", action="append", metavar="文件=起,止",
                    help='给单个文件指定时间范围，可重复。例：--file-range "a.raw=1,2" '
                         '--file-range "b.raw=3-4"。会覆盖 --rt-range')
    ap.add_argument("--align-start", action="store_true",
                    help="绘图时每条曲线各自减去自己的起始时间，让它们都从 0 开始")
    ap.add_argument("--out", default="xic_out", help="输出目录（默认 ./xic_out）")
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--label-en", action="store_true", help="图片标签用英文")

    # 图片外观
    ap.add_argument("--title", default=None,
                    help='图标题；写 none 表示不要标题（PowerShell 里 "" 会被吞掉）')
    ap.add_argument("--x-name", default=None, help="X 轴名称（默认：保留时间）")
    ap.add_argument("--x-unit", default=None, help="X 轴单位（默认：min；写 none 表示不显示）")
    ap.add_argument("--y-name", default=None, help="Y 轴名称（默认：强度）")
    ap.add_argument("--y-unit", default=None, help="Y 轴单位（默认：空；写 none 表示不显示）")
    ap.add_argument("--dpi", type=int, default=130, help="图片分辨率，默认 130")
    ap.add_argument("--name", action="append", metavar="文件=显示名",
                    help='自定义曲线名称，可重复。例：--name "100.raw=100 度"')
    args = ap.parse_args()

    # 解析 --name 列表
    curve_names = {}
    for item in (args.name or []):
        if "=" not in item:
            print(f"[警告] --name 需要「文件=显示名」格式，已忽略：{item}", file=sys.stderr)
            continue
        k, v = item.split("=", 1)
        curve_names[k.strip()] = v.strip()

    # 解析 --file-range 列表
    file_ranges = {}
    for item in (args.file_range or []):
        if "=" not in item:
            print(f"[警告] --file-range 需要「文件=起,止」格式，已忽略：{item}", file=sys.stderr)
            continue
        k, v = item.split("=", 1)
        rng = parse_time_range(v)
        if rng is None:
            print(f"[警告] 时间范围无法解析，已忽略：{item}", file=sys.stderr)
            continue
        file_ranges[k.strip()] = rng

    # PowerShell 会把空字符串参数吞掉，所以用 none / null 表示"留空"
    def blank_if_sentinel(v):
        if v is None:
            return None
        return "" if v.strip().lower() in ("none", "null", "empty") else v

    if not args.tic and args.mz is None:
        print("[错误] 需要指定 --mz，或加 --tic 提取总离子流色谱图", file=sys.stderr)
        return 2
    if args.tic and args.mz is not None:
        print("[提示] 已指定 --tic，--mz 将被忽略", file=sys.stderr)

    plot_style = {
        "title": blank_if_sentinel(args.title),
        "x_name": blank_if_sentinel(args.x_name),
        "x_unit": blank_if_sentinel(args.x_unit),
        "y_name": blank_if_sentinel(args.y_name),
        "y_unit": blank_if_sentinel(args.y_unit),
        "dpi": args.dpi,
        "names": curve_names,
        "kind": "tic" if args.tic else "xic",
        "align_start": args.align_start,
    }

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
        limit = _range_for(path, file_ranges)
        try:
            rows, info = process_one(path, args, rt_range=limit)
        except Exception as e:
            print(f"  [失败] {type(e).__name__}: {e}")
            continue

        if info.get("empty"):
            print(f"  谱图数 {info['n_spectra']}，但没有符合 --ms-level {args.ms_level} 的谱")
            print(f"  该文件的 ms level 分布：{info['hist']}")
            continue

        stem = os.path.splitext(os.path.basename(path))[0]
        tag = "tic" if args.tic else f"{args.mz:g}".replace(".", "p")
        fname = f"{stem}_tic.csv" if args.tic else f"{stem}_xic_{tag}.csv"
        csv_path = os.path.join(args.out, fname)
        write_csv(rows, csv_path)
        traces.append((path, rows))
        ok += 1

        label = "总离子流" if args.tic else "窗口内"
        used = info.get("rt_limit")
        rng_txt = (f"{used[0]:.2f} ~ {used[1]:.2f} min" if used else "整个文件")
        print(f"  谱图数 / 使用   : {info['n_spectra']} / {info['n_used']}  ({info['level_desc']})")
        print(f"  取用时间段      : {rng_txt}")
        print(f"  文件完整时间    : {info['rt_first']/60:.2f} ~ {info['rt_last']/60:.2f} min")
        print(f"  {label}有信号的谱: {info['n_signal']}")

        if info.get("apex_rt") is not None:
            print(f"  信号最强处      : RT={info['apex_rt']:.3f} min, 强度={info['apex_int']:.4g}")
            if args.tic:
                print(f"                    该时刻基峰 m/z={info['apex_mz']:.4f}，共 {info['apex_n']} 个峰")
            else:
                print(f"                    该处峰 m/z={info['apex_mz']:.4f}，命中 {info['apex_n']} 个点")
        elif not args.tic:
            print("  [警告] 该 m/z 窗口内整个色谱没有信号")
            diag = info.get("diagnostic")
            if diag:
                print(f"  {args.mz}±0.5 附近实际存在的强峰：")
                for m, i in diag:
                    print(f"      m/z = {m:10.4f}   强度 = {i:.4g}")
        print(f"  → {csv_path}")

    if ok and not args.no_plot:
        tag = "tic" if args.tic else f"{args.mz:g}".replace(".", "p")
        prefix = "TIC" if args.tic else "XIC"
        # 用户指定了标题就用标题当文件名，否则用默认命名
        base = safe_filename(plot_style.get("title"))
        png = os.path.join(args.out, f"{base}.png" if base
                           else f"{prefix}_{tag}_overlay.png")
        try:
            plot_overlay(traces, args.mz if args.mz is not None else 0.0,
                         args.tol, args.tol_type, png,
                         label_en=args.label_en, style=plot_style)
            print(f"\n曲线图 → {png}")
        except Exception as e:
            print(f"\n[警告] 画图失败（CSV 已正常输出）：{e}")

    print(f"\n完成：{ok}/{len(files)} 个文件，输出目录 {os.path.abspath(args.out)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
