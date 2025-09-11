from __future__ import annotations

import fnmatch
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Literal
import threading
import queue
import asyncio
import json
from datetime import datetime

from watchdog.events import FileSystemEventHandler, FileSystemEvent, FileCreatedEvent, FileModifiedEvent, FileMovedEvent, FileDeletedEvent, DirCreatedEvent, DirMovedEvent, DirDeletedEvent
from watchdog.observers import Observer

from .organizer import organize_by_mtime_multi_async
from .logging_setup import get_log_file

logger = logging.getLogger(__name__)

FAIL_LOG = get_log_file().with_name("failures.jsonl")
EVENT_LOG = get_log_file().with_name("events.jsonl")


Operation = Literal["mkdir", "copy", "move", "unlink", "rmtree"]


def _record_failure(item: "WorkItem", err: Exception) -> None:
	try:
		FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
		with FAIL_LOG.open("a", encoding="utf-8") as f:
			f.write(json.dumps({
				"ts": datetime.now().isoformat(),
				"op": item.op,
				"src": str(item.src) if item.src else None,
				"dst": str(item.dst) if item.dst else None,
				"error": str(err),
				"retries": item.retries,
			}) + "\n")
	except Exception as e:  # pragma: no cover
		logger.error("write failure log failed: %s", e)


@dataclass
class WorkItem:
	op: Operation
	src: Optional[Path]
	dst: Optional[Path]
	retries: int = 3
	backoff_ms: int = 200
	verify: bool = True
	dedup_strategy: str = "rename"  # skip | rename | overwrite


@dataclass
class SourceSpec:
	root: Path
	targets: Sequence[Path]


def _filesize(p: Path) -> int:
	try:
		return p.stat().st_size
	except FileNotFoundError:
		return -1


class WorkerPool:
	def __init__(self, num_workers: int = 4):
		self.num_workers = max(1, num_workers)
		self._q: "queue.Queue[Optional[WorkItem]]" = queue.Queue(maxsize=4096)
		self._threads: List[threading.Thread] = []
		self._stopped = threading.Event()

	def start(self) -> None:
		for i in range(self.num_workers):
			th = threading.Thread(target=self._run, name=f"sync-worker-{i}", daemon=True)
			self._threads.append(th)
			th.start()

	def stop(self) -> None:
		self._stopped.set()
		for _ in self._threads:
			self._q.put(None)
		for th in self._threads:
			th.join()

	def submit(self, item: WorkItem) -> None:
		try:
			self._q.put(item, block=True, timeout=5)
		except queue.Full:
			logger.warning("work queue full, dropping: %s %s -> %s", item.op, item.src, item.dst)

	def _run(self) -> None:
		while not self._stopped.is_set():
			try:
				item = self._q.get(timeout=0.5)
			except queue.Empty:
				continue
			if item is None:
				break
			try:
				self._process_with_retry(item)
			except Exception as e:
				logger.warning("work failed: %s (%s -> %s): %s", item.op, item.src, item.dst, e)
			finally:
				self._q.task_done()

	def _process_with_retry(self, item: WorkItem) -> None:
		attempt = 0
		backoff = max(50, item.backoff_ms)
		while True:
			try:
				self._process_once(item)
				return
			except Exception as e:
				attempt += 1
				if attempt > max(0, item.retries):
					_record_failure(item, e)
					raise
				logger.warning("op %s failed (attempt %d/%d): %s", item.op, attempt, item.retries, e)
				time.sleep(backoff / 1000)
				backoff = min(5000, backoff * 2)

	def _apply_dedup(self, dst: Path, strategy: str) -> Path:
		if not dst.exists():
			return dst
		if strategy == "overwrite":
			return dst
		if strategy == "skip":
			raise FileExistsError(f"skip duplicate: {dst}")
		base = dst.stem
		suffix = dst.suffix
		parent = dst.parent
		idx = 1
		while True:
			candidate = parent / f"{base}({idx}){suffix}"
			if not candidate.exists():
				return candidate
			idx += 1

	def _process_once(self, item: WorkItem) -> None:
		if item.op == "mkdir" and item.dst is not None:
			item.dst.mkdir(parents=True, exist_ok=True)
			logger.info("mkdir: %s", item.dst)
		elif item.op == "copy" and item.src is not None and item.dst is not None:
			dst = self._apply_dedup(item.dst, item.dedup_strategy)
			dst.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(item.src, dst)
			if item.verify:
				src_size = _filesize(item.src)
				dst_size = _filesize(dst)
				if src_size != -1 and dst_size != -1 and src_size != dst_size:
					raise IOError(f"size mismatch after copy: {item.src} ({src_size}) != {dst} ({dst_size})")
			logger.info("copy2: %s -> %s", item.src, dst)
		elif item.op == "move" and item.src is not None and item.dst is not None:
			dst = self._apply_dedup(item.dst, item.dedup_strategy)
			dst.parent.mkdir(parents=True, exist_ok=True)
			shutil.move(str(item.src), str(dst))
			if item.verify:
				dst_size = _filesize(dst)
				src_size = _filesize(item.src)
				if src_size != -1 and dst_size != -1 and src_size != dst_size:
					raise IOError(f"size mismatch after move: {item.src} -> {dst}")
			logger.info("move: %s -> %s", item.src, dst)
		elif item.op == "unlink" and item.dst is not None:
			item.dst.unlink(missing_ok=True)
			logger.info("unlink: %s", item.dst)
		elif item.op == "rmtree" and item.dst is not None:
			shutil.rmtree(item.dst, ignore_errors=True)
			logger.info("rmtree: %s", item.dst)


