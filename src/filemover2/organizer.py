from __future__ import annotations

import logging
import shutil
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


def organize_by_mtime_multi(target_dirs: Sequence[Path], chunk_size: int, prefix: Optional[str], dry_run: bool = False, keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S", range_naming: bool = True, range_date_format: str = "%Y%m%d", fill_previous: bool = True) -> List[Path]:
	# 强制使用日期范围命名，禁用自定义模板以避免冲突
	range_naming = True
	name_template = None
	files: List[Path] = []
	for root in target_dirs:
		if not root.exists():
			logger.warning("skip missing dir: %s", root)
			continue
		files.extend(list(_iter_files(root)))
	valid_files = []
	for f in files:
		try:
			_ = f.stat().st_mtime
			valid_files.append(f)
		except (FileNotFoundError, OSError):
			logger.warning("文件在扫描过程中被删除或无法访问: %s", f)
			continue
	if not valid_files:
		logger.info("organize completed: 0 files (no inputs)")
		return []
	valid_files.sort(key=_safe_get_mtime)
	created_dirs: List[Path] = []
	part = 1
	total = moved = copied = skipped = 0
	base = target_dirs[0].parent if target_dirs else Path.cwd()
	# 预扫描既有批次用于填充
	existing = _scan_existing_batches(base, range_date_format) if range_naming and fill_previous else []
	for i in range(0, len(valid_files), chunk_size):
		chunk = valid_files[i:i+chunk_size]
		oldest_dt = datetime.fromtimestamp(_safe_get_mtime(chunk[0]))
		newest_dt = datetime.fromtimestamp(_safe_get_mtime(chunk[-1]))
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
		# 先尝试将一部分塞入已存在的未满分区
		start_idx = 0
		if chosen is not None and chosen_capacity > 0:
			take = min(len(chunk), chosen_capacity)
			if not dry_run:
				for f in chunk[:take]:
					if not f.exists():
						logger.warning("文件在处理过程中被删除: %s", f)
						continue
					dst0 = chosen / f.name
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
			# 更新 existing 列表中的计数（用于后续分片继续填充）
			for j in range(len(existing)):
				if existing[j][0] == chosen:
					p_dir, o_dt, n_dt, cnt = existing[j]
					existing[j] = (p_dir, o_dt, n_dt, cnt + take)
					break
			start_idx = take
		# 剩余文件创建新分区
		remaining = chunk[start_idx:]
		if remaining:
			old2 = datetime.fromtimestamp(_safe_get_mtime(remaining[0]))
			new2 = datetime.fromtimestamp(_safe_get_mtime(remaining[-1]))
			# 始终使用日期范围命名
			batch_dir = _range_name(base, old2, new2, part, range_date_format)
			created_dirs.append(batch_dir)
			if not dry_run:
				batch_dir.mkdir(parents=True, exist_ok=True)
				for f in remaining:
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
			logger.info("created batch: %s (%d files)", batch_dir, len(remaining))
			part += 1
	logger.info("organize completed: processed=%d moved=%d copied=%d skipped=%d batches=%d", total, moved, copied, skipped, len(created_dirs))
	return created_dirs


async def organize_by_mtime_multi_async(target_dirs: Sequence[Path], chunk_size: int, prefix: Optional[str], dry_run: bool = False, concurrency: int = 8, keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S", range_naming: bool = True, range_date_format: str = "%Y%m%d", fill_previous: bool = True) -> List[Path]:
	# 强制使用日期范围命名，禁用自定义模板以避免冲突
	range_naming = True
	name_template = None
	files: List[Path] = []
	for root in target_dirs:
		if not root.exists():
			logger.warning("skip missing dir: %s", root)
			continue
		files.extend(list(_iter_files(root)))
	valid_files = []
	for f in files:
		try:
			_ = f.stat().st_mtime
			valid_files.append(f)
		except (FileNotFoundError, OSError):
			logger.warning("文件在扫描过程中被删除或无法访问: %s", f)
			continue
	if not valid_files:
		logger.info("organize completed: 0 files (no inputs)")
		return []
	valid_files.sort(key=_safe_get_mtime)
	created_dirs: List[Path] = []
	part = 1
	base = target_dirs[0].parent if target_dirs else Path.cwd()
	sem = asyncio.Semaphore(max(1, concurrency))
	total = moved = copied = skipped = 0
	existing = _scan_existing_batches(base, range_date_format) if range_naming and fill_previous else []
	for i in range(0, len(valid_files), chunk_size):
		chunk = valid_files[i:i+chunk_size]
		oldest_dt = datetime.fromtimestamp(_safe_get_mtime(chunk[0]))
		newest_dt = datetime.fromtimestamp(_safe_get_mtime(chunk[-1]))
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
		# 先尝试将一部分塞入已存在的未满分区
		start_idx = 0
		if chosen is not None and chosen_capacity > 0:
			take = min(len(chunk), chosen_capacity)
			if not dry_run:
				async def move_or_copy_fill(src: Path) -> None:
					async with sem:
						if not src.exists():
							logger.warning("文件在处理过程中被删除: %s", src)
							return
						dst0 = chosen / src.name
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
				tasks = [asyncio.create_task(move_or_copy_fill(f)) for f in chunk[:take]]
				await asyncio.gather(*tasks)
			# 更新 existing 列表中的计数（用于后续分片继续填充）
			for j in range(len(existing)):
				if existing[j][0] == chosen:
					p_dir, o_dt, n_dt, cnt = existing[j]
					existing[j] = (p_dir, o_dt, n_dt, cnt + take)
					break
			start_idx = take
		# 剩余文件创建新分区
		remaining = chunk[start_idx:]
		if remaining:
			old2 = datetime.fromtimestamp(_safe_get_mtime(remaining[0]))
			new2 = datetime.fromtimestamp(_safe_get_mtime(remaining[-1]))
			# 始终使用日期范围命名
			batch_dir = _range_name(base, old2, new2, part, range_date_format)
			created_dirs.append(batch_dir)
			if not dry_run:
				batch_dir.mkdir(parents=True, exist_ok=True)
				async def move_or_copy_new(src: Path) -> None:
					async with sem:
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
				tasks = [asyncio.create_task(move_or_copy_new(f)) for f in remaining]
				await asyncio.gather(*tasks)
			logger.info("created batch: %s (%d files)", batch_dir, len(remaining))
			part += 1
	logger.info("organize completed: processed=%d moved=%d copied=%d skipped=%d batches=%d", total, moved, copied, skipped, len(created_dirs))
	return created_dirs
