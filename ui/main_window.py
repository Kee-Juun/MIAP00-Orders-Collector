from __future__ import annotations

from dataclasses import replace
import sys
from pathlib import Path
import threading
from typing import Mapping

from PyQt6.QtCore import (
    QDate,
    QEasingCurve,
    QObject,
    QPoint,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRectF,
    QThread,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QColor,
    QCloseEvent,
    QDesktopServices,
    QIcon,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QCalendarWidget,
    QDateEdit,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config.settings import Settings
from core.collector import MIAP00Collector


EXCLUDED_STATUSES = (
    "duplicate",
    "consolidated_duplicate",
    "local_duplicate",
    "content_duplicate",
)


def system_animations_enabled() -> bool:
    """Honor the Windows accessibility setting for UI animations."""

    if sys.platform != "win32":
        return True
    try:
        import ctypes

        enabled = ctypes.c_int()
        succeeded = ctypes.windll.user32.SystemParametersInfoW(
            0x1042, 0, ctypes.byref(enabled), 0
        )
        return not succeeded or bool(enabled.value)
    except (AttributeError, OSError):
        return True


def set_startup_topmost(widget: QWidget, enabled: bool) -> None:
    """Temporarily place a visible startup window above other applications."""

    if sys.platform == "win32" and widget.isVisible():
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetWindowPos.argtypes = (
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            )
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
            user32.SetForegroundWindow.restype = wintypes.BOOL

            insert_after = wintypes.HWND(
                -1 if enabled else -2
            )  # HWND_TOPMOST / HWND_NOTOPMOST
            flags = 0x0001 | 0x0002 | 0x0040  # NOSIZE | NOMOVE | SHOWWINDOW
            if not enabled:
                flags |= 0x0010  # Do not steal focus when releasing topmost.
            hwnd = wintypes.HWND(int(widget.winId()))
            positioned = user32.SetWindowPos(
                hwnd, insert_after, 0, 0, 0, 0, flags
            )
            if positioned:
                if enabled:
                    user32.SetForegroundWindow(hwnd)
                return
        except (AttributeError, OSError):
            pass

    was_visible = widget.isVisible()
    widget.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
    if was_visible:
        widget.show()


class PaperIntroSplash(QWidget):
    """Frameless, skippable paper-uncrumpling startup animation."""

    finished = pyqtSignal()
    FRAME_COUNT = 16
    COLUMNS = 4
    TWEEN_STEPS = 2

    def __init__(self, sprite_path: Path, parent=None):
        super().__init__(parent)
        self._sprite = QPixmap(str(sprite_path))
        self._step = 0
        self._finished = False
        self._timer = QTimer(self)
        self._timer.setInterval(36)
        self._timer.timeout.connect(self._advance)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click or press any key to skip")
        self.setFixedSize(650, 330)

    def start(self) -> None:
        if self._sprite.isNull() or not system_animations_enabled():
            QTimer.singleShot(0, self._finish)
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(
                area.center().x() - self.width() // 2,
                area.center().y() - self.height() // 2,
            )
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._timer.start()

    def paintEvent(self, event) -> None:
        del event
        if self._sprite.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        frame_position = self._step / self.TWEEN_STEPS
        first_frame = min(int(frame_position), self.FRAME_COUNT - 1)
        second_frame = min(first_frame + 1, self.FRAME_COUNT - 1)
        blend = frame_position - first_frame
        blend = blend * blend * (3 - 2 * blend)

        first_source = self._frame_source(first_frame)
        second_source = self._frame_source(second_frame)
        target = QRectF(self.rect())
        # Keep continuous alpha coverage on the transparent splash. Fading the
        # outgoing silhouette down exposes the desktop wherever the two poses
        # do not overlap, which reads as a flash. Hold it fully opaque and ease
        # the next, closely spaced pose over it instead.
        painter.setOpacity(1.0)
        painter.drawPixmap(target, self._sprite, first_source)
        if blend:
            painter.setOpacity(blend)
            painter.drawPixmap(target, self._sprite, second_source)

    def _frame_source(self, frame: int) -> QRectF:
        cell_width = self._sprite.width() / self.COLUMNS
        cell_height = self._sprite.height() / self.COLUMNS
        column = frame % self.COLUMNS
        row = frame // self.COLUMNS
        return QRectF(
            column * cell_width,
            row * cell_height,
            cell_width,
            cell_height,
        )

    def final_frame_pixmap(self) -> QPixmap:
        return self._sprite.copy(self._frame_source(self.FRAME_COUNT - 1).toRect())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        self._finish()

    def keyPressEvent(self, event) -> None:
        event.accept()
        self._finish()

    def _advance(self) -> None:
        final_step = (self.FRAME_COUNT - 1) * self.TWEEN_STEPS
        if self._step >= final_step:
            self._timer.stop()
            QTimer.singleShot(100, self._finish)
            return
        self._step += 1
        self.update()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._timer.stop()
        self.finished.emit()


def format_outcome_summary(
    counts: Mapping[str, int], *, failed: bool = False
) -> str:
    collected = int(counts.get("collected", 0))
    excluded = sum(int(counts.get(status, 0)) for status in EXCLUDED_STATUSES)
    errors = int(counts.get("error", 0))
    if failed and errors == 0:
        errors = 1
    return f"Collected: {collected}\nExcluded: {excluded}\nErrors: {errors}"


class MessageGlyph(QWidget):
    """Code-drawn status glyph for the collector's custom dialogs."""

    COLORS = {
        "success": QColor("#23c6b3"),
        "warning": QColor("#f0b65a"),
        "error": QColor("#ee7884"),
        "info": QColor("#65bde8"),
    }

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind if kind in self.COLORS else "info"
        self.setFixedSize(54, 54)

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.COLORS[self.kind]
        fill = QColor(color)
        fill.setAlpha(28)
        painter.setPen(QPen(color, 1.5))
        painter.setBrush(fill)
        painter.drawEllipse(self.rect().adjusted(3, 3, -3, -3))

        pen = QPen(color, 3.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == "success":
            path = QPainterPath()
            path.moveTo(16, 28)
            path.lineTo(24, 36)
            path.lineTo(39, 19)
            painter.drawPath(path)
        elif self.kind == "error":
            painter.drawLine(19, 19, 35, 35)
            painter.drawLine(35, 19, 19, 35)
        else:
            font = painter.font()
            font.setBold(True)
            font.setPointSize(19 if self.kind == "warning" else 17)
            painter.setFont(font)
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "!" if self.kind == "warning" else "i",
            )


class ThemedMessageDialog(QDialog):
    """Frameless, modal message dialog aligned with the MIAP00 UI theme."""

    def __init__(
        self,
        parent: QWidget,
        title: str,
        message: str,
        *,
        kind: str = "info",
        action_text: str = "OK",
    ):
        super().__init__(parent)
        self.kind = kind if kind in MessageGlyph.COLORS else "info"
        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumWidth(430)
        self.setMaximumWidth(430)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        panel = QFrame(objectName="messagePanel")
        panel.setProperty("messageKind", self.kind)
        outer.addWidget(panel)

        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 7)
        shadow.setColor(QColor(0, 0, 0, 155))
        panel.setGraphicsEffect(shadow)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 14, 20, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(7)
        header.addWidget(QLabel("MIAP00", objectName="messageBrand"))
        header.addWidget(
            QLabel(self.kind.upper(), objectName="messageKindLabel")
        )
        header.addStretch()
        close_button = QPushButton("×", objectName="messageCloseButton")
        close_button.setToolTip("Close")
        close_button.clicked.connect(self.reject)
        header.addWidget(close_button)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.addWidget(MessageGlyph(self.kind), 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        copy.setSpacing(7)
        title_label = QLabel(title, objectName="messageTitle")
        title_label.setWordWrap(True)
        message_label = QLabel(message, objectName="messageBody")
        message_label.setWordWrap(True)
        message_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        copy.addWidget(title_label)
        copy.addWidget(message_label)
        body.addLayout(copy, 1)
        layout.addLayout(body)

        footer = QHBoxLayout()
        footer.addStretch()
        ok_button = QPushButton(action_text, objectName="messageActionButton")
        ok_button.setDefault(True)
        ok_button.clicked.connect(self.accept)
        footer.addWidget(ok_button)
        layout.addLayout(footer)

        self.setStyleSheet(STYLE_SHEET)
        self.adjustSize()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None:
            parent_center = parent.frameGeometry().center()
            frame = self.frameGeometry()
            frame.moveCenter(parent_center)
            self.move(frame.topLeft())
        self.raise_()
        self.activateWindow()


def show_themed_message(
    parent: QWidget,
    title: str,
    message: str,
    *,
    kind: str = "info",
    action_text: str = "OK",
) -> bool:
    dialog = ThemedMessageDialog(
        parent,
        title,
        message,
        kind=kind,
        action_text=action_text,
    )
    return dialog.exec() == QDialog.DialogCode.Accepted


class CalendarIconButton(QToolButton):
    """Small code-drawn calendar button, following the WVPUC0 control pattern."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("calendarButton")
        self.setFixedSize(38, 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Choose a date from the calendar")
        self.setAccessibleName("Open calendar")

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self.isEnabled():
            background, stroke = QColor("#101a27"), QColor("#607589")
        elif self.underMouse():
            background, stroke = QColor("#28506a"), QColor("#8ff2e5")
        else:
            background, stroke = QColor("#203249"), QColor("#5dd6c7")
        painter.setPen(QPen(QColor("#38536e"), 1))
        painter.setBrush(background)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(stroke, 1.7))
        body = self.rect().adjusted(10, 9, -10, -9)
        painter.drawRoundedRect(body, 2, 2)
        painter.drawLine(body.left(), body.top() + 5, body.right(), body.top() + 5)
        painter.drawLine(body.left() + 5, body.top() - 2, body.left() + 5, body.top() + 3)
        painter.drawLine(body.right() - 5, body.top() - 2, body.right() - 5, body.top() + 3)
        painter.setPen(QPen(stroke, 2.2))
        for x in (body.left() + 5, body.left() + 10):
            for y in (body.top() + 9, body.top() + 13):
                painter.drawPoint(x, y)


class CalendarDateEdit(QDateEdit):
    """A date field that is selected exclusively through a calendar dialog."""

    def __init__(self):
        super().__init__()
        self.setDisplayFormat("MMM d, yyyy")
        self.setCalendarPopup(True)
        self.lineEdit().setReadOnly(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def open_calendar(self) -> None:
        dialog = QDialog(self, Qt.WindowType.Popup)
        dialog.setObjectName("calendarDialog")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(8, 8, 8, 8)
        calendar = QCalendarWidget(dialog)
        calendar.setGridVisible(False)
        calendar.setSelectedDate(self.date())
        calendar.clicked.connect(self.setDate)
        calendar.clicked.connect(lambda _date: dialog.accept())
        layout.addWidget(calendar)
        dialog.adjustSize()
        anchor = self.mapToGlobal(self.rect().bottomLeft())
        dialog.move(anchor)
        dialog.exec()


class DatePicker(QFrame):
    """Read-only date display with an unmistakable calendar action."""

    def __init__(self, selected_date):
        super().__init__(objectName="datePicker")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.editor = CalendarDateEdit()
        self.editor.setDate(QDate(selected_date.year, selected_date.month, selected_date.day))
        button = CalendarIconButton()
        button.clicked.connect(self.editor.open_calendar)
        row.addWidget(self.editor, 1)
        row.addWidget(button)

    def date(self):
        return self.editor.date()

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.editor.setEnabled(enabled)


class SheenProgressBar(QProgressBar):
    """Compact progress bar with a restrained moving highlight while active."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._sheen_active = False
        self._timer = QTimer(self)
        self._timer.setInterval(24)
        self._timer.timeout.connect(self._advance_sheen)
        self.setTextVisible(False)

    def start_sheen(self) -> None:
        self._sheen_active = True
        self._phase = 0.0
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop_sheen(self) -> None:
        self._sheen_active = False
        self._timer.stop()
        self.update()

    def _advance_sheen(self) -> None:
        self._phase = (self._phase + 0.018) % 1.0
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QRectF(self.rect())
        radius = track.height() / 2
        painter.setBrush(QColor("#182638"))
        painter.drawRoundedRect(track, radius, radius)

        if self.maximum() == self.minimum():
            if not self._sheen_active:
                return
            width = max(48.0, track.width() * 0.28)
            travel = track.width() + width
            x = track.left() + self._phase * travel - width
            fill = QRectF(x, track.top(), width, track.height())
        else:
            span = self.maximum() - self.minimum()
            ratio = max(0.0, min(1.0, (self.value() - self.minimum()) / span))
            if ratio <= 0:
                return
            fill = QRectF(track.left(), track.top(), track.width() * ratio, track.height())

        track_path = QPainterPath()
        track_path.addRoundedRect(track, radius, radius)
        painter.save()
        painter.setClipPath(track_path)
        painter.setBrush(QColor("#1bb7a5"))
        painter.drawRect(fill)

        if self._sheen_active:
            band_width = max(30.0, track.width() * 0.14)
            sheen_x = track.left() + self._phase * (track.width() + band_width) - band_width
            gradient = QLinearGradient(sheen_x, 0, sheen_x + band_width, 0)
            gradient.setColorAt(0.0, QColor(220, 255, 250, 0))
            gradient.setColorAt(0.48, QColor(220, 255, 250, 150))
            gradient.setColorAt(1.0, QColor(220, 255, 250, 0))
            painter.setBrush(gradient)
            painter.drawRect(fill)
        painter.restore()


class CollectionWorker(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(bool, object, str, object)

    def __init__(self, settings: Settings, cancel_event: threading.Event):
        super().__init__()
        self.settings = settings
        self.cancel_event = cancel_event

    @pyqtSlot()
    def run(self) -> None:
        collector = MIAP00Collector(
            self.settings,
            log_callback=self.log.emit,
            progress_callback=self.progress.emit,
            cancel_event=self.cancel_event,
        )
        try:
            run_dir = collector.run()
            self.finished.emit(
                True,
                run_dir,
                "Collection finished",
                dict(collector.last_counts),
            )
        except Exception as exc:
            self.finished.emit(
                False,
                collector.run_dir,
                f"{type(exc).__name__}: {exc}",
                dict(collector.last_counts),
            )


def friendly_status(line: str) -> str | None:
    """Turn detailed file-log entries into compact operator milestones."""
    modern_parts = line.split(" - ", 3)
    if len(modern_parts) == 4:
        message = modern_parts[3]
    else:
        # Continue accepting logs created by older collector versions.
        message = line.split(" | ")[-1]
    if message.startswith("MIAP00 Orders Collector started"):
        return "Preparing the collection run…"
    if message.startswith("Opening Michigan Courts"):
        return "Opening Michigan Court of Appeals orders…"
    if message.startswith("Collecting orders released from"):
        return "Reading PDF links in the selected date range…"
    if message.startswith("Results page") and "in date range" in message:
        try:
            count = message.split(" PDF order(s), ", 1)[1].split(" in date range", 1)[0]
            return f"Found {count} PDF links in the selected date range."
        except IndexError:
            return "PDF links loaded."
    if message.startswith("Date-range boundary reached"):
        return "PDF links ready. Starting downloads…"
    if message.startswith("[") and "Downloading to temporary storage:" in message:
        progress = message.split("]", 1)[0].lstrip("[")
        return f"Downloading and preparing PDFs  •  {progress}"
    if message.startswith("Reading certification footer with OCR:"):
        return "Reading the certified court decision date..."
    if message.startswith("IRT startup attempt"):
        return "Connecting to IRT duplicate search…"
    if message.startswith("IRT search ready"):
        return "IRT duplicate search ready."
    if message.startswith("IRT bulk duplicate check:"):
        return "Loading the complete IRT date-range snapshot…"
    if message.startswith("Loading one complete IRT snapshot"):
        return "Waiting for the complete IRT results table…"
    if message.startswith("IRT snapshot results page"):
        return "Capturing IRT duplicate records…"
    if message.startswith("Comparing") and "renamed PDF filename(s)" in message:
        return "Comparing renamed PDFs against the IRT snapshot…"
    if message.startswith("IRT-backed consolidated check:"):
        return "Checking consolidated cases against online parent copies…"
    if message.startswith("IRT consolidated duplicate skipped:"):
        return "Consolidated copy found online and excluded."
    if message.startswith("IRT duplicate skipped:"):
        return "Duplicate found and skipped."
    if message.startswith("Collected:"):
        return f"Saved {message.split(':', 1)[1].split(' (', 1)[0].strip()}"
    if message.startswith("Post-run content duplicate check:"):
        return "Checking downloaded PDFs for true duplicatesâ€¦"
    if message.startswith("Content duplicate removed:"):
        return "True duplicate found and removed."
    if message.startswith("Post-run content duplicate check complete:"):
        return "PDF content quality check complete."
    if message.startswith("Run complete:"):
        return "Collection complete."
    return None


class TitleBar(QFrame):
    def __init__(self, window: "CollectorWindow"):
        super().__init__(objectName="titleBar")
        self.window = window
        self.drag_offset: QPoint | None = None
        row = QHBoxLayout(self)
        row.setContentsMargins(22, 10, 10, 10)
        title = QLabel("MIAP00", objectName="brand")
        subtitle = QLabel("ORDERS COLLECTOR", objectName="brandCaption")
        row.addWidget(title)
        row.addWidget(subtitle)
        row.addStretch()
        minimize = QPushButton("−", objectName="windowButton")
        minimize.clicked.connect(window.showMinimized)
        close = QPushButton("×", objectName="closeButton")
        close.clicked.connect(window.close)
        row.addWidget(minimize)
        row.addWidget(close)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window.move(event.globalPosition().toPoint() - self.drag_offset)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.drag_offset = None


class CollectorWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.thread: QThread | None = None
        self.worker: CollectionWorker | None = None
        self.is_running = False
        self.cancel_event = threading.Event()
        self.last_run_dir: Path | None = None
        self.setWindowTitle("MIAP00 Orders Collector")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowSystemMenuHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(650, 330)
        self._build()
        self.setStyleSheet(STYLE_SHEET)

    def _build(self) -> None:
        root = QWidget(objectName="transparentRoot")
        self.setCentralWidget(root)
        root_layout = QStackedLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.intro_paper = QLabel(objectName="introPaper")
        self.intro_paper.setScaledContents(True)
        self.intro_paper.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.intro_paper.hide()
        window_layer = QWidget(objectName="windowLayer")
        root_layout.addWidget(self.intro_paper)
        root_layout.addWidget(window_layer)
        root_layout.setCurrentWidget(window_layer)

        outer = QVBoxLayout(window_layer)
        outer.setContentsMargins(8, 8, 8, 8)
        shell = QFrame(objectName="windowShell")
        outer.addWidget(shell)
        shell_layout = QStackedLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.window_surface = QFrame(objectName="windowSurface")
        self.window_surface.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.intro_content = QWidget(objectName="introContent")
        shell_layout.addWidget(self.window_surface)
        shell_layout.addWidget(self.intro_content)
        shell_layout.setCurrentWidget(self.intro_content)
        page = QVBoxLayout(self.intro_content)
        page.setContentsMargins(0, 0, 0, 12)
        page.setSpacing(0)
        page.addWidget(TitleBar(self))

        content = QVBoxLayout()
        content.setContentsMargins(16, 12, 16, 0)
        content.setSpacing(10)

        settings_card = QFrame(objectName="card")
        grid = QGridLayout(settings_card)
        grid.setContentsMargins(14, 11, 14, 11)
        grid.setHorizontalSpacing(9)
        grid.setVerticalSpacing(7)
        grid.addWidget(QLabel("RELEASE DATE RANGE", objectName="fieldLabel"), 0, 0, 1, 4)
        start, end = self.settings.resolved_date_range()
        self.start_date = DatePicker(start)
        self.end_date = DatePicker(end)
        grid.addWidget(QLabel("From", objectName="softLabel"), 1, 0)
        grid.addWidget(self.start_date, 1, 1)
        grid.addWidget(QLabel("Through", objectName="softLabel"), 1, 2)
        grid.addWidget(self.end_date, 1, 3)

        self.headless = QCheckBox("Run Chrome in background")
        self.headless.setChecked(self.settings.headless)
        grid.addWidget(QLabel("BROWSER", objectName="fieldLabel"), 2, 0)
        grid.addWidget(self.headless, 2, 1, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        content.addWidget(settings_card)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.start_button = QPushButton("COLLECT ORDERS", objectName="primaryButton")
        self.start_button.clicked.connect(self._primary_action)
        self.open_button = QPushButton("VIEW FOLDER", objectName="secondaryButton")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_run)
        controls.addWidget(self.start_button, 1)
        controls.addWidget(self.open_button)
        content.addLayout(controls)

        self.progress_bar = SheenProgressBar()
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        content.addWidget(self.progress_bar)

        status_card = QFrame(objectName="statusCard")
        status_card.setFixedHeight(44)
        status_row = QHBoxLayout(status_card)
        status_row.setContentsMargins(12, 6, 12, 6)
        self.status_dot = QLabel("●", objectName="statusDot")
        self.status_label = QLabel("Ready.", objectName="statusText")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setToolTip(self.status_label.text())
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label, 1)
        content.addWidget(status_card)

        page.addLayout(content, 1)

    def _primary_action(self) -> None:
        if self.is_running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        start = self.start_date.date().toPyDate()
        end = self.end_date.date().toPyDate()
        if start > end:
            show_themed_message(
                self,
                "Invalid date range",
                "The start date must be on or before the end date.",
                kind="warning",
            )
            return
        self.settings = replace(
            self.settings,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            headless=self.headless.isChecked(),
        )
        self.cancel_event.clear()
        self.last_run_dir = None
        self.progress_bar.setRange(0, 0)
        self._set_status("Opening newest orders…")
        self._set_running(True)

        self.thread = QThread(self)
        self.worker = CollectionWorker(self.settings, self.cancel_event)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()

    def _stop(self) -> None:
        if self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.start_button.setEnabled(False)
        self.start_button.setText("STOPPING…")
        self._set_status("Stopping safely after the current operation…")

    @pyqtSlot(str)
    def _log(self, line: str) -> None:
        status = friendly_status(line)
        if status:
            self._set_status(status)

    @pyqtSlot(int, int)
    def _progress(self, current: int, total: int) -> None:
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setMaximum(max(1, total))
        self.progress_bar.setValue(current)
        self._set_status(f"Processing renamed PDFs  •  {current} / {total}")

    @pyqtSlot(bool, object, str, object)
    def _finished(
        self,
        success: bool,
        run_dir: object,
        _message: str,
        counts: object,
    ) -> None:
        cancelled = self.cancel_event.is_set()
        if run_dir:
            self.last_run_dir = Path(run_dir)
            self.open_button.setEnabled(True)
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 1)
        if success and not cancelled:
            self.progress_bar.setValue(self.progress_bar.maximum())
        self._set_status(
            "Stopped safely" if cancelled else "Complete" if success else "Stopped with errors"
        )
        self._set_running(False)
        summary = format_outcome_summary(
            counts if isinstance(counts, Mapping) else {},
            failed=not success,
        )
        if cancelled:
            acknowledged = show_themed_message(
                self,
                "Collection stopped",
                f"Collection stopped safely.\n\n{summary}",
                kind="info",
            )
        elif success:
            acknowledged = show_themed_message(
                self,
                "Collection complete",
                f"Collection finished.\n\n{summary}",
                kind="success",
            )
        else:
            acknowledged = show_themed_message(
                self,
                "Collection failed",
                f"Collection stopped with errors.\n\n{summary}",
                kind="error",
            )
        if acknowledged and self.last_run_dir:
            self._open_run()
        self._reset_ready_state()

    def _thread_finished(self) -> None:
        self.thread = None
        self.worker = None

    def _set_status(self, message: str) -> None:
        available_width = max(100, self.status_label.width() - 8)
        rendered = self.status_label.fontMetrics().elidedText(
            message, Qt.TextElideMode.ElideRight, available_width
        )
        self.status_label.setText(rendered)
        self.status_label.setToolTip(message)

    def _reset_ready_state(self) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._set_status("Ready.")

    def _set_running(self, running: bool) -> None:
        self.is_running = running
        if running:
            self.progress_bar.start_sheen()
        else:
            self.progress_bar.stop_sheen()
        for widget in (self.start_date, self.end_date, self.headless):
            widget.setEnabled(not running)
        self.start_button.setEnabled(True)
        self.start_button.setObjectName("dangerButton" if running else "primaryButton")
        self.start_button.setText("STOP SAFELY" if running else "COLLECT ORDERS")
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)

    def _open_run(self) -> None:
        target = self.last_run_dir or self.settings.resolved_output_root()
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.thread and self.thread.isRunning():
            self._stop()
            show_themed_message(
                self,
                "Stopping collection",
                "A safe stop was requested. Close the app when the current operation finishes.",
                kind="info",
            )
            event.ignore()
            return
        event.accept()


