"""Gallery of the ODCS widgets.

    python -m odcs_ui.demo                  # Match System
    python -m odcs_ui.demo --theme dark
    python -m odcs_ui.demo --theme light --shot gallery.png   # render and exit
"""
from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from .theming import BASE_QSS, ThemeController, set_role, set_surface
from .widgets import (AboutDialog, CollapsibleSection, EmptyState, SettingsList, StatusFacts,
                      ViewSwitch)


def build() -> QWidget:
    window = QWidget()
    window.setWindowTitle("ODCS widgets")
    window.setObjectName("odcsDemo")
    set_surface(window, "window")
    root = QVBoxLayout(window)
    root.setContentsMargins(24, 24, 24, 24)
    root.setSpacing(20)

    top = QHBoxLayout()
    switch = ViewSwitch(["Status", "Restore"])
    top.addWidget(switch)
    top.addStretch(1)
    secondary = QPushButton("Log")
    primary = QPushButton("Back up now")
    set_role(primary, "primary")
    disabled = QPushButton("Convert")
    set_role(disabled, "primary")
    disabled.setEnabled(False)
    disabled.setToolTip("Add videos to the queue first")
    for button in (secondary, disabled, primary):
        top.addWidget(button)
    root.addLayout(top)

    body = QHBoxLayout()
    body.setSpacing(32)
    from PySide6.QtGui import QIcon
    settings = SettingsList("What's backed up")
    settings.addRow("Folders", "5 folders", icon=QIcon.fromTheme("folder"))
    settings.addRow("Applications", "97 selected · 2 to review", icon=QIcon.fromTheme("applications-all")).setValue(
        "97 selected · 2 to review", "warning")
    settings.addRow("Destination", "", icon=QIcon.fromTheme("drive-harddisk")).setValue(
        "TITAN-i · Not connected", "error", indicator="error")
    settings.addRow("Schedule", "Daily at 4:00 AM", icon=QIcon.fromTheme("chronometer"))
    settings.setFixedWidth(300)
    body.addWidget(settings, 0)

    right = QVBoxLayout()
    right.setSpacing(20)
    facts = StatusFacts("Can you get your files back?")
    facts.addFact("backup", "Backup completed", "Files were saved to TITAN-i", "ok", "Today at 8:51 AM")
    facts.addFact("check", "Integrity checked", "Stored data is readable and consistent", "ok", "Today at 6:25 AM")
    facts.addFact("recovery", "Recovery tested", "A restore using only your passphrase", "never",
                  action=("Test recovery…", lambda: None))
    right.addWidget(facts)
    expert = QLabel("Expert settings would go here.")
    set_role(expert, "caption")
    right.addWidget(CollapsibleSection("Expert", expert))
    right.addWidget(EmptyState("Drop videos here or choose Add Videos…"))
    about = QPushButton("About…")
    about.clicked.connect(lambda: AboutDialog("Keep", "0.9.2", "Back up and recover your files.", parent=window).exec())
    right.addWidget(about)
    right.addStretch(1)
    body.addLayout(right, 1)
    root.addLayout(body, 1)
    window.resize(1000, 620)
    return window


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="odcs_ui.demo")
    parser.add_argument("--theme", choices=["dark", "light", "system"], default="system")
    parser.add_argument("--shot", help="save a PNG of the gallery and exit")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    ThemeController(app, [BASE_QSS], choice=args.theme).apply()
    window = build()
    window.show()
    if args.shot:
        QTimer.singleShot(300, lambda: (window.grab().save(args.shot), app.quit()))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
