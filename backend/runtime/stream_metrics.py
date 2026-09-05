from __future__ import annotations


class StreamMetrics:
    def __init__(self):
        self._marks: dict[str, float] = {}

    def mark(self, name: str, seconds: float) -> None:
        self._marks[name] = seconds

    def summary(self) -> dict[str, float]:
        return dict(self._marks)
