from __future__ import annotations

import re
import time
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)


class AutomationError(RuntimeError):
    pass


class AutomationStopped(AutomationError):
    pass


class SkipCurrent(AutomationError):
    pass


@dataclass
class TrackResult:
    raw_path: Path
    matched_title: str
    matched_artist: str
    matched_album: str
    matched_score: float
    resource_name: str
    expected_size: int
    actual_size: int


def normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[·•\-—–_/\\|&]+", "", text)
    return text


def similarity(a: str, b: str) -> float:
    a = normalize(a)
    b = normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _compact_for_suffix(text: str) -> str:
    """
    Compact text for structural title analysis while preserving brackets.
    """
    text = (text or "").lower().strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[·•\-—–_/\\|&]+", "", text)
    return text


def _split_base_and_trailing_brackets(text: str) -> tuple[str, list[str]]:
    """
    Split a title into:
      base title
      trailing ordinary/full-width bracket blocks

    Examples:
      LOVE SCENARIO（曾经爱过）
        -> ("lovescenario", ["曾经爱过"])

      RHB（熊猫血）（DJ 超短版）
        -> ("rhb", ["熊猫血", "dj超短版"])

      RHB（熊猫血）《灵魂驱动》广播剧插曲
        -> base remains the full compact title because 《...》 is deliberately
           NOT treated as an ordinary version-style bracket block here.
    """
    s = _compact_for_suffix(text)

    pairs = {
        ")": "(",
        "）": "（",
        "]": "[",
        "］": "［",
        "】": "【",
        "〕": "〔",
    }

    blocks = []

    while s and s[-1] in pairs:
        close = s[-1]
        open_ch = pairs[close]

        depth = 0
        found = -1

        for i in range(len(s) - 1, -1, -1):
            ch = s[i]
            if ch == close:
                depth += 1
            elif ch == open_ch:
                depth -= 1
                if depth == 0:
                    found = i
                    break

        if found < 0:
            break

        block = s[found + 1:-1]
        blocks.insert(0, block)
        s = s[:found]

    return s, blocks


def _strip_book_title_suffix_for_scoring(text: str) -> str:
    """
    Scoring only: if a QQ Music candidate title contains 《, ignore that
    character and everything after it.

    Example:
      梦之彼岸《初三的六一儿童节》猫耳FM广播剧主题曲
        -> 梦之彼岸

    This does NOT change the search query, displayed QQ title, playback target,
    or final filename. It only changes the title text used for match scoring.
    """
    text = text or ""
    pos = text.find("《")
    if pos >= 0:
        text = text[:pos]
    return text.strip()


def _has_book_title_suffix(text: str) -> bool:
    """
    True when QQ Music displays additional 《...》 information after a
    non-empty song-title prefix. In this task set, that is useful evidence
    for an OST / drama / film soundtrack result.

    The 《...》 suffix is still ignored for title-similarity calculation;
    this function only supplies an extra ranking bonus.
    """
    text = (text or "").strip()
    pos = text.find("《")
    return pos > 0


def analyze_title_structure(target_title: str, candidate_title: str) -> dict:
    # QQ Music often appends grey descriptive text beginning with 《...》 to
    # the visible candidate title. Ignore that suffix for scoring only.
    candidate_title = _strip_book_title_suffix_for_scoring(candidate_title)

    target_full = _compact_for_suffix(target_title)
    candidate_full = _compact_for_suffix(candidate_title)

    target_base, target_blocks = _split_base_and_trailing_brackets(target_title)
    candidate_base, candidate_blocks = _split_base_and_trailing_brackets(candidate_title)

    full_exact = bool(target_full) and target_full == candidate_full
    base_exact = bool(target_base) and target_base == candidate_base

    # Case A:
    # candidate contains the exact full requested title and then adds another
    # ordinary bracket block, e.g.
    #   RHB（熊猫血）（DJ 超短版）
    added_after_full = False
    if target_full and candidate_full.startswith(target_full) and candidate_full != target_full:
        tail = candidate_full[len(target_full):]
        if tail:
            added_after_full = tail[0] in "（([【〔［"

    # Case B:
    # both have the same base title, but candidate has a different trailing
    # bracket annotation than the target, e.g.
    #   target:    LOVE SCENARIO（曾经爱过）
    #   candidate: LOVE SCENARIO（国语）
    #
    # Important:
    # if candidate has NO trailing bracket at all, do NOT penalize it. That
    # lets the clean base title beat a conflicting localized/version suffix.
    conflicting_bracket = False
    if base_exact and candidate_blocks:
        if target_blocks:
            conflicting_bracket = candidate_blocks != target_blocks
        else:
            conflicting_bracket = True

    return {
        "full_exact": full_exact,
        "base_exact": base_exact,
        "added_after_full": added_after_full,
        "conflicting_bracket": conflicting_bracket,
        "target_blocks": target_blocks,
        "candidate_blocks": candidate_blocks,
    }


