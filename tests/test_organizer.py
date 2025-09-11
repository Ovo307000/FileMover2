from __future__ import annotations

from pathlib import Path
import tempfile
import time

from filemover2.organizer import organize_by_mtime, organize_by_mtime_multi


def _touch(p: Path, mtime: float) -> None:
	p.parent.mkdir(parents=True, exist_ok=True)
	p.write_text("x", encoding="utf-8")
	# set mtime
	import os
	os.utime(p, (mtime, mtime))


def test_organize_single_dir_moves_into_batches(tmp_path: Path) -> None:
	# create 3 files with increasing mtime
	base = tmp_path / "data"
	base.mkdir()
	now = time.time()
	for i in range(3):
		_touch(base / f"f{i}.txt", now + i)
	batches = organize_by_mtime(base, chunk_size=2, prefix="batch", dry_run=False)
	assert len(batches) == 2
	assert batches[0].exists()
	assert batches[1].exists()
	# all files moved
	assert not any(base.glob("*.txt"))


def test_organize_multi_dir(tmp_path: Path) -> None:
	root1 = tmp_path / "d1"
	root2 = tmp_path / "d2"
	root1.mkdir()
	root2.mkdir()
	now = time.time()
	_touch(root1 / "a.txt", now)
	_touch(root2 / "b.txt", now + 1)
	batches = organize_by_mtime_multi([root1, root2], chunk_size=1, prefix=None, dry_run=False)
	assert len(batches) == 2
	# batches should be created at parent of first dir
	assert batches[0].parent == tmp_path
