from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence, Optional
import asyncio

logger = logging.getLogger(__name__)


def _iter_files(root: Path) -> Iterable[Path]:
	for p in root.rglob("*"):
		if p.is_file():
			yield p


def _format_batch_dir(base: Path, prefix: Optional[str], part_index: int, name_template: Optional[str], timestamp_format: str) -> Path:
	ts = datetime.now().strftime(timestamp_format)
	if name_template:
		# 可使用 {timestamp} {prefix} {index}
		name = name_template.format(timestamp=ts, prefix=(prefix or ""), index=part_index)
	else:
		name = f"{ts}_{prefix}_part{part_index:02d}" if prefix else f"{ts}_part{part_index:02d}"
	return base / name


def _apply_dedup(dst: Path, strategy: str) -> Path | None:
	if not dst.exists():
		return dst
	if strategy == "overwrite":
		return dst
	if strategy == "skip":
		return None
	# rename
	base = dst.stem
	suffix = dst.suffix
	parent = dst.parent
	idx = 1
	while True:
		candidate = parent / f"{base}({idx}){suffix}"
		if not candidate.exists():
			return candidate
		idx += 1


def _safe_get_mtime(p: Path) -> float:
	"""安全获取文件修改时间，如果文件不存在则返回0"""
	try:
		return p.stat().st_mtime
	except (FileNotFoundError, OSError):
		logger.warning("文件在扫描过程中被删除或无法访问: %s", p)
		return 0.0


def organize_by_mtime_multi(target_dirs: Sequence[Path], chunk_size: int, prefix: Optional[str], dry_run: bool = False, keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S") -> List[Path]:
	files: List[Path] = []
	for root in target_dirs:
		if not root.exists():
			logger.warning("skip missing dir: %s", root)
			continue
		files.extend(list(_iter_files(root)))
	
	# 安全排序：跳过在收集过程中被删除的文件
	valid_files = []
	for f in files:
		try:
			_ = f.stat().st_mtime
			valid_files.append(f)
		except (FileNotFoundError, OSError):
			logger.warning("文件在扫描过程中被删除或无法访问: %s", f)
			continue
	
	valid_files.sort(key=_safe_get_mtime)
	if not valid_files:
		logger.info("organize completed: 0 files (no inputs)")
		return []
	created_dirs: List[Path] = []
	part = 1
	# 统计
	total = 0
	moved = 0
	copied = 0
	skipped = 0
	# 批次目录创建在第一个目录的父目录下，或当前目录
	base = target_dirs[0].parent if target_dirs else Path.cwd()
	for i in range(0, len(valid_files), chunk_size):
		chunk = valid_files[i:i+chunk_size]
		batch_dir = _format_batch_dir(base, prefix, part, name_template, timestamp_format)
		created_dirs.append(batch_dir)
		if not dry_run:
			batch_dir.mkdir(parents=True, exist_ok=True)
			for f in chunk:
				# 再次检查文件是否仍然存在
				if not f.exists():
					logger.warning("文件在处理过程中被删除: %s", f)
					continue
				dst0 = batch_dir / f.name
				dst = _apply_dedup(dst0, dedup_strategy)
				if dst is None:
					logger.info("skip existing (dedup=skip): %s", dst0)
					skipped += 1
					continue
				if keep_source:
					shutil.copy2(f, dst)
					copied += 1
					logger.info("copy2: %s -> %s", f, dst)
				else:
					shutil.move(str(f), str(dst))
					moved += 1
					logger.info("move: %s -> %s", f, dst)
				total += 1
		logger.info("created batch: %s (%d files)", batch_dir, len(chunk))
		part += 1
	logger.info("organize completed: processed=%d moved=%d copied=%d skipped=%d batches=%d", total, moved, copied, skipped, len(created_dirs))
	return created_dirs


async def organize_by_mtime_multi_async(target_dirs: Sequence[Path], chunk_size: int, prefix: Optional[str], dry_run: bool = False, concurrency: int = 8, keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S") -> List[Path]:
	files: List[Path] = []
	for root in target_dirs:
		if not root.exists():
			logger.warning("skip missing dir: %s", root)
			continue
		files.extend(list(_iter_files(root)))
	
	# 安全排序：跳过在收集过程中被删除的文件
	valid_files = []
	for f in files:
		try:
			_ = f.stat().st_mtime
			valid_files.append(f)
		except (FileNotFoundError, OSError):
			logger.warning("文件在扫描过程中被删除或无法访问: %s", f)
			continue
	
	valid_files.sort(key=_safe_get_mtime)
	if not valid_files:
		logger.info("organize completed: 0 files (no inputs)")
		return []
	created_dirs: List[Path] = []
	part = 1
	base = target_dirs[0].parent if target_dirs else Path.cwd()
	sem = asyncio.Semaphore(max(1, concurrency))
	# 统计
	total = 0
	moved = 0
	copied = 0
	skipped = 0
	for i in range(0, len(valid_files), chunk_size):
		chunk = valid_files[i:i+chunk_size]
		batch_dir = _format_batch_dir(base, prefix, part, name_template, timestamp_format)
		created_dirs.append(batch_dir)
		if not dry_run:
			batch_dir.mkdir(parents=True, exist_ok=True)
			async def move_or_copy(src: Path) -> None:
				async with sem:
					# 再次检查文件是否仍然存在
					if not src.exists():
						logger.warning("文件在处理过程中被删除: %s", src)
						return
					dst0 = batch_dir / src.name
					dst = _apply_dedup(dst0, dedup_strategy)
					if dst is None:
						logger.info("skip existing (dedup=skip): %s", dst0)
						nonlocal skipped
						skipped += 1
						return
					if keep_source:
						await asyncio.to_thread(shutil.copy2, src, dst)
						nonlocal copied
						copied += 1
						logger.info("copy2: %s -> %s", src, dst)
					else:
						await asyncio.to_thread(shutil.move, str(src), str(dst))
						nonlocal moved
						moved += 1
						logger.info("move: %s -> %s", src, dst)
					nonlocal total
					total += 1
			tasks = [asyncio.create_task(move_or_copy(f)) for f in chunk]
			await asyncio.gather(*tasks)
		logger.info("created batch: %s (%d files)", batch_dir, len(chunk))
		part += 1
	logger.info("organize completed: processed=%d moved=%d copied=%d skipped=%d batches=%d", total, moved, copied, skipped, len(created_dirs))
	return created_dirs
