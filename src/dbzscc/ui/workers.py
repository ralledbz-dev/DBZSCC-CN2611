"""Small Qt worker used to keep blocking work off the GUI thread."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from dbzscc.app.chat_client import ChatStreamCancelled


class WorkerSignals(QObject):
    completed = Signal(object)
    failed = Signal(str)


class FunctionWorker(QRunnable):
    """Execute one bounded callable and emit a result or safe error message."""

    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except Exception as exc:  # noqa: BLE001 -- worker boundary must report any task failure.
            self.signals.failed.emit(str(exc) or type(exc).__name__)
        else:
            self.signals.completed.emit(result)


class ChatStreamSignals(QObject):
    delta = Signal(int, str)
    completed = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)


class ChatStreamWorker(QRunnable):
    """Stream chat off-thread and report correlated events through Qt signals."""

    def __init__(
        self,
        operation_id: int,
        function: Callable[[Callable[[str], None], Event], str],
        cancel_event: Event,
    ) -> None:
        super().__init__()
        self.operation_id = operation_id
        self.function = function
        self.cancel_event = cancel_event
        self.signals = ChatStreamSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        emit_delta = lambda text: self.signals.delta.emit(self.operation_id, text)
        try:
            result = self.function(emit_delta, self.cancel_event)
        except ChatStreamCancelled:
            self.signals.cancelled.emit(self.operation_id)
        except Exception as exc:  # noqa: BLE001 -- worker boundary reports any task failure.
            if self.cancel_event.is_set():
                self.signals.cancelled.emit(self.operation_id)
            else:
                self.signals.failed.emit(
                    self.operation_id, str(exc) or type(exc).__name__
                )
        else:
            if self.cancel_event.is_set():
                self.signals.cancelled.emit(self.operation_id)
            else:
                self.signals.completed.emit(self.operation_id, result)
