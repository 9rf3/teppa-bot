"""Машинная проверка чека (bot/receipt_check.py) и билет в PDF (bot/ticket_pdf.py).

Проверяем:
  1. анализ картинки: белый/чёрный/однотонный экран, мелкое фото — suspect;
  2. нормальный скриншот — ok; повтор того же файла — дубликат (suspect);
  3. пустые байты — unknown (авто-принятие не блокируется);
  4. describe() — три формы строки для поста в группе;
  5. PDF-билет собирается и начинается с %PDF (дизайн — как на сайте).

Запуск:  python tests/test_receipt_check.py
"""

from __future__ import annotations

import io
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from bot import receipt_check as rc  # noqa: E402
from bot.ticket_pdf import build_ticket_pdf  # noqa: E402

# Шрифт, который tesseract точно разбирает (в т.ч. кириллицу).
_FONT_CANDIDATES = (
    '/System/Library/Fonts/Supplemental/Arial.ttf',           # macOS
    '/Library/Fonts/Arial.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',        # Linux
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
)


def _font(size: int = 46) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)  # Pillow ≥ 10.1 — векторный
    except TypeError:  # pragma: no cover
        return ImageFont.load_default()


def jpeg(image: Image.Image, quality: int = 92) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG', quality=quality)
    return buffer.getvalue()


def render_receipt(amount: str, when: str) -> bytes:
    """Скриншот чека: читается и человеком, и OCR (сумма, дата, слова о переводе)."""
    image = Image.new('RGB', (900, 1400), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = _font()
    draw.rectangle([0, 0, 900, 170], fill=(21, 32, 25))
    draw.text((40, 55), 'Payme', font=font, fill=(185, 239, 82))
    draw.text((60, 320), 'Перевод получен', font=font, fill=(21, 32, 25))
    draw.text((60, 420), amount, font=font, fill=(21, 32, 25))
    draw.text((60, 520), f'{when} 13:10', font=font, fill=(21, 32, 25))
    draw.text((60, 620), 'Получатель: TEPPA', font=font, fill=(21, 32, 25))
    for row in range(720, 1350, 90):
        draw.rectangle([40, row, 860, row + 60], fill=(236, 238, 234), outline=(170, 176, 168))
    return jpeg(image, quality=95)


def screenshot() -> bytes:
    """Обычный скриншот приложения — тёмная шапка, строки (для проверки картинки)."""
    return render_receipt('350 990 сум', date.today().strftime('%d.%m.%Y'))


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: PLC0415
        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001
        return False


def test_image_checks() -> None:
    # почти белый экран — пусто
    r = rc.analyze(jpeg(Image.new('RGB', (800, 600), (255, 255, 255))), expected_amount=350990)
    assert r.verdict == rc.SUSPECT and r.reasons, r
    # почти чёрный
    r = rc.analyze(jpeg(Image.new('RGB', (800, 600), (5, 5, 5))))
    assert r.verdict == rc.SUSPECT, r
    # мелкое фото
    r = rc.analyze(jpeg(Image.new('RGB', (120, 120), (120, 130, 120))))
    assert r.verdict == rc.SUSPECT and 'мелкое' in r.reasons[0], r
    # нормальный скриншот
    r = rc.analyze(screenshot(), expected_amount=350990)
    assert r.verdict == rc.OK and not r.reasons, (r.reasons, r.checked)
    assert any('фото в порядке' in line for line in r.checked), r.checked
    print('ok: белый/чёрный/мелкое — suspect, нормальный скриншот — ok')


def test_duplicate_and_unknown() -> None:
    seen: set[str] = set()
    first = rc.analyze(screenshot(), seen=seen)
    second = rc.analyze(screenshot(), seen=seen)  # тот же файл
    assert first.verdict == rc.OK, first
    assert second.verdict == rc.SUSPECT and 'уже присылали' in second.reasons[0], second
    # фото не скачалось — unknown, но авто-принятие не блокируется
    empty = rc.analyze(b'')
    assert empty.verdict == rc.UNKNOWN and empty.auto_accept is True, empty
    assert 'не скачалось' in empty.reasons[0], empty
    print('ok: дубликат чека — suspect; нет фото — unknown, авто-принятие возможно')


def test_describe() -> None:
    ok = rc.analyze(screenshot(), expected_amount=350990)
    line = rc.describe(ok)
    assert line.startswith('🤖 Автопроверка: ок'), line

    bad = rc.CheckResult(verdict=rc.SUSPECT, reasons=['сумма не совпала'])
    line = rc.describe(bad)
    assert line.startswith('⚠️ Автопроверка:') and 'вручную' in line, line

    unknown = rc.describe(rc.analyze(b''))
    assert 'не выполнена' in unknown, unknown
    print('ok: describe() — три формы строки для группы админов')


def test_ocr_sum_and_date() -> None:
    """Если tesseract установлен: чужая сумма и старая дата ловятся, своя — ок."""
    if not ocr_available():
        print('skip: tesseract не установлен — OCR-часть пропущена (поставьте: brew install tesseract tesseract-lang)')
        return
    today = date.today().strftime('%d.%m.%Y')

    ok = rc.analyze(render_receipt('350 990 сум', today), expected_amount=350990, seen=set())
    assert ok.verdict == rc.OK, (ok.reasons, ok.checked)
    assert ok.amount_found == 350990, ok.amount_found
    assert ok.date_found, ok.date_found
    assert any('сумма' in line for line in ok.checked), ok.checked

    wrong = rc.analyze(render_receipt('100 000 сум', today), expected_amount=350990, seen=set())
    assert wrong.verdict == rc.SUSPECT and 'не совпадает' in wrong.reasons[0], wrong

    old = (date.today() - timedelta(days=7)).strftime('%d.%m.%Y')
    stale = rc.analyze(render_receipt('350 990 сум', old), expected_amount=350990, seen=set())
    assert stale.verdict == rc.SUSPECT and 'не из последних двух дней' in stale.reasons[0], stale
    print('ok: OCR сверяет сумму и дату — чужая сумма и старая дата дают suspect')


def test_ticket_pdf() -> None:
    booking = {
        'id': 'BLD-220800140', 'trip_id': 't1', 'trip_title': 'Amirsoy Day',
        'direction': 'Ташкент → Амирsoy', 'seat': 12, 'price': 350990,
        'first_name': 'Азиз', 'last_name': 'Каримов', 'status': 'Оплачена',
        'lunch': True, 'qr_token': 'BLD-220800140.' + 'a' * 32,
    }
    trip = {
        'id': 't1', 'title': 'Amirsoy Day',
        'date': (date.today() + timedelta(days=10)).isoformat(), 'time': '06:30',
        'comment': 'Выезд от метро Чиланзар.',
    }
    data = build_ticket_pdf(booking, trip)
    assert data.startswith(b'%PDF') and len(data) > 10_000, (len(data), data[:20])

    # без рейса и с пустыми полями — тоже собирается
    minimal = build_ticket_pdf({'id': 'BLD-0', 'seat': 3, 'price': 0, 'status': 'Новая'}, None)
    assert minimal.startswith(b'%PDF'), minimal[:20]
    print('ok: PDF-билет собирается (A4, поля, QR, подвал)')


def main() -> None:
    test_image_checks()
    test_duplicate_and_unknown()
    test_describe()
    test_ocr_sum_and_date()
    test_ticket_pdf()
    print('\n5 проверок чека и PDF — прошли')


if __name__ == '__main__':
    main()
