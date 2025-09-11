from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from .config import load_config, ConfigError

console = Console()


def timestamp() -> str:
	return datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_config(config_path: Path) -> Path:
	if not config_path.exists():
		raise FileNotFoundError(f"配置文件不存在: {config_path}")
	bak = config_path.with_suffix(config_path.suffix + f".{timestamp()}.bak")
	shutil.copy2(config_path, bak)
	return bak


def restore_config(backup_path: Path, dest_path: Optional[Path] = None) -> Path:
	if not backup_path.exists():
		raise FileNotFoundError(f"备份文件不存在: {backup_path}")
	target = dest_path or backup_path.with_suffix("")
	shutil.copy2(backup_path, target)
	return target


def validate_config(config_path: Path) -> bool:
	try:
		cfg = load_config(config_path)
	except ConfigError as e:
		console.print(f"[red]配置无效:[/red] {e}")
		return False
	table = Table(title="配置校验结果")
	table.add_column("项")
	table.add_column("结果")
	watch_ok = bool(cfg.watch and cfg.watch.mappings)
	table.add_row("watch.mappings", "OK" if watch_ok else "缺失")
	if cfg.watch:
		table.add_row("include_patterns", ", ".join(cfg.watch.include_patterns))
		table.add_row("exclude_patterns", ", ".join(cfg.watch.exclude_patterns))
		auto = cfg.watch.auto_organize
		table.add_row("auto_organize", "启用" if (auto and auto.enabled) else "未启用")
	org_ok = bool(cfg.organize)
	table.add_row("organize", "已配置" if org_ok else "未配置")
	console.print(table)
	return watch_ok
