import asyncio
import os.path
import time
import sys
import platform
import queue
import traceback
from distutils.version import LooseVersion
from functools import partial
from typing import NamedTuple, Callable, Optional, TYPE_CHECKING
import base64

from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *

from electrum_ltc import version
from electrum_ltc import ecc
from electrum_ltc import constants
from electrum_ltc.i18n import _, languages
from electrum_ltc.util import FileImportFailed, FileExportFailed, make_aiohttp_session, PrintError
from electrum_ltc.paymentrequest import PR_UNPAID, PR_PAID, PR_EXPIRED

if TYPE_CHECKING:
    from .main_window import ElectrumWindow


if platform.system() == 'Windows':
    MONOSPACE_FONT = 'Lucida Console'
elif platform.system() == 'Darwin':
    MONOSPACE_FONT = 'Monaco'
else:
    MONOSPACE_FONT = 'monospace'


dialogs = []

pr_icons = {
    PR_UNPAID:":icons/unpaid.png",
    PR_PAID:":icons/confirmed.png",
    PR_EXPIRED:":icons/expired.png"
}

pr_tooltips = {
    PR_UNPAID:_('Pending'),
    PR_PAID:_('Paid'),
    PR_EXPIRED:_('Expired')
}

expiration_values = [
    (_('1 hour'), 60*60),
    (_('1 day'), 24*60*60),
    (_('1 week'), 7*24*60*60),
    (_('Never'), None)
]


class EnterButton(QPushButton):
    def __init__(self, text, func):
        QPushButton.__init__(self, text)
        self.func = func
        self.clicked.connect(func)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Return:
            self.func()


class ThreadedButton(QPushButton):
    def __init__(self, text, task, on_success=None, on_error=None):
        QPushButton.__init__(self, text)
        self.task = task
        self.on_success = on_success
        self.on_error = on_error
        self.clicked.connect(self.run_task)

    def run_task(self):
        self.setEnabled(False)
        self.thread = TaskThread(self)
        self.thread.add(self.task, self.on_success, self.done, self.on_error)

    def done(self):
        self.setEnabled(True)
        self.thread.stop()


class WWLabel(QLabel):
    def __init__ (self, text="", parent=None):
        QLabel.__init__(self, text, parent)
        self.setWordWrap(True)


class HelpLabel(QLabel):

    def __init__(self, text, help_text):
        QLabel.__init__(self, text)
        self.help_text = help_text
        self.app = QCoreApplication.instance()
        self.font = QFont()

    def mouseReleaseEvent(self, x):
        QMessageBox.information(self, 'Help', self.help_text)

    def enterEvent(self, event):
        self.font.setUnderline(True)
        self.setFont(self.font)
        self.app.setOverrideCursor(QCursor(Qt.PointingHandCursor))
        return QLabel.enterEvent(self, event)

    def leaveEvent(self, event):
        self.font.setUnderline(False)
        self.setFont(self.font)
        self.app.setOverrideCursor(QCursor(Qt.ArrowCursor))
        return QLabel.leaveEvent(self, event)


class HelpButton(QPushButton):
    def __init__(self, text):
        QPushButton.__init__(self, '?')
        self.help_text = text
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(20)
        self.clicked.connect(self.onclick)

    def onclick(self):
        QMessageBox.information(self, 'Help', self.help_text)


class InfoButton(QPushButton):
    def __init__(self, text):
        QPushButton.__init__(self, 'Info')
        self.help_text = text
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(60)
        self.clicked.connect(self.onclick)

    def onclick(self):
        QMessageBox.information(self, 'Info', self.help_text)


class Buttons(QHBoxLayout):
    def __init__(self, *buttons):
        QHBoxLayout.__init__(self)
        self.addStretch(1)
        for b in buttons:
            self.addWidget(b)

class CloseButton(QPushButton):
    def __init__(self, dialog):
        QPushButton.__init__(self, _("Close"))
        self.clicked.connect(dialog.close)
        self.setDefault(True)

