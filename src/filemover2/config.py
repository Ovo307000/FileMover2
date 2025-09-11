from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class WatchMapping:
	source: Path
	targets: List[Path]


@dataclass
class AutoOrganizeConfig:
	enabled: bool = False
	threshold: int = 1000
	cooldown_s: int = 60
	prefix: Optional[str] = None


@dataclass
class WatchConfig:
	mappings: List[WatchMapping] = field(default_factory=list)
	include_patterns: List[str] = field(default_factory=lambda: ["*"])
	exclude_patterns: List[str] = field(default_factory=list)
	ignore_dotfiles: bool = True
	debounce_ms: int = 200
	auto_organize: Optional[AutoOrganizeConfig] = None
	# New options
	interested_events: List[str] = field(default_factory=lambda: ["created", "modified", "moved", "deleted"])
	worker_threads: int = 4
	io_retries: int = 3
	io_backoff_ms: int = 200
	verify_mode: str = "size"  # one of: "size", "none"
	keep_source: bool = False   # mirror by copy (no delete)
	dedup_strategy: str = "rename"  # one of: skip, rename, overwrite
	use_async: bool = False

	@property
	def use_mappings(self) -> bool:
		return len(self.mappings) > 0


@dataclass
class OrganizeConfig:
	target_dirs: List[Path]
	chunk_size: int = 1000
	prefix: Optional[str] = None
	name_template: Optional[str] = None
	timestamp_format: str = "%Y%m%d_%H%M%S"
	# 新增：区间命名与填充旧分区
	range_naming: bool = True               # 使用 最旧-最新-分片 命名
	range_date_format: str = "%Y%m%d"      # 区间命名的日期格式
	fill_previous: bool = True              # 若存在同区间且未满，优先填充
	dry_run: bool = False
	keep_source: bool = False
	dedup_strategy: str = "rename"  # one of: skip, rename, overwrite
	use_async: bool = False


@dataclass
class AppConfig:
	watch: Optional[WatchConfig] = None
	organize: Optional[OrganizeConfig] = None


class ConfigError(Exception):
	pass


def _coerce_path(value: str | os.PathLike[str]) -> Path:
	p = Path(str(value).replace("\\", "/")).expanduser()
	return p


def _load_raw_config(path: Path) -> dict:
	text = path.read_text(encoding="utf-8")
	if path.suffix.lower() in {".yaml", ".yml"}:
		return yaml.safe_load(text) or {}
	elif path.suffix.lower() == ".json":
		return json.loads(text)
	else:
		raise ConfigError(f"不支持的配置文件格式: {path.suffix}")


def _parse_auto_organize(cfg: dict | None) -> Optional[AutoOrganizeConfig]:
	if not cfg:
		return None
	enabled = bool(cfg.get("enabled", False))
	threshold = int(cfg.get("threshold", 1000))
	cooldown_s = int(cfg.get("cooldown_s", 60))
	prefix = cfg.get("prefix")
	return AutoOrganizeConfig(enabled=enabled, threshold=threshold, cooldown_s=cooldown_s, prefix=prefix)


