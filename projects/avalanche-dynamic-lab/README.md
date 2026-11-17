# 雪脉 3D · 将军山雪崩动态实验室

这是从 8765 本地工程中独立出来的将军山雪崩模拟项目。目录内包含前端、AvaFrame 接入脚本、地形敏感性分析结果、将军山原始高程输入，以及已计算的 `jiangjunshan_r01` 浏览器数据。

## 当前模型

- 地形：Copernicus DEM GLO-30 将军山裁剪区，计算坐标系 EPSG:32645，网格 30 m。
- 起崩区：由坡度、局部凸度和相对高程在建模阶段自动筛选，当前选择排名第一的 `JJS-R01`。
- 物理模型：AvaFrame `com1DFA`，情景类型 `ent`。
- 全山雪层：0.35 m 可侵蚀雪层覆盖计算区域；其他 7 个高危区作为 secondary release 输入。
- 可视化：60 m 显示网格、连续雪体表面、424,566 个静态/流动/沉积状态粒子。

已发布的 R01 结果：模拟 74.7 s，最大流深 5.664 m，最大流速 21.634 m/s，影响面积 685,800 m²；侵蚀卷入质量约 8.081×10⁶ kg，总质量增长约 19.0%。这些参数尚未结合现场雪况标定，不构成业务预警。

## 本地启动

```powershell
npm install
./start.ps1
```

浏览器访问 <http://127.0.0.1:8765/>。若 AvaFrame Python 不在默认位置：

```powershell
$env:AVAFRAME_PYTHON = 'C:\path\to\avaframe\python.exe'
./start.ps1
```

## 重新计算 R01

```powershell
& $env:AVAFRAME_PYTHON -B scripts/release_zones.py
& $env:AVAFRAME_PYTHON -B scripts/simulate.py `
  --id jiangjunshan_r01_new `
  --terrain jiangjunshan `
  --zone JJS-R01 `
  --scenario ent `
  --thickness 1.0 `
  --snow-depth 0.35 `
  --secondary-depth 0.65
```

为避免覆盖已发布结果，重算时先使用新的 `--id`。确认结果后再决定是否替换 `data/public/jiangjunshan_r01`。

## 目录

- `web/`：CesiumJS 三维地形、雪体和粒子状态机。
- `scripts/release_zones.py`：地形高危区自动筛选。
- `scripts/simulate.py`：生成 REL、ENT、SECREL 输入并调用 AvaFrame。
- `scripts/pack.py`：将 AvaFrame 时序栅格打包为浏览器数据。
- `data/source/jiangjunshan/`：高程与冬季影像输入。
- `data/analysis/jiangjunshan/`：地形风险栅格和候选区。
- `data/public/jiangjunshan_r01/`：已验证的 R01 时序数据。

## 检查

```powershell
npm run check
python -m unittest discover -s tests -v
```