class CopyButton(QPushButton):
    def __init__(self, text_getter, app):
        QPushButton.__init__(self, _("Copy"))
        self.clicked.connect(lambda: app.clipboard().setText(text_getter()))

class CopyCloseButton(QPushButton):
    def __init__(self, text_getter, app, dialog):
        QPushButton.__init__(self, _("Copy and Close"))
        self.clicked.connect(lambda: app.clipboard().setText(text_getter()))
        self.clicked.connect(dialog.close)
        self.setDefault(True)

class OkButton(QPushButton):
    def __init__(self, dialog, label=None):
        QPushButton.__init__(self, label or _("OK"))
        self.clicked.connect(dialog.accept)
        self.setDefault(True)

class CancelButton(QPushButton):
    def __init__(self, dialog, label=None):
        QPushButton.__init__(self, label or _("Cancel"))
        self.clicked.connect(dialog.reject)

class MessageBoxMixin(object):
    def top_level_window_recurse(self, window=None, test_func=None):
        window = window or self
        classes = (WindowModalDialog, QMessageBox)
        if test_func is None:
            test_func = lambda x: True
        for n, child in enumerate(window.children()):
            # Test for visibility as old closed dialogs may not be GC-ed.
            # Only accept children that confirm to test_func.
            if isinstance(child, classes) and child.isVisible() \
                    and test_func(child):
                return self.top_level_window_recurse(child, test_func=test_func)
        return window

    def top_level_window(self, test_func=None):
        return self.top_level_window_recurse(test_func)

    def question(self, msg, parent=None, title=None, icon=None):
        Yes, No = QMessageBox.Yes, QMessageBox.No
        return self.msg_box(icon or QMessageBox.Question,
                            parent, title or '',
                            msg, buttons=Yes|No, defaultButton=No) == Yes

    def show_warning(self, msg, parent=None, title=None, **kwargs):
        return self.msg_box(QMessageBox.Warning, parent,
                            title or _('Warning'), msg, **kwargs)

    def show_error(self, msg, parent=None, **kwargs):
        return self.msg_box(QMessageBox.Warning, parent,
                            _('Error'), msg, **kwargs)

    def show_critical(self, msg, parent=None, title=None, **kwargs):
        return self.msg_box(QMessageBox.Critical, parent,
                            title or _('Critical Error'), msg, **kwargs)

    def show_message(self, msg, parent=None, title=None, **kwargs):
        return self.msg_box(QMessageBox.Information, parent,
                            title or _('Information'), msg, **kwargs)

    def msg_box(self, icon, parent, title, text, buttons=QMessageBox.Ok,
                defaultButton=QMessageBox.NoButton, rich_text=False):
        parent = parent or self.top_level_window()
        if type(icon) is QPixmap:
            d = QMessageBox(QMessageBox.Information, title, str(text), buttons, parent)
            d.setIconPixmap(icon)
        else:
            d = QMessageBox(icon, title, str(text), buttons, parent)
        d.setWindowModality(Qt.WindowModal)
        d.setDefaultButton(defaultButton)
        if rich_text:
            d.setTextInteractionFlags(Qt.TextSelectableByMouse| Qt.LinksAccessibleByMouse)
            d.setTextFormat(Qt.RichText)
        else:
            d.setTextInteractionFlags(Qt.TextSelectableByMouse)
            d.setTextFormat(Qt.PlainText)
        return d.exec_()

class WindowModalDialog(QDialog, MessageBoxMixin):
    '''Handy wrapper; window modal dialogs are better for our multi-window
    daemon model as other wallet windows can still be accessed.'''
    def __init__(self, parent, title=None):
        QDialog.__init__(self, parent)
        self.setWindowModality(Qt.WindowModal)
        if title:
            self.setWindowTitle(title)


