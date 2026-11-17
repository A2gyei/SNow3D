# 实现与验收记录

## ent / 次生释放验证（JJS-R01）

`scripts/simulate.py` 现在默认按 `ent` 求解：`Inputs/ENT` 为整山可卷入雪层（默认 0.35 m），
`Inputs/SECREL` 为其余 7 个高危区（默认 0.65 m），`secRelArea=True`。

| 项 | null（旧默认） | ent（当前默认） |
| --- | --- | --- |
| 初始质量 | 42.52 Mt | 42.52 Mt |
| 结束质量 | 42.52 Mt | **50.60 Mt** |
| 质量增长 | 0 % | **+19.0 %** |
| 卷入证据 | `entrained=0` 全程 | 峰值卷入速率 36.5 t/s @ 13.5 s |
| 次生释放 | — | **0 次触发** |

结论：

1. **沿途卷入已通过真实求解验证**：整山 0.35 m 可动雪层让雪崩质量沿途增加 19%，质量曲线单调上升。
2. **次生释放一次都没触发**，不是配置问题（`secRelArea=True`、7 个次生区 0.65 m、`secondaryRelThId=0|1|…|6` 全部读入），
   而是几何问题：AvaFrame 的判据是 `(SECREL 栅格>0) & (FT>0)`，而 R01 的雪流**从未进入**其他任何候选区。
   用 100 m / 300 m 缓冲区逐帧测 ρhv² 也是 **0**：

   | 区域 | 区内被击中 | 100 m 内 max ρhv² | 300 m 内 max ρhv² |
   | --- | --- | --- | --- |
   | JJS-R01（本体） | 是 | 186 410 Pa | 186 410 Pa |
   | JJS-R02 – R08 | 否 | 0 | 0 |

3. 换句话：现有 8 个候选彼此**不在同一条 falls line 的上下游**，是高危坡面的平行集合。
   只要候选区不变，无论 AvaFrame 的接触判据还是我们自己的 ρhv² 判据，都不会产生次生释放。

### 下一步的二选一

- **A. 给筛查加"下游相邻"类候选**：把"雪流走廊两侧 300 m 内坡度 ≥28° 的坡面"也纳入候选，
  这类区域才会被经过的雪流加载。当前走廊里陡坡全部集中在起崩区附近（到达时间 <10 s），
  下游是缓和跑出区，所以需要换主起崩区或换地形范围才能形成真正的链。
- **B. 连通性扫描**：对 R02–R08 各跑一次 `null`，逐帧检查是否进入其他候选多边形。
  7 次求解约 8 分钟，能把"这座山上到底有没有相互连通的区"这件事一次性说清。

### 连通性扫描(8 区,56 个有序组合)

每个候选区单独跑一次 `null`(仅释放、无卷入、无次生),逐帧检查是否进入其他候选多边形。3.3 分钟跑完:

| 源区 | 跑出面积 | 峰值流深 | 最近的三个目标(距离) |
| --- | --- | --- | --- |
| JJS-R01 | 67.4 ha | 4.77 m | R06 450 m, R02 853 m, R04 1020 m |
| JJS-R02 | 38.2 ha | 5.50 m | R06 655 m, R01 860 m, R04 1931 m |
| JJS-R03 | 18.4 ha | 3.03 m | R02 3503 m(与其余全部 >3.5 km) |
| JJS-R04 | 28.4 ha | 4.97 m | R05 510 m, R01 1148 m |
| JJS-R05 | 23.4 ha | 4.07 m | R04 541 m, R07 1499 m |
| JJS-R06 | 7.1 ha | 2.61 m | R01 480 m, R02 641 m |
| JJS-R07 | 12.7 ha | 2.01 m | R04 1337 m, R05 1339 m |
| JJS-R08 | 12.8 ha | 2.90 m | R02 2300 m |

**连通对数 = 0 / 56。** 最近的一对是 R01 ↔ R06,约 450 m。这组候选是彼此平行的独立 falls line,
不存在"上游雪流冲进下游起崩区"的关系。

### 但是:每个区 30 m 内就有未被覆盖的陡坡

