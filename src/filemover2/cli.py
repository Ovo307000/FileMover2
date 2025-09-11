from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional, List
import asyncio
import os
import json
import sys

from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.console import Console

from .config import AppConfig, load_config, ConfigError
from .syncer import start_watching, SourceSpec, start_watching_async
from .organizer import organize_by_mtime_multi_async
from .logging_setup import setup_logging, get_log_file
from .wizard import run_wizard
from .guard import validate_config, backup_config, restore_config

console = Console()

ENV_KEY = "FILEMOVER2_CONFIG"

# 启动模式记忆（APPDATA 优先，退回用户目录）
STATE_DIR = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "FileMover2"
STATE_FILE = STATE_DIR / "state.json"


def _load_state() -> dict:
	try:
		if STATE_FILE.exists():
			return json.loads(STATE_FILE.read_text(encoding="utf-8"))
		return {}
	except Exception:
		return {}


def _save_state(data: dict) -> None:
	try:
		STATE_DIR.mkdir(parents=True, exist_ok=True)
		STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
	except Exception:
		pass


def _is_admin() -> bool:
	if os.name != "nt":
		return os.geteuid() == 0 if hasattr(os, "geteuid") else False
	try:
		import ctypes  # type: ignore
		return bool(ctypes.windll.shell32.IsUserAnAdmin())
	except Exception:
		return False


def _elevate_if_needed(enable_admin: bool) -> None:
	if not enable_admin:
		return
	if _is_admin():
		return
	if os.name == "nt":
		# Windows 以管理员重启自身
		import ctypes  # type: ignore
		params = " ".join([f'"{p}"' if " " in p else p for p in sys.argv])
		rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
		if rc <= 32:
			console.print("[red]管理员提权失败（ShellExecuteW 返回码 <= 32）。[/red]")
			sys.exit(5)
		sys.exit(0)
	else:
		console.print("[yellow]--admin 仅在 Windows 下支持自动提权。[/yellow]")


def _cmd_watch(app_cfg: AppConfig, auto_mode: Optional[bool] = None) -> None:
	if not app_cfg.watch:
		raise ConfigError("配置中缺少 watch 块")
	w = app_cfg.watch
	setup_logging(logging.INFO)
	sources = [SourceSpec(root=m.source, targets=m.targets) for m in w.mappings]
	# 根据 auto_mode 覆盖自动整理开关
	use_auto = (auto_mode if auto_mode is not None else (w.auto_organize and w.auto_organize.enabled))
	auto_threshold = w.auto_organize.threshold if use_auto and w.auto_organize else None
	auto_prefix = w.auto_organize.prefix if use_auto and w.auto_organize else None
	auto_cooldown_s = w.auto_organize.cooldown_s if use_auto and w.auto_organize else 60
	# 组织命名相关配置（若缺少 organize 块则给出安全默认值）
	org = app_cfg.organize
	name_template = (org.name_template if org else None)
	timestamp_format = (org.timestamp_format if org else "%Y%m%d_%H%M%S")
	range_naming = (org.range_naming if org else True)
	range_date_format = (org.range_date_format if org else "%Y%m%d")
	fill_previous = (org.fill_previous if org else True)
	# 获取整理目标目录
	organize_target_dirs = org.target_dirs if org else []
	
	if w.use_async:
		asyncio.run(start_watching_async(
			sources=sources,
			include_patterns=w.include_patterns,
			exclude_patterns=w.exclude_patterns,
			ignore_dotfiles=w.ignore_dotfiles,
			debounce_ms=w.debounce_ms,
			auto_threshold=auto_threshold,
			auto_prefix=auto_prefix,
			auto_cooldown_s=auto_cooldown_s,
			num_workers=w.worker_threads,
			io_retries=w.io_retries,
			io_backoff_ms=w.io_backoff_ms,
			verify_mode=w.verify_mode,
			keep_source=w.keep_source,
			dedup_strategy=w.dedup_strategy,
			name_template=name_template,
			timestamp_format=timestamp_format,
			range_naming=range_naming,
			range_date_format=range_date_format,
			fill_previous=fill_previous,
			interested_events=w.interested_events,
			organize_target_dirs=organize_target_dirs,
		))
	else:
		start_watching(
			sources=sources,
			include_patterns=w.include_patterns,
			exclude_patterns=w.exclude_patterns,
			ignore_dotfiles=w.ignore_dotfiles,
			debounce_ms=w.debounce_ms,
			auto_threshold=auto_threshold,
			auto_prefix=auto_prefix,
			auto_cooldown_s=auto_cooldown_s,
			num_workers=w.worker_threads,
			io_retries=w.io_retries,
			io_backoff_ms=w.io_backoff_ms,
			verify_mode=w.verify_mode,
			keep_source=w.keep_source,
			dedup_strategy=w.dedup_strategy,
			name_template=name_template,
			timestamp_format=timestamp_format,
			range_naming=range_naming,
			range_date_format=range_date_format,
			fill_previous=fill_previous,
			interested_events=w.interested_events,
			organize_target_dirs=organize_target_dirs,
		)