class AsyncWorkerPool:
	def __init__(self, num_workers: int = 4, maxsize: int = 4096, io_retries: int = 3, io_backoff_ms: int = 200, verify: bool = True):
		self.num_workers = max(1, num_workers)
		self._q: "asyncio.Queue[WorkItem]" = asyncio.Queue(maxsize=maxsize)
		self._tasks: List[asyncio.Task[None]] = []
		self._io_retries = io_retries
		self._io_backoff_ms = io_backoff_ms
		self._verify = verify
		self._stopping = asyncio.Event()

	async def start(self) -> None:
		for _ in range(self.num_workers):
			self._tasks.append(asyncio.create_task(self._run()))

	async def stop(self) -> None:
		self._stopping.set()
		for t in self._tasks:
			await t

	async def submit(self, item: WorkItem) -> None:
		item.retries = self._io_retries
		item.backoff_ms = self._io_backoff_ms
		item.verify = self._verify
		try:
			await asyncio.wait_for(self._q.put(item), timeout=5)
		except asyncio.TimeoutError:
			logger.warning("async work queue full, dropping: %s %s -> %s", item.op, item.src, item.dst)

	def _apply_dedup(self, dst: Path, strategy: str) -> Path:
		if not dst.exists():
			return dst
		if strategy == "overwrite":
			return dst
		if strategy == "skip":
			raise FileExistsError(f"skip duplicate: {dst}")
		base = dst.stem
		suffix = dst.suffix
		parent = dst.parent
		idx = 1
		while True:
			candidate = parent / f"{base}({idx}){suffix}"
			if not candidate.exists():
				return candidate
			idx += 1

	def _process_once(self, item: WorkItem) -> None:
		if item.op == "mkdir" and item.dst is not None:
			item.dst.mkdir(parents=True, exist_ok=True)
			logger.info("mkdir: %s", item.dst)
		elif item.op == "copy" and item.src is not None and item.dst is not None:
			dst = self._apply_dedup(item.dst, item.dedup_strategy)
			dst.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(item.src, dst)
			if item.verify:
				src_size = _filesize(item.src)
				dst_size = _filesize(dst)
				if src_size != -1 and dst_size != -1 and src_size != dst_size:
					raise IOError(f"size mismatch after copy: {item.src} ({src_size}) != {dst} ({dst_size})")
			logger.info("copy2: %s -> %s", item.src, dst)
		elif item.op == "move" and item.src is not None and item.dst is not None:
			dst = self._apply_dedup(item.dst, item.dedup_strategy)
			dst.parent.mkdir(parents=True, exist_ok=True)
			shutil.move(str(item.src), str(dst))
			if item.verify:
				dst_size = _filesize(dst)
				src_size = _filesize(item.src)
				if src_size != -1 and dst_size != -1 and src_size != dst_size:
					raise IOError(f"size mismatch after move: {item.src} -> {dst}")
			logger.info("move: %s -> %s", item.src, dst)
		elif item.op == "unlink" and item.dst is not None:
			item.dst.unlink(missing_ok=True)
			logger.info("unlink: %s", item.dst)
		elif item.op == "rmtree" and item.dst is not None:
			shutil.rmtree(item.dst, ignore_errors=True)
			logger.info("rmtree: %s", item.dst)

	async def _run(self) -> None:
		while not self._stopping.is_set():
			try:
				item = await asyncio.wait_for(self._q.get(), timeout=0.5)
			except asyncio.TimeoutError:
				continue
			try:
				await self._process_with_retry(item)
			except Exception as e:
				logger.warning("async work failed: %s (%s -> %s): %s", item.op, item.src, item.dst, e)
			finally:
				self._q.task_done()

	async def _process_with_retry(self, item: WorkItem) -> None:
		attempt = 0
		backoff = max(50, item.backoff_ms)
		while True:
			try:
				await asyncio.to_thread(self._process_once, item)
				return
			except FileExistsError:
				logger.info("skip existing (dedup=skip): %s", item.dst)
				return
			except Exception as e:
				attempt += 1
				if attempt > max(0, item.retries):
					_record_failure(item, e)
					raise
				logger.warning("op %s failed (attempt %d/%d): %s", item.op, attempt, item.retries, e)
				await asyncio.sleep(backoff / 1000)
				backoff = min(5000, backoff * 2)


