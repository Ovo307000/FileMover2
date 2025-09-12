from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence, Optional, Tuple
import asyncio
import re

logger = logging.getLogger(__name__)


def _iter_files(root: Path) -> Iterable[Path]:
	for p in root.rglob("*"):
		if p.is_file():
			yield p


def _format_batch_dir(base: Path, prefix: Optional[str], part_index: int, name_template: Optional[str], timestamp_format: str) -> Path:
	ts = datetime.now().strftime(timestamp_format)
	if name_template:
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
	try:
		return p.stat().st_mtime
	except (FileNotFoundError, OSError):
		logger.warning("文件在扫描过程中被删除或无法访问: %s", p)
		return 0.0


# 填充相关函数已移除，简化逻辑


def _is_batch_dir_name(name: str, date_fmt: str) -> bool:
	"""判断目录名是否为批次目录名：oldest-newest-partXX"""
	try:
		date_len = len(datetime.now().strftime(date_fmt))
	except Exception:
		date_len = 8
	pattern = re.compile(fr"^(\d{{{date_len}}})-(\d{{{date_len}}})-part(\d+)$")
	return bool(pattern.match(name))


def _ensure_unique_dir(path: Path) -> Path:
	"""若目录已存在，则追加 (n) 后缀确保唯一。"""
	if not path.exists():
		return path
	idx = 1
	while True:
		candidate = path.parent / f"{path.name}({idx})"
		if not candidate.exists():
			return candidate
		idx += 1


def _next_part_index(root: Path, oldest: datetime, newest: datetime, date_fmt: str) -> int:
	"""扫描 root 下已有相同日期范围的批次，返回下一个可用的 part 序号（从1开始）。"""
	date_str_old = oldest.strftime(date_fmt)
	date_str_new = newest.strftime(date_fmt)
	pattern = re.compile(rf"^{re.escape(date_str_old)}-{re.escape(date_str_new)}-part(\d+)$")
	max_part = 0
	try:
		for p in root.iterdir():
			if not p.is_dir():
				continue
			m = pattern.match(p.name)
			if m:
				try:
					idx = int(m.group(1))
					if idx > max_part:
						max_part = idx
				except ValueError:
					continue
	except FileNotFoundError:
		pass
	return max_part + 1


def _range_name(base: Path, oldest: datetime, newest: datetime, index: int, fmt: str) -> Path:
	return base / f"{oldest.strftime(fmt)}-{newest.strftime(fmt)}-part{index:02d}"


# 同步版本已移除，统一使用异步版本


@dataclass
class OrganizeStats:
	"""整理统计信息"""
	processed: int = 0
	moved: int = 0
	copied: int = 0
	skipped: int = 0
	batches_created: int = 0
	errors: int = 0


