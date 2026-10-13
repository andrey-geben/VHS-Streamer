"""
Хелперы для работы в режиме разработки и в собранном PyInstaller .exe.

Ключевое различие:
    * app_dir()       — куда писать (config.yaml, bin/, logs/, archive/).
                        В разработке — корень проекта.
                        В собранном .exe — папка рядом с .exe.
    * resource_dir()  — откуда читать встроенные ресурсы (templates/,
                        статические файлы). В собранном .exe — _MEIPASS.
"""
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True, если мы внутри собранного PyInstaller приложения."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def app_dir() -> Path:
    """
    Папка приложения (куда писать пользовательские данные).

    - В разработке: корень проекта (там, где лежит этот файл).
    - В собранном .exe: папка, в которой лежит .exe.
    """
    if is_frozen():
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


def resource_dir() -> Path:
    """
    Папка встроенных ресурсов (только для чтения).

    - В разработке: корень проекта.
    - В собранном .exe: временная папка _MEIPASS, куда PyInstaller
      распаковывает содержимое из .exe.
    """
    if is_frozen():
        return Path(sys._MEIPASS).resolve()
    return Path(__file__).parent.resolve()


def resource_path(relative: str) -> Path:
    """Путь к встроенному ресурсу."""
    return resource_dir() / relative


def app_path(relative: str) -> Path:
    """Путь к пользовательскому файлу рядом с .exe."""
    return app_dir() / relative