class WaitingDialog(WindowModalDialog):
    '''Shows a please wait dialog whilst running a task.  It is not
    necessary to maintain a reference to this dialog.'''
    def __init__(self, parent, message, task, on_success=None, on_error=None):
        assert parent
        if isinstance(parent, MessageBoxMixin):
            parent = parent.top_level_window()
        WindowModalDialog.__init__(self, parent, _("Please wait"))
        vbox = QVBoxLayout(self)
        vbox.addWidget(QLabel(message))
        self.accepted.connect(self.on_accepted)
        self.show()
        self.thread = TaskThread(self)
        self.thread.finished.connect(self.deleteLater)  # see #3956
        self.thread.add(task, on_success, self.accept, on_error)

    def wait(self):
        self.thread.wait()

    def on_accepted(self):
        self.thread.stop()


def line_dialog(parent, title, label, ok_label, default=None):
    dialog = WindowModalDialog(parent, title)
    dialog.setMinimumWidth(500)
    l = QVBoxLayout()
    dialog.setLayout(l)
    l.addWidget(QLabel(label))
    txt = QLineEdit()
    if default:
        txt.setText(default)
    l.addWidget(txt)
    l.addLayout(Buttons(CancelButton(dialog), OkButton(dialog, ok_label)))
    if dialog.exec_():
        return txt.text()

def text_dialog(parent, title, header_layout, ok_label, default=None, allow_multi=False):
    from .qrtextedit import ScanQRTextEdit
    dialog = WindowModalDialog(parent, title)
    dialog.setMinimumWidth(600)
    l = QVBoxLayout()
    dialog.setLayout(l)
    if isinstance(header_layout, str):
        l.addWidget(QLabel(header_layout))
    else:
        l.addLayout(header_layout)
    txt = ScanQRTextEdit(allow_multi=allow_multi)
    if default:
        txt.setText(default)
    l.addWidget(txt)
    l.addLayout(Buttons(CancelButton(dialog), OkButton(dialog, ok_label)))
    if dialog.exec_():
        return txt.toPlainText()

class ChoicesLayout(object):
    def __init__(self, msg, choices, on_clicked=None, checked_index=0):
        vbox = QVBoxLayout()
        if len(msg) > 50:
            vbox.addWidget(WWLabel(msg))
            msg = ""
        gb2 = QGroupBox(msg)
        vbox.addWidget(gb2)

        vbox2 = QVBoxLayout()
        gb2.setLayout(vbox2)

        self.group = group = QButtonGroup()
        for i,c in enumerate(choices):
            button = QRadioButton(gb2)
            button.setText(c)
            vbox2.addWidget(button)
            group.addButton(button)
            group.setId(button, i)
            if i==checked_index:
                button.setChecked(True)

        if on_clicked:
            group.buttonClicked.connect(partial(on_clicked, self))

        self.vbox = vbox

    def layout(self):
        return self.vbox

    def selected_index(self):
        return self.group.checkedId()

def address_field(addresses):
    hbox = QHBoxLayout()
    address_e = QLineEdit()
    if addresses and len(addresses) > 0:
        address_e.setText(addresses[0])
    else:
        addresses = []
    def func():
        try:
            i = addresses.index(str(address_e.text())) + 1
            i = i % len(addresses)
            address_e.setText(addresses[i])
        except ValueError:
            # the user might have changed address_e to an
            # address not in the wallet (or to something that isn't an address)
            if addresses and len(addresses) > 0:
                address_e.setText(addresses[0])
    button = QPushButton(_('Address'))
    button.clicked.connect(func)
    hbox.addWidget(button)
    hbox.addWidget(address_e)
    return hbox, address_e