| 源区 | 100 m 内陡坡格子 | 200 m | 400 m | 最大连通陡坡块 |
| --- | --- | --- | --- | --- |
| JJS-R01 | 46 | 72 | 171 | 1.89 ha @ 30 m |
| JJS-R02 | 39 | 75 | 186 | 6.84 ha @ 30 m |
| JJS-R05 | 36 | 81 | 127 | 4.59 ha @ 30 m |
| JJS-R06 | 40 | 87 | 170 | 6.21 ha @ 30 m |

所以"坡脚被加载后失稳"的次生区是存在的:把陡坡块朝跑出区扩一格,它就能与雪流相接(实测 R01 那块 36 格全部与雪流足迹重叠)。

### AvaFrame 的次生释放路径在本机安装里没有初始化

按上述构造做了 `jjs_chain_r01_v1`(主雪崩 `ent` + 坡脚被加载的 0.65 m 陡坡 36 格):

- 配置全部被接受:`secRelArea=True`、`secondaryRelThFromFile=True`,日志也打印
  `release volume is: 202263.53 m3 and secondary release volume is: 24009.35 m3`;
- 但**从未出现** `Initializing secondary release area:`(该行在 `initializeSecRelease` 内部),
  质量曲线也无任何跳变,`initialized` 列全程为 0。

也就是说 `initializeSecRelease` 没有进入(其守卫是 `entResInfo["flagSecondaryRelease"]=="Yes"`),
`releaseSecRelArea` 因此从未被调用。这是 AvaFrame 侧的行为问题,不是我们配置错误——
`jjs_ent_v1` 里 7 个次生区同样沉默。

**结论:不要把"连锁"押在 AvaFrame 的 secondary release 上,改用我们自己的控制器**:
主雪崩跑到坡脚接触该陡坡的时刻 t_c,用 ρhv² 判定是否够加载,再以该陡坡为释放区单独跑一次,
把第二条时序按 t_c 平移后与主雪崩合并。这条路径不依赖 AvaFrame 的次生释放实现。

## 本轮改造:从合成地形换成将军山

| 项 | 结果 |
| --- | --- |
| 地形源 | `data/source/jiangjunshan/heights.f32`（684×540，1 弧秒 GLO-30，EGM2008→WGS84） |
| 计算网格 | UTM 45N（EPSG:32645）30 m，481×561 格点，双线性重采样，高程未缩放 |
| 显示网格 | 每 3 格取 1，161×188 顶点，首末行列保留 |
| 纵向增强 | 默认 1.5×，滑块 1–3×；只作用于着色器，地形/雪体同一系数，法线同步修正 |
| 原始高程 | 打包的 `terrain.bin` z、坡度/凸度分析、com1DFA 求解、流深/流速指标全部为原始米制高程 |
| 起崩区筛查 | 8 个候选，坡度(37°高斯) 0.75 + 凸度 0.15 + 相对高程 0.10，阈值 0.62，最小面积 3000 m² |
| 最高危候选 | JJS-R01：17.5 ha，平均坡度 33.36°，峰值坡度 39.4°，得分 0.883 |
| 默认情景 | 用 JJS-R01、1.0 m 释放厚度真实求解并发布为 `jiangjunshan_r01`（`default: true`） |
| 换区情景 | 界面选 JJS-R03、1.2 m 后提交，17.6 s 求解完成并自动载入（11 帧 / 19.8 s / 峰值 3.33 m） |
| 求解耗时 | 43.6 s（com1DFA，`simTypeList=null`，30 m 网格，单核） |
| 时序 | 39 帧，模拟时长 74.9 s，每 2 s 一帧 |
| 峰值 | 流深 4.77 m，流速 21.70 m/s |
| 发布体积 | terrain 0.69 MB + frames 4.50 MB + snow 24.0 MB + albedo 0.22 MB |
| 地形贴图 | 冬季 Sentinel-2 裁剪图（1425×1668）与 30 m 网格按同一经纬范围线性对齐，朝向用 DEM 山影比对确认 |
| 雪粒子 | 484,716 个；整山每格 12 个，雪崩路径上的格 1,512 个；带 arrival / peakSpeed / column |
| 雪体渲染 | 流动区不再绘制网格面，改由粒子在流层厚度内填充（`column` 0→1），密度按流深加权 |
| 默认镜头 | 按 `flowBounds`（流深 > 0.05 m 的实际影响范围）取景，不再看整幅 14×17 km 裁切 |
| 保留的旧情景 | `baseline` / `resistance`（avaParabola 合成地形）仍可切换对照 |

