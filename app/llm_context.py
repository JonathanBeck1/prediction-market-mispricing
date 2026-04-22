"""Shared LLM context loader — reads config/llm/*.md files into prompt strings.

All MD files in config/llm/ are loaded once and cached. Scripts call
load_context() to get a dict of {filename_stem: content} which they inject
into system prompts and user prompts.

Files:
  config/llm/mission.md          — What this system does and the LLM's core job
  config/llm/event_formats.md    — Per-format suppression/boost profiles
  config/llm/trump_patterns.md   — Trump-specific speech patterns and frequencies
  config/llm/calibration_guide.md — How to produce calibrated multipliers
  config/llm/global_signals_guide.md — Guide for global (analyze_signals) step
  config/llm/per_event_guide.md  — Guide for per-event (analyze_event) step
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LLM_CONFIG_DIR = Path("config/llm")

_CACHE: dict[str, str] = {}
_CACHE_MTIME: dict[str, float] = {}


def _load_file(path: Path) -> str:
    try:
        mtime = path.stat().st_mtime
        cached_mtime = _CACHE_MTIME.get(path.name, 0.0)
        if mtime <= cached_mtime and path.name in _CACHE:
            return _CACHE[path.name]
        content = path.read_text(encoding="utf-8").strip()
        _CACHE[path.name] = content
        _CACHE_MTIME[path.name] = mtime
        return content
    except Exception as exc:
        logger.warning("Could not load LLM context file %s: %s", path, exc)
        return ""


def load_context(keys: list[str] | None = None) -> dict[str, str]:
    """Load and return LLM context files as {stem: content}.

    Args:
        keys: List of file stems to load (e.g. ['mission', 'event_formats']).
              If None, loads all .md files in config/llm/.
    """
    config_dir = LLM_CONFIG_DIR
    if not config_dir.exists():
        # Try relative to repo root
        repo_root = Path(__file__).resolve().parent.parent
        config_dir = repo_root / "config" / "llm"

    result: dict[str, str] = {}

    if keys is None:
        files = sorted(config_dir.glob("*.md")) if config_dir.exists() else []
    else:
        files = [config_dir / f"{k}.md" for k in keys]

    for path in files:
        if not path.exists():
            logger.debug("LLM context file not found: %s", path)
            continue
        content = _load_file(path)
        if content:
            result[path.stem] = content

    return result


def build_system_prompt(keys: list[str]) -> str:
    """Build a system prompt string by concatenating the specified MD files.

    The files are joined with clear section headers so the LLM can navigate them.
    """
    ctx = load_context(keys)
    if not ctx:
        return ""

    sections: list[str] = []
    for stem, content in ctx.items():
        sections.append(f"{'='*60}\n{content}")

    return "\n\n".join(sections)


def list_available() -> list[str]:
    """Return list of available context file stems."""
    config_dir = LLM_CONFIG_DIR
    if not config_dir.exists():
        repo_root = Path(__file__).resolve().parent.parent
        config_dir = repo_root / "config" / "llm"
    return [p.stem for p in sorted(config_dir.glob("*.md"))] if config_dir.exists() else []
