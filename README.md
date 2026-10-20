# xic-raw-tool

从 **Thermo `.raw`** 文件直接提取指定 m/z 的**强度—时间曲线**（XIC / 提取离子色谱）。

不需要先把 `.raw` 转成 mzML，也不需要仪器厂商的软件：底层用 [pyopenms](https://github.com/OpenMS/OpenMS) 内置的 Thermo RawFileReader 直接读原始文件。

## 功能

- **直接读 `.raw`** —— Agilent / Thermo 的原始文件免转换
- **图形界面** —— 双击即可用，选文件、填 m/z、点一下出图
- **批量处理** —— 一次跑整个目录，自动生成多条曲线对比图
- **时间范围可选** —— 只看某一段色谱，不填就是整个文件
- **中文路径自动处理** —— 内部自动转 ASCII 副本，不用改文件名

## 安装

要求：**Python 3.11+**（实测 3.14）、**.NET 8 或更新运行时**（[下载](https://dotnet.microsoft.com/download)）。

```bash
pip install -r requirements.txt
```

如果 `pip` 因为网络或权限问题装不上，可以改用离线方式：把 wheel 直接下载解压到 `./pkgs/`，脚本会自动加载这个目录。

```bash
python setup_deps.py
```

Windows 用户也可以直接双击 `安装依赖.cmd`，它会先试 pip，失败再自动走离线方式。

## 快速开始

### 图形界面

双击 `一键提取曲线.cmd`，然后：

1. 「选择文件…」挑单个 `.raw`，或「选择文件夹…」把整个目录加进来
2. 填目标 **m/z**
3. 时间范围：保持勾选「取整个文件的时间」，或取消勾选后填起止分钟（留空表示该端不限）
4. 点「开始提取」

完成后窗口内直接显示曲线，结果同时写入输出目录。

### 命令行

```bash
# 单个文件
python xic_from_raw.py "D:\\data\\sample.raw" --mz 58.83

# 整个目录
python xic_from_raw.py "D:\\data\\runs" --mz 58.83

# 只取 1.0–2.0 分钟这一段
python xic_from_raw.py "D:\\data\\runs" --mz 58.83 --rt-range 1.0 2.0

# 用 ppm 表达容差
python xic_from_raw.py "D:\\data\\runs" --mz 58.83 --tol 20 --tol-type ppm
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `inputs` | 必填 | `.raw` 文件、目录或通配符，可给多个 |
| `--mz` | 必填 | 目标 m/z |
| `--tol` | `0.01` | 容差；配合 `--tol-type ppm` 时表示 ppm 数 |
| `--tol-type` | `da` | `da` 或 `ppm` |
| `--mode` | `sum` | 窗口内强度取和 `sum` 或取最大值 `max` |
| `--ms-level` | `auto` | `auto` = 有 MS1 用 MS1，没有则用全部谱；也可指定 `all` / `1` / `2` |
| `--centroid` | 关 | 让仪器端先做峰检测再取数（默认用采集到的原始点） |
| `--rt-range MIN MIN` | — | 只处理该保留时间区间（分钟） |
| `--out` | `xic_out` | 输出目录 |
| `--no-plot` | 关 | 不生成 PNG |
| `--label-en` | 关 | 图片标签强制用英文 |

## 输出

每个 `.raw` 生成一个 CSV：

| 列 | 含义 |
|---|---|
| `RT_min` / `RT_sec` | 保留时间（分 / 秒） |
| `intensity_sum` | 窗口内强度累加 |
| `intensity_max` | 窗口内最强点的强度 |
| `n_points` | 窗口内命中的数据点数 |
| `nearest_mz` | 窗口内最强点的实际 m/z |
| `nearest_intensity` | 该点强度 |

多文件时另有一张 `XIC_<mz>_overlay.png` 叠加对比图。

## 实现中踩过的几个坑

这些不是理论问题，都是实测遇到的，记在这里也方便以后查：

1. **Thermo 读取器不接受中文路径**
   直接传含中文的路径会报 `Could not open RAW file`。脚本检测到非 ASCII 路径时，会在本地建一份 ASCII 副本再读。

2. **不能用硬链接做那份副本**
   对同一个文件，硬链接会让读取器抛 `string query: managed exception`，真实复制则正常。所以副本是实打实的复制（几十 MB，读完即删）。

3. **`tempfile.mkdtemp` 在 Windows 上会建限制性 ACL 的目录**
   用它建目录后，后续写入会被拒绝（连 `icacls` 都改不动）。改用普通 `os.makedirs`。

4. **ms level 可能标错**
   遇到过单级质谱数据被整批标成 MS level 2 的文件。如果严格只取 MS1 会得到空结果，所以默认 `auto` 会自动降级为使用全部谱。

5. **内存占用**
   pyopenms 会把整个文件读进内存，100 MB 的 `.raw` 大致需要 0.5–1 GB。批量跑大文件时注意。

## 环境

| 组件 | 版本 |
|---|---|
| Python | 3.11+（实测 3.14.3） |
| pyopenms | 3.6.0 |
| .NET | 8.0+ |
| numpy / matplotlib | 任意近期版本 |
