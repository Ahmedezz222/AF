# -*- coding: utf-8 -*-
"""Сборка yolka.exe (GUI, один файл) через PyInstaller.

    pip install pyinstaller
    python build_exe.py

Результат: dist/yolka.exe — можно класть в GitHub Releases.
"""
import PyInstaller.__main__

PyInstaller.__main__.run([
    "--onefile",
    "--windowed",
    "--name", "yolka",
    "--clean",
    "--noconfirm",
    "run_gui.py",
])
