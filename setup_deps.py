#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线依赖安装：直接从 PyPI 下载 wheel（本质是 zip）解压到 ./pkgs/。

适用于 pip 因网络或权限限制装不上的环境。装好的东西在 ./pkgs/ 下，
主脚本会自动把这个目录加进 sys.path，所以不需要设环境变量。

用法：
    python setup_deps.py
"""

import importlib
import json
import os
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKGS = os.path.join(HERE, "pkgs")
DOWNLOAD = os.path.join(HERE, "_download")

# 需要安装的包。pyopenms 读 .raw，matplotlib 画图，其余是 matplotlib 的运行依赖。
WANTED = [
    "pyopenms",
    "nanobind-backend",
    "numpy",
    "matplotlib",
    "contourpy",
    "cycler",
    "fonttools",
    "kiwisolver",
    "packaging",
    "pillow",
    "pyparsing",
    "python-dateutil",
    "six",
]

# 包名 -> 导入名，用于判断是否已经可用
IMPORT_NAME = {
    "pyopenms": "pyopenms",
    "nanobind-backend": "nanobind_backend",
    "python-dateutil": "dateutil",
}


def already_available(pkg):
    mod = IMPORT_NAME.get(pkg, pkg.replace("-", "_"))
    if mod in sys.modules:
        return True
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def pick_wheel(files):
    """优先与当前解释器完全匹配的 win_amd64，其次 abi3，再其次纯 python wheel。"""
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"

    def score(fn):
        n = fn.lower()
        if not n.endswith(".whl"):
            return -1
        if "win_amd64" in n and f"{tag}-{tag}" in n:
            return 100
        if "win_amd64" in n and "abi3" in n:
            return 90
        if "none-any" in n:
            return 80
        if "win_amd64" in n:
            return 10
        return 0

    scored = [(score(f["filename"]), f) for f in files]
    scored = [s for s in scored if s[0] > 0]
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


def download(url, dest, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            return True
        except Exception as e:
            print(f"    重试 {i + 1}/{tries}: {type(e).__name__}: {e}")
            if i == tries - 1:
                return False
    return False


def install(pkg):
    if already_available(pkg):
        print(f"  跳过 {pkg}（已可用）")
        return True

    print(f"  处理 {pkg} ...")
    try:
        meta = fetch_json(f"https://pypi.org/pypi/{pkg}/json")
    except Exception as e:
        print(f"    查询失败: {e}")
        return False

    ver = meta["info"]["version"]
    wheel = pick_wheel(meta["releases"].get(ver, []))
    if not wheel:
        print(f"    没有适合本机的 wheel（{ver}）")
        return False

    dest = os.path.join(DOWNLOAD, wheel["filename"])
    if not os.path.exists(dest) or os.path.getsize(dest) != wheel["size"]:
        print(f"    下载 {wheel['filename']} ({wheel['size'] / 1e6:.1f} MB)")
        if not download(wheel["url"], dest):
            return False

    try:
        with zipfile.ZipFile(dest) as z:
            z.extractall(PKGS)
    except Exception as e:
        print(f"    解压失败: {e}")
        return False

    print("    完成")
    return True


def main():
    os.makedirs(PKGS, exist_ok=True)
    os.makedirs(DOWNLOAD, exist_ok=True)

    print(f"Python {sys.version.split()[0]}")
    print(f"安装目标: {PKGS}\n")

    results = {}
    for pkg in WANTED:
        try:
            results[pkg] = install(pkg)
        except Exception as e:
            print(f"  {pkg} 失败: {type(e).__name__}: {e}")
            results[pkg] = False

    print("\n汇总:")
    for pkg, ok in results.items():
        print(f"  {'OK  ' if ok else 'FAIL'}  {pkg}")

    failed = [p for p, ok in results.items() if not ok]
    print()
    if failed:
        print(f"有 {len(failed)} 个包未装成功：{failed}")
        print("核心功能依赖 pyopenms 和 nanobind-backend，这两个失败则无法读取 .raw。")
        print("matplotlib 缺失不影响 CSV 输出，只是没有 PNG 图。")
        return 1

    print("全部就绪。可以运行：")
    print('    python xic_from_raw.py "D:\\data" --mz 58.83')
    return 0


if __name__ == "__main__":
    sys.exit(main())