def filename_field(parent, config, defaultname, select_msg):

    vbox = QVBoxLayout()
    vbox.addWidget(QLabel(_("Format")))
    gb = QGroupBox("format", parent)
    b1 = QRadioButton(gb)
    b1.setText(_("CSV"))
    b1.setChecked(True)
    b2 = QRadioButton(gb)
    b2.setText(_("json"))
    vbox.addWidget(b1)
    vbox.addWidget(b2)

    hbox = QHBoxLayout()

    directory = config.get('io_dir', os.path.expanduser('~'))
    path = os.path.join( directory, defaultname )
    filename_e = QLineEdit()
    filename_e.setText(path)

    def func():
        text = filename_e.text()
        _filter = "*.csv" if text.endswith(".csv") else "*.json" if text.endswith(".json") else None
        p, __ = QFileDialog.getSaveFileName(None, select_msg, text, _filter)
        if p:
            filename_e.setText(p)

    button = QPushButton(_('File'))
    button.clicked.connect(func)
    hbox.addWidget(button)
    hbox.addWidget(filename_e)
    vbox.addLayout(hbox)

    def set_csv(v):
        text = filename_e.text()
        text = text.replace(".json",".csv") if v else text.replace(".csv",".json")
        filename_e.setText(text)

    b1.clicked.connect(lambda: set_csv(True))
    b2.clicked.connect(lambda: set_csv(False))

    return vbox, filename_e, b1

class ElectrumItemDelegate(QStyledItemDelegate):
    def __init__(self, tv):
        super().__init__(tv)
        self.tv = tv
        self.opened = None
        def on_closeEditor(editor: QLineEdit, hint):
            self.opened = None
        def on_commitData(editor: QLineEdit):
            new_text = editor.text()
            idx = QModelIndex(self.opened)
            row, col = idx.row(), idx.column()
            _prior_text, user_role = self.tv.text_txid_from_coordinate(row, col)
            # check that we didn't forget to set UserRole on an editable field
            assert user_role is not None, (row, col)
            self.tv.on_edited(idx, user_role, new_text)
        self.closeEditor.connect(on_closeEditor)
        self.commitData.connect(on_commitData)

    def createEditor(self, parent, option, idx):
        self.opened = QPersistentModelIndex(idx)
        return super().createEditor(parent, option, idx)

