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


def _scan_existing_batches(base: Path, range_date_format: str) -> List[Tuple[Path, datetime, datetime, int]]:
	"""扫描已存在的批次目录，解析出 (path, oldest_dt, newest_dt, file_count)。"""
	batches: List[Tuple[Path, datetime, datetime, int]] = []
	date_len = len(datetime.now().strftime(range_date_format))
	pattern = re.compile(fr"^(\d{{{date_len}}})-(\d{{{date_len}}})-part(\d+)$")
	for p in base.iterdir():
		if not p.is_dir():
			continue
		m = pattern.match(p.name)
		if not m:
			continue
		try:
			oldest = datetime.strptime(m.group(1), range_date_format)
			newest = datetime.strptime(m.group(2), range_date_format)
		except ValueError:
			continue
		count = sum(1 for f in p.rglob("*") if f.is_file())
		batches.append((p, oldest, newest, count))
	return batches


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
	files_filled: int = 0  # 填充到已有分区的文件数
	errors: int = 0


@dataclass
class FileTask:
	"""文件处理任务"""
	src: Path
	dst: Path
	operation: str  # "move" or "copy"
	dedup_strategy: str
	keep_source: bool


async def organize_by_mtime_multi_async(target_dirs: Sequence[Path], chunk_size: int, prefix: Optional[str], dry_run: bool = False, concurrency: int = 8, keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S", range_naming: bool = True, range_date_format: str = "%Y%m%d", fill_previous: bool = True) -> OrganizeStats:
	"""异步整理文件，使用任务队列优化性能"""
	# 强制使用日期范围命名，禁用自定义模板以避免冲突
	range_naming = True
	name_template = None
	
	logger.info("开始整理: %d 个目录, chunk_size=%d, concurrency=%d", len(target_dirs), chunk_size, concurrency)
	
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
	
	# 初始化统计和配置
	stats = OrganizeStats()
	base = target_dirs[0].parent if target_dirs else Path.cwd()
	existing = _scan_existing_batches(base, range_date_format) if range_naming and fill_previous else []
	
	if existing:
		logger.info("发现 %d 个现有批次，启用填充算法", len(existing))
		for batch_path, oldest, newest, count in existing:
			logger.debug("现有批次: %s (%d 文件, 范围: %s-%s)", 
				batch_path.name, count, oldest.strftime("%Y%m%d"), newest.strftime("%Y%m%d"))
	
	# 创建任务队列
	task_queue: asyncio.Queue[Optional[FileTask]] = asyncio.Queue(maxsize=concurrency * 2)
	stats_lock = asyncio.Lock()
	
	# 启动工作协程
	workers = [asyncio.create_task(_file_worker(task_queue, stats, stats_lock, dry_run)) for _ in range(concurrency)]
	
	try:
		# 处理文件分片
		part = 1
		i = 0
		while i < len(valid_files):
			# 获取当前分片
			chunk_start = i
			chunk_end = min(i + chunk_size, len(valid_files))
			chunk = valid_files[chunk_start:chunk_end]
			
			oldest_dt = datetime.fromtimestamp(_safe_get_mtime(chunk[0]))
			newest_dt = datetime.fromtimestamp(_safe_get_mtime(chunk[-1]))
			
			# 尝试填充到已有分区
			chosen: Optional[Path] = None
			chosen_capacity = 0
			if range_naming and fill_previous:
				candidates = [b for b in existing if (b[1] <= oldest_dt <= b[2]) or (b[1] <= newest_dt <= b[2])]
				candidates.sort(key=lambda x: (x[2], x[3]))  # by newest then count
				for idx in range(len(candidates)-1, -1, -1):
					p_dir, o_dt, n_dt, cnt = candidates[idx]
					cap = max(0, chunk_size - cnt)
					if cap > 0:
						chosen = p_dir
						chosen_capacity = cap
						break
			
			# 处理填充
			if chosen is not None and chosen_capacity > 0:
				take = min(len(chunk), chosen_capacity)
				logger.info("填充批次 %s: %d 个文件", chosen.name, take)
				
				# 提交填充任务
				for f in chunk[:take]:
					dst0 = chosen / f.name
					dst = _apply_dedup(dst0, dedup_strategy)
					if dst is None:
						async with stats_lock:
							stats.skipped += 1
						continue
					
					task = FileTask(
						src=f,
						dst=dst,
						operation="copy" if keep_source else "move",
						dedup_strategy=dedup_strategy,
						keep_source=keep_source
					)
					await task_queue.put(task)
				
				# 更新现有批次计数
				for j in range(len(existing)):
					if existing[j][0] == chosen:
						p_dir, o_dt, n_dt, cnt = existing[j]
						existing[j] = (p_dir, o_dt, n_dt, cnt + take)
						break
				
				async with stats_lock:
					stats.files_filled += take
				
				i += take
				remaining = chunk[take:]
			else:
				remaining = chunk
				i += len(chunk)
			
			# 处理剩余文件，创建新分区
			if remaining:
				actual_batch_size = min(len(remaining), chunk_size)
				batch_files = remaining[:actual_batch_size]
				
				old2 = datetime.fromtimestamp(_safe_get_mtime(batch_files[0]))
				new2 = datetime.fromtimestamp(_safe_get_mtime(batch_files[-1]))
				batch_dir = _range_name(base, old2, new2, part, range_date_format)
				
				logger.info("创建新批次 %s: %d 个文件 (范围: %s-%s)", 
					batch_dir.name, len(batch_files), old2.strftime("%Y%m%d"), new2.strftime("%Y%m%d"))
				
				if not dry_run:
					batch_dir.mkdir(parents=True, exist_ok=True)
				
				# 提交新批次任务
				for f in batch_files:
					dst0 = batch_dir / f.name
					dst = _apply_dedup(dst0, dedup_strategy)
					if dst is None:
						async with stats_lock:
							stats.skipped += 1
						continue
					
					task = FileTask(
						src=f,
						dst=dst,
						operation="copy" if keep_source else "move",
						dedup_strategy=dedup_strategy,
						keep_source=keep_source
					)
					await task_queue.put(task)
				
				async with stats_lock:
					stats.batches_created += 1
				
				part += 1
				
				# 处理溢出文件
				if len(remaining) > chunk_size:
					valid_files = remaining[chunk_size:] + valid_files[i:]
					i = 0
		
		# 等待所有任务完成
		await task_queue.join()
		
	finally:
		# 停止工作协程
		for _ in workers:
			await task_queue.put(None)
		await asyncio.gather(*workers, return_exceptions=True)
	
	logger.info("整理完成: 处理=%d 移动=%d 复制=%d 跳过=%d 错误=%d 批次=%d 填充=%d", 
		stats.processed, stats.moved, stats.copied, stats.skipped, stats.errors, 
		stats.batches_created, stats.files_filled)
	
	return stats


async def _file_worker(queue: asyncio.Queue[Optional[FileTask]], stats: OrganizeStats, stats_lock: asyncio.Lock, dry_run: bool) -> None:
	"""文件处理工作协程"""
	while True:
		task = await queue.get()
		if task is None:
			queue.task_done()
			break
		
		try:
			if not task.src.exists():
				logger.warning("文件在处理过程中被删除: %s", task.src)
				async with stats_lock:
					stats.errors += 1
				continue
			
			if not dry_run:
				if task.operation == "copy":
					await asyncio.to_thread(shutil.copy2, task.src, task.dst)
					async with stats_lock:
						stats.copied += 1
				else:
					await asyncio.to_thread(shutil.move, str(task.src), str(task.dst))
					async with stats_lock:
						stats.moved += 1
			else:
				logger.debug("DRY RUN: %s %s -> %s", task.operation, task.src, task.dst)
			
			async with stats_lock:
				stats.processed += 1
			
		except Exception as e:
			logger.error("处理文件失败 %s -> %s: %s", task.src, task.dst, e)
			async with stats_lock:
				stats.errors += 1
		finally:
			queue.task_done()
