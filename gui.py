import os
import subprocess
import sys
import threading
import ctypes
import winreg
import customtkinter as ctk
from PIL import Image
import pystray
import tkinter as tk

# Идентификатор процесса для корректной иконки на панели задач Windows
myappid = 'mybot.adminpanel.app.1.0'
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except Exception:
    pass

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

ICON_PATH = "icon.ico"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "BotControlPanel"

# ── Автоперезапуск упавшего процесса ─────────────────────────────────────────
# Если процесс бота всё же завершился сам (не по кнопке «Остановить»), панель
# поднимает его снова через AUTO_RESTART_DELAY_MS. Бот также умеет
# перезапускать свой внутренний цикл (см. main.py), но эта страховка нужна на
# случай фатальных ошибок (например, крах интерпретатора).
AUTO_RESTART_DELAY_MS = 60_000   # попытка перезапуска раз в минуту
AUTO_RESTART_MAX_ATTEMPTS = 30   # ограничение, чтобы не зацикливаться вечно


class BotControlApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Панель управления ботом")
        self.geometry("380x480")
        self.resizable(False, False)

        self.process = None
        self.log_thread = None
        self.tray_icon = None
        # Флаги управления автоперезапуском
        self._restart_enabled = False   # перезапускать упавший процесс?
        self._restart_attempts = 0      # сколько попыток уже сделано подряд
        self._restart_job = None        # id таймера self.after
        self._stopping = False          # остановка по кнопке/выходу

        self.setup_window_icon()
        self.protocol("WM_DELETE_WINDOW", self.minimize_to_tray)

        # Панель статуса
        self.status_frame = ctk.CTkFrame(self)
        self.status_frame.pack(padx=15, pady=(15, 10), fill="x")

        self.lbl_status = ctk.CTkLabel(
            self.status_frame,
            text="Статус: Остановлен",
            font=("Arial", 14, "bold"),
            text_color="#ff5252"
        )
        self.lbl_status.pack(pady=10)

        # Панель с кнопками
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(padx=15, pady=5, fill="x")

        self.btn_start = ctk.CTkButton(
            self.btn_frame,
            text="Запустить бота",
            command=self.start_bot,
            fg_color="#2ea6ff",
            hover_color="#1e85d0",
            font=("Arial", 12, "bold")
        )
        self.btn_start.pack(side="left", expand=True, padx=(0, 5))

        self.btn_stop = ctk.CTkButton(
            self.btn_frame,
            text="Остановить бота",
            command=self.stop_bot,
            fg_color="#ff5252",
            hover_color="#d32f2f",
            font=("Arial", 12, "bold")
        )
        self.btn_stop.pack(side="right", expand=True, padx=(5, 0))

        # Автозапуск
        self.switch_autostart = ctk.CTkSwitch(
            self,
            text="Автозапуск при старте Windows",
            command=self.toggle_autostart,
            font=("Arial", 12)
        )
        self.switch_autostart.pack(padx=15, pady=(10, 5), anchor="w")

        if self.check_autostart():
            self.switch_autostart.select()

        # Заголовок логов и кнопка копирования
        self.logs_header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.logs_header_frame.pack(padx=15, pady=(10, 5), fill="x")

        self.lbl_logs = ctk.CTkLabel(
            self.logs_header_frame,
            text="Логи работы:",
            font=("Arial", 12, "bold")
        )
        self.lbl_logs.pack(side="left")

        self.btn_copy_logs = ctk.CTkButton(
            self.logs_header_frame,
            text="Скопировать логи",
            width=120,
            height=24,
            font=("Arial", 11),
            fg_color="#3a3a3a",
            hover_color="#4a4a4a",
            command=self.copy_all_logs
        )
        self.btn_copy_logs.pack(side="right")

        # Текстовое поле логов
        self.txt_logs = ctk.CTkTextbox(self, font=("Consolas", 11))
        self.txt_logs.pack(padx=15, pady=(0, 15), fill="both", expand=True)

        # Контекстное меню ПКМ
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Скопировать", command=self.copy_selected_logs)
        self.context_menu.add_command(label="Выделить всё", command=self.select_all_logs)

        # Привязка событий к внутреннему виджету
        internal_box = self.txt_logs._textbox
        internal_box.bind("<Button-3>", self.show_context_menu)
        internal_box.bind("<<Copy>>", self.copy_selected_logs)
        internal_box.bind("<<SelectAll>>", self.select_all_logs)
        internal_box.bind("<Key>", self.block_text_editing)

    # РАБОТА С ЛОГАМИ И БУФЕРОМ ОБМЕНА

    def block_text_editing(self, event):
        """Блокирует редактирование, разрешая навигацию, Ctrl+C и Ctrl+A."""
        if event.state & 0x4 or event.keysym in [
            'c', 'C', 'a', 'A', 'Up', 'Down', 'Left', 'Right', 'Home', 'End', 'Prior', 'Next'
        ]:
            return None
        return "break"

    def show_context_menu(self, event):
        """Вызов меню по ПКМ."""
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def copy_selected_logs(self, event=None):
        """Скопировать выделенный текст."""
        try:
            selected_text = self.txt_logs._textbox.get("sel.first", "sel.last")
            if selected_text:
                self.clipboard_clear()
                self.clipboard_append(selected_text)
        except Exception:
            pass
        return "break"

    def copy_all_logs(self):
        """Скопировать весь текст логов по кнопке."""
        all_text = self.txt_logs.get("1.0", "end-1c")
        if all_text.strip():
            self.clipboard_clear()
            self.clipboard_append(all_text)
            self.btn_copy_logs.configure(text="Скопировано!")
            self.after(1500, lambda: self.btn_copy_logs.configure(text="Скопировать логи"))

    def select_all_logs(self, event=None):
        """Выделить весь текст."""
        self.txt_logs._textbox.tag_add("sel", "1.0", "end")
        return "break"

    def append_log(self, text: str):
        self.txt_logs.insert("end", text)
        self.txt_logs.see("end")

    # ЛОГИКА АВТОЗАПУСКА И ПРОЦЕССА

    def check_autostart(self) -> bool:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False

    def toggle_autostart(self):
        is_enabled = self.switch_autostart.get() == 1
        script_path = os.path.abspath(sys.argv[0])
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exec_cmd = f'"{pythonw}" "{script_path}"' if os.path.exists(pythonw) else f'"{sys.executable}" "{script_path}"'

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE)
            if is_enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exec_cmd)
                self.append_log("[СИСТЕМА] Автозапуск включен.\n")
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    self.append_log("[СИСТЕМА] Автозапуск отключен.\n")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            self.append_log(f"[ОШИБКА] Настройка автозапуска: {e}\n")

    def setup_window_icon(self):
        if os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                img = ctk.CTkImage(file=ICON_PATH)
                self.iconphoto(False, img)

    def read_output(self):
        if not self.process:
            return

        try:
            for line in iter(self.process.stdout.readline, ''):
                if line:
                    self.after(0, self.append_log, line)
                else:
                    break
        except Exception as exc:
            # Ошибка чтения логов не должна молча убивать поток (иначе пайп
            # переполнится и бот зависнет на записи в stdout).
            self.after(0, self.append_log, f"[ОШИБКА] Чтение логов прервано: {exc}\n")

        self.after(0, self.on_process_finished)

    def on_process_finished(self):
        self.process = None
        self.append_log("[СИСТЕМА] Процесс бота завершен.\n")

        # Не перезапускаем при штатной остановке из GUI.
        if self._stopping:
            self.lbl_status.configure(text="Статус: Остановлен", text_color="#ff5252")
            return

        # Автоперезапуск: пробуем поднять процесс снова раз в минуту.
        if self._restart_enabled and self._restart_attempts < AUTO_RESTART_MAX_ATTEMPTS:
            self._restart_attempts += 1
            self.lbl_status.configure(
                text=f"Статус: Перезапуск ({self._restart_attempts}/{AUTO_RESTART_MAX_ATTEMPTS})…",
                text_color="#ffb74d",
            )
            self.append_log(
                f"[СИСТЕМА] Автоперезапуск через {AUTO_RESTART_DELAY_MS // 1000} сек "
                f"(попытка {self._restart_attempts}/{AUTO_RESTART_MAX_ATTEMPTS})…\n"
            )
            self._restart_job = self.after(AUTO_RESTART_DELAY_MS, self._auto_restart)
        else:
            self.lbl_status.configure(text="Статус: Остановлен", text_color="#ff5252")
            if self._restart_enabled:
                self.append_log(
                    "[СИСТЕМА] Достигнут лимит автоперезапусков. "
                    "Проверьте логи и запустите бота вручную.\n"
                )

    def _auto_restart(self):
        """Вызывается таймером: поднимает упавший процесс бота заново."""
        self._restart_job = None
        # Если процесс уже поднят (например, вручную) — ничего не делаем.
        if self.process is not None or self._stopping:
            return
        self.append_log("[СИСТЕМА] Перезапуск бота…\n")
        self.start_bot(from_restart=True)

    @staticmethod
    def _bot_env() -> dict:
        """Окружение для процесса бота: принудительный UTF-8 для stdout/stderr."""
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def start_bot(self, from_restart: bool = False):
        if self.process is None:
            try:
                if getattr(sys, 'frozen', False):
                    # В скомпилированном .exe запускаем этот же .exe с флагом --bot
                    cmd = [sys.executable, "--bot"]
                else:
                    # В режиме разработка запускаем main.py через Python
                    cmd = [sys.executable, "-u", "main.py"]

                # Ручной запуск — включаем автоперезапуск и сбрасываем счётчик.
                if not from_restart:
                    self._restart_attempts = 0
                self._restart_enabled = True
                self._stopping = False

                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=self._bot_env(),
                )
                self.lbl_status.configure(text="Статус: Работает", text_color="#4caf50")
                self.append_log("[СИСТЕМА] Запуск бота...\n")

                self.log_thread = threading.Thread(target=self.read_output, daemon=True)
                self.log_thread.start()
            except Exception as e:
                self._restart_enabled = False
                self.append_log(f"[ОШИБКА] Не удалось запустить скрипт: {e}\n")

    def stop_bot(self):
        # Штатная остановка: гасим автоперезапуск и висящий таймер.
        self._stopping = True
        self._restart_enabled = False
        self._restart_attempts = 0
        if self._restart_job is not None:
            try:
                self.after_cancel(self._restart_job)
            except Exception:
                pass
            self._restart_job = None

        if self.process:
            self.process.terminate()
            self.process = None
            self.lbl_status.configure(text="Статус: Остановлен", text_color="#ff5252")
            self.append_log("[СИСТЕМА] Остановка бота...\n")

    def minimize_to_tray(self):
        self.withdraw()
        if self.tray_icon is None:
            self.create_tray_icon()

    def create_tray_icon(self):
        if os.path.exists(ICON_PATH):
            image = Image.open(ICON_PATH)
        else:
            image = Image.new('RGB', (64, 64), color=(46, 166, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Открыть", self.show_from_tray, default=True),
            pystray.MenuItem("Закрыть", self.exit_app)
        )

        self.tray_icon = pystray.Icon("bot_control", image, "Панель управления ботом", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def show_from_tray(self, icon=None, item=None):
        self.after(0, self._restore_window)

    def _restore_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def exit_app(self, icon=None, item=None):
        if self.tray_icon:
            self.tray_icon.stop()
        self.stop_bot()
        self.after(0, self.destroy)


if __name__ == "__main__":
    if "--bot" in sys.argv:
        # Если пришел флаг --bot, запускаем логику из main.py
        import asyncio
        import main
        if hasattr(main, 'main'):
            asyncio.run(main.main())
        sys.exit(0)
    else:
        # Иначе запускаем графическую панель управления
        app = BotControlApp()
        app.mainloop()