class MyTreeView(QTreeView):

    def __init__(self, parent: 'ElectrumWindow', create_menu, stretch_column=None, editable_columns=None):
        super().__init__(parent)
        self.parent = parent
        self.config = self.parent.config
        self.stretch_column = stretch_column
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(create_menu)
        self.setUniformRowHeights(True)

        self.icon_cache = IconCache()

        # Control which columns are editable
        if editable_columns is None:
            editable_columns = {stretch_column}
        else:
            editable_columns = set(editable_columns)
        self.editable_columns = editable_columns
        self.setItemDelegate(ElectrumItemDelegate(self))
        self.current_filter = ""

        self.setRootIsDecorated(False)  # remove left margin
        self.toolbar_shown = False

        # When figuring out the size of columns, Qt by default looks at
        # the first 1000 rows (at least if resize mode is QHeaderView.ResizeToContents).
        # This would be REALLY SLOW, and it's not perfect anyway.
        # So to speed the UI up considerably, set it to
        # only look at as many rows as currently visible.
        self.header().setResizeContentsPrecision(0)

    def set_editability(self, items):
        for idx, i in enumerate(items):
            i.setEditable(idx in self.editable_columns)

    def selected_in_column(self, column: int):
        items = self.selectionModel().selectedIndexes()
        return list(x for x in items if x.column() == column)

    def current_item_user_role(self, col) -> Optional[QStandardItem]:
        idx = self.selectionModel().currentIndex()
        idx = idx.sibling(idx.row(), col)
        item = self.model().itemFromIndex(idx)
        if item:
            return item.data(Qt.UserRole)

    def set_current_idx(self, set_current: QPersistentModelIndex):
        if set_current:
            assert isinstance(set_current, QPersistentModelIndex)
            assert set_current.isValid()
            self.selectionModel().select(QModelIndex(set_current), QItemSelectionModel.SelectCurrent)

    def update_headers(self, headers):
        model = self.model()
        model.setHorizontalHeaderLabels(headers)
        self.header().setStretchLastSection(False)
        for col in range(len(headers)):
            sm = QHeaderView.Stretch if col == self.stretch_column else QHeaderView.ResizeToContents
            self.header().setSectionResizeMode(col, sm)

    def keyPressEvent(self, event):
        if self.itemDelegate().opened:
            return
        if event.key() in [ Qt.Key_F2, Qt.Key_Return ]:
            self.on_activated(self.selectionModel().currentIndex())
            return
        super().keyPressEvent(event)

    def on_activated(self, idx):
        # on 'enter' we show the menu
        pt = self.visualRect(idx).bottomLeft()
        pt.setX(50)
        self.customContextMenuRequested.emit(pt)

    def edit(self, idx, trigger=QAbstractItemView.AllEditTriggers, event=None):
        """
        this is to prevent:
           edit: editing failed
        from inside qt
        """
        return super().edit(idx, trigger, event)

    def on_edited(self, idx: QModelIndex, user_role, text):
        self.parent.wallet.set_label(user_role, text)
        self.parent.history_model.refresh('on_edited in MyTreeView')
        self.parent.update_completions()

    def should_hide(self, row):
        """
        row_num is for self.model(). So if there is a proxy, it is the row number
        in that!
        """
        return False

    def text_txid_from_coordinate(self, row_num, column):
        assert not isinstance(self.model(), QSortFilterProxyModel)
        idx = self.model().index(row_num, column)
        item = self.model().itemFromIndex(idx)
        user_role = item.data(Qt.UserRole)
        return item.text(), user_role

    def hide_row(self, row_num):
        """
        row_num is for self.model(). So if there is a proxy, it is the row number
        in that!
        """
        should_hide = self.should_hide(row_num)
        if not self.current_filter and should_hide is None:
            # no filters at all, neither date nor search
            self.setRowHidden(row_num, QModelIndex(), False)
            return
        for column in self.filter_columns:
            txt, _ = self.text_txid_from_coordinate(row_num, column)
            txt = txt.lower()
            if self.current_filter in txt:
                # the filter matched, but the date filter might apply
                self.setRowHidden(row_num, QModelIndex(), bool(should_hide))
                break
        else:
            # we did not find the filter in any columns, hide the item
            self.setRowHidden(row_num, QModelIndex(), True)

    def filter(self, p):
        p = p.lower()
        self.current_filter = p
        self.hide_rows()

    def hide_rows(self):
        for row in range(self.model().rowCount()):
            self.hide_row(row)

    def create_toolbar(self, config=None):
        hbox = QHBoxLayout()
        buttons = self.get_toolbar_buttons()
        for b in buttons:
            b.setVisible(False)
            hbox.addWidget(b)
        hide_button = QPushButton('x')
        hide_button.setVisible(False)
        hide_button.pressed.connect(lambda: self.show_toolbar(False, config))
        self.toolbar_buttons = buttons + (hide_button,)
        hbox.addStretch()
        hbox.addWidget(hide_button)
        return hbox

    def save_toolbar_state(self, state, config):
        pass  # implemented in subclasses

    def show_toolbar(self, state, config=None):
        if state == self.toolbar_shown:
            return
        self.toolbar_shown = state
        if config:
            self.save_toolbar_state(state, config)
        for b in self.toolbar_buttons:
            b.setVisible(state)
        if not state:
            self.on_hide_toolbar()

    def toggle_toolbar(self, config=None):
        self.show_toolbar(not self.toolbar_shown, config)


