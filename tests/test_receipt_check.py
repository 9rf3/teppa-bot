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

from PIL import Image, ImageDraw  # noqa: E402

from bot import receipt_check as rc  # noqa: E402
from bot.ticket_pdf import build_ticket_pdf  # noqa: E402


def jpeg(image: Image.Image, quality: int = 92) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG', quality=quality)
    return buffer.getvalue()


def screenshot() -> bytes:
    """Скриншот приложения: тёмная шапка, строки — как настоящий чек в телефоне."""
    image = Image.new('RGB', (720, 1280), (245, 247, 243))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 720, 170], fill=(21, 32, 25))
    draw.text((40, 60), 'Payme', fill=(185, 239, 82))
    draw.text((40, 230), 'Перевод', fill=(21, 32, 25))
    draw.text((40, 270), '350 990 сум', fill=(21, 32, 25))
    draw.text((40, 310), '17.10.2026', fill=(21, 32, 25))
    for row in range(400, 1150, 70):
        draw.rectangle([30, row, 690, row + 46], fill=(230, 233, 228), outline=(170, 176, 168))
    return jpeg(image)


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
    test_ticket_pdf()
    print('\n4 проверки чека и PDF — прошли')


if __name__ == '__main__':
    main()