def _cmd_organize(app_cfg: AppConfig, dirs: Optional[List[str]], chunk_size_override: Optional[int], prefix_override: Optional[str], dry_run: bool, verbose: bool, keep_source_override: Optional[bool] = None) -> None:
	setup_logging(logging.DEBUG if verbose else logging.INFO)
	org = app_cfg.organize
	# 组装候选目录
	if dirs and len(dirs) > 0:
		candidates = [Path(d) for d in dirs]
	elif org and org.target_dirs:
		candidates = org.target_dirs
	else:
		raise ConfigError("未提供 organize.target_dirs，且未通过 --dir 指定")
	# 过滤不存在的目录并提示
	present = [p for p in candidates if p.exists()]
	missing = [p for p in candidates if not p.exists()]
	if missing:
		console.print(Panel.fit("以下整理根目录不存在，已跳过：\n" + "\n".join(str(p) for p in missing), title="整理目录检查", border_style="yellow"))
	if not present:
		console.print("[yellow]未找到可用的整理目录（全部不存在）。[/yellow]")
		if missing and len(missing) > 0:
			console.print(f"[yellow]请检查路径是否正确，或使用 --dir 参数指定存在的目录。[/yellow]")
		return
	elif missing and len(missing) > 0:
		# 部分目录不存在，但有些存在，询问是否继续
		console.print(f"[green]找到 {len(present)} 个可用目录，将进行整理。[/green]")
	# 参数
	target_dirs = present
	chunk_size = chunk_size_override if chunk_size_override else (org.chunk_size if org else 1000)
	prefix = prefix_override if prefix_override is not None else (org.prefix if org else None)
	dry = dry_run if dry_run else (org.dry_run if org else False)
	keep_src = keep_source_override if keep_source_override is not None else (org.keep_source if org else False)
	dedup = org.dedup_strategy if org else "rename"
	name_template = org.name_template if org else None
	ts_fmt = org.timestamp_format if org else "%Y%m%d_%H%M%S"
	concurrency = 8  # 默认并发数
	
	# 统一使用异步版本
	stats = asyncio.run(organize_by_mtime_multi_async(
		target_dirs=target_dirs, 
		chunk_size=chunk_size, 
		prefix=prefix, 
		dry_run=dry, 
		concurrency=concurrency,
		keep_source=keep_src, 
		dedup_strategy=dedup, 
		name_template=name_template, 
		timestamp_format=ts_fmt,
		range_naming=org.range_naming if org else True,
		range_date_format=org.range_date_format if org else "%Y%m%d",
		fill_previous=org.fill_previous if org else True
	))
	
	# 显示统计信息
	console.print(Panel.fit(
		f"整理完成！\n"
		f"处理文件: {stats.processed}\n"
		f"移动文件: {stats.moved}\n"
		f"复制文件: {stats.copied}\n"
		f"跳过文件: {stats.skipped}\n"
		f"错误文件: {stats.errors}\n"
		f"创建批次: {stats.batches_created}\n"
		f"填充文件: {stats.files_filled}",
		title="整理统计",
		border_style="green" if stats.errors == 0 else "yellow"
	))


def _ensure_targets(cfg: AppConfig) -> None:
	if not cfg.watch:
		return
	missing: list[Path] = []
	for m in cfg.watch.mappings:
		for t in m.targets:
			if not t.exists():
				missing.append(t)
	if not missing:
		return
	console.print(Panel.fit("检测到以下目标目录不存在：\n" + "\n".join(str(p) for p in missing), title="目标目录校验"))
	if Confirm.ask("是否立即创建上述目录?", default=True):
		for p in missing:
			p.mkdir(parents=True, exist_ok=True)
		console.print("已创建缺失目录。")
	else:
		console.print("保留现状，运行中若缺失将按需逐步创建（写入时）。")