### 两个必须处理的坑

1. **AvaFrame 默认 `meshCellSize=5`**：会把 30 m DEM 重网格成近千万格点，并让发布地形与计算结果错位。现在强制 `meshCellSize = DEM cellsize`。
2. **本机 PATH 里的 PostgreSQL PROJ 库过旧**：rasterio 解析 EPSG 时报 `DATABASE.LAYOUT.VERSION.MINOR = 2`。现在启动求解前清空 `PROJ_LIB / PROJ_DATA / GDAL_DATA`。

此外 `pack.py` 里局部变量 `terrain`（顶点数组）曾覆盖同名参数，导致 `terrain == "jiangjunshan"` 变成数组比较，已更名为 `vertex_data`。

### 视觉改造中修掉的三个缺陷

1. **粒子位置用了绝对 UTM 坐标**（忘了先加 `x0/y0` 再减场景中心），整团雪被放到 586 km 外，屏幕上什么也看不到。现已改为与 `terrain.bin` 相同的局部坐标，并加了回归测试（`test_cloud_shape_and_extent`）。
2. **合成地形没有影像**时 `u_albedo` 采样器绑定为 `null`，Cesium 渲染线程直接抛 `Cannot read properties of null (reading '_target')`，切回 `baseline` 就白屏。现补 1×1 兜底纹理。
3. **静态资源被浏览器缓存**，改完代码刷新看到的还是旧包。`server.py` 现在对包括 `/data/*` 在内的所有响应发 `Cache-Control: no-store`。

## 自动检查

```text
python -B -m unittest discover -s tests     → 14 项通过
node --check web/app.js                     → 通过
node scripts/browser-check.mjs              → 通过（含 baseline 回归）
$env:SNOWWORK_RUN_CHECK='1'; node scripts/browser-check.mjs → 通过
```

无头 Chrome 实测（`terrain` 为页面内读到的真实状态）：

```json
{"terrain":{"terrain":"jiangjunshan","cell":30,"vertical":1.5,"exaggeration":1.5,
 "frames":39,"zone":"JJS-R01","candidates":8,"zonesRendered":8,"gpu":true,
 "cesium":"1.135.0","primitives":3},
 "vertical":{"value":2.6,"badge":"纵向 2.6×","relief":955.979},
 "modes":{"speed":{"legend":"流速 / m·s⁻¹"},"depth":{"legend":"流深 / m"},"snow":{"legend":"流深 / m"}},
 "playing":{"playing":true,"time":2.43},
 "scrub":{"time":40,"depth":"3.47","speed":"11.50","frame":"第 21 / 39 帧 · 采样 40.0 s"},
 "selection":{"zone":"JJS-R03","select":"JJS-R03"},
 "legacy":{"scenario":"baseline","terrain":null,"vertical":1,"frames":79,"loaded":true},
 "restored":{"scenario":"jiangjunshan_r01","zone":"JJS-R01"},"errors":[]}
```

截图：`.cache/browser-check/jiangjunshan-oblique.png`、`jiangjunshan-top.png`，以及提交新情景后的 `jiangjunshan-new-run.png`。

## 仍需人工确认

1. 纵向增强 1.5× 时的地形观感是否合适（可在 1–3× 间调）。
2. 起崩区筛查的阈值/坡度区间是否符合预期；当前是地形敏感性口径，未标定雪况。
3. 若要接入 Sentinel-2 冬季影像（源数据已有 `sentinel2-winter.webp`）与雪道/设施要素，需要新增贴图层，本轮未做。
4. 摩擦参数沿用的是 AvaFrame 默认 samosAT 自动分档，未按将军山雪况标定。
