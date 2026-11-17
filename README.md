# 雪脉 3D · 将军山雪崩动态实验室

基于 **AvaFrame com1DFA + CesiumJS** 的本地三维雪崩过程演示。计算情景为**阿勒泰将军山真实地形**，起崩区由系统根据地形自行筛查，再用真实物理求解器算出一条完整雪崩时序。所有源代码、计算目录和浏览器数据都放在本工程内，AvaFrame 使用已安装好的 `D:\AvaFrame_demo\.pixi\envs\default` 环境。

## 启动

双击 **start.cmd**，或在工程目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

打开 <http://127.0.0.1:8765>。启动窗口需保持打开；关闭窗口即停止服务。服务仅监听回环地址。

端口占用时可 `.\start.ps1 -Port 8766`。Cesium 1.135.0 的浏览器构建已在 `node_modules/cesium/Build/Cesium`，运行不依赖在线底图、Cesium ion 令牌或 CDN（缺失时会 `npm install` 补齐）。

### 指定 AvaFrame 的 Python

服务端与求解器都要用**带 AvaFrame 的解释器**，系统默认的 `python` 通常不含。`start.ps1` 按以下顺序解析，并会实际 `import avaframe` 验证：

1. 环境变量 `AVAFRAME_PYTHON`；
2. 开发机路径 `D:\AvaFrame_demo\.pixi\envs\default\python.exe`；
3. PATH 里能 `import avaframe` 的 `python` / `python3`。

换机器时设置一次即可：

```powershell
$env:AVAFRAME_PYTHON = 'C:\path\to\avaframe\envs\default\python.exe'
.\start.ps1
```

## 重建已发布情景

仓库只跟踪源码与 `data/source`（地形输入），**已发布的情景二进制不随仓库分发**——它们加起来约 110 MB，且完全可以重建。克隆后按顺序跑：

```powershell
# 1) 地形敏感性筛查 -> data/analysis/jiangjunshan/
& $env:AVAFRAME_PYTHON -B scripts/release_zones.py

# 2) 真实求解最高危起崩区（约 45 s），发布为默认情景
& $env:AVAFRAME_PYTHON -B scripts/simulate.py --id jiangjunshan_r01 --zone JJS-R01

# 3) 可选：重建两个合成地形对照情景
& $env:AVAFRAME_PYTHON -B scripts/simulate.py --id baseline --terrain parabola --scenario null
& $env:AVAFRAME_PYTHON -B scripts/simulate.py --id resistance --terrain parabola --scenario res
```

然后 `.\start.ps1`。生成的 `data/runs/`、`data/public/`、`data/analysis/` 都在 `.gitignore` 里，不会再被提交。

## 地形与坐标

- 源数据：`data/source/jiangjunshan/heights.f32`（684×540，1 弧秒 Copernicus GLO-30，EGM2008 已换算为 WGS84 椭球高）＋ `terrain.json`（经纬度范围）。
- 计算网格：重采样到 **UTM 45N（EPSG:32645）30 m 正方网格**，481×561 个格点，双线性插值，**不改变任何高程数值**，高程范围与源数据一致（692–1651 m）。
- 显示网格：原生 30 m 每 3 格取 1 格（约 90 m），首末行列保留边界，共 161×188 个顶点下发给浏览器。

### 纵向增强（显示）与原始高程（分析/计算）

- `manifest.json` 里的地形顶点 z 值就是 **原始物理高程（米）**；`verticalExaggeration` 字段只告诉浏览器怎么画。
- 前端在顶点着色器里把 `z` 乘以增强系数（默认 1.5×，滑块 1–3×），并同步修正法线，让光照与拉伸后的表面一致；雪体沿地形法向的位移也一起吃这个系数，所以雪面始终贴合地形。
- 坡度/凸度/起崩区筛查、com1DFA 求解、流深与流速指标全部使用原始高程，不受滑块影响。

## 起崩区识别（先筛查，再选一个高危区模拟）

`scripts/release_zones.py` 在建模阶段读取原始高程，输出 `data/analysis/jiangjunshan/release-zones.json`：

- 坡度评分：以 37° 为中心的高斯核，硬性限制 28–50°，权重 0.75；
- 局部凸度（与 5σ 平滑面之差的正值），权重 0.15；
- 相对高程分位，权重 0.10；
- 阈值 0.62 提取连通域，形态学开闭 + 填洞，最小面积 3000 m²；
- 仅在将军山雪场周边半径 4.5 km 内筛查，并剔除 120 m 边缘带（导数不完整会造出假陡坡）；
- 候选按 `0.55·平均分 + 0.30·峰值分 + 0.15·面积比` 排序，默认取 **rank 1（JJS-R01）**。

结果同时给出坡度栅格、风险栅格、高危掩膜。**这是静态地形敏感性筛查，不含积雪、风、温度、植被与现场观测，不能当预警用。**

选择逻辑：`/api/run` 不带 `zone` 时自动用最高危候选；带 `zone` 则用指定候选。前端列出全部候选，可点选并只对选中的区发起计算。

## 模拟

`scripts/simulate.py --terrain jiangjunshan --zone JJS-R01`：

1. 生成度量 DEM `Inputs/DEM_JJS_Topo.asc`；
2. 把候选区经纬度投影到 UTM，写成 `Inputs/REL/<zone>.shp`（带 thickness 字段）；
3. 调 com1DFA（`simTypeList=null`，`tSteps=0:2`，输出 `pft|pfv|ppr|FT|FV`，`relTh` 由界面给定 0.2–3 m）；
4. 用 `scripts/pack.py` 转成浏览器二进制。