def _resolve_config_path(cli_path: Optional[str]) -> Optional[str]:
	if cli_path:
		return cli_path
	env_path = os.environ.get(ENV_KEY)
	if env_path and Path(env_path).exists():
		return env_path
	# 回退到当前目录下的默认配置文件名
	local = Path("filemover.yaml")
	if local.exists():
		return str(local)
	return None


def _maybe_store_env(path: str) -> None:
	# 若已设置环境变量且指向相同路径，则不再提示
	existing = os.getenv(ENV_KEY)
	if existing and Path(existing) == Path(path):
		return
	console.print(Panel.fit(f"是否将该配置路径写入环境变量 {ENV_KEY} 以便下次自动使用?", title="保存配置路径"))
	if Confirm.ask(f"写入 {ENV_KEY}?", default=True):
		console.print(f"请在系统环境变量中添加或更新 {ENV_KEY}={path}")
		console.print("提示：在当前会话中临时生效可执行：")
		console.print(f"$env:{ENV_KEY} = '{path}'")


def _cmd_start_interactive(config_hint: Optional[str]) -> None:
	console.print(Panel.fit("交互式启动：将帮助你选择配置并启动监听", title="Start"))
	path = _resolve_config_path(config_hint) or Prompt.ask("配置文件路径", default="filemover.yaml")
	if not Path(path).exists():
		console.print(Panel.fit("未找到配置，是否使用向导创建?", title="缺少配置"))
		if Confirm.ask("现在创建?", default=True):
			out = run_wizard(path)
			path = str(out)
		else:
			console.print("已取消。")
			return
	
	cfg = load_config(path)
	_ensure_targets(cfg)
	_maybe_store_env(path)
	
	# 选择启动模式（记忆上次选择）
	state = _load_state()
	last_mode = state.get("last_mode", "1")
	console.print("\n[bold]选择启动模式：[/bold]")
	console.print("1. [cyan]watch[/cyan] - 仅监听（不自动整理）")
	console.print("2. [cyan]watch+organize[/cyan] - 监听并按阈值自动整理")
	console.print("3. [cyan]organize-now[/cyan] - 立即整理（不监听）")
	mode = Prompt.ask("请选择模式", choices=["1", "2", "3"], default=str(last_mode))
	state["last_mode"] = mode
	_save_state(state)
	
	if mode == "1":
		# 仅监听：强制关闭自动整理（不看配置开关）
		_cmd_watch(cfg, auto_mode=False)
	elif mode == "2":
		# 监听 + 自动整理：强制启用（不看配置开关）
		_cmd_watch(cfg, auto_mode=True)
	elif mode == "3":
		# organize-now 分支保持不变（使用 organize 目录）
		target_dirs: List[Path] = []
		if cfg.organize and cfg.organize.target_dirs:
			target_dirs = cfg.organize.target_dirs
		else:
			while True:
				dir_path = Prompt.ask("输入要整理的目录路径（留空结束）")
				if not dir_path:
					break
				target_dirs.append(Path(dir_path))
			if not target_dirs:
				console.print("未指定目录，使用配置中的 watch 目标目录")
				target_dirs = [target for mapping in cfg.watch.mappings for target in mapping.targets]
		_cmd_organize(cfg, target_dirs, None, None, False, False, False)


def _read_failures(limit: Optional[int] = None) -> List[dict]:
	fail_file = get_log_file().with_name("failures.jsonl")
	if not fail_file.exists():
		return []
	rows: List[dict] = []
	with fail_file.open("r", encoding="utf-8") as f:
		for line in f:
			line = line.strip()
			if not line:
				continue
			try:
				rows.append(json.loads(line))
			except Exception:
				continue
	return rows[-limit:] if limit else rows


