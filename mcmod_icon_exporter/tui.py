#!/usr/bin/env python3
"""Interactive TUI for offline mcmod.cn icon export."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .render import (
    AssetResolver,
    OfflineRenderConfig,
    discover_source_asset_roots,
    extract_archive_assets,
    run_offline_job,
    scan_offline_catalog,
)


app = typer.Typer(add_completion=False, help="离线导出 MCMod 图标和条目清单。")
console = Console()


@app.command()
def main(
    source: Optional[Path] = typer.Option(None, "--source", "-s", help="模组源码目录或 assets 目录。"),
    jar: Optional[Path] = typer.Option(None, "--jar", "-j", help="构建后的模组 JAR/ZIP。"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="输出目录。"),
) -> None:
    console.rule("[bold cyan]离线 MCMod 图标导出")
    with tempfile.TemporaryDirectory(prefix="mcmod-icons-") as temp_dir:
        temp_root = Path(temp_dir)
        source, jar = choose_input(source, jar)
        roots = discover_roots(source, jar, temp_root)
        if not roots:
            console.print("[red]没有发现可用的 assets 目录。[/red]")
            raise typer.Exit(2)

        roots = edit_roots(roots)
        resolver = AssetResolver(roots)
        namespaces = resolver.iter_namespaces()
        if not namespaces:
            console.print("[red]assets 中没有发现命名空间。[/red]")
            raise typer.Exit(2)

        selected_namespaces = choose_namespaces(namespaces)
        entries = scan_offline_catalog(resolver, selected_namespaces)
        show_scan_summary(selected_namespaces, roots, entries)

        include = choose_include()
        sizes = choose_sizes()
        selected_entries = filter_entries(entries, include)
        if not selected_entries:
            console.print("[red]当前范围下没有可导出的条目。[/red]")
            raise typer.Exit(2)

        output_dir = out or choose_output_dir(source or jar, selected_namespaces)
        report_path = output_dir / "report.json"
        catalog_jsonl = output_dir / "catalog.jsonl"
        catalog_csv = output_dir / "catalog.csv"
        show_confirmation(selected_namespaces, roots, include, sizes, output_dir, len(selected_entries))
        if not Confirm.ask("开始导出？", default=True):
            raise typer.Exit(0)

        config = OfflineRenderConfig(
            assets_roots=roots,
            output_dir=output_dir,
            sizes=sizes,
            namespaces=selected_namespaces,
            include=include,
            report_path=report_path,
            catalog_jsonl_path=catalog_jsonl,
            catalog_csv_path=catalog_csv,
        )
        results = run_with_progress(config, len(selected_entries))
        show_results(results, output_dir, report_path, catalog_jsonl, catalog_csv)
        if Confirm.ask("打开输出目录？", default=False):
            open_directory(output_dir)


def choose_input(source: Optional[Path], jar: Optional[Path]) -> tuple[Optional[Path], Optional[Path]]:
    if source and jar:
        console.print("[yellow]同时传入了 source 和 jar，将合并两者 assets。[/yellow]")
        return source.resolve(), jar.resolve()
    if source or jar:
        return source.resolve() if source else None, jar.resolve() if jar else None

    choice = Prompt.ask("选择输入类型", choices=["source", "jar"], default="source")
    if choice == "source":
        value = Prompt.ask("输入模组源码目录 / assets 目录路径", default=str(Path.cwd()))
        return Path(value).expanduser().resolve(), None
    value = Prompt.ask("输入构建后的 JAR/ZIP 路径")
    return None, Path(value).expanduser().resolve()


def discover_roots(source: Optional[Path], jar: Optional[Path], temp_root: Path) -> list[Path]:
    roots: list[Path] = []
    if source:
        roots.extend(discover_source_asset_roots(source))
    if jar:
        roots.append(extract_archive_assets(jar, temp_root))
    return [root for root in roots if root.exists()]


def edit_roots(roots: list[Path]) -> list[Path]:
    while True:
        table = Table(title="已发现 assets roots")
        table.add_column("#", justify="right")
        table.add_column("路径")
        for index, root in enumerate(roots, 1):
            table.add_row(str(index), str(root))
        console.print(table)
        action = Prompt.ask("assets roots", choices=["ok", "add", "remove"], default="ok")
        if action == "ok":
            return roots
        if action == "add":
            value = Prompt.ask("输入额外 assets root")
            path = Path(value).expanduser().resolve()
            if path.exists():
                roots.append(path)
            else:
                console.print(f"[red]路径不存在：{path}[/red]")
        if action == "remove" and roots:
            value = Prompt.ask("输入要移除的编号", default=str(len(roots)))
            if value.isdigit() and 1 <= int(value) <= len(roots):
                roots.pop(int(value) - 1)


def choose_namespaces(namespaces: list[str]) -> list[str]:
    table = Table(title="发现的命名空间")
    table.add_column("#", justify="right")
    table.add_column("modid")
    for index, namespace in enumerate(namespaces, 1):
        table.add_row(str(index), namespace)
    console.print(table)
    default = ",".join(namespaces)
    value = Prompt.ask("选择 modid，逗号分隔，输入 all 表示全部", default=default)
    if value.strip().lower() == "all":
        return namespaces
    chosen = [part.strip() for part in value.split(",") if part.strip()]
    return [namespace for namespace in chosen if namespace in namespaces]


def show_scan_summary(namespaces: list[str], roots: list[Path], entries: list) -> None:
    counts = Counter(entry.type for entry in entries)
    table = Table(title="扫描结果")
    table.add_column("项目")
    table.add_column("值")
    table.add_row("modid", ", ".join(namespaces))
    table.add_row("assets roots", str(len(roots)))
    table.add_row("Block", str(counts.get("Block", 0)))
    table.add_row("Item", str(counts.get("Item", 0)))
    table.add_row("Total", str(len(entries)))
    console.print(table)


def choose_include() -> str:
    return Prompt.ask("导出范围", choices=["all", "blocks", "items"], default="all")


def choose_sizes() -> list[int]:
    choice = Prompt.ask("输出尺寸", choices=["mcmod", "effect", "custom"], default="mcmod")
    if choice == "effect":
        return [36, 144]
    if choice == "custom":
        value = Prompt.ask("输入尺寸，空格分隔", default="32 128")
        sizes = [int(part) for part in value.split() if part.isdigit()]
        return sizes or [32, 128]
    return [32, 128]


def choose_output_dir(input_path: Optional[Path], namespaces: list[str]) -> Path:
    base = Path(__file__).resolve().parents[1]
    slug = namespaces[0] if len(namespaces) == 1 else (input_path.stem if input_path else "offline-export")
    default = base / slug / "generated-icons-offline"
    value = Prompt.ask("输出目录", default=str(default))
    return Path(value).expanduser().resolve()


def filter_entries(entries: list, include: str) -> list:
    if include == "blocks":
        return [entry for entry in entries if entry.type == "Block"]
    if include == "items":
        return [entry for entry in entries if entry.type == "Item"]
    return entries


def show_confirmation(
    namespaces: list[str],
    roots: list[Path],
    include: str,
    sizes: list[int],
    output_dir: Path,
    count: int,
) -> None:
    table = Table(title="确认导出参数")
    table.add_column("参数")
    table.add_column("值")
    table.add_row("modid", ", ".join(namespaces))
    table.add_row("assets roots", str(len(roots)))
    table.add_row("范围", include)
    table.add_row("尺寸", " ".join(str(size) for size in sizes))
    table.add_row("条目数", str(count))
    table.add_row("输出目录", str(output_dir))
    table.add_row("报告", str(output_dir / "report.json"))
    table.add_row("清单", f"{output_dir / 'catalog.jsonl'} / {output_dir / 'catalog.csv'}")
    console.print(table)
    console.print("[yellow]同名 PNG、report 和 catalog 会被覆盖。[/yellow]")


def run_with_progress(config: OfflineRenderConfig, total: int):
    results = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("导出中", total=total)

        def on_result(result, index: int, _total: int) -> None:
            results.append(result)
            progress.update(task, advance=1, description=f"{result.status} {result.registry_name}")

        run_offline_job(config, on_result=on_result)
    return results


def show_results(results: list, output_dir: Path, report_path: Path, catalog_jsonl: Path, catalog_csv: Path) -> None:
    counts = Counter(result.status for result in results)
    table = Table(title="导出完成")
    table.add_column("状态")
    table.add_column("数量", justify="right")
    for status in sorted(counts):
        table.add_row(status, str(counts[status]))
    table.add_row("Total", str(len(results)))
    console.print(table)
    console.print(f"输出目录：{output_dir}")
    console.print(f"报告：{report_path}")
    console.print(f"清单：{catalog_jsonl}")
    console.print(f"CSV：{catalog_csv}")
    failed = [result for result in results if result.status == "failed"]
    if failed:
        fail_table = Table(title="失败条目")
        fail_table.add_column("registryName")
        fail_table.add_column("原因")
        for result in failed[:30]:
            fail_table.add_row(result.registry_name, result.message)
        console.print(fail_table)
        if len(failed) > 30:
            console.print(f"[yellow]还有 {len(failed) - 30} 个失败项，完整信息见 report.json。[/yellow]")


def open_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def cli() -> None:
    app()


if __name__ == "__main__":
    cli()
