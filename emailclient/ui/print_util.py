"""Print-with-preview helpers shared by the reader and the attachment viewer.

We use our OWN preview window (a scrollable view of the exact pages + a clear
Print / Close bar) instead of Qt's QPrintPreviewDialog, whose toolbar renders
unreliably (the Print button got clipped on macOS).

The preview picks the printer itself — a combo listing every installed printer,
defaulting to the system default — so printing is one click and the user can
see WHICH printer they are about to use. "Settings…" still opens the full
system dialog (paper size, duplex, page range).

PDFs (and the email, which we render to a PDF first via QtWebEngine) are drawn
page-by-page with QtPdf; images and text are drawn directly.
"""
from __future__ import annotations

import os
import uuid

from PySide6.QtCore import QPoint, QSize, Qt, QUrl
from PySide6.QtGui import (QDesktopServices, QGuiApplication, QPainter,
                           QPixmap, QStandardItem, QStandardItemModel)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QToolButton, QVBoxLayout, QWidget)


def parse_pages(text: str, page_count: int) -> list[int] | None:
    """Page selection text -> sorted 0-based page indices (None = all pages).

    Accepts what people actually type: "3", "2-5", "1,2,5", "1, 3-5, 9".
    Everything is 1-based on input and clamped to the document, so a typo can
    never print more than the document has. Junk or an empty box means "all",
    never "nothing" — printing zero pages would just look broken.
    """
    text = (text or "").strip()
    if not text or page_count <= 0:
        return None
    pages: set[int] = set()
    for chunk in text.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("-", 1)
        try:
            first = int(parts[0].strip())
            last = int(parts[1].strip()) if len(parts) == 2 and parts[1].strip() \
                else first
        except ValueError:
            continue          # ignore the bad piece, keep the good ones
        first = max(1, min(first, page_count))
        last = max(1, min(last, page_count))
        if last < first:
            first, last = last, first
        pages.update(range(first - 1, last))
    return sorted(pages) or None


def parse_page_range(text: str, page_count: int) -> tuple[int, int] | None:
    """The 1-based (first, last) span covering the selection — what a QPrinter
    can express. Used for the system dialog and for widgets that print
    themselves; the exact list (which may have gaps) comes from parse_pages."""
    pages = parse_pages(text, page_count)
    if not pages:
        return None
    return pages[0] + 1, pages[-1] + 1


