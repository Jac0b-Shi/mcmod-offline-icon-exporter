# MCMod 离线图标导出工具

Minecraft 模组资产的离线图标导出工具。

无需进入游戏，也无需 LetMeSeeSee，直接扫描 `assets/<modid>/`（可来自模组源码目录或构建好的 JAR/ZIP），即可导出 MCMod 百科文档工作所需的图标。

## 导出内容

- 由 blockstates、方块模型、纹理和常见原版父模型渲染的**方块图标**
- 由常见物品模型和物品纹理渲染的**物品图标**
- 默认输出 `32x32` 和 `128x128` 的 PNG 图标
- `report.json` — 导出报告
- `catalog.jsonl` — 条目清单
- `catalog.csv` — CSV 格式清单

## 重要提示：Catalog 输出并非完整的运行时数据

`catalog.jsonl` 和 `catalog.csv` 是**离线辅助文件**，仅包含可从静态资产推断的信息，如名称、注册名、类型、图标路径和渲染状态。

它们**不包含**完整的运行时数据，包括：

- 最大堆叠数
- 耐久度
- 创造模式标签页
- 标签 / 矿物词典数据
- 运行时注册信息
- 动态物品渲染器输出

**请勿**将离线的 `catalog.jsonl` / `catalog.csv` 直接上传到 mcmod.cn。**请勿**将其视为完整的 LetMeSeeSee 导出结果。仅将它们用作本地草稿、检查和资产管理辅助工具。

## 安装

```bash
cd /path/to/mcmod-offline-icon-exporter
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell：

```powershell
cd /path/to/mcmod-offline-icon-exporter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

## 交互式界面（TUI）

```bash
mcmod-icon-exporter
```

不安装直接运行：

```bash
python -m mcmod_icon_exporter.tui
```

TUI 会引导你完成以下步骤：

1. 选择源码目录或 JAR/ZIP
2. 确认已发现的 `assets` 根目录
3. 选择 modid
4. 选择导出范围：全部、方块或物品
5. 选择图标尺寸
6. 选择输出目录

## 命令行示例

从源码目录导出：

```bash
mcmod-icon-exporter-cli \
  --source /path/to/mod-project \
  --modid examplemod \
  --out /path/to/output \
  --sizes 32 128
```

从源码目录导出（含依赖模组的 assets）：

```bash
mcmod-icon-exporter-cli \
  --source /path/to/mod-project \
  --assets-root /path/to/dependency/src/main/resources/assets \
  --assets-root /path/to/dependency/src/generated/resources/assets \
  --modid examplemod \
  --out /path/to/output \
  --sizes 32 128
```

从构建好的 JAR 导出：

```bash
mcmod-icon-exporter-cli \
  --jar /path/to/mod.jar \
  --modid examplemod \
  --out /path/to/output \
  --sizes 32 128
```

旧版 LetMeSeeSee JSONL 模式（仅用于对比和迁移验证）：

```bash
mcmod-icon-exporter-cli \
  --input /path/to/let_me_see_see/examplemod.json \
  --assets-root /path/to/mod-project/src/main/resources/assets \
  --assets-root /path/to/mod-project/src/generated/assets \
  --namespace examplemod \
  --out /path/to/output \
  --fallback-icons
```

## 输出结构

```text
/path/to/output/
  report.json
  catalog.jsonl
  catalog.csv
  <modid>/
    <registry_path>_32x.png
    <registry_path>_128x.png
```

`report.json` 记录每个条目的状态：

- `rendered`：图标已离线生成
- `failed`：静态资产不足以完成渲染

## 当前覆盖范围

方块：

- blockstate 变体（variants）和多方块（multipart）的默认状态
- 常见原版父模型：`cube_all`、`cube_column`、`slab`、`stairs`、`cross`、`lantern`、`torch`、`wall`、`trapdoor`
- 自定义长方体元素、UV 和元素旋转
- 流体类方块的 particle/still 纹理回退

物品：

- `minecraft:item/generated`
- `minecraft:item/handheld`
- 多层物品纹理，如 `layer0`、`layer1` 等
- 指向方块模型的物品模型
- 直接的 `textures/item/<id>.png`

已知限制：

- 刷怪蛋等动态物品模板
- 自定义运行时物品渲染器
- 需要 Minecraft 完整烘焙/渲染管线的模型行为

失败的条目会列在 `report.json` 中，供手动处理。

## 许可协议

GNU Lesser General Public License v3.0 - 详见 [LICENSE](LICENSE)

---

本 README 也有[英文版本](README.md)。
