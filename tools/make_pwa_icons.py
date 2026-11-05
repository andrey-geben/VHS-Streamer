"""
Конвертирует static/favicon.ico (126x128) в PNG для PWA.
Растягивает до 192/512 с LANCZOS — на иконке рабочего стола
будет чётко, на splash-экране — мягко.
"""
from pathlib import Path
from PIL import Image

SRC = Path("static/favicon.ico")
OUT = Path("static/icons")
OUT.mkdir(parents=True, exist_ok=True)

# Открываем ICO, выбираем самый большой размер
ico = Image.open(SRC)
sizes = ico.info.get("sizes", {ico.size})
largest = max(sizes, key=lambda s: s[0] * s[1])
ico.size = largest
img = ico.convert("RGBA")
print(f"favicon.ico: доступные размеры {sorted(sizes)}, берём {largest}")
print(f"исходная иконка: {img.size[0]}×{img.size[1]}")

if img.size[0] < 512:
    print(f"⚠ исходник {img.size[0]}px — растягиваем до 512 (LANCZOS)")


def save(size: int, name: str, maskable: bool = False):
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    if maskable:
        # Фон theme_color, иконка в safe-zone 80% (для Android adaptive)
        canvas.paste((30, 33, 38, 255), (0, 0, size, size))
        inner = int(size * 0.8)
        # Сохраняем пропорции: берём квадратный кроп 126x126 из центра
        side = min(img.size)
        left = (img.size[0] - side) // 2
        top = (img.size[1] - side) // 2
        sq = img.crop((left, top, left + side, top + side))
        resized = sq.resize((inner, inner), Image.LANCZOS)
        offset = (size - inner) // 2
        canvas.paste(resized, (offset, offset), resized)
    else:
        # Центральный квадратный кроп + resize
        side = min(img.size)
        left = (img.size[0] - side) // 2
        top = (img.size[1] - side) // 2
        sq = img.crop((left, top, left + side, top + side))
        resized = sq.resize((size, size), Image.LANCZOS)
        canvas.paste(resized, (0, 0), resized)

    canvas.save(OUT / name, "PNG", optimize=True)
    print(f"  → {OUT / name}  ({size}×{size})")


save(192, "icon-192.png")
save(512, "icon-512.png")
save(192, "icon-192-maskable.png", maskable=True)
save(512, "icon-512-maskable.png", maskable=True)
save(180, "apple-touch-icon.png")

print("\nГотово. 5 PNG в static/icons/")