class PagePicker(QToolButton):
    """Compact page chooser: a button that drops a small scrollable checklist.

    A long document is the normal case — a 78-page menu wallpapered the screen
    — so the list is a fixed-height scroller with "All pages" pinned above it
    and Select all / Clear underneath. The popup stays open while ticking, so
    picking five pages is five clicks in one visit.
    """

    LIST_HEIGHT = 240
    POPUP_WIDTH = 230

    def __init__(self, page_count: int, parent=None):
        super().__init__(parent)
        self._page_count = max(0, page_count)
        self._guard = False
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Choose the pages to print")
        self.setMinimumWidth(150)
        self.clicked.connect(self._show_popup)

        self._popup = QFrame(self, Qt.WindowType.Popup)
        self._popup.setObjectName("pagePickerPopup")
        self._popup.setFrameShape(QFrame.Shape.StyledPanel)
        self._popup.setFixedWidth(self.POPUP_WIDTH)

        self._all_cb = QCheckBox("All pages", self._popup)
        self._all_cb.setChecked(True)
        self._all_cb.toggled.connect(self._on_all_toggled)

        self._list = QListWidget(self._popup)
        self._list.setFixedHeight(self.LIST_HEIGHT)   # re-fit below
        self._list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        for i in range(1, self._page_count + 1):
            item = QListWidgetItem(f"Page {i}", self._list)
            # Deliberately NOT ItemIsUserCheckable: with it, a click on the box
            # was toggled by Qt and then again by our handler, so ticking the
            # box did nothing while clicking the label worked. The box still
            # draws from the check state; every click now goes through one path.
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setCheckState(Qt.CheckState.Unchecked)
        # Anywhere on the row — box or label — toggles it.
        self._list.itemClicked.connect(self._toggle_item)
        self._list.itemChanged.connect(lambda _i: self._on_page_toggled())

        select_all = QPushButton("Select all", self._popup)
        select_all.clicked.connect(lambda: self.set_selected(
            range(self._page_count)))
        clear = QPushButton("Clear", self._popup)
        clear.clicked.connect(lambda: self.set_selected(()))
        btns = QHBoxLayout()
        btns.setContentsMargins(0, 0, 0, 0)
        btns.addWidget(select_all)
        btns.addWidget(clear)
        btns.addStretch(1)

        lay = QVBoxLayout(self._popup)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        lay.addWidget(self._all_cb)
        lay.addWidget(self._list)
        lay.addLayout(btns)

        # Short documents get a short list instead of a half-empty box.
        if self._page_count:
            row_h = self._list.sizeHintForRow(0) or 20
            self._list.setFixedHeight(
                min(self.LIST_HEIGHT, self._page_count * row_h + 8))
        else:
            self.setEnabled(False)
        self._refresh_text()

    # -- popup ---------------------------------------------------------
    def _show_popup(self):
        self._popup.adjustSize()
        self._popup.move(self.mapToGlobal(QPoint(0, self.height() + 2)))
        self._popup.show()

    # Qt calls this for the button's own menu indicator.
    def showMenu(self):
        self._show_popup()

    # -- internal ------------------------------------------------------
    def _items(self):
        return [self._list.item(i) for i in range(self._list.count())]

    def _checked_pages(self) -> list[int]:
        return [i for i, it in enumerate(self._items())
                if it.checkState() == Qt.CheckState.Checked]

    def _toggle_item(self, item):
        if self._guard:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked)

    def _on_all_toggled(self, checked: bool):
        if self._guard:
            return
        self._guard = True
        try:
            if checked:
                for it in self._items():
                    it.setCheckState(Qt.CheckState.Unchecked)
            elif not self._checked_pages():
                self._all_cb.setChecked(True)   # "All" can't be turned off
        finally:
            self._guard = False
        self._refresh_text()

    def _on_page_toggled(self):
        if self._guard:
            return
        self._guard = True
        try:
            self._all_cb.setChecked(not self._checked_pages())
        finally:
            self._guard = False
        self._refresh_text()

    def _refresh_text(self):
        pages = self._checked_pages()
        if not pages:
            self.setText("All pages")
            return
        label = ", ".join(str(p + 1) for p in pages)
        self.setText(label if len(label) <= 20 else f"{len(pages)} pages")

    # -- API used by the preview window --------------------------------
    def selected(self) -> list[int] | None:
        """0-based pages to print, or None for the whole document."""
        return self._checked_pages() or None

    def set_selected(self, pages) -> None:
        """Tick exactly *pages* (0-based); empty/None means All."""
        self._guard = True
        try:
            wanted = set(pages or ())
            for i, it in enumerate(self._items()):
                it.setCheckState(Qt.CheckState.Checked if i in wanted
                                 else Qt.CheckState.Unchecked)
            self._all_cb.setChecked(not wanted)
        finally:
            self._guard = False
        self._refresh_text()