class SyncEventHandler(FileSystemEventHandler):
	def __init__(self, sources: List[SourceSpec], include_patterns: List[str], exclude_patterns: List[str], ignore_dotfiles: bool, auto_threshold: Optional[int] = None, auto_prefix: Optional[str] = None, auto_cooldown_s: int = 60, num_workers: int = 4, keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S", range_naming: bool = True, range_date_format: str = "%Y%m%d", fill_previous: bool = True, interested_events: Optional[List[str]] = None, organize_target_dirs: Optional[List[Path]] = None):
		self.sources = sources
		self.include_patterns = include_patterns
		self.exclude_patterns = exclude_patterns
		self.ignore_dotfiles = ignore_dotfiles
		self.auto_threshold = auto_threshold
		self.auto_prefix = auto_prefix
		self.auto_cooldown_s = auto_cooldown_s
		self._last_auto_ts: dict[Path, float] = {}  # 每个目录单独的冷却时间
		self._pool = WorkerPool(num_workers=num_workers)
		self._pool.start()
		self._async_pool: Optional[AsyncWorkerPool] = None
		self._loop: Optional[asyncio.AbstractEventLoop] = None
		self._keep_source = keep_source
		self._dedup_strategy = dedup_strategy
		self._name_template = name_template
		self._timestamp_format = timestamp_format
		self._range_naming = range_naming
		self._range_date_format = range_date_format
		self._fill_previous = fill_previous
		self._interested = set((interested_events or ["created","modified","moved","deleted"]))
		self._organize_target_dirs = organize_target_dirs or []

	def attach_async_pool(self, pool: AsyncWorkerPool, loop: asyncio.AbstractEventLoop) -> None:
		self._async_pool = pool
		self._loop = loop

	def shutdown(self) -> None:
		self._pool.stop()

	def _match(self, rel_path: str) -> bool:
		name = rel_path.split("/")[-1]
		if self.ignore_dotfiles and name.startswith('.'):
			return False
		if self.exclude_patterns and any(fnmatch.fnmatch(rel_path, p) for p in self.exclude_patterns):
			return False
		if self.include_patterns and not any(fnmatch.fnmatch(rel_path, p) for p in self.include_patterns):
			return False
		return True

	def _resolve_targets(self, src_path: Path) -> Optional[List[Path]]:
		for spec in self.sources:
			try:
				rel = src_path.relative_to(spec.root)
			except ValueError:
				continue
			return [t / rel for t in spec.targets]
		return None

	def _auto_bases(self) -> list[Path]:
		bases: list[Path] = []
		# 添加 watch.mappings 中的目标目录
		for spec in self.sources:
			for t in spec.targets:
				if t not in bases:
					bases.append(t)
		# 添加 organize.target_dirs 中的目录
		for t in self._organize_target_dirs:
			if t not in bases:
				bases.append(t)
		return bases

	def _maybe_auto_organize(self) -> None:
		if not self.auto_threshold:
			return
		now = time.time()
		bases = self._auto_bases()
		if not bases:
			return
		triggered = False
		for base in bases:
			# 检查该目录的冷却时间
			last_ts = self._last_auto_ts.get(base, 0.0)
			if now - last_ts < self.auto_cooldown_s:
				logger.debug("auto organize cooldown for %s: %.1fs remaining", base, self.auto_cooldown_s - (now - last_ts))
				continue
			try:
				count = sum(1 for p in base.rglob("*") if p.is_file())
			except FileNotFoundError:
				continue
			if count >= self.auto_threshold:
				logger.info("auto organize triggered: %d >= %d (base=%s)", count, self.auto_threshold, base)
				# 使用与配置一致的参数进行自动整理
				import asyncio
				stats = asyncio.run(organize_by_mtime_multi_async(
					[base], 
					chunk_size=self.auto_threshold, 
					prefix=self.auto_prefix, 
					dry_run=False,
					concurrency=4,  # 自动整理使用较低的并发数
					keep_source=self._keep_source,
					dedup_strategy=getattr(self, '_dedup_strategy', 'rename'),
					name_template=getattr(self, '_name_template', None),
					timestamp_format=getattr(self, '_timestamp_format', "%Y%m%d_%H%M%S"),
					range_naming=getattr(self, '_range_naming', True),
					range_date_format=getattr(self, '_range_date_format', "%Y%m%d"),
					fill_previous=getattr(self, '_fill_previous', True)
				))
				logger.info("auto organize completed: %s", stats)
				# 只更新该目录的冷却时间
				self._last_auto_ts[base] = now
				triggered = True

	def _submit(self, item: WorkItem) -> None:
		if self._async_pool and self._loop:
			asyncio.run_coroutine_threadsafe(self._async_pool.submit(item), self._loop)
		else:
			self._pool.submit(item)

	def _log_event(self, kind: str, **payload: object) -> None:
		try:
			EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
			with EVENT_LOG.open("a", encoding="utf-8") as f:
				f.write(json.dumps({"ts": datetime.now().isoformat(), "type": kind, **payload}) + "\n")
		except Exception:
			pass

	def on_created(self, event: FileSystemEvent) -> None:
		if "created" not in self._interested:
			return
		logger.debug("created: %s", event.src_path)
		self._log_event("created", path=event.src_path, is_dir=event.is_directory)
		self._handle_create_or_modify(event)
		self._maybe_auto_organize()

	def on_modified(self, event: FileSystemEvent) -> None:
		if "modified" not in self._interested:
			return
		logger.debug("modified: %s", event.src_path)
		self._log_event("modified", path=event.src_path, is_dir=event.is_directory)
		self._handle_create_or_modify(event)
		self._maybe_auto_organize()

	def _handle_create_or_modify(self, event: FileSystemEvent) -> None:
		if event.is_directory:
			tgts = self._resolve_targets(Path(event.src_path))
			if tgts is not None:
				for d in tgts:
					self._submit(WorkItem(op="mkdir", src=None, dst=d))
			return
		tgts = self._resolve_targets(Path(event.src_path))
		if tgts is None:
			return
		rel = '/'.join(tgts[0].parts[-2:]) if len(tgts[0].parts) >= 2 else tgts[0].name
		if not self._match(rel):
			logger.debug("skip (pattern): %s", rel)
			return
		for tgt in tgts:
			self._submit(WorkItem(op="copy", src=Path(event.src_path), dst=tgt))

	def on_moved(self, event: FileMovedEvent | DirMovedEvent) -> None:
		if "moved" not in self._interested:
			return
		self._log_event("moved", src=event.src_path, dst=event.dest_path, is_dir=event.is_directory)
		src_tgts = self._resolve_targets(Path(event.src_path))
		dst_tgts = self._resolve_targets(Path(event.dest_path))
		if not src_tgts and not dst_tgts:
			return
		if event.is_directory:
			for src_tgt, dst_tgt in zip(src_tgts or [], dst_tgts or []):
				if self._keep_source:
					# In keep mode, treat move as create at new path
					self._submit(WorkItem(op="mkdir", src=None, dst=dst_tgt))
					continue
				if src_tgt.exists():
					self._submit(WorkItem(op="move", src=src_tgt, dst=dst_tgt))
				else:
					self._submit(WorkItem(op="mkdir", src=None, dst=dst_tgt))
			return
		for src_tgt, dst_tgt in zip(src_tgts or [], dst_tgts or []):
			if self._keep_source:
				# copy dest path from original source file
				self._submit(WorkItem(op="copy", src=Path(event.dest_path), dst=dst_tgts[0] if dst_tgts else dst_tgt))
				continue
			if src_tgt.exists():
				self._submit(WorkItem(op="move", src=src_tgt, dst=dst_tgt))
			else:
				self._submit(WorkItem(op="copy", src=Path(event.dest_path), dst=dst_tgt))
		self._maybe_auto_organize()

	def on_deleted(self, event: FileDeletedEvent | DirDeletedEvent) -> None:
		if "deleted" not in self._interested:
			return
		if self._keep_source:
			# keep mode: do not delete at targets
			return
		self._log_event("deleted", path=event.src_path, is_dir=event.is_directory)
		tgts = self._resolve_targets(Path(event.src_path))
		if tgts is None:
			return
		for tgt in tgts:
			if event.is_directory:
				self._submit(WorkItem(op="rmtree", src=None, dst=tgt))
			else:
				self._submit(WorkItem(op="unlink", src=None, dst=tgt))
		self._maybe_auto_organize()


def start_watching(sources: List[SourceSpec], include_patterns: List[str], exclude_patterns: List[str], ignore_dotfiles: bool, debounce_ms: int, auto_threshold: Optional[int] = None, auto_prefix: Optional[str] = None, auto_cooldown_s: int = 60, num_workers: int = 4, io_retries: int = 3, io_backoff_ms: int = 200, verify_mode: str = "size", keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S", range_naming: bool = True, range_date_format: str = "%Y%m%d", fill_previous: bool = True, interested_events: Optional[List[str]] = None, organize_target_dirs: Optional[List[Path]] = None) -> None:
	observer = Observer()
	handler = SyncEventHandler(sources, include_patterns, exclude_patterns, ignore_dotfiles, auto_threshold=auto_threshold, auto_prefix=auto_prefix, auto_cooldown_s=auto_cooldown_s, num_workers=num_workers, keep_source=keep_source, dedup_strategy=dedup_strategy, name_template=name_template, timestamp_format=timestamp_format, range_naming=range_naming, range_date_format=range_date_format, fill_previous=fill_previous, interested_events=interested_events, organize_target_dirs=organize_target_dirs)
	def submit_with_defaults(item: WorkItem) -> None:
		item.retries = io_retries
		item.backoff_ms = io_backoff_ms
		item.verify = (verify_mode == "size")
		item.dedup_strategy = dedup_strategy
		handler._pool.submit(item)
	handler._pool.submit = submit_with_defaults  # type: ignore
	for spec in sources:
		spec.root.mkdir(parents=True, exist_ok=True)
		observer.schedule(handler, str(spec.root), recursive=True)
		logger.info("watching: %s -> %s (keep_source=%s, dedup=%s)", spec.root, list(spec.targets), keep_source, dedup_strategy)
	observer.start()
	try:
		while True:
			time.sleep(max(0.05, debounce_ms / 1000))
	finally:
		observer.stop()
		observer.join()
		handler.shutdown()
		logger.info("observer stopped")


async def start_watching_async(sources: List[SourceSpec], include_patterns: List[str], exclude_patterns: List[str], ignore_dotfiles: bool, debounce_ms: int, auto_threshold: Optional[int] = None, auto_prefix: Optional[str] = None, auto_cooldown_s: int = 60, num_workers: int = 4, io_retries: int = 3, io_backoff_ms: int = 200, verify_mode: str = "size", keep_source: bool = False, dedup_strategy: str = "rename", name_template: Optional[str] = None, timestamp_format: str = "%Y%m%d_%H%M%S", range_naming: bool = True, range_date_format: str = "%Y%m%d", fill_previous: bool = True, interested_events: Optional[List[str]] = None, organize_target_dirs: Optional[List[Path]] = None) -> None:
	observer = Observer()
	handler = SyncEventHandler(sources, include_patterns, exclude_patterns, ignore_dotfiles, auto_threshold=auto_threshold, auto_prefix=auto_prefix, auto_cooldown_s=auto_cooldown_s, num_workers=1, keep_source=keep_source, dedup_strategy=dedup_strategy, name_template=name_template, timestamp_format=timestamp_format, range_naming=range_naming, range_date_format=range_date_format, fill_previous=fill_previous, interested_events=interested_events, organize_target_dirs=organize_target_dirs)
	async_pool = AsyncWorkerPool(num_workers=num_workers, io_retries=io_retries, io_backoff_ms=io_backoff_ms, verify=(verify_mode == "size"))
	handler.attach_async_pool(async_pool, asyncio.get_running_loop())
	# wrap async submit to inject defaults
	orig_submit = async_pool.submit
	async def submit_with_defaults(item: WorkItem) -> None:
		item.dedup_strategy = dedup_strategy
		await orig_submit(item)
	async_pool.submit = submit_with_defaults  # type: ignore
	await async_pool.start()
	for spec in sources:
		spec.root.mkdir(parents=True, exist_ok=True)
		observer.schedule(handler, str(spec.root), recursive=True)
		logger.info("watching(async): %s -> %s (keep_source=%s, dedup=%s)", spec.root, list(spec.targets), keep_source, dedup_strategy)
	observer.start()
	try:
		while True:
			await asyncio.sleep(max(0.05, debounce_ms / 1000))
	finally:
		observer.stop()
		observer.join()
		await async_pool.stop()
		handler.shutdown()
		logger.info("observer stopped (async)")