class ButtonsWidget(QWidget):

    def __init__(self):
        super(QWidget, self).__init__()
        self.buttons = []

    def resizeButtons(self):
        frameWidth = self.style().pixelMetric(QStyle.PM_DefaultFrameWidth)
        x = self.rect().right() - frameWidth
        y = self.rect().bottom() - frameWidth
        for button in self.buttons:
            sz = button.sizeHint()
            x -= sz.width()
            button.move(x, y - sz.height())

    def addButton(self, icon_name, on_click, tooltip):
        button = QToolButton(self)
        button.setIcon(QIcon(icon_name))
        button.setIconSize(QSize(25,25))
        button.setStyleSheet("QToolButton { border: none; hover {border: 1px} pressed {border: 1px} padding: 0px; }")
        button.setVisible(True)
        button.setToolTip(tooltip)
        button.clicked.connect(on_click)
        self.buttons.append(button)
        return button

    def addCopyButton(self, app):
        self.app = app
        self.addButton(":icons/copy.png", self.on_copy, _("Copy to clipboard"))

    def on_copy(self):
        self.app.clipboard().setText(self.text())
        QToolTip.showText(QCursor.pos(), _("Text copied to clipboard"), self)

class ButtonsLineEdit(QLineEdit, ButtonsWidget):
    def __init__(self, text=None):
        QLineEdit.__init__(self, text)
        self.buttons = []

    def resizeEvent(self, e):
        o = QLineEdit.resizeEvent(self, e)
        self.resizeButtons()
        return o

class ButtonsTextEdit(QPlainTextEdit, ButtonsWidget):
    def __init__(self, text=None):
        QPlainTextEdit.__init__(self, text)
        self.setText = self.setPlainText
        self.text = self.toPlainText
        self.buttons = []

    def resizeEvent(self, e):
        o = QPlainTextEdit.resizeEvent(self, e)
        self.resizeButtons()
        return o


class TaskThread(QThread):
    '''Thread that runs background tasks.  Callbacks are guaranteed
    to happen in the context of its parent.'''

    class Task(NamedTuple):
        task: Callable
        cb_success: Optional[Callable]
        cb_done: Optional[Callable]
        cb_error: Optional[Callable]

    doneSig = pyqtSignal(object, object, object)

    def __init__(self, parent, on_error=None):
        super(TaskThread, self).__init__(parent)
        self.on_error = on_error
        self.tasks = queue.Queue()
        self.doneSig.connect(self.on_done)
        self.start()

    def add(self, task, on_success=None, on_done=None, on_error=None):
        on_error = on_error or self.on_error
        self.tasks.put(TaskThread.Task(task, on_success, on_done, on_error))

    def run(self):
        while True:
            task = self.tasks.get()  # type: TaskThread.Task
            if not task:
                break
            try:
                result = task.task()
                self.doneSig.emit(result, task.cb_done, task.cb_success)
            except BaseException:
                self.doneSig.emit(sys.exc_info(), task.cb_done, task.cb_error)

    def on_done(self, result, cb_done, cb_result):
        # This runs in the parent's thread.
        if cb_done:
            cb_done()
        if cb_result:
            cb_result(result)

    def stop(self):
        self.tasks.put(None)


class ColorSchemeItem:
    def __init__(self, fg_color, bg_color):
        self.colors = (fg_color, bg_color)

    def _get_color(self, background):
        return self.colors[(int(background) + int(ColorScheme.dark_scheme)) % 2]

    def as_stylesheet(self, background=False):
        css_prefix = "background-" if background else ""
        color = self._get_color(background)
        return "QWidget {{ {}color:{}; }}".format(css_prefix, color)

    def as_color(self, background=False):
        color = self._get_color(background)
        return QColor(color)


class ColorScheme:
    dark_scheme = False

    GREEN = ColorSchemeItem("#117c11", "#8af296")
    YELLOW = ColorSchemeItem("#897b2a", "#ffff00")
    RED = ColorSchemeItem("#7c1111", "#f18c8c")
    BLUE = ColorSchemeItem("#123b7c", "#8cb3f2")
    DEFAULT = ColorSchemeItem("black", "white")

    @staticmethod
    def has_dark_background(widget):
        brightness = sum(widget.palette().color(QPalette.Background).getRgb()[0:3])
        return brightness < (255*3/2)

    @staticmethod
    def update_from_widget(widget, force_dark=False):
        if force_dark or ColorScheme.has_dark_background(widget):
            ColorScheme.dark_scheme = True