两个关键设置：

- `meshCellSize` 强制等于 DEM 的 30 m。AvaFrame 默认会把 DEM 重网格到 5 m，那会把 481×561 撑到近千万格点，还会让发布地形与计算结果错位。
- 启动前清掉 `PROJ_LIB/PROJ_DATA/GDAL_DATA`：本机 PATH 里另有一套较旧的 PostgreSQL PROJ 数据库，会让 rasterio 解析 EPSG 失败。

一次实测算例（JJS-R01、1.0 m 释放厚度）：求解 43.6 s，模拟时长 74.9 s，39 帧（每 2 s 一帧），峰值流深 4.77 m，峰值流速 21.70 m/s。

## 视觉表现

- **地形贴图**：冬季 Sentinel-2（2026-02-01，1425×1668）裁剪图直接作为地形反照率，与 30 m 网格按同一经纬范围线性对齐；山脊、雪道与村落都来自真实影像，不是程序纹理。
- **整山雪粒子**：`snow.bin` 烘焙了 **484,716 个雪粒子**（整山每格 12 个，雪崩路径上的格 1,512 个），铺满整片将军山。每个粒子带有 `arrival`（雪崩首次到达该格的时间）、`peakSpeed`、以及它在流层厚度内的位置 `column`。
- **雪体本身也是粒子**：流动区的雪体不再绘制连续网格面（那张面只保留下来读取流深/流速纹理），改由粒子在整层厚度内填充——`column=0` 贴底、`column=1` 在表面。密度由流深加权，因此雪崩体看起来是"一团颗粒在冲"，而不是一张光滑曲面。
- **连锁扩散**：时间轴前进时，只有 `u_time ≥ arrival` 的粒子才算进入流场：起崩区先动，随后沿沟槽逐格扩散到北、东、南三支。
- **颗粒与雪雾**：约 78% 是细小雪粒（不透明、构成主体），22% 是宽而淡的雪雾粒子（透明度 0.18），叠在雪体上方。
- **雪体表面**：按流深做蓝→白渐变，叠加冰面高光、积雪颗粒噪声与前锋边缘增亮。
- 视角按钮：山谷视角 / **起崩区特写** / 俯视；`雪山雪粒子` 开关可整体关闭。

一次发布的体积：terrain 0.69 MB + frames 4.5 MB + snow 24.0 MB + albedo 0.22 MB。

## 显示与交互

- **雪体 / 流速 / 流深** 三种显示方式全部保留：雪体按流深着色，流速与流深各走一套色带，图例随模式切换。
- 纵向增强 1–3×（默认 1.5×），雪体厚度显示倍率 1–8×（默认 3×），二者独立；指标始终是物理原值。
- 播放/暂停/重播、拖动时间轴、0.5–4× 倍速；相邻物理帧之间在 GPU 上插值，指标取最近的实际采样帧。
- 山谷视角 / 俯视 / 重置，拖动旋转、滚轮缩放、右键平移；相机范围按地形跨度自适应。
- 候选起崩区虚线框：选中的高亮为金黄并带标签，其余淡色；「全部候选区」开关可一次性显示。
- 当前指标：最大流深、最大流速、覆盖面积（FT > 0.05 m 的水平投影面积）。

## 工程结构

```text
server.py                    本地静态资源、任务 API、/api/zones
start.cmd / start.ps1        启动入口
web/                         三维界面、GPU 网格渲染、时间轴、起崩区面板
scripts/jiangjunshan.py      原始 heights.f32 → 度量 DEM、投影换算
scripts/release_zones.py     起崩区筛查（坡度/凸度/相对高程）
scripts/simulate.py          将军山或 avaParabola 的独立计算任务
scripts/pack.py              原始时序栅格 → 浏览器二进制 + 清单
scripts/browser-check.mjs    无头 Chrome 端到端检查（可选用真实重算）
tests/                       数据完整性、起崩区、API 校验
data/source/jiangjunshan/    只读地形源数据
data/analysis/jiangjunshan/  筛查结果与风险栅格
data/runs/<id>/              每次计算的输入、配置、日志与原始输出
data/public/<id>/            地形、时序二进制和清单
data/jobs/<id>.json          后台任务状态
logs/                        后台计算进程日志
.cache/                      可重建的缓存与验收截图
```

## 验收

```powershell
& 'D:\AvaFrame_demo\.pixi\envs\default\python.exe' -B -m unittest discover -s tests -v
node --check web/app.js
node scripts/browser-check.mjs                    # 交互检查
$env:SNOWWORK_RUN_CHECK='1'; node scripts/browser-check.mjs   # 追加一次真实重算
```

`browser-check.mjs` 会验证：地形是将军山、纵向增强默认 1.5× 且滑块生效、三种显示方式可切换、播放推进、时间轴定位、切换候选起崩区。设置 `SNOWWORK_RUN_CHECK=1` 时会实际提交一个换区情景并等到计算完成。

## 限制

- 地形为 GLO-30（约 30 m），无法表达雪崩尺度的微地形；起崩区筛查是地形敏感性模型，未标定。
- 摩擦参数沿用 AvaFrame 默认 samosAT 自动取值，未按将军山雪况标定；结果不构成任何预警或决策依据。
- 目前只叠加地形与雪崩体，尚未接入 Sentinel-2 冬季影像、路网、设施与实时气象。
- 计划中的下一步：真实雪深/风场同化、多起崩区联合情景、粉雪云与地形阴影。

## 许可

CesiumJS 使用 Apache-2.0 许可；AvaFrame 使用 EUPL-1.2。第三方许可证随各自软件保留。
