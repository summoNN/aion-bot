"""Text-to-speech generation and on-disk caching using edge-tts."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
from pathlib import Path

import edge_tts

LOGGER = logging.getLogger(__name__)


class TTSService:
    """Generate MP3 speech once and reuse it for repeated announcements."""

    def __init__(self, voice: str, cache_dir: Path) -> None:
        self.voice = voice
        self.cache_dir = cache_dir
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def close(self) -> None:
        """Release per-key lock bookkeeping."""
        self._locks.clear()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def synthesize(self, text: str) -> Path:
        """Return a cached or newly generated MP3 path."""
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            raise ValueError("TTS text cannot be empty")
        cache_key = hashlib.sha256(f"{self.voice}\0{normalized}".encode("utf-8")).hexdigest()
        destination = self.cache_dir / f"{cache_key}.mp3"
        lock = await self._lock_for(cache_key)
        async with lock:
            if destination.is_file() and destination.stat().st_size > 0:
                return destination
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.cache_dir,
                    prefix=f".{cache_key}.",
                    suffix=".mp3",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                communicator = edge_tts.Communicate(normalized, self.voice)
                await communicator.save(str(temporary_path))
                temporary_path.replace(destination)
                LOGGER.info("Generated TTS cache file %s", destination.name)
                return destination
            except Exception:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                LOGGER.exception("TTS generation failed")
                raise