class AcceptFileDragDrop:
    def __init__(self, file_type=""):
        assert isinstance(self, QWidget)
        self.setAcceptDrops(True)
        self.file_type = file_type

    def validateEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return False
        for url in event.mimeData().urls():
            if not url.toLocalFile().endswith(self.file_type):
                event.ignore()
                return False
        event.accept()
        return True

    def dragEnterEvent(self, event):
        self.validateEvent(event)

    def dragMoveEvent(self, event):
        if self.validateEvent(event):
            event.setDropAction(Qt.CopyAction)

    def dropEvent(self, event):
        if self.validateEvent(event):
            for url in event.mimeData().urls():
                self.onFileAdded(url.toLocalFile())

    def onFileAdded(self, fn):
        raise NotImplementedError()


def import_meta_gui(electrum_window, title, importer, on_success):
    filter_ = "JSON (*.json);;All files (*)"
    filename = electrum_window.getOpenFileName(_("Open {} file").format(title), filter_)
    if not filename:
        return
    try:
        importer(filename)
    except FileImportFailed as e:
        electrum_window.show_critical(str(e))
    else:
        electrum_window.show_message(_("Your {} were successfully imported").format(title))
        on_success()


def export_meta_gui(electrum_window, title, exporter):
    filter_ = "JSON (*.json);;All files (*)"
    filename = electrum_window.getSaveFileName(_("Select file to save your {}").format(title),
                                               'electrum-ltc_{}.json'.format(title), filter_)
    if not filename:
        return
    try:
        exporter(filename)
    except FileExportFailed as e:
        electrum_window.show_critical(str(e))
    else:
        electrum_window.show_message(_("Your {0} were exported to '{1}'")
                                     .format(title, str(filename)))


def get_parent_main_window(widget):
    """Returns a reference to the ElectrumWindow this widget belongs to."""
    from .main_window import ElectrumWindow
    for _ in range(100):
        if widget is None:
            return None
        if not isinstance(widget, ElectrumWindow):
            widget = widget.parentWidget()
        else:
            return widget
    return None

class IconCache:

    def __init__(self):
        self.__cache = {}

    def get(self, file_name):
        if file_name not in self.__cache:
            self.__cache[file_name] = QIcon(file_name)
        return self.__cache[file_name]


def get_default_language():
    name = QLocale.system().name()
    return name if name in languages else 'en_UK'

class FromList(QTreeWidget):
    def __init__(self, parent, create_menu):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setMaximumHeight(300)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(create_menu)
        self.setUniformRowHeights(True)
        # remove left margin
        self.setRootIsDecorated(False)
        self.setColumnCount(2)
        self.header().setStretchLastSection(False)
        sm = QHeaderView.ResizeToContents
        self.header().setSectionResizeMode(0, sm)
        self.header().setSectionResizeMode(1, sm)


