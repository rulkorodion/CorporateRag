from __future__ import annotations

import math
import re


LEXEME_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])(?:[\"»')\]]*)\s+")


class TokenEstimator:
    """Offline, deterministic and deliberately conservative token estimator."""

    method = "max(lexemes,ceil(utf8_bytes/4)); unicode-bytes-and-lexemes-v1"

    @staticmethod
    def count(text: str) -> int:
        normalized = text.strip()
        if not normalized:
            return 0
        lexemes = len(LEXEME_RE.findall(normalized))
        byte_estimate = math.ceil(len(normalized.encode("utf-8")) / 4)
        return max(1, lexemes, byte_estimate)

    def split_text(self, text: str, hard_max_tokens: int) -> list[str]:
        """Split only when one source element itself exceeds the hard limit."""
        text = text.strip()
        if not text:
            return []
        if self.count(text) <= hard_max_tokens:
            return [text]
        sentences = [item.strip() for item in SENTENCE_BOUNDARY_RE.split(text) if item.strip()]
        if len(sentences) <= 1:
            return self._split_words(text, hard_max_tokens)
        pieces: list[str] = []
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}".strip()
            if current and self.count(candidate) <= hard_max_tokens:
                current = candidate
                continue
            if current:
                pieces.append(current)
                current = ""
            if self.count(sentence) > hard_max_tokens:
                pieces.extend(self._split_words(sentence, hard_max_tokens))
            else:
                current = sentence
        if current:
            pieces.append(current)
        return pieces

    def _split_words(self, text: str, hard_max_tokens: int) -> list[str]:
        words = text.split()
        if not words:
            return self._split_characters(text, hard_max_tokens)
        pieces: list[str] = []
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if current and self.count(candidate) > hard_max_tokens:
                pieces.append(" ".join(current))
                current = []
            if self.count(word) > hard_max_tokens:
                pieces.extend(self._split_characters(word, hard_max_tokens))
            else:
                current.append(word)
        if current:
            pieces.append(" ".join(current))
        return pieces

    def _split_characters(self, text: str, hard_max_tokens: int) -> list[str]:
        pieces: list[str] = []
        remaining = text
        while remaining:
            low, high = 1, len(remaining)
            best = 1
            while low <= high:
                middle = (low + high) // 2
                if self.count(remaining[:middle]) <= hard_max_tokens:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            pieces.append(remaining[:best])
            remaining = remaining[best:]
        return pieces