STYLE_SHEET = """
QWidget#transparentRoot, QWidget#windowLayer, QWidget#introContent, QLabel#introPaper { background: transparent; }
QFrame#windowShell { background: transparent; border: none; }
QFrame#windowSurface { background: #090f1a; border: 1px solid #283a50; border-radius: 16px; }
QFrame#titleBar { background: #0d1725; border-top-left-radius: 15px; border-top-right-radius: 15px; }
QWidget { color: #e8f0f7; font-family: "Segoe UI"; font-size: 9.5pt; }
QLabel { background: transparent; }
QLabel#brand { color: #f6fbff; font-size: 15pt; font-weight: 900; letter-spacing: 1px; }
QLabel#brandCaption, QLabel#fieldLabel { color: #5dd6c7; font-size: 7.5pt; font-weight: 800; letter-spacing: 1px; }
QLabel#softLabel { color: #8fa5ba; }
QLabel#statusDot { color: #21c0ad; }
QLabel#statusText { color: #d8e5ef; font-weight: 650; }
QPushButton#windowButton, QPushButton#closeButton { background: transparent; border: none; border-radius: 8px; min-width: 38px; min-height: 28px; color: #9eb2c5; font-size: 14pt; }
QPushButton#windowButton:hover { background: #20344b; color: white; }
QPushButton#closeButton:hover { background: #d9525e; color: white; }
QFrame#card, QFrame#statusCard { background: #111c2b; border: 1px solid #26394e; border-radius: 10px; }
QDateEdit { background: #182638; color: #f1f7fb; border: 1px solid #36506a; border-radius: 8px; padding: 7px 9px; min-height: 20px; selection-background-color: #18aa9b; }
QDateEdit:focus { border-color: #23c6b3; background: #1b2b40; }
QDateEdit::drop-down { border: none; width: 26px; }
QFrame#datePicker { background: transparent; border: none; }
QDialog#calendarDialog, QCalendarWidget { background: #111c2b; color: #eaf2f8; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: #17263a; }
QCalendarWidget QToolButton { color: #eaf2f8; background: transparent; border: none; padding: 6px; font-weight: 700; }
QCalendarWidget QToolButton:hover { background: #203b50; border-radius: 5px; }
QCalendarWidget QAbstractItemView { background: #0f1927; color: #dbe7f1; selection-background-color: #1bb7a5; selection-color: white; outline: none; }
QCheckBox { color: #cfdae5; spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 4px; border: 1px solid #3c566f; background: #182638; }
QCheckBox::indicator:checked { background: #1bb7a5; border-color: #39d1bf; }
QPushButton#primaryButton, QPushButton#secondaryButton, QPushButton#dangerButton, QPushButton#smallButton { border-radius: 9px; min-height: 34px; padding: 3px 14px; font-weight: 750; }
QPushButton#primaryButton { background: #19ad9d; color: white; border: none; }
QPushButton#primaryButton:hover { background: #21c4b1; }
QPushButton#primaryButton:disabled { background: #285a5a; color: #8fb7b4; }
QPushButton#secondaryButton, QPushButton#smallButton { background: #152235; color: #d8e4ef; border: 1px solid #344b63; }
QPushButton#secondaryButton:hover, QPushButton#smallButton:hover { background: #20354b; border-color: #4c6a86; }
QPushButton#dangerButton { background: #251b27; color: #ef9aa4; border: 1px solid #633540; }
QPushButton#dangerButton:hover { background: #43232b; }
QPushButton:disabled { color: #607589; background: #101a27; border-color: #27394a; }
QProgressBar { background: #182638; border: none; border-radius: 3px; }
QProgressBar::chunk { background: #1bb7a5; border-radius: 3px; }
QFrame#messagePanel { background: #0d1725; border: 1px solid #30475e; border-radius: 15px; }
QLabel#messageBrand { color: #f4f9fc; font-size: 10pt; font-weight: 900; letter-spacing: 1px; }
QLabel#messageKindLabel { color: #5dd6c7; font-size: 7pt; font-weight: 800; letter-spacing: 1px; }
QLabel#messageTitle { color: #f2f7fb; font-size: 13pt; font-weight: 750; }
QLabel#messageBody { color: #adc0d0; font-size: 9.5pt; line-height: 1.35; }
QPushButton#messageCloseButton { background: transparent; color: #7890a5; border: none; border-radius: 7px; min-width: 28px; max-width: 28px; min-height: 26px; max-height: 26px; font-size: 14pt; }
QPushButton#messageCloseButton:hover { background: #26384b; color: #ffffff; }
QPushButton#messageActionButton { background: #19ad9d; color: white; border: none; border-radius: 8px; min-width: 88px; min-height: 32px; padding: 2px 16px; font-weight: 750; }
QPushButton#messageActionButton:hover { background: #22c4b2; }
"""