def _split_artist_tokens(text: str) -> list[str]:
    """
    Split multi-artist strings into normalized artist tokens.

    Separators commonly seen in the source/QQ results:
      &, /, comma, Chinese/Japanese comma, semicolon, plus sign

    Example:
      "Phuwin&Satang Kittiphop"
        -> ["phuwin", "satangkittiphop"]

      "Phuwin / Satang Kittiphop / Winny Thanawin"
        -> ["phuwin", "satangkittiphop", "winnythanawin"]
    """
    raw = (text or "").strip()
    parts = re.split(r"\s*(?:&|/|,|，|、|;|；|\+)\s*", raw)
    return [normalize(p) for p in parts if normalize(p)]


def _artist_subset_match(target_artist: str, candidate_artist: str) -> bool:
    target_parts = _split_artist_tokens(target_artist)
    candidate_parts = _split_artist_tokens(candidate_artist)

    if not target_parts or not candidate_parts:
        return False

    candidate_set = set(candidate_parts)
    return all(part in candidate_set for part in target_parts)


def _has_ost_evidence(text: str) -> bool:
    """
    Detect OST/soundtrack identity evidence in the QQ candidate title.

    This is not a version vocabulary. These markers all mean soundtrack/OST
    identity and should not be punished like an alternate version.
    """
    raw = (text or "").lower()

    return (
        "《" in raw
        or "original soundtrack" in raw
        or re.search(r"(?<![a-z])ost(?![a-z])", raw) is not None
        or "soundtrack" in raw
        or "เพลงประกอบ" in raw
    )


def score_result(
    target_title: str,
    target_artist: str,
    item: dict,
) -> float:
    candidate_title = item.get("title", "")
    candidate_artist = item.get("artist", "")

    # Keep original QQ title for logging/UI, but strip 《...》 suffix from the
    # title text used for similarity scoring.
    candidate_title_for_scoring = _strip_book_title_suffix_for_scoring(candidate_title)

    title_sim = similarity(target_title, candidate_title_for_scoring)
    artist_sim = similarity(target_artist, candidate_artist)

    artist_exact = normalize(candidate_artist) == normalize(target_artist)
    artist_subset = _artist_subset_match(target_artist, candidate_artist)

    structure = analyze_title_structure(
        target_title,
        candidate_title_for_scoring,
    )

    target_compact = _compact_for_suffix(target_title)
    candidate_compact = _compact_for_suffix(candidate_title_for_scoring)

    title_exact = structure["full_exact"]

    # Strong title relation for truncated source titles:
    # if QQ's cleaned candidate starts with the complete source title, treat
    # that as strong evidence rather than ordinary fuzzy similarity.
    title_prefix = (
        bool(target_compact)
        and bool(candidate_compact)
        and candidate_compact.startswith(target_compact)
    )

    ost_evidence = _has_ost_evidence(candidate_title)

    # If the candidate adds soundtrack identity text after the requested title,
    # do not treat that added bracket as a version conflict.
    added_after_full = structure["added_after_full"]
    conflicting_bracket = structure["conflicting_bracket"]

    if ost_evidence and title_prefix:
        added_after_full = False
        conflicting_bracket = False

    exact_pair = title_exact and artist_exact

    score = title_sim * 70 + artist_sim * 30

    if title_exact:
        score += 30

    if artist_exact:
        score += 25
    elif artist_subset:
        # Source artist list is fully contained in QQ's longer collaboration list.
        score += 25

    if title_prefix and not title_exact:
        score += 25

    if title_sim < 0.35:
        score -= 40
    if artist_sim < 0.25 and not artist_subset:
        score -= 20

    # Generic alternate-version penalties remain structural.
    if added_after_full:
        score -= 35

    if conflicting_bracket:
        score -= 35

    # Positive OST evidence. Strong only when artist identity is trustworthy.
    if ost_evidence:
        if artist_exact or artist_subset:
            score += 35
        elif artist_sim >= 0.75:
            score += 15

    item["_exact_pair"] = exact_pair
    item["_artist_exact"] = artist_exact
    item["_artist_subset"] = artist_subset
    item["_title_prefix"] = title_prefix
    item["_ost_evidence"] = ost_evidence
    item["_book_title_suffix"] = _has_book_title_suffix(candidate_title)
    item["_base_exact"] = structure["base_exact"]
    item["_added_after_full"] = added_after_full
    item["_conflicting_bracket"] = conflicting_bracket
    item["_title_sim"] = round(title_sim, 4)
    item["_artist_sim"] = round(artist_sim, 4)

    return round(score, 2)


