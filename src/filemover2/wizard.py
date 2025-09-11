from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm, IntPrompt

from .guard import backup_config

console = Console()


def _norm(p: str) -> str:
	return p.replace("\\", "/").strip()


def _ask_paths(label: str) -> List[str]:
	console.print(Panel.fit(f"输入{label}，可一次输入多个，用分号 ; 分隔。例如: C:/A;D:/B", title=f"{label}", subtitle="留空结束"))
	out: List[str] = []
	while True:
		line = Prompt.ask(f"{label} (或直接回车结束)", default="")
		if not line:
			break
		parts = [_norm(p) for p in line.split(";") if p.strip()]
		out.extend(parts)
		console.print(f"已添加 {len(parts)} 条，总计 {len(out)}")
	return out


def _ask_mappings() -> list[dict]:
	console.rule("映射设置：一个 source -> 多个 targets")
	mappings: list[dict] = []
	while True:
		src = _norm(Prompt.ask("源目录 source (留空结束)", default=""))
		if not src:
			break
		tgts = _ask_paths("目标目录 targets")
		if not tgts:
			console.print("[red]至少需要 1 个 target[/red]")
			continue
		mappings.append({"source": src, "targets": tgts})
		console.print(f"[green]映射已添加[/green]: {src} -> {tgts}")
		if not Confirm.ask("继续添加映射吗?", default=True):
			break
	return mappings


def _ask_patterns() -> tuple[list[str], list[str]]:
	include_line = Prompt.ask("include_patterns (分号 ; 分隔，默认 *)", default="*")
	includes = [p.strip() for p in include_line.split(";") if p.strip()]
	exclude_line = Prompt.ask("exclude_patterns (分号 ; 分隔，可留空)", default="")
	excludes = [p.strip() for p in exclude_line.split(";") if p.strip()]
	return includes, excludes


def _ask_auto_organize() -> dict | None:
	if not Confirm.ask("启用自动整理 auto_organize?", default=False):
		return None
	threshold = IntPrompt.ask("阈值 threshold（达到多少文件触发）", default=1000)
	cooldown = IntPrompt.ask("冷却时间 cooldown_s（秒）", default=60)
	prefix = Prompt.ask("批次前缀 prefix (可留空)", default="")
	return {
		"enabled": True,
		"threshold": int(threshold),
		"cooldown_s": int(cooldown),
		"prefix": (prefix or None),
	}


def _ask_organize_block() -> dict | None:
	if not Confirm.ask("需要在配置中加入 organize 默认块吗?", default=True):
		return None
	target_dir = Prompt.ask("organize.target_dir（默认整理目录）", default="")
	if not target_dir:
		return None
	chunk_size = IntPrompt.ask("organize.chunk_size（每批文件数）", default=1000)
	prefix = Prompt.ask("organize.prefix（批次前缀，可留空）", default="")
	dry = Confirm.ask("organize.dry_run（仅演示不移动）?", default=False)
	return {
		"target_dir": target_dir,
		"chunk_size": int(chunk_size),
		"prefix": (prefix or None),
		"dry_run": bool(dry),
	}


def _preview_config(cfg: dict) -> None:
	table = Table(title="配置预览")
	table.add_column("字段")
	table.add_column("说明")
	table.add_column("值")
	watch = cfg.get("watch", {})
	mappings = watch.get("mappings", [])
	table.add_row("watch.mappings", "source 到 targets 的映射数组", str(len(mappings)))
	for i, m in enumerate(mappings, 1):
		table.add_row(f"  mappings[{i}].source", "源目录", m.get("source", ""))
		table.add_row(f"  mappings[{i}].targets", "目标目录列表", "; ".join(m.get("targets", [])))
	includes = watch.get("include_patterns", ["*"])
	excludes = watch.get("exclude_patterns", [])
	table.add_row("watch.include_patterns", "包含通配（列表）", ", ".join(includes))
	table.add_row("watch.exclude_patterns", "排除通配（列表）", ", ".join(excludes))
	table.add_row("watch.ignore_dotfiles", "忽略以 . 开头文件", str(watch.get("ignore_dotfiles", True)))
	table.add_row("watch.debounce_ms", "事件节流毫秒数", str(watch.get("debounce_ms", 200)))
	auto = watch.get("auto_organize")
	table.add_row("watch.auto_organize", "自动整理设置（可选）", "启用" if (auto and auto.get("enabled")) else "未启用")
	org = cfg.get("organize")
	table.add_row("organize", "默认整理块（可选）", "已配置" if org else "未配置")
	console.print(table)


def run_wizard(output_path: str | None) -> Path:
	console.rule("FileMover2 配置向导")
	console.print("[bold]说明：[/bold] 本向导将帮助你创建一个 YAML 配置，包括监听映射、过滤规则与可选的自动整理。")
	mappings = _ask_mappings()
	if not mappings:
		console.print("[red]至少需要一条映射！[/red]")
		sys.exit(2)
	includes, excludes = _ask_patterns()
	ignore = Confirm.ask("忽略以 . 开头的文件?", default=True)
	debounce = IntPrompt.ask("事件节流（毫秒）debounce_ms", default=200)
	auto = _ask_auto_organize()
	org = _ask_organize_block()

	cfg = {
		"watch": {
			"mappings": mappings,
			"include_patterns": includes or ["*"],
			"exclude_patterns": excludes or [],
			"ignore_dotfiles": bool(ignore),
			"debounce_ms": int(debounce),
			"auto_organize": auto,
		},
		"organize": org,
	}

	_preview_config(cfg)
	if not Confirm.ask("确认保存该配置?", default=True):
		console.print("已取消保存。")
		sys.exit(0)

	out = Path(output_path) if output_path else Path.cwd() / "filemover.yaml"
	if out.exists():
		bak = backup_config(out)
		console.print(Panel.fit(f"已备份旧配置为: [yellow]{bak}[/yellow]", title="备份"))
	out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
	console.print(Panel.fit(f"配置已保存: [green]{out}[/green]", title="完成"))
	return out
