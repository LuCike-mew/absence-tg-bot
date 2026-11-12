# -*- mode: python ; coding: utf-8 -*-
#
# Сборка MiraiBot в режиме "папка" (onedir): экзешник + все зависимости в
# отдельной папке dist/MiraiBot. Это надёжнее, чем onefile (не страдает от
# ошибок временного кеша при распаковке).
#
# Точка входа — gui.py: без флага запускает панель управления, с флагом
# --bot запускает логику бота (main.py).

from PyInstaller.utils.hooks import collect_data_files

# Ассеты интерфейса customtkinter (темы .json) не подхватываются автоматически.
datas = collect_data_files('customtkinter')
# Иконка — кладём рядом с экзешником, чтобы панель/трей нашли её по
# относительному пути "icon.ico".
datas += [('icon.ico', '.')]

# Динамические (внутрифункциональные) импорты, которые PyInstaller не видит
# при статическом анализе:
#   - pypdf  — грузится лениво в parser/schedule.py (_pdf_extract_text)
#   - pystray._win32 — бэкенд трея на Windows подключается динамически
hiddenimports = [
    'pypdf',
    'pystray._win32',
]

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MiraiBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='MiraiBot',
)