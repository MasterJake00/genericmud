"""Autocomplete from recent MUD output (the MUSHclient/MUDBall Tab-complete idea).

Two pure pieces: :class:`OutputWordIndex` collects words as displayed lines
arrive (most-recent-first, deduplicated), and :class:`CompletionCycler` holds
the cycling state for one completion session in the input box. The index is
written on the engine loop thread and read on the UI thread, so its state is
lock-guarded; the cycler lives on the UI thread only.
"""

from __future__ import annotations

import re
import threading

MIN_WORD_LENGTH = 3  # shorter words are cheaper to type than to cycle to
MAX_WORDS = 500  # names/items from the recent past; older words age out
# A word is a letter in any alphabet followed by letters, apostrophes or hyphens.
# [^\W\d_] is "a Unicode letter": ASCII-only matching completed nothing on a Cyrillic
# MUD and cut accented words short ("Café" became "Caf").
_WORD_RE = re.compile(r"[^\W\d_](?:[^\W\d_]|['-])+")


class OutputWordIndex:
    """Words seen in recent output, newest first, one entry per word."""

    def __init__(
        self, *, max_words: int = MAX_WORDS, min_length: int = MIN_WORD_LENGTH
    ) -> None:
        self._max = max_words
        self._min = min_length
        # Lowercased word -> the word as last seen, oldest first; one entry per word. Every
        # displayed line comes through here on the engine thread, so a word moves to the newest
        # end in constant time: rebuilding a list of every indexed word, once per word of every
        # line, was a third of the time a line took to process.
        self._words: dict[str, str] = {}
        self._lock = threading.Lock()

    def add_line(self, text: str) -> None:
        words = [w for w in _WORD_RE.findall(text) if len(w) >= self._min]
        if not words:
            return
        with self._lock:
            for word in words:
                key = word.lower()
                self._words.pop(key, None)
                self._words[key] = word  # in again at the newest end
            while len(self._words) > self._max:
                del self._words[next(iter(self._words))]  # the oldest ages out

    def complete(self, prefix: str) -> list[str]:
        """Words starting with ``prefix`` (case-insensitive), newest first."""
        if not prefix:
            return []
        needle = prefix.lower()
        with self._lock:
            return [
                word for key, word in reversed(self._words.items())
                if key.startswith(needle) and key != needle
            ]


class CompletionCycler:
    """Cycling state for one completion run: begin() once, then next()/prev().

    The caller resets on any other keypress so a fresh prefix starts a fresh run.
    """

    def __init__(self) -> None:
        self._candidates: list[str] = []
        self._index = -1
        self.prefix = ""

    @property
    def active(self) -> bool:
        return bool(self._candidates)

    def begin(self, prefix: str, candidates: list[str]) -> None:
        self.prefix = prefix
        self._candidates = list(candidates)
        self._index = -1

    def next(self) -> str | None:
        if not self._candidates:
            return None
        self._index = (self._index + 1) % len(self._candidates)
        return self._candidates[self._index]

    def prev(self) -> str | None:
        if not self._candidates:
            return None
        if self._index == -1:
            self._index = len(self._candidates) - 1
        else:
            self._index = (self._index - 1) % len(self._candidates)
        return self._candidates[self._index]

    def reset(self) -> None:
        self._candidates = []
        self._index = -1
        self.prefix = ""
