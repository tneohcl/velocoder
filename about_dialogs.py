"""About VeloCoder, System Information, and Licenses -- three small,
self-contained modal dialogs (no MainWindow coupling; the caller passes
in already-resolved data -- an icon, the hardware backend list, the
resolved theme name, the ffmpeg version string -- rather than this
module reaching into MainWindow's own internals for it).
"""
import platform

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)

from constants import APP_NAME, APP_ORGANIZATION, APP_VERSION


def system_info_text(available_backends, theme_name: str, ffmpeg_version: str | None) -> str:
    """Plain text for System Information's viewer and its Copy button --
    built ONLY from the app's own version/name constants, platform.*,
    each backend's display_name, and the resolved theme name. No file
    path, filename, or username ever enters this function's inputs in
    the first place, so there's nothing to filter out afterward -- the
    "no personal data in copied System Information" requirement holds
    by construction, not by scrubbing."""
    backend_lines = "\n".join(f"- {backend.display_name}" for backend in available_backends)
    return (
        f"{APP_NAME} {APP_VERSION}\n"
        f"Platform: {platform.system()} {platform.machine()}\n"
        f"FFmpeg: {ffmpeg_version or 'Not found'}\n"
        f"\n"
        f"Available Processing:\n"
        f"{backend_lines}\n"
        f"\n"
        f"Theme: {theme_name}\n"
    )


class SystemInfoDialog(QDialog):
    def __init__(self, parent, available_backends, theme_name: str, ffmpeg_version: str | None):
        super().__init__(parent)
        self.setWindowTitle("System Information")
        self._text = system_info_text(available_backends, theme_name, ffmpeg_version)

        text_view = QPlainTextEdit()
        text_view.setPlainText(self._text)
        text_view.setReadOnly(True)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self._copy)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setDefault(True)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.copy_btn)
        buttons_row.addStretch(1)
        buttons_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(text_view)
        layout.addLayout(buttons_row)
        self.resize(420, 320)

    def _copy(self):
        QApplication.clipboard().setText(self._text)


_LICENSE_TEXT = f"""{APP_NAME}

Copyright © 2026 {APP_ORGANIZATION}. All rights reserved.

License terms for {APP_NAME} itself are provisional pending packaging and \
distribution -- this placeholder will be replaced with the actual license \
text once that is finalized.


FFmpeg

This application uses FFmpeg (https://ffmpeg.org) but does not include, \
bundle, or modify its source code. FFmpeg is a trademark of Fabrice \
Bellard, originator of the FFmpeg project. Depending on how the specific \
FFmpeg build in use was configured, it is licensed under the GNU Lesser \
General Public License (LGPL) version 2.1 or later, or the GNU General \
Public License (GPL) version 2 or later.


Third-Party Notices

This application is built with PySide6 (Qt for Python), licensed under \
the GNU Lesser General Public License (LGPL) version 3.
"""


class LicensesDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Licenses")

        text_view = QPlainTextEdit()
        text_view.setPlainText(_LICENSE_TEXT)
        text_view.setReadOnly(True)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setDefault(True)

        layout = QVBoxLayout(self)
        layout.addWidget(text_view)
        buttons_row = QHBoxLayout()
        buttons_row.addStretch(1)
        buttons_row.addWidget(close_btn)
        layout.addLayout(buttons_row)
        self.resize(440, 380)


class AboutDialog(QDialog):
    def __init__(self, parent, icon: QIcon, available_backends, theme_name: str, ffmpeg_version: str | None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        # Compact and fixed -- About is a small, static identity card,
        # not a resizable workspace (System Information/Licenses, the
        # two dialogs it opens into, are the ones with real scrollable
        # content and get their own resizable size instead).
        self.setFixedSize(320, 340)

        icon_label = QLabel()
        icon_label.setPixmap(icon.pixmap(64, 64))
        icon_label.setAlignment(Qt.AlignCenter)

        name_label = QLabel(APP_NAME)
        name_label.setAlignment(Qt.AlignCenter)
        name_font = QFont(name_label.font())
        name_font.setPointSize(name_font.pointSize() + 4)
        name_font.setWeight(QFont.Weight(600))
        name_label.setFont(name_font)

        version_label = QLabel(f"Version {APP_VERSION}")
        version_label.setAlignment(Qt.AlignCenter)

        tagline_label = QLabel("Focused video transcoding without the clutter.")
        tagline_label.setAlignment(Qt.AlignCenter)
        tagline_label.setWordWrap(True)

        ffmpeg_label = QLabel("Powered by FFmpeg")
        ffmpeg_label.setAlignment(Qt.AlignCenter)

        copyright_label = QLabel(f"© 2026 {APP_ORGANIZATION}")
        copyright_label.setAlignment(Qt.AlignCenter)

        system_info_btn = QPushButton("System Information…")
        system_info_btn.clicked.connect(
            lambda: SystemInfoDialog(self, available_backends, theme_name, ffmpeg_version).exec()
        )
        licenses_btn = QPushButton("Licenses…")
        licenses_btn.clicked.connect(lambda: LicensesDialog(self).exec())
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setDefault(True)

        secondary_buttons_row = QHBoxLayout()
        secondary_buttons_row.addWidget(system_info_btn)
        secondary_buttons_row.addWidget(licenses_btn)

        layout = QVBoxLayout(self)
        for widget in (icon_label, name_label, version_label, tagline_label, ffmpeg_label, copyright_label):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addLayout(secondary_buttons_row)
        layout.addWidget(close_btn)