class UpdateCheck(QWidget, PrintError):
    url = "https://electrum-ltc.org/version"
    download_url = "https://electrum-ltc.org/#download"

    VERSION_ANNOUNCEMENT_SIGNING_KEYS = (
        "LWZzbv5SbiRRjBDL6dUYRdBX9Dp89RDZgG",
    )

    def __init__(self, main_window, latest_version=None):
        self.main_window = main_window
        QWidget.__init__(self)
        self.setWindowTitle('Electrum-LTC - ' + _('Update Check'))
        self.content = QVBoxLayout()
        self.content.setContentsMargins(*[10]*4)

        self.heading_label = QLabel()
        self.content.addWidget(self.heading_label)

        self.detail_label = QLabel()
        self.content.addWidget(self.detail_label)

        self.pb = QProgressBar()
        self.pb.setMaximum(0)
        self.pb.setMinimum(0)
        self.content.addWidget(self.pb)

        versions = QHBoxLayout()
        versions.addWidget(QLabel(_("Current version: {}".format(version.ELECTRUM_VERSION))))
        self.latest_version_label = QLabel(_("Latest version: {}".format(" ")))
        versions.addWidget(self.latest_version_label)
        self.content.addLayout(versions)

        self.update_view(latest_version)

        self.update_check_thread = UpdateCheckThread(self.main_window)
        self.update_check_thread.checked.connect(self.on_version_retrieved)
        self.update_check_thread.failed.connect(self.on_retrieval_failed)
        self.update_check_thread.start()

        close_button = QPushButton(_("Close"))
        close_button.clicked.connect(self.close)
        self.content.addWidget(close_button)
        self.setLayout(self.content)
        self.show()

    def on_version_retrieved(self, version):
        self.update_view(version)

    def on_retrieval_failed(self):
        self.heading_label.setText('<h2>' + _("Update check failed") + '</h2>')
        self.detail_label.setText(_("Sorry, but we were unable to check for updates. Please try again later."))
        self.pb.hide()

    @staticmethod
    def is_newer(latest_version):
        return latest_version > LooseVersion(version.ELECTRUM_VERSION)

    def update_view(self, latest_version=None):
        if latest_version:
            self.pb.hide()
            self.latest_version_label.setText(_("Latest version: {}".format(latest_version)))
            if self.is_newer(latest_version):
                self.heading_label.setText('<h2>' + _("There is a new update available") + '</h2>')
                url = "<a href='{u}'>{u}</a>".format(u=UpdateCheck.download_url)
                self.detail_label.setText(_("You can download the new version from {}.").format(url))
            else:
                self.heading_label.setText('<h2>' + _("Already up to date") + '</h2>')
                self.detail_label.setText(_("You are already on the latest version of Electrum."))
        else:
            self.heading_label.setText('<h2>' + _("Checking for updates...") + '</h2>')
            self.detail_label.setText(_("Please wait while Electrum checks for available updates."))


class UpdateCheckThread(QThread, PrintError):
    checked = pyqtSignal(object)
    failed = pyqtSignal()

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window

    async def get_update_info(self):
        async with make_aiohttp_session(proxy=self.main_window.network.proxy) as session:
            async with session.get(UpdateCheck.url) as result:
                signed_version_dict = await result.json(content_type=None)
                # example signed_version_dict:
                # {
                #     "version": "3.9.9",
                #     "signatures": {
                #         "1Lqm1HphuhxKZQEawzPse8gJtgjm9kUKT4": "IA+2QG3xPRn4HAIFdpu9eeaCYC7S5wS/sDxn54LJx6BdUTBpse3ibtfq8C43M7M1VfpGkD5tsdwl5C6IfpZD/gQ="
                #     }
                # }
                version_num = signed_version_dict['version']
                sigs = signed_version_dict['signatures']
                for address, sig in sigs.items():
                    if address not in UpdateCheck.VERSION_ANNOUNCEMENT_SIGNING_KEYS:
                        continue
                    sig = base64.b64decode(sig)
                    msg = version_num.encode('utf-8')
                    if ecc.verify_message_with_address(address=address, sig65=sig, message=msg,
                                                       net=constants.BitcoinMainnet):
                        self.print_error(f"valid sig for version announcement '{version_num}' from address '{address}'")
                        break
                else:
                    raise Exception('no valid signature for version announcement')
                return LooseVersion(version_num.strip())

    def run(self):
        try:
            self.checked.emit(asyncio.run_coroutine_threadsafe(self.get_update_info(), self.main_window.network.asyncio_loop).result())
        except Exception:
            self.print_error(traceback.format_exc())
            self.failed.emit()


if __name__ == "__main__":
    app = QApplication([])
    t = WaitingDialog(None, 'testing ...', lambda: [time.sleep(1)], lambda x: QMessageBox.information(None, 'done', "done"))
    t.start()
    app.exec_()