def launch(settings: Settings) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("MIAP00 Orders Collector")
    app.setStyle("Fusion")
    icon_path = Path(__file__).with_name("assets") / "miap00_app_icon.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = CollectorWindow(settings)
    sprite_path = Path(__file__).with_name("assets") / "paper_uncrumple_sprite.png"
    splash = PaperIntroSplash(sprite_path)

    def reveal_window() -> None:
        if not system_animations_enabled() or not splash.isVisible():
            window.show()
            set_startup_topmost(window, True)
            window.raise_()
            window.activateWindow()
            splash.hide()
            QTimer.singleShot(900, lambda: set_startup_topmost(window, False))
            return
        window.move(splash.pos())
        window.intro_paper.setPixmap(splash.final_frame_pixmap())
        paper_effect = QGraphicsOpacityEffect(window.intro_paper)
        paper_effect.setOpacity(1.0)
        window.intro_paper.setGraphicsEffect(paper_effect)
        window.intro_paper.show()
        content_effect = QGraphicsOpacityEffect(window.intro_content)
        content_effect.setOpacity(0.0)
        window.intro_content.setGraphicsEffect(content_effect)
        surface_effect = QGraphicsOpacityEffect(window.window_surface)
        surface_effect.setOpacity(0.0)
        window.window_surface.setGraphicsEffect(surface_effect)
        window.setWindowOpacity(1.0)
        window.show()
        set_startup_topmost(window, True)
        window.raise_()
        window.activateWindow()
        splash.hide()

        content_fade = QPropertyAnimation(content_effect, b"opacity", window)
        content_fade.setDuration(480)
        content_fade.setStartValue(0.0)
        content_fade.setEndValue(1.0)
        content_fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        def reveal_window_surface() -> None:
            surface_transition = QParallelAnimationGroup(window)
            surface_fade = QPropertyAnimation(
                surface_effect, b"opacity", surface_transition
            )
            surface_fade.setDuration(720)
            surface_fade.setStartValue(0.0)
            surface_fade.setKeyValueAt(0.18, 0.0)
            surface_fade.setEndValue(1.0)
            surface_fade.setEasingCurve(QEasingCurve.Type.InOutSine)

            paper_fade = QPropertyAnimation(
                paper_effect, b"opacity", surface_transition
            )
            paper_fade.setDuration(720)
            paper_fade.setStartValue(1.0)
            paper_fade.setKeyValueAt(0.18, 1.0)
            paper_fade.setKeyValueAt(0.68, 0.55)
            paper_fade.setEndValue(0.0)
            paper_fade.setEasingCurve(QEasingCurve.Type.InOutSine)

            surface_transition.addAnimation(surface_fade)
            surface_transition.addAnimation(paper_fade)

            def finish_startup_handoff() -> None:
                window.intro_paper.hide()
                # Keep the completed window above other applications long enough
                # for the native compositor to present the final, fully opaque
                # frame. Releasing topmost in the animation's finished callback
                # can otherwise demote the window during that final presentation.
                window.raise_()
                window.activateWindow()
                QTimer.singleShot(
                    750, lambda: set_startup_topmost(window, False)
                )

            surface_transition.finished.connect(finish_startup_handoff)
            surface_transition.start(
                QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped
            )

        content_fade.finished.connect(reveal_window_surface)
        content_fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    splash.finished.connect(reveal_window)
    splash.start()
    app.exec()
