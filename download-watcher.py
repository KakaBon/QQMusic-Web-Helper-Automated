from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional


def expand_path(value: str, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    p = Path(expanded)
    if not p.is_absolute():
        p = base_dir / p
    return p.resolve()


def sanitize_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = ''.join('_' if ch in invalid else ch for ch in name)
    cleaned = cleaned.rstrip(' .')
    return cleaned or 'untitled'


def unique_destination(folder: Path, base_name: str, extension: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / f'{base_name}{extension}'
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = folder / f'{base_name} ({i}){extension}'
        if not candidate.exists():
            return candidate
        i += 1


class DownloadWatcher:
    def __init__(self, watch_folder: Path, output_folder: Path,
                 ignored_extensions: set[str], poll_interval: float = 0.5,
                 stable_seconds: float = 2.0) -> None:
        self.watch_folder = watch_folder
        self.output_folder = output_folder
        self.ignored_extensions = {x.lower() for x in ignored_extensions}
        self.poll_interval = poll_interval
        self.stable_seconds = stable_seconds

    def _snapshot(self) -> dict[Path, tuple[int, float]]:
        result = {}
        if not self.watch_folder.exists():
            return result
        for p in self.watch_folder.iterdir():
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            result[p] = (st.st_size, st.st_mtime)
        return result

    def wait_for_new_file(self, armed_at: float,
                          stop_requested: Callable[[], bool],
                          status_callback: Optional[Callable[[str], None]] = None) -> Optional[Path]:
        before = self._snapshot()
        while not stop_requested():
            if status_callback:
                status_callback('Waiting for a new completed download...')
            candidates = []
            if self.watch_folder.exists():
                for p in self.watch_folder.iterdir():
                    if not p.is_file():
                        continue
                    if p.suffix.lower() in self.ignored_extensions:
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if p not in before and st.st_mtime >= armed_at:
                        candidates.append((st.st_mtime, p))
            if candidates:
                candidates.sort(reverse=True)
                newest = candidates[0][1]
                stable_since = time.time()
                last_size = -1
                while not stop_requested():
                    if not newest.exists():
                        break
                    try:
                        size = newest.stat().st_size
                    except OSError:
                        break
                    if size == last_size and size > 0:
                        if time.time() - stable_since >= self.stable_seconds:
                            return newest
                    else:
                        last_size = size
                        stable_since = time.time()
                    if status_callback:
                        status_callback(f'Download found, waiting until complete: {newest.name}')
                    time.sleep(self.poll_interval)
            time.sleep(self.poll_interval)
        return None

    def move_and_rename(self, src: Path, final_base_name: str) -> Path:
        extension = src.suffix
        dst = unique_destination(self.output_folder, sanitize_filename(final_base_name), extension)
        shutil.move(str(src), str(dst))
        return dst