class _PreviewWindow(QDialog):
    """A preview of what will print + printer picker + Print / Close bar.

    *print_fn* is called with a configured QPrinter.
    """

    def __init__(self, parent, title, preview_widget, print_fn,
                 page_count: int = 0):
        super().__init__(parent)
        self._page_count = page_count
        self.setWindowTitle(title)
        self.resize(*self._fitting_size())
        self._print_fn = print_fn

        from PySide6.QtPrintSupport import QPrinterInfo
        bar_widget = QWidget(self)
        self.printer_box = QComboBox(bar_widget)
        self.printer_box.setMinimumWidth(240)
        printers = QPrinterInfo.availablePrinters()
        for info in printers:
            self.printer_box.addItem(info.printerName())
        default = QPrinterInfo.defaultPrinterName()
        if default:
            idx = self.printer_box.findText(default)
            if idx >= 0:
                self.printer_box.setCurrentIndex(idx)

        print_btn = QPushButton("Print", bar_widget)
        print_btn.setObjectName("primary")
        print_btn.setDefault(True)
        print_btn.clicked.connect(self._do_print)
        settings_btn = QPushButton("Settings…", bar_widget)
        settings_btn.setToolTip("Paper size, orientation, copies, page range")
        settings_btn.clicked.connect(self._do_print_with_dialog)
        close_btn = QPushButton("Close", bar_widget)
        close_btn.clicked.connect(self.reject)

        # Page selection lives right here, next to Print — printing a 15-page
        # quoted thread when the user wanted page 1 wastes real paper, and
        # nobody should have to dig into "Settings…" to avoid that.
        # Two ways in: tick pages in the dropdown, or type a range like "2-5".
        self.page_picker = PagePicker(page_count, bar_widget)
        self.pages_edit = QLineEdit(bar_widget)
        self.pages_edit.setPlaceholderText("or type 2-5")
        self.pages_edit.setFixedWidth(110)
        self.pages_edit.setToolTip('Pages to print — e.g. "1", "2-5" or '
                                   '"1,2,5". Empty uses the dropdown.')
        self.pages_edit.editingFinished.connect(self._typed_pages_changed)

        bar = QHBoxLayout(bar_widget)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        bar.addWidget(QLabel("Printer:", bar_widget))
        bar.addWidget(self.printer_box)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Pages:", bar_widget))
        bar.addWidget(self.page_picker)
        bar.addWidget(self.pages_edit)
        if page_count:
            bar.addWidget(QLabel(f"of {page_count}", bar_widget))
        bar.addStretch(1)
        bar.addWidget(settings_btn)
        bar.addWidget(close_btn)
        bar.addWidget(print_btn)

        if not printers:
            # Silently doing nothing here is what made Print look broken.
            self.printer_box.addItem("No printer installed")
            self.printer_box.setEnabled(False)
            print_btn.setEnabled(False)
            settings_btn.setToolTip("No printer is installed on this PC")

        # The bar goes ABOVE the preview: at the bottom it was the first thing
        # to fall off a small screen, and people were resizing the window just
        # to reach Print. Up here it is the first thing you see, and the
        # preview (which may shrink to nothing) can never push it away.
        preview_widget.setMinimumHeight(80)
        preview_widget.setParent(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        lay.addWidget(bar_widget)
        lay.addWidget(preview_widget, 1)

    WANTED_SIZE = (900, 820)

    def _fitting_size(self) -> tuple[int, int]:
        """Never taller/wider than the screen: a fixed 900x820 ran off the
        bottom of small laptop displays, taking the Printer picker and the
        Print button with it — the window looked like it had no Print button."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return self.WANTED_SIZE
        geo = screen.availableGeometry()
        return (min(self.WANTED_SIZE[0], int(geo.width() * 0.92)),
                min(self.WANTED_SIZE[1], int(geo.height() * 0.92)))

    def showEvent(self, event):
        super().showEvent(event)
        # Re-fit on the screen the window actually landed on (multi-monitor),
        # then centre it there.
        w, h = self._fitting_size()
        if w < self.width() or h < self.height():
            self.resize(w, h)
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2,
                      max(geo.top(), geo.center().y() - self.height() // 2))

    def _printer(self):
        """QPrinter bound to the printer chosen in the combo."""
        from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
        name = self.printer_box.currentText()
        info = next((i for i in QPrinterInfo.availablePrinters()
                     if i.printerName() == name), None)
        printer = (QPrinter(QPrinter.PrinterMode.HighResolution) if info is None
                   else QPrinter(info, QPrinter.PrinterMode.HighResolution))
        pages = self._selected_pages()
        rng = (pages[0] + 1, pages[-1] + 1) if pages else None
        if rng:
            printer.setPrintRange(QPrinter.PrintRange.PageRange)
            printer.setFromTo(*rng)
        else:
            printer.setPrintRange(QPrinter.PrintRange.AllPages)
        return printer

    def _typed_pages_changed(self):
        """A typed range just ticks the matching boxes, so the dropdown always
        shows the truth about what will print."""
        typed = parse_pages(self.pages_edit.text(), self._page_count)
        if typed is not None:
            self.page_picker.set_selected(typed)

    def _selected_pages(self):
        """0-based pages to print, or None for the whole document."""
        typed = parse_pages(self.pages_edit.text(), self._page_count)
        if typed is not None:
            return typed
        return self.page_picker.selected()

    def _do_print(self):
        self._print_fn(self._printer(), self._selected_pages())

    def _do_print_with_dialog(self):
        from PySide6.QtPrintSupport import QPrintDialog
        printer = self._printer()
        dlg = QPrintDialog(printer, self)
        if self._page_count:
            dlg.setOption(QPrintDialog.PrintDialogOption.PrintPageRange, True)
            dlg.setMinMax(1, self._page_count)
        if dlg.exec() == QPrintDialog.DialogCode.Accepted:
            # The dialog owns the range once the user has been in it.
            self._print_fn(printer, None)


def selected_pages(printer, page_count: int) -> list[int]:
    """0-based pages the user actually asked for.

    The printer's page range was being ignored: asking for page 1 of a long
    quoted thread printed all 15 pages. Anything outside the document is
    clamped away, and an empty/invalid range falls back to the whole document
    rather than printing nothing.
    """
    from PySide6.QtPrintSupport import QPrinter
    all_pages = list(range(page_count))
    if printer.printRange() != QPrinter.PrintRange.PageRange:
        return all_pages
    first = printer.fromPage() - 1
    last = printer.toPage() - 1
    if first < 0 and last < 0:
        return all_pages
    first = max(first, 0)
    last = page_count - 1 if last < 0 else min(last, page_count - 1)
    if last < first:
        return all_pages
    return list(range(first, last + 1))


def _print_pdf_doc(doc, printer, pages=None):
    """Render the requested pages of *doc* onto *printer*.

    *pages* is an explicit 0-based list (it can have gaps, e.g. 1,2,5 — which a
    QPrinter's single from/to range cannot express). Without it we fall back to
    the printer's own range, which is what the system dialog sets.
    """
    if pages is None:
        pages = selected_pages(printer, doc.pageCount())
    pages = [p for p in pages if 0 <= p < doc.pageCount()] or \
        list(range(doc.pageCount()))
    painter = QPainter(printer)
    rect = painter.viewport()
    dpi = printer.resolution()
    for n, i in enumerate(pages):
        if n > 0:
            printer.newPage()
        pt = doc.pagePointSize(i)
        w = int(pt.width() / 72.0 * dpi)
        h = int(pt.height() / 72.0 * dpi)
        if w <= 0 or h <= 0:
            continue
        scale = min(rect.width() / w, rect.height() / h, 1.0)
        img = doc.render(i, QSize(max(1, int(w * scale)), max(1, int(h * scale))))
        if img.isNull():
            continue
        x = (rect.width() - img.width()) // 2
        y = (rect.height() - img.height()) // 2
        painter.drawImage(x, y, img)
    painter.end()


def show_pdf_preview(parent, pdf_path: str, title: str = "Print preview") -> bool:
    try:
        from PySide6.QtPdf import QPdfDocument
        from PySide6.QtPdfWidgets import QPdfView
    except Exception:
        return False
    doc = QPdfDocument(parent)
    if doc.load(pdf_path) != QPdfDocument.Error.None_:
        return False
    view = QPdfView()
    view.setDocument(doc)
    view.setPageMode(QPdfView.PageMode.MultiPage)
    view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
    win = _PreviewWindow(parent, title, view,
                         lambda printer, pages: _print_pdf_doc(doc, printer,
                                                               pages),
                         page_count=doc.pageCount())
    win._doc = doc  # keep alive
    win.exec()
    return True


def show_image_preview(parent, image_path: str, title: str = "Print preview") -> bool:
    pix = QPixmap(image_path)
    if pix.isNull():
        return False
    label = QLabel()
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    shown = pix
    if pix.width() > 1000 or pix.height() > 1300:
        shown = pix.scaled(1000, 1300, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
    label.setPixmap(shown)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
    scroll.setWidget(label)

    def do_print(printer, _pages=None):   # a single image is one page
        painter = QPainter(printer)
        rect = painter.viewport()
        scaled = pix.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        x = (rect.width() - scaled.width()) // 2
        y = (rect.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        painter.end()

    _PreviewWindow(parent, title, scroll, do_print).exec()
    return True


def show_text_preview(parent, text: str, title: str = "Print preview") -> bool:
    box = QPlainTextEdit()
    box.setReadOnly(True)
    box.setPlainText(text)
    box.setStyleSheet("font-family: 'SF Mono', Menlo, Consolas, monospace; "
                      "font-size: 12px;")
    # QTextDocument honours the printer's own from/to range.
    _PreviewWindow(parent, title, box,
                   lambda printer, _pages=None: box.print_(printer)).exec()
    return True


def show_email_preview(parent, webview, title: str = "Print email") -> None:
    """Render the email webview to a temporary PDF (QtWebEngine print is async),
    then show the PDF preview — what prints matches the reader exactly.

    Every failure path ends in something visible: Print used to be a no-op when
    the PDF render failed or QtPdf was missing from the build, which is exactly
    what "the print button does nothing" looks like.
    """
    from ..config import data_dir
    path = str(data_dir() / f"print_{uuid.uuid4().hex}.pdf")

    def done(ok_path, ok):
        try:
            webview.pdfPrintingFinished.disconnect(done)
        except (RuntimeError, TypeError):
            pass
        if not ok or not os.path.isfile(ok_path):
            QMessageBox.warning(parent, title,
                                "Could not prepare this email for printing.")
            return
        if show_pdf_preview(parent, ok_path, title):
            return
        # No PDF viewer component in this build — hand the file to the OS so the
        # user can still print it from their normal PDF app.
        QMessageBox.information(
            parent, title,
            "Opening the email in your PDF viewer — print it from there.")
        QDesktopServices.openUrl(QUrl.fromLocalFile(ok_path))

    webview.pdfPrintingFinished.connect(done)
    webview.printToPdf(path)
