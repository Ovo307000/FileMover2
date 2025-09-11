from __future__ import annotations

from pathlib import Path
import textwrap
import tempfile

from filemover2.config import load_config, ConfigError


def _write_tmp(text: str, suffix: str = ".yaml") -> Path:
	p = Path(tempfile.mkdtemp()) / f"cfg{suffix}"
	p.write_text(text, encoding="utf-8")
	return p


def test_invalid_when_no_blocks() -> None:
	p = _write_tmp("{}", suffix=".json")
	try:
		load_config(p)
	except ConfigError:
		pass
	else:
		raise AssertionError("expected ConfigError")


def test_load_mappings_multiple_targets() -> None:
	cfg_text = textwrap.dedent(
		"""
		watch:
		  mappings:
		    - source: "C:/A"
		      targets: ["D:/B", "E:/C"]
		organize:
		  target_dir: "D:/B"
		  chunk_size: 500
		"""
	)
	p = _write_tmp(cfg_text)
	cfg = load_config(p)
	assert cfg.watch is not None
	assert cfg.watch.use_mappings is True
	assert len(cfg.watch.mappings) == 1
	assert len(cfg.watch.mappings[0].targets) == 2
	# defaults
	assert cfg.watch.worker_threads == 4
	assert cfg.watch.io_retries == 3
	assert cfg.watch.io_backoff_ms == 200
	assert cfg.watch.verify_mode == "size"


def test_load_new_options_override() -> None:
	cfg_text = textwrap.dedent(
		"""
		watch:
		  mappings:
		    - source: "C:/A"
		      targets: ["D:/B"]
		  worker_threads: 8
		  io_retries: 5
		  io_backoff_ms: 500
		  verify_mode: none
		organize:
		  target_dir: "D:/B"
		"""
	)
	p = _write_tmp(cfg_text)
	cfg = load_config(p)
	assert cfg.watch.worker_threads == 8
	assert cfg.watch.io_retries == 5
	assert cfg.watch.io_backoff_ms == 500
	assert cfg.watch.verify_mode == "none"


def test_organize_keep_source_default_and_override() -> None:
	cfg_text = textwrap.dedent(
		"""
		watch:
		  mappings:
		    - source: "C:/A"
		      targets: ["D:/B"]
		organize:
		  target_dir: "D:/B"
		"""
	)
	p = _write_tmp(cfg_text)
	cfg = load_config(p)
	assert cfg.organize is not None
	assert cfg.organize.keep_source is False

	cfg_text2 = textwrap.dedent(
		"""
		watch:
		  mappings:
		    - source: "C:/A"
		      targets: ["D:/B"]
		organize:
		  target_dir: "D:/B"
		  keep_source: true
		"""
	)
	p2 = _write_tmp(cfg_text2)
	cfg2 = load_config(p2)
	assert cfg2.organize is not None
	assert cfg2.organize.keep_source is True
