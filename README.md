# MCMod Offline Icon Exporter

[中文文档](README_zh.md)

Offline icon exporter for Minecraft mod assets.

It scans `assets/<modid>/` from a mod source tree or a built JAR/ZIP and exports icons useful for MCMod documentation work, without entering the game and without using LetMeSeeSee.

## What It Exports

- Block icons rendered from blockstates, block models, textures, and common vanilla parent models
- Item icons rendered from common item models and item textures
- `32x32` and `128x128` PNG icons by default
- `report.json`
- `catalog.jsonl`
- `catalog.csv`

## Important: Catalog Output Is Not Complete Runtime Data

`catalog.jsonl` and `catalog.csv` are offline helper files. They only contain information that can be inferred from static assets, such as names, registry names, types, icon paths, and render status.

They do not contain complete runtime data, including:

- max stack size
- durability
- creative tab
- tags / oredict data
- runtime registrations
- dynamic item renderer output

Do not upload the offline `catalog.jsonl` / `catalog.csv` directly to mcmod.cn. Do not treat them as complete LetMeSeeSee exports. Use them only as local drafting, checking, and asset-management helpers.

## Install

```bash
cd /path/to/mcmod-offline-icon-exporter
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

On Windows PowerShell:

```powershell
cd /path/to/mcmod-offline-icon-exporter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

## TUI

```bash
mcmod-icon-exporter
```

Without installation:

```bash
python -m mcmod_icon_exporter.tui
```

The TUI guides you through:

1. choosing a source directory or JAR/ZIP
2. confirming discovered `assets` roots
3. selecting modid
4. choosing export scope: all, blocks, or items
5. choosing icon sizes
6. choosing output directory

## CLI Examples

Export from a source tree:

```bash
mcmod-icon-exporter-cli \
  --source /path/to/mod-project \
  --modid examplemod \
  --out /path/to/output \
  --sizes 32 128
```

Export from a source tree with dependency assets:

```bash
mcmod-icon-exporter-cli \
  --source /path/to/mod-project \
  --assets-root /path/to/dependency/src/main/resources/assets \
  --assets-root /path/to/dependency/src/generated/resources/assets \
  --modid examplemod \
  --out /path/to/output \
  --sizes 32 128
```

Export from a built JAR:

```bash
mcmod-icon-exporter-cli \
  --jar /path/to/mod.jar \
  --modid examplemod \
  --out /path/to/output \
  --sizes 32 128
```

Legacy LetMeSeeSee JSONL mode is kept only for comparison and migration checks:

```bash
mcmod-icon-exporter-cli \
  --input /path/to/let_me_see_see/examplemod.json \
  --assets-root /path/to/mod-project/src/main/resources/assets \
  --assets-root /path/to/mod-project/src/generated/assets \
  --namespace examplemod \
  --out /path/to/output \
  --fallback-icons
```

## Output Layout

```text
/path/to/output/
  report.json
  catalog.jsonl
  catalog.csv
  <modid>/
    <registry_path>_32x.png
    <registry_path>_128x.png
```

`report.json` records each entry as:

- `rendered`: icon generated offline
- `failed`: static assets were not enough to render it

## Current Coverage

Blocks:

- blockstate variants and multipart default state
- common vanilla parents: `cube_all`, `cube_column`, `slab`, `stairs`, `cross`, `lantern`, `torch`, `wall`, `trapdoor`
- custom cuboid elements, UVs, and element rotations
- particle/still texture fallback for fluid-like blocks

Items:

- `minecraft:item/generated`
- `minecraft:item/handheld`
- layered item textures, such as `layer0`, `layer1`, ...
- item models that point to block models
- direct `textures/item/<id>.png`

Known limitations:

- spawn eggs and other dynamic item templates
- custom runtime item renderers
- model behavior that requires Minecraft's full bake/render pipeline

Failed entries are listed in `report.json` for manual handling.
