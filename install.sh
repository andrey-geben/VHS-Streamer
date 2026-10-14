#!/usr/bin/env bash
# =====================================================================
#  VHS-Streamer — установка на новую машину (Linux)
# =====================================================================
set -e
cd "$(dirname "$0")"

echo
echo "============================================"
echo "  VHS-Streamer — установка"
echo "============================================"
echo

# ---------------------------------------------------------------
# 1. Проверка Python
# ---------------------------------------------------------------
echo "[1/5] Проверяю Python..."

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "[!] Python3 не найден в PATH."
    echo
    echo "    Установите:  sudo apt install python3 python3-venv python3-pip"
    echo
    exit 1
fi

PYVER=$(python3 --version | awk '{print $2}')
echo "    Найден Python $PYVER"

# Проверка 3.12+
PYMAJ=$(echo "$PYVER" | cut -d. -f1)
PYMIN=$(echo "$PYVER" | cut -d. -f2)
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 12 ]; }; then
    echo "[!] Требуется Python 3.12 или новее. Обнаружено: $PYVER"
    exit 1
fi
echo "    Версия подходит."

# ---------------------------------------------------------------
# 2. Виртуальное окружение
# ---------------------------------------------------------------
echo
echo "[2/5] Виртуальное окружение .venv..."

if [ -x ".venv/bin/python" ]; then
    echo "    Уже существует — пропускаю создание."
else
    echo "    Создаю .venv..."
    python3 -m venv .venv
    echo "    Готово."
fi

VENV_PY=".venv/bin/python"
VENV_PIP=".venv/bin/pip"

# ---------------------------------------------------------------
# 3. Зависимости
# ---------------------------------------------------------------
echo
echo "[3/5] Устанавливаю зависимости..."

if [ ! -f "requirements.txt" ]; then
    echo "[!] Файл requirements.txt не найден."
    exit 1
fi

"$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1
"$VENV_PY" -m pip install -r requirements.txt
echo "    Зависимости установлены."

# ---------------------------------------------------------------
# 4. Конфиг
# ---------------------------------------------------------------
echo
echo "[4/5] Конфигурация config.yaml..."

if [ -f "config.yaml" ]; then
    echo "    config.yaml уже существует — не трогаю."
else
    if [ -f "config.example.yaml" ]; then
        cp config.example.yaml config.yaml
        echo "    Создан config.yaml из config.example.yaml."
        echo
        echo "    ВАЖНО: откройте config.yaml и задайте:"
        echo "       - server.secret_key"
        echo "       - security.users"
        echo "       - security.api_token"
    else
        echo "[!] Не найден config.example.yaml."
        exit 1
    fi
fi

# ---------------------------------------------------------------
# 5. Структура
# ---------------------------------------------------------------
echo
echo "[5/5] Проверяю структуру проекта..."

MISSING=0
[ ! -d "templates" ]    && echo "    [!] нет папки templates/"    && MISSING=1
[ ! -d "static" ]       && echo "    [!] нет папки static/"       && MISSING=1
[ ! -f "mediamtx.yml" ] && echo "    [!] нет mediamtx.yml"        && MISSING=1
[ ! -f "icon.ico" ]     && echo "    [!] нет icon.ico"            && MISSING=1
[ ! -f "app.py" ]       && echo "    [!] нет app.py"              && MISSING=1

if [ "$MISSING" -eq 1 ]; then
    echo
    echo "[!] Некоторые файлы отсутствуют. Возможно, репозиторий склонирован не полностью."
    exit 1
fi
echo "    Структура в порядке."

# ---------------------------------------------------------------
# Права на скрипты
# ---------------------------------------------------------------
chmod +x start.sh stop.sh 2>/dev/null || true

# ---------------------------------------------------------------
# Готово
# ---------------------------------------------------------------
echo
echo "============================================"
echo "  Установка завершена"
echo "============================================"
echo
echo "  Виртуальное окружение:  .venv/"
echo "  Зависимости:            установлены"
echo "  Конфиг:                 config.yaml"
echo "  MediaMTX и FFmpeg:      скачаются автоматически при первом запуске"
echo
echo "  Запуск:  ./start.sh"
echo "  Стоп:    ./stop.sh"
echo

read -p "Запустить сервер сейчас? (y/n): " ANSWER
if [ "$ANSWER" = "y" ] || [ "$ANSWER" = "Y" ]; then
    echo
    echo "[*] Запускаю start.sh..."
    sleep 1
    ./start.sh
else
    echo
    echo "Хорошо. Когда будете готовы — запустите ./start.sh"
fi