def _cmd_retry_failures(app_cfg: AppConfig, limit: Optional[int]) -> None:
	setup_logging(logging.INFO)
	rows = _read_failures(limit)
	if not rows:
		print("no failures recorded")
		return
	if not app_cfg.watch:
		raise ConfigError("需要 watch 配置以定位目标目录")
	w = app_cfg.watch
	sources = [SourceSpec(root=m.source, targets=m.targets) for m in w.mappings]
	# 简单策略：根据原失败记录中的路径，尝试重建 WorkItem 并提交到同步 WorkerPool（非监听）
	from .syncer import WorkerPool, WorkItem
	pool = WorkerPool(num_workers=w.worker_threads)
	pool.start()
	try:
		for r in rows:
			op = r.get("op")
			src = Path(r["src"]) if r.get("src") else None
			dst = Path(r["dst"]) if r.get("dst") else None
			item = WorkItem(op=op, src=src, dst=dst, retries=w.io_retries, backoff_ms=w.io_backoff_ms, verify=(w.verify_mode=="size"), dedup_strategy=w.dedup_strategy)
			pool.submit(item)
		logger.info("submitted %d failure(s) for retry", len(rows))
	finally:
		pool._q.join()  # type: ignore[attr-defined]
		pool.stop()


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="filemover2", description="Watch folders and sync changes; organize files into time-stamped chunks")
	parser.add_argument("--config", "-c", required=False, help="配置文件路径（YAML/JSON）")
	parser.add_argument("--admin", action="store_true", help="在 Windows 下以管理员身份运行（需要 UAC 确认）")
	sub = parser.add_subparsers(dest="command", required=True)

	sub.add_parser("start", help="交互式启动：选择配置并启动监听")

	sub.add_parser("watch", help="监听并同步目录（映射模式）")

	p_org = sub.add_parser("organize", help="整理文件到时间戳目录（dry-run 可见规划）")
	p_org.add_argument("--dir", action="append", help="要整理的目录（可多次传入以合并整理）")
	p_org.add_argument("--chunk-size", type=int, required=False, help="覆盖配置的每批文件数")
	p_org.add_argument("--prefix", required=False, help="覆盖配置的批次目录前缀")
	p_org.add_argument("--dry-run", action="store_true", help="仅演示不移动文件")
	p_org.add_argument("--verbose", action="store_true", help="输出详细日志")
	p_org.add_argument("--keep-source", action="store_true", help="覆盖配置，改为复制不移动")

	sub.add_parser("organize-now", help="立即整理（不监听）")

	p_wiz = sub.add_parser("config-wizard", help="交互式生成配置文件")
	p_wiz.add_argument("--out", required=False, default="filemover.yaml", help="保存路径（默认 filemover.yaml）")

	p_val = sub.add_parser("config-validate", help="验证配置")
	p_val.add_argument("--config", required=True, help="配置文件")

	p_backup = sub.add_parser("config-backup", help="备份配置")
	p_backup.add_argument("--config", required=True, help="配置文件")

	p_restore = sub.add_parser("config-restore", help="从备份恢复配置")
	p_restore.add_argument("--backup", required=True, help="备份文件路径")
	p_restore.add_argument("--dest", help="恢复到的配置路径，默认去除 .bak 后缀")

	p_retry = sub.add_parser("retry-failures", help="读取失败记录并重试")
	p_retry.add_argument("--limit", type=int, required=False, help="仅重试最后 N 条记录")

	return parser


def main(argv: Optional[list[str]] = None) -> None:
	parser = build_parser()
	args = parser.parse_args(argv)
	# 可选管理员模式
	_elevate_if_needed(getattr(args, "admin", False))
	try:
		if args.command == "config-wizard":
			path = run_wizard(args.out)
			print(path)
			return
		if args.command == "config-validate":
			ok = validate_config(Path(args.config))
			print("OK" if ok else "INVALID")
			return
		if args.command == "config-backup":
			bak = backup_config(Path(args.config))
			print(bak)
			return
		if args.command == "config-restore":
			dest = restore_config(Path(args.backup), Path(args.dest) if args.dest else None)
			print(dest)
			return
		if args.command == "start":
			_cmd_start_interactive(args.config)
			return
		if args.command == "retry-failures":
			_cmd_retry_failures(cfg, args.limit)  # type: ignore[arg-type]
			return
		resolved = _resolve_config_path(args.config)
		if not resolved:
			parser.error(f"未找到配置。你可以：1) 使用 --config 指定；2) 预先设置环境变量 {ENV_KEY}；3) 在当前目录放置 filemover.yaml。")
		cfg = load_config(resolved)
		_ensure_targets(cfg)
		if args.command == "watch":
			_cmd_watch(cfg)  # type: ignore[arg-type]
		elif args.command == "organize":
			_cmd_organize(cfg, args.dir, args.chunk_size, args.prefix, args.dry_run, args.verbose, args.keep_source)  # type: ignore[arg-type]
		elif args.command == "organize-now":
			_cmd_organize(cfg, args.dir, args.chunk_size, args.prefix, False, args.verbose, args.keep_source)  # type: ignore[arg-type]
		else:
			parser.error("未知的命令")
	except ConfigError as e:
		parser.error(str(e))