async def organize_by_mtime_multi_async(target_dirs: Sequence[Path], chunk_size: int, prefix: Optional[str], dry_run: bool = False, concurrency: int = 8, keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S", range_naming: bool = True, range_date_format: str = "%Y%m%d", fill_previous: bool = True) -> OrganizeStats:
	"""简化的异步整理文件逻辑"""
	# 强制使用日期范围命名，禁用自定义模板以避免冲突
	range_naming = True
	name_template = None
	
	logger.info("开始简化整理: %d 个目录, chunk_size=%d, concurrency=%d", len(target_dirs), chunk_size, concurrency)
	
	# 扫描所有文件
	files: List[Path] = []
	for root in target_dirs:
		if not root.exists():
			logger.warning("跳过不存在的目录: %s", root)
			continue
		root_files = list(_iter_files(root))
		files.extend(root_files)
		logger.debug("扫描目录 %s: 找到 %d 个文件", root, len(root_files))
	
	# 验证文件并排序
	valid_files = []
	for f in files:
		try:
			_ = f.stat().st_mtime
			valid_files.append(f)
		except (FileNotFoundError, OSError):
			logger.warning("文件在扫描过程中被删除或无法访问: %s", f)
			continue
	
	if not valid_files:
		logger.info("整理完成: 0 个文件 (无输入)")
		return OrganizeStats()
	
	valid_files.sort(key=_safe_get_mtime)
	logger.info("扫描完成: 共 %d 个有效文件", len(valid_files))
	
	# 初始化统计
	stats = OrganizeStats()
	base = target_dirs[0].parent if target_dirs else Path.cwd()
	
	# 针对每个目标目录分别整理，避免跨目录合并导致溢出；跳过已分区目录内的文件
	for root in target_dirs:
		if not root.exists():
			continue
		sem = asyncio.Semaphore(max(1, concurrency))
		part = 1
		# 选取 root 下非批次目录内的文件
		root_files_all = list(_iter_files(root))
		root_valid: List[Path] = []
		for f in root_files_all:
			try:
				rel = f.relative_to(root)
			except Exception:
				continue
			if len(rel.parts) > 1 and _is_batch_dir_name(rel.parts[0], range_date_format):
				continue
			root_valid.append(f)
		root_valid.sort(key=_safe_get_mtime)
		if not root_valid:
			continue
		logger.info("目录 %s 待整理文件: %d", root, len(root_valid))
		# 仅创建满配分区：不足 chunk_size 的尾部文件不创建分区
		last_full = len(root_valid) - (len(root_valid) % chunk_size)
		for i in range(0, last_full, chunk_size):
			batch_files = root_valid[i:i + chunk_size]
			if not batch_files:
				continue
			oldest_dt = datetime.fromtimestamp(_safe_get_mtime(batch_files[0]))
			newest_dt = datetime.fromtimestamp(_safe_get_mtime(batch_files[-1]))
			# 动态计算下一个 part 序号，避免 (1)(2) 后缀
			part_index = _next_part_index(root, oldest_dt, newest_dt, range_date_format)
			candidate_dir = _range_name(root, oldest_dt, newest_dt, part_index, range_date_format)
			batch_dir = candidate_dir if not candidate_dir.exists() else _ensure_unique_dir(candidate_dir)
			logger.info("创建分区 %s: %d 个文件 (范围: %s-%s)", batch_dir.name, len(batch_files), oldest_dt.strftime("%Y%m%d"), newest_dt.strftime("%Y%m%d"))
			if not dry_run:
				batch_dir.mkdir(parents=True, exist_ok=False)
			stats_lock = asyncio.Lock()
			async def process_file(src: Path) -> None:
				async with sem:
					try:
						if not src.exists():
							logger.warning("文件在处理过程中被删除: %s", src)
							async with stats_lock:
								stats.errors += 1
							return
						dst0 = batch_dir / src.name
						dst = _apply_dedup(dst0, dedup_strategy)
						if dst is None:
							logger.info("跳过已存在文件 (dedup=skip): %s", dst0)
							# 若配置为移动模式，保持目标无残留，删除源文件
							if not keep_source and src.exists() and not dry_run:
								try:
									await asyncio.to_thread(src.unlink)
									async with stats_lock:
										stats.moved += 1
										stats.processed += 1
								except Exception as e:
									logger.error("删除源文件失败 %s: %s", src, e)
									async with stats_lock:
										stats.errors += 1
							else:
								async with stats_lock:
									stats.skipped += 1
							return
						if not dry_run:
							if keep_source:
								await asyncio.to_thread(shutil.copy2, src, dst)
								async with stats_lock:
									stats.copied += 1
								logger.debug("复制: %s -> %s", src, dst)
							else:
								await asyncio.to_thread(shutil.move, str(src), str(dst))
								async with stats_lock:
									stats.moved += 1
								logger.debug("移动: %s -> %s", src, dst)
						else:
							logger.debug("DRY RUN: %s %s -> %s", "copy" if keep_source else "move", src, dst)
						async with stats_lock:
							stats.processed += 1
					except Exception as e:
						logger.error("处理文件失败 %s -> %s: %s", src, dst, e)
						async with stats_lock:
							stats.errors += 1
			tasks = [asyncio.create_task(process_file(f)) for f in batch_files]
			await asyncio.gather(*tasks, return_exceptions=True)
			stats.batches_created += 1
			part += 1
		# 如有尾部不足 chunk_size 的文件，记录说明但不创建新分区
		rest = len(root_valid) - last_full
		if rest > 0:
			logger.info("目录 %s 剩余 %d 个文件 (< chunk_size=%d)，本次不创建分区", root, rest, chunk_size)
	
	logger.info("简化整理完成: 处理=%d 移动=%d 复制=%d 跳过=%d 错误=%d 批次=%d", 
		stats.processed, stats.moved, stats.copied, stats.skipped, stats.errors, stats.batches_created)
	
	return stats