def _parse_watch(cfg: dict) -> Optional[WatchConfig]:
	if not cfg:
		return None

	if not isinstance(cfg.get("mappings"), list) or not cfg.get("mappings"):
		raise ConfigError("watch.mappings 必须是非空数组（source + targets[]）")

	mappings: List[WatchMapping] = []
	for m in cfg["mappings"]:
		if not isinstance(m, dict) or "source" not in m:
			raise ConfigError("watch.mappings[*].source 必填")
		src = _coerce_path(m["source"]) 
		targets_raw = m.get("targets")
		if not isinstance(targets_raw, list) or not targets_raw:
			raise ConfigError("watch.mappings[*].targets 必须是非空数组")
		targets = [_coerce_path(t) for t in targets_raw]
		mappings.append(WatchMapping(source=src, targets=targets))

	include_patterns = cfg.get("include_patterns") or ["*"]
	exclude_patterns = cfg.get("exclude_patterns") or []
	ignore_dotfiles = bool(cfg.get("ignore_dotfiles", True))
	debounce_ms = int(cfg.get("debounce_ms", 200))
	auto_organize = _parse_auto_organize(cfg.get("auto_organize"))
	worker_threads = int(cfg.get("worker_threads", 4))
	io_retries = int(cfg.get("io_retries", 3))
	io_backoff_ms = int(cfg.get("io_backoff_ms", 200))
	verify_mode = str(cfg.get("verify_mode", "size")).lower()
	if verify_mode not in {"size", "none"}:
		raise ConfigError("watch.verify_mode 仅支持 'size' 或 'none'")
	keep_source = bool(cfg.get("keep_source", False))
	dedup_strategy = str(cfg.get("dedup_strategy", "rename")).lower()
	if dedup_strategy not in {"skip", "rename", "overwrite"}:
		raise ConfigError("watch.dedup_strategy 仅支持 'skip' | 'rename' | 'overwrite'")
	use_async = bool(cfg.get("use_async", False))
	interested_events = cfg.get("interested_events") or ["created", "modified", "moved", "deleted"]
	if not isinstance(interested_events, list) or not all(isinstance(e, str) for e in interested_events):
		raise ConfigError("watch.interested_events 必须是字符串数组")
	valid_events = {"created", "modified", "moved", "deleted"}
	for e in interested_events:
		if e not in valid_events:
			raise ConfigError("watch.interested_events 仅支持: created/modified/moved/deleted")
	return WatchConfig(
		mappings=mappings,
		include_patterns=include_patterns,
		exclude_patterns=exclude_patterns,
		ignore_dotfiles=ignore_dotfiles,
		debounce_ms=debounce_ms,
		auto_organize=auto_organize,
		interested_events=interested_events,
		worker_threads=worker_threads,
		io_retries=io_retries,
		io_backoff_ms=io_backoff_ms,
		verify_mode=verify_mode,
		keep_source=keep_source,
		dedup_strategy=dedup_strategy,
		use_async=use_async,
	)


def _parse_organize(cfg: dict) -> Optional[OrganizeConfig]:
	if not cfg:
		return None
	target_dirs_raw = cfg.get("target_dirs")
	if not isinstance(target_dirs_raw, list) or not target_dirs_raw:
		raise ConfigError("organize.target_dirs 必须是非空数组")
	target_dirs = [_coerce_path(p) for p in target_dirs_raw]
	chunk_size = int(cfg.get("chunk_size", 1000))
	prefix = cfg.get("prefix")
	name_template = cfg.get("name_template")
	timestamp_format = str(cfg.get("timestamp_format", "%Y%m%d_%H%M%S"))
	range_naming = bool(cfg.get("range_naming", True))
	range_date_format = str(cfg.get("range_date_format", "%Y%m%d"))
	fill_previous = bool(cfg.get("fill_previous", True))
	dry_run = bool(cfg.get("dry_run", False))
	keep_source = bool(cfg.get("keep_source", False))
	dedup_strategy = str(cfg.get("dedup_strategy", "rename")).lower()
	if dedup_strategy not in {"skip", "rename", "overwrite"}:
		raise ConfigError("organize.dedup_strategy 仅支持 'skip' | 'rename' | 'overwrite'")
	use_async = bool(cfg.get("use_async", False))
	return OrganizeConfig(
		target_dirs=target_dirs,
		chunk_size=chunk_size,
		prefix=prefix,
		name_template=name_template,
		timestamp_format=timestamp_format,
		range_naming=range_naming,
		range_date_format=range_date_format,
		fill_previous=fill_previous,
		dry_run=dry_run,
		keep_source=keep_source,
		dedup_strategy=dedup_strategy,
		use_async=use_async,
	)


def load_config(path: str | os.PathLike[str]) -> AppConfig:
	cfg_path = _coerce_path(path)
	if not cfg_path.exists():
		raise ConfigError(f"配置文件不存在: {cfg_path}")
	raw = _load_raw_config(cfg_path)
	watch_cfg = _parse_watch(raw.get("watch")) if raw.get("watch") else None
	organize_cfg = _parse_organize(raw.get("organize")) if raw.get("organize") else None
	if not watch_cfg and not organize_cfg:
		raise ConfigError("配置中至少需要提供 watch 或 organize 块")
	return AppConfig(watch=watch_cfg, organize=organize_cfg)