class QQMusicAutomation:
    def __init__(
        self,
        config: dict,
        log: Optional[Callable[[str], None]] = None,
        step: Optional[Callable[[str, str], None]] = None,
        stop_requested: Optional[Callable[[], bool]] = None,
        pause_requested: Optional[Callable[[], bool]] = None,
        skip_requested: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.config = config
        self.log_cb = log or (lambda _msg: None)
        self.step_cb = step or (lambda _name, _value: None)
        self.stop_requested = stop_requested or (lambda: False)
        self.pause_requested = pause_requested or (lambda: False)
        self.skip_requested = skip_requested or (lambda: False)

        self.cdp_url = config.get("cdp_url", "http://127.0.0.1:9222")
        self.cat_id = config.get("cat_catch_extension_id", "jfedfbgedapdagkghmgibemcoggfppbb")
        self.cat_prefix = f"chrome-extension://{self.cat_id}/"
        self.search_timeout_ms = int(float(config.get("search_timeout_seconds", 18)) * 1000)
        self.search_max_wait = float(config.get("search_max_wait_seconds", 45))
        self.search_poll_interval = float(config.get("search_poll_interval_seconds", 0.5))
        self.capture_timeout = float(config.get("capture_timeout_seconds", 20))
        self.match_threshold = float(config.get("match_threshold", 80))

        self._pw = None
        self.browser = None
        self.context = None
        self.cat_page = None
        self.created_cat_page = False

    def log(self, msg: str) -> None:
        self.log_cb(msg)

    def step(self, name: str, value: str) -> None:
        self.step_cb(name, value)

    def check_control(self) -> None:
        if self.stop_requested():
            raise AutomationStopped("Stopped by user.")
        if self.skip_requested():
            raise SkipCurrent("Skipped by user.")
        while self.pause_requested():
            if self.stop_requested():
                raise AutomationStopped("Stopped by user.")
            if self.skip_requested():
                raise SkipCurrent("Skipped by user.")
            time.sleep(0.2)

    def start(self) -> None:
        self.step("browser", "Connecting...")
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        if not self.browser.contexts:
            raise AutomationError("No browser context found.")
        self.context = self.browser.contexts[0]
        self.step("browser", "✓ Connected")
        self.log(f"Connected to Chrome via CDP: {self.cdp_url}")

    def close(self) -> None:
        try:
            if self.created_cat_page and self.cat_page is not None:
                self.cat_page.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass

    def _goto_qq(self, page, url: str, timeout: int = 18000) -> None:
        """
        Best-effort QQ Music navigation.

        QQ Music occasionally throws Chromium HTTP/2 protocol errors even when
        the page can succeed on an immediate retry.  Navigation itself is not
        our readiness signal; the caller waits for the actual UI element it
        needs, so a transient protocol error must not immediately kill the task.
        """
        last_error = None

        for attempt in range(1, 3):
            self.check_control()

            try:
                page.goto(url, wait_until="commit", timeout=timeout)
                return
            except PlaywrightTimeoutError as exc:
                last_error = exc
                current = page.url or ""

                if "y.qq.com" in current:
                    self.log(
                        f'QQ Music navigation timed out on attempt {attempt}, '
                        f'but the tab already reached "{current}"; continuing.'
                    )
                    return

                self.log(
                    f"QQ Music navigation timeout on attempt {attempt}/2; retrying."
                )

            except PlaywrightError as exc:
                last_error = exc
                current = page.url or ""
                message = str(exc)

                if "ERR_HTTP2_PROTOCOL_ERROR" in message:
                    self.log(
                        f"QQ Music HTTP/2 protocol error on attempt {attempt}/2; "
                        "retrying navigation."
                    )
                elif "y.qq.com" in current:
                    self.log(
                        f'QQ Music navigation raised "{message}" after the tab '
                        f'already reached "{current}"; continuing.'
                    )
                    return
                else:
                    self.log(
                        f"QQ Music navigation error on attempt {attempt}/2: {message}"
                    )

            if attempt < 2:
                try:
                    page.wait_for_timeout(600)
                except Exception:
                    time.sleep(0.6)

        # Final fallback: ask the existing tab to navigate itself. This avoids
        # failing the whole task solely because page.goto hit a transient
        # Chromium network-stack error. The downstream element wait decides
        # whether the page actually became usable.
        try:
            page.evaluate("(target) => { window.location.href = target; }", url)
            self.log(
                "QQ Music page.goto failed twice; used window.location fallback "
                "and will continue with element-based readiness."
            )
            return
        except Exception:
            pass

        if last_error is not None:
            raise last_error
        raise AutomationError(f"Could not navigate to QQ Music URL: {url}")

    def _find_search_page(self):
        for page in self.context.pages:
            if "y.qq.com" in page.url and "/ryqq_v2/player" not in page.url:
                return page

        # No need to load the QQ Music homepage first.  run_track immediately
        # navigates this tab to the actual search URL, so the homepage hop only
        # adds latency and another opportunity for QQ's HTTP/2 error.
        page = self.context.new_page()
        self.log("Created a blank search tab; skipping the QQ Music homepage hop.")
        return page

    def _ensure_cat_host(self):
        # IMPORTANT: keep this identical in principle to the working
        # qq_catcatch_test.py.  Run chrome.tabs / chrome.runtime from an
        # actual Cat Catch extension PAGE, not from the MV3 service worker.
        # The stage test that successfully read Cat Catch data did exactly
        # this: reuse an existing chrome-extension://... page, otherwise open
        # popup.html temporarily.
        if self.cat_page is not None:
            try:
                if (self.cat_page.url or "").startswith(self.cat_prefix):
                    return self.cat_page
            except Exception:
                self.cat_page = None

        for page in self.context.pages:
            if (page.url or "").startswith(self.cat_prefix):
                self.cat_page = page
                self.created_cat_page = False
                return page

        page = self.context.new_page()
        page.goto(
            f"{self.cat_prefix}popup.html",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        self.cat_page = page
        self.created_cat_page = True
        self.log("Created temporary Cat Catch extension page (same method as qq_catcatch_test.py).")
        return page

    def _player_tabs(self) -> list[dict]:
        cat_host = self._ensure_cat_host()
        tabs = cat_host.evaluate(
            """
            async () => {
                return await new Promise((resolve, reject) => {
                    chrome.tabs.query(
                        {url: "*://y.qq.com/n/ryqq_v2/player*"},
                        tabs => {
                            if (chrome.runtime.lastError) {
                                reject(new Error(chrome.runtime.lastError.message));
                                return;
                            }
                            resolve(tabs.map(tab => ({
                                id: tab.id,
                                url: tab.url,
                                title: tab.title,
                                active: !!tab.active,
                                lastAccessed: tab.lastAccessed || 0
                            })));
                        }
                    );
                });
            }
            """
        )
        return list(tabs or [])

    def _player_pages(self):
        return [
            page for page in self.context.pages
            if "/ryqq_v2/player" in (page.url or "")
        ]

    def _get_cat_data(self, tab_id: int) -> list[dict]:
        cat_host = self._ensure_cat_host()
        data = cat_host.evaluate(
            """
            async (tabId) => {
                return await new Promise((resolve, reject) => {
                    chrome.runtime.sendMessage(
                        chrome.runtime.id,
                        {Message: "getData", tabId: tabId},
                        result => {
                            if (chrome.runtime.lastError) {
                                reject(new Error(chrome.runtime.lastError.message));
                                return;
                            }
                            resolve(result || []);
                        }
                    );
                });
            }
            """,
            tab_id,
        )
        return list(data or [])

    @staticmethod
    def _looks_like_audio_resource(item: dict) -> bool:
        ext = str(item.get("ext", "")).lower().lstrip(".")
        mime = str(item.get("type", "")).lower()
        name = str(item.get("name", "")).lower()
        url = str(item.get("url", "")).lower()

        audio_exts = {
            "m4a", "mp3", "flac", "aac", "ogg", "wav", "opus",
            "m4s", "mp4", "webm",
        }

        if ext in audio_exts:
            return True
        if mime.startswith("audio/"):
            return True
        if any(name.endswith("." + x) for x in audio_exts):
            return True
        if any(("." + x) in url.split("?")[0] for x in audio_exts):
            return True

        # QQ sometimes reports playable media as generic binary/video.
        if mime in {"application/octet-stream", "video/mp4"}:
            size = int(item.get("size") or 0)
            if size > 0:
                return True

        return False

    def _get_audio_data(self, tab_id: int) -> list[dict]:
        return [
            item for item in self._get_cat_data(tab_id)
            if self._looks_like_audio_resource(item)
        ]

    @staticmethod
    def _resource_key(item: dict) -> tuple[str, str, str]:
        return (
            str(item.get("requestId", "")),
            str(item.get("url", "")),
            str(item.get("getTime", "")),
        )

    def _wait_for_search_results(self, search_page) -> None:
        """
        Adaptive wait for QQ Music search results.

        QQ Music is very inconsistent: sometimes results appear in a few seconds,
        sometimes much later.  Poll the actual result rows and continue
        immediately once they are visible, up to a larger total ceiling.
        """
        started = time.time()
        last_log_bucket = -1

        while True:
            self.check_control()

            try:
                first = search_page.locator(".songlist__item").first
                if first.count() and first.is_visible():
                    elapsed = time.time() - started
                    self.log(f"Search results became visible after {elapsed:.1f}s.")
                    return
            except Exception:
                pass

            elapsed = time.time() - started
            if elapsed >= self.search_max_wait:
                raise AutomationError(
                    f"QQ Music search results did not become visible within "
                    f"{self.search_max_wait:.0f} seconds."
                )

            # Log every ~5 seconds so long waits are visible but not spammy.
            bucket = int(elapsed // 5)
            if bucket != last_log_bucket:
                last_log_bucket = bucket
                self.step("search", f"Waiting... {elapsed:.0f}s")
                self.log(
                    f"Waiting for QQ Music search results... "
                    f"{elapsed:.0f}/{self.search_max_wait:.0f}s"
                )

            time.sleep(self.search_poll_interval)

    def _read_search_results(self, search_page) -> tuple:
        rows = search_page.locator(".songlist__item")
        count = rows.count()
        results = []

        for i in range(count):
            row = rows.nth(i)

            def text_of(selector: str) -> str:
                loc = row.locator(selector)
                if not loc.count():
                    return ""
                try:
                    return loc.first.inner_text().strip()
                except Exception:
                    return ""

            results.append({
                "index": i,
                # Read QQ Music's complete displayed title again.
                # Do not try to strip grey descriptive text here.
                "title": text_of(".songlist__songname_txt"),
                "artist": text_of(".songlist__artist"),
                "album": text_of(".songlist__album"),
                "duration": text_of(".songlist__time"),
            })

        return rows, results

    def _download_resource(self, item: dict, output_dir: Path) -> tuple[Path, int, int]:
        name = item.get("name")
        url = item.get("url")
        expected_size = int(item.get("size") or 0)

        if not name or not url:
            raise AutomationError("Captured resource is missing name or URL.")

        headers = {"User-Agent": "Mozilla/5.0"}
        request_headers = item.get("requestHeaders") or {}

        if request_headers.get("origin"):
            headers["Origin"] = request_headers["origin"]
        if request_headers.get("referer"):
            headers["Referer"] = request_headers["referer"]
        if item.get("cookie"):
            headers["Cookie"] = item["cookie"]

        output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", str(name)).rstrip(" .") or "qqmusic_audio.m4a"
        raw_path = output_dir / f".qqauto_{time.time_ns()}_{safe_name}"

        request = urllib.request.Request(url, headers=headers, method="GET")

        self.check_control()
        with urllib.request.urlopen(request, timeout=60) as response:
            with open(raw_path, "wb") as f:
                while True:
                    self.check_control()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)

        actual_size = raw_path.stat().st_size

        if expected_size and actual_size != expected_size:
            try:
                raw_path.unlink()
            except OSError:
                pass
            raise AutomationError(
                f"Downloaded size mismatch: expected {expected_size}, got {actual_size}."
            )

        return raw_path, expected_size, actual_size

    def run_track(
        self,
        target_title: str,
        target_artist: str,
        search_query: str,
        output_dir: Path,
    ) -> TrackResult:
        self.check_control()
        search_page = self._find_search_page()
        self._ensure_cat_host()

        self.step("capture", "Snapshotting...")
        before_count_by_tab = {}
        before_resource_count = 0
        player_tabs_before = self._player_tabs()
        first_player_session = len(player_tabs_before) == 0

        for tab in player_tabs_before:
            tab_id = int(tab["id"])
            try:
                audio = self._get_audio_data(tab_id)
            except Exception:
                audio = []
            before_count_by_tab[tab_id] = len(audio)
            before_resource_count += len(audio)
            self.log(
                f"Cat Catch BEFORE: player tab {tab_id} has {len(audio)} audio resource(s)."
            )

        self.log(
            f"Cat Catch snapshot before playback: {before_resource_count} "
            f"audio resource(s) across {len(before_count_by_tab)} player tab(s)."
        )

        self.step("search", "Searching...")
        query = (search_query or f"{target_title} {target_artist}").strip()
        search_url = "https://y.qq.com/n/ryqq_v2/search?w=" + quote(query)

        rows = None
        results = []
        best = None

        # QQ Music sometimes returns an incomplete/poor first result set for
        # the exact same query. If attempt 1 has no readable result or the best
        # score is below threshold, run the SAME query once more.
        for search_attempt in (1, 2):
            self.check_control()

            if search_attempt == 1:
                attempt_url = search_url
                self.log("QQ Music search attempt 1/2.")
            else:
                self.step("search", "Retrying same search...")
                self.log(
                    "Search attempt 1 did not produce an acceptable match; "
                    "running the same QQ Music search a second time."
                )
                separator = "&" if "?" in search_url else "?"
                attempt_url = (
                    search_url
                    + separator
                    + "_qqauto_retry="
                    + str(time.time_ns())
                )

            try:
                self._goto_qq(search_page, attempt_url)
                self._wait_for_search_results(search_page)
            except AutomationStopped:
                raise
            except SkipCurrent:
                raise
            except Exception as exc:
                self.log(
                    f"Search attempt {search_attempt}/2 failed before readable "
                    f"results were available: {exc}"
                )

                if search_attempt == 1:
                    self.step("search", "Retrying same search...")
                    self.log(
                        "Search attempt 1/2 did not become usable; "
                        "running the same QQ Music search a second time."
                    )
                    time.sleep(0.8)
                    continue

                raise AutomationError(
                    f"QQ Music search failed on both attempts. "
                    f"Last error: {exc}"
                ) from exc

            rows, results = self._read_search_results(search_page)

            if not results:
                self.log(
                    f"Search attempt {search_attempt}/2 returned no readable results."
                )
                if search_attempt == 1:
                    self.step("search", "Retrying same search...")
                    self.log(
                        "Search attempt 1/2 returned no readable results; "
                        "running the same QQ Music search a second time."
                    )
                    time.sleep(0.8)
                    continue
                raise AutomationError(
                    "QQ Music returned no readable song results after two searches."
                )

            for item in results:
                item["score"] = score_result(
                    target_title,
                    target_artist,
                    item,
                )

            # Ranking priority:
            # 1) exact title + exact artist
            # 2) exact artist OR target-artist subset of QQ collaboration list
            # 3) strong title prefix relation
            # 4) composite score
            results.sort(
                key=lambda x: (
                    bool(x.get("_exact_pair")),
                    bool(x.get("_artist_exact") or x.get("_artist_subset")),
                    bool(x.get("_title_prefix")),
                    float(x["score"]),
                ),
                reverse=True,
            )
            best = results[0]

            self.log(
                f'Search attempt {search_attempt}/2 best: '
                f'#{best["index"] + 1}, score={best["score"]:.2f}, '
                f'exact_pair={bool(best.get("_exact_pair"))}, '
                f'artist_exact={bool(best.get("_artist_exact"))}, '
                f'artist_subset={bool(best.get("_artist_subset"))}, '
                f'title_prefix={bool(best.get("_title_prefix"))}, '
                f'ost_evidence={bool(best.get("_ost_evidence"))}, '
                f'book_title_suffix={bool(best.get("_book_title_suffix"))}, '
                f'added_after_full={bool(best.get("_added_after_full"))}, '
                f'conflicting_bracket={bool(best.get("_conflicting_bracket"))}, '
                f'{best["title"]} / {best["artist"]} / {best["album"]}'
            )

            if best["score"] >= self.match_threshold:
                break

            if search_attempt == 1:
                self.log(
                    f'Best score {best["score"]:.2f} is below threshold '
                    f'{self.match_threshold:.2f}; retrying once.'
                )
                time.sleep(0.8)

        if best is None:
            raise AutomationError("QQ Music search did not produce a match candidate.")

        self.step("search", f"✓ {len(results)} results")
        self.step(
            "match",
            f'✓ #{best["index"] + 1}  {best["score"]:.2f}  |  {best["title"]} / {best["artist"]}',
        )

        if best["score"] < self.match_threshold:
            raise AutomationError(
                f'Best match score {best["score"]:.2f} is below threshold '
                f'{self.match_threshold:.2f} after two searches.'
            )

        self.check_control()
        row = rows.nth(best["index"])
        row.hover()

        play_button = row.locator('[title="播放"]')
        if not play_button.count():
            raise AutomationError('Could not find the result-row play button [title="播放"].')

        self.step("play", "Clicking...")
        play_button.first.click()
        self.step("play", "✓ Playback requested")
        self.log(
            "Playback request sent. QQ Music reminder dialogs are ignored; "
            "Cat Catch resource creation is the playback-success signal."
        )

        self.step("capture", "Waiting for new resource...")
        new_audio = []
        detected_tab_id = None
        started = time.time()

        # Keep the already-working browser/search/player flow untouched.
        # Only restore the Cat Catch resource-detection idea that succeeded in
        # qq_full_test.py:
        #
        # - for player tabs that already existed before playback, compare against
        #   their before_audio snapshot;
        # - for a brand-new player tab created by this track, baseline is empty,
        #   so the first audio resource Cat Catch reports is immediately "new";
        # - do NOT invent a special "preload resource #1 / playback resource #2"
        #   rule. The successful stage test downloaded the first new audio
        #   resource and verified its byte size.
        while time.time() - started < self.capture_timeout:
            self.check_control()

            for tab in self._player_tabs():
                tab_id = int(tab["id"])
                baseline_count = before_count_by_tab.get(tab_id, 0)

                try:
                    current_audio = self._get_audio_data(tab_id)
                except Exception as exc:
                    self.log(f"Cat Catch getData failed on player tab {tab_id}: {exc}")
                    continue

                # Cat Catch's getData result is a per-tab resource list.  The
                # working stage test read that list directly.  Therefore use
                # list growth as the signal: if the tab had N audio resources
                # before playback, items N..end are the newly captured ones.
                # This avoids depending on requestId/getTime fields that Cat
                # Catch does not guarantee to populate consistently.
                if len(current_audio) > baseline_count:
                    candidates = current_audio[baseline_count:]
                    new_audio = candidates
                    detected_tab_id = tab_id
                    self.log(
                        f"Cat Catch AFTER: player tab {tab_id} grew from "
                        f"{baseline_count} to {len(current_audio)} audio resource(s)."
                    )
                    break

            if new_audio:
                break

            time.sleep(0.5)

        if not new_audio:
            raise AutomationError(
                f"No new Cat Catch audio resource appeared within "
                f"{self.capture_timeout:.0f} seconds."
            )

        # Match the previously successful qq_full_test.py behavior:
        # take the FIRST newly captured audio resource.
        item = new_audio[0]

        self.step(
            "capture",
            f'✓ {len(new_audio)} new | {item.get("name", "")} | {item.get("size", 0)} bytes',
        )
        self.log(
            f'New Cat Catch resource on player tab {detected_tab_id}: '
            f'{item.get("name")} ({item.get("size", 0)} bytes).'
        )

        self.step("download", "Downloading...")
        raw_path, expected_size, actual_size = self._download_resource(item, output_dir)
        self.step("download", f"✓ {actual_size} bytes")
        self.log(f"Direct download complete: {raw_path.name}, {actual_size} bytes.")

        return TrackResult(
            raw_path=raw_path,
            matched_title=best["title"],
            matched_artist=best["artist"],
            matched_album=best["album"],
            matched_score=float(best["score"]),
            resource_name=str(item.get("name", "")),
            expected_size=expected_size,
            actual_size=actual_size,
        )
