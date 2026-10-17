"""Билет в PDF — как «Скачать билет в PDF» на сайте (app/lib/ticketPdf.ts).

Тот же дизайн: A4, шапка TEPPA с лаймовой полосой, карточка полей, QR-код
при посадке, подвал. Шрифты NotoSans лежат в bot/assets/fonts (те же файлы,
что public/fonts на сайте).
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .utils import fmt_date, money

log = logging.getLogger('teppa.ticket_pdf')

# Палитра — из app/assets/css/main.css (там же --accent и т.д.).
_INK = (0.082, 0.125, 0.098)       # #152019
_MUTED = (0.4, 0.443, 0.408)       # #667168
_LINE = (0.89, 0.91, 0.89)         # #e3e8e3
_SOFT = (0.957, 0.969, 0.953)      # #f4f7f3
_ACCENT = (0.725, 0.937, 0.322)    # #b9ef52
_SUCCESS = (0.176, 0.502, 0.333)   # #2d8055
_WARNING = (0.851, 0.537, 0.141)   # #d98924
_DANGER = (0.749, 0.259, 0.235)    # #bf423c

_FONTS_DIR = Path(__file__).resolve().parent / 'assets' / 'fonts'
_REGULAR = 'NotoSans'
_BOLD = 'NotoSans-Bold'

# Глифы вне подмножества шрифта заменяем/выкидываем — как на сайте
# (UNSUPPORTED = /[^\u0020-\u00FF\u0100-\u017F\u0400-\u04FF\u2000-\u206F\u2116]/).
_SUBSTITUTE = {'→': '—', '←': '—', '↑': '—', '↓': '—', 'ʻ': '’', 'ʼ': '’'}
_ALLOWED_RANGES = ((0x20, 0x100), (0x100, 0x180), (0x400, 0x450), (0x2000, 0x2070))
_ALLOWED_EXTRA = {0x2116}  # №


def _clean(value: Any) -> str:
    text = str(value or '').replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    out = []
    for ch in text:
        ch = _SUBSTITUTE.get(ch, ch)
        code = ord(ch)
        if code in _ALLOWED_EXTRA or any(a <= code < b for a, b in _ALLOWED_RANGES):
            out.append(ch)
    return ''.join(out).strip()


@lru_cache(maxsize=1)
def _register_fonts() -> None:
    pdfmetrics.registerFont(TTFont(_REGULAR, str(_FONTS_DIR / 'NotoSans-Regular.ttf')))
    pdfmetrics.registerFont(TTFont(_BOLD, str(_FONTS_DIR / 'NotoSans-Bold.ttf')))


def _status_color(status: str) -> tuple[float, float, float]:
    if status in ('Оплачена', 'Подтверждена'):
        return _SUCCESS
    if status == 'Отменена':
        return _DANGER
    return _WARNING


def _qr_image(token: str) -> ImageReader | None:
    """QR токена — ровно тот же payload, что кодирует сайт (app/lib/ticket.ts)."""
    try:
        image = qrcode.make(token, box_size=6, border=1)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        buffer.seek(0)
        return ImageReader(buffer)
    except Exception:  # noqa: BLE001
        log.exception('QR для PDF не собрался')
        return None


def build_ticket_pdf(booking: dict[str, Any], trip: dict[str, Any] | None = None) -> bytes:
    """Собирает A4-билет. Падает, если нет шрифтов — вызывающий глушит исключение."""
    _register_fonts()
    regular, bold = _REGULAR, _BOLD
    width, height = A4
    margin = 48
    content_w = width - margin * 2
    inner_x = margin + 18
    inner_w = content_w - 36

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Билет {booking.get('id') or ''}".strip())

    def draw(value: Any, x: float, y: float, size: float, font: str = regular,
             color: tuple[float, float, float] = _INK) -> None:
        text = _clean(value)
        if not text:
            return
        c.setFont(font, size)
        c.setFillColorRGB(*color)
        c.drawString(x, y, text)

    def draw_right(value: Any, right: float, y: float, size: float, font: str = regular,
                   color: tuple[float, float, float] = _INK) -> None:
        text = _clean(value)
        draw(text, right - pdfmetrics.stringWidth(text, font, size), y, size, font, color)

    def fit(value: Any, max_w: float, size: float, font: str = regular) -> str:
        text = _clean(value)
        if pdfmetrics.stringWidth(text, font, size) <= max_w:
            return text
        while text and pdfmetrics.stringWidth(text + '…', font, size) > max_w:
            text = text[:-1]
        return text + '…'

    def wrap(value: Any, max_w: float, size: float, font: str = regular, limit: int = 3) -> list[str]:
        words = _clean(value).split()
        lines: list[str] = []
        current = ''
        for word in words:
            candidate = f'{current} {word}'.strip()
            if pdfmetrics.stringWidth(candidate, font, size) <= max_w:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
                if len(lines) >= limit:
                    break
        if current and len(lines) < limit:
            lines.append(current)
        return lines

    def caption(value: Any, x: float, y: float) -> None:
        draw(value, x, y, 8, regular, _MUTED)

    def round_rect(x: float, y: float, w: float, h: float, radius: float, *,
                   fill: tuple[float, float, float] | None = None,
                   stroke: tuple[float, float, float] | None = None) -> None:
        if fill:
            c.setFillColorRGB(*fill)
        if stroke:
            c.setStrokeColorRGB(*stroke)
            c.setLineWidth(1)
        c.roundRect(x, y, w, h, radius, stroke=1 if stroke else 0, fill=1 if fill else 0)

    # ---------- Шапка ----------
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, width, height, stroke=0, fill=1)
    c.setFillColorRGB(*_ACCENT)
    c.rect(0, height - 8, width, 8, stroke=0, fill=1)

    y = height - 66
    draw('TEPPA', margin, y, 26, bold)
    draw_right('ЭЛЕКТРОННЫЙ БИЛЕТ', width - margin, y + 7, 9, bold, _MUTED)

    y -= 18
    c.setStrokeColorRGB(*_LINE)
    c.setLineWidth(1)
    c.line(margin, y, width - margin, y)

    # ---------- Направление и рейс ----------
    booking_row = booking or {}
    direction = booking_row.get('direction') or booking_row.get('trip_title') or 'Поездка'
    y -= 50
    draw(fit(direction, content_w, 24, bold), margin, y, 24, bold)

    trip_row = trip or {}
    date_text = fmt_date(str(trip_row.get('date'))) if trip_row.get('date') else ''
    subtitle = ' · '.join(part for part in (
        trip_row.get('title') or booking_row.get('trip_title'), date_text, trip_row.get('time'),
    ) if part)
    y -= 26
    draw(fit(subtitle, content_w, 11), margin, y, 11, regular, _MUTED)

    # ---------- Карточка полей (3 колонки) ----------
    y -= 28
    cell_h = 54
    status = str(booking_row.get('status') or 'Новая')
    cells = [
        {'label': 'Дата', 'value': date_text or '—'},
        {'label': 'Время', 'value': trip_row.get('time') or '—'},
        {'label': 'Место', 'value': f"№ {booking_row.get('seat')}"},
        {'label': 'Категория', 'value': booking_row.get('passenger_type') or 'Взрослый'},
        {'label': 'Обед', 'value': 'Заказан' if booking_row.get('lunch') else 'Нет'},
        {'label': 'Цена', 'value': money(int(booking_row.get('price') or 0))},
        {'label': 'Статус', 'value': status, 'color': _status_color(status)},
        {'label': 'Заявка', 'value': booking_row.get('id')},
    ]
    rows = (len(cells) + 2) // 3
    card_h = 20 + cell_h * rows
    card_top = y
    card_y = card_top - card_h
    round_rect(margin, card_y, content_w, card_h, 12, fill=_SOFT)

    col_w = inner_w / 3
    for i, cell in enumerate(cells):
        x = inner_x + (i % 3) * col_w
        label_y = card_top - 20 - (i // 3) * cell_h
        caption(cell['label'], x, label_y)
        draw(fit(cell['value'], col_w - 12, 13, bold), x, label_y - 20, 13, bold,
             cell.get('color', _INK))

    # ---------- Пассажир ----------
    y = card_y - 36
    caption('Пассажир', margin, y)
    passenger = f"{booking_row.get('first_name') or ''} {booking_row.get('last_name') or ''}".strip()
    draw(fit(passenger, content_w, 15, bold), margin, y - 22, 15, bold)

    # ---------- О поездке ----------
    note = trip_row.get('comment') or trip_row.get('description') or ''
    y -= 60
    if note:
        note_lines = wrap(note, inner_w, 10, regular)
        box_h = 34 + len(note_lines) * 15
        round_rect(margin, y - box_h, content_w, box_h, 12, fill=_SOFT)
        caption('О поездке', inner_x, y - 18)
        for i, line in enumerate(note_lines):
            draw(line, inner_x, y - 38 - i * 15, 10, regular, _MUTED)
        y -= box_h

    # ---------- QR-код ----------
    y -= 40
    qr_size, box_size = 170, 194
    box_y = y - box_size
    round_rect(margin, box_y, box_size, box_size, 12, fill=(1, 1, 1), stroke=_LINE)

    token = str(booking_row.get('qr_token') or '')
    qr = _qr_image(token) if token else None
    if qr:
        c.drawImage(qr, margin + 12, box_y + 12, width=qr_size, height=qr_size)
    else:
        draw('QR-код не выпущен', margin + 16, box_y + qr_size / 2, 10, regular, _MUTED)

    tx = margin + box_size + 20
    tw = width - margin - tx
    box_top = box_y + box_size
    draw(fit('Отсканируйте код при посадке', tw, 13, bold), tx, box_top - 36, 13, bold)
    for i, line in enumerate(wrap(
            'При сканировании проверяется статус билета и ставится отметка о проходе.',
            tw, 10, regular)):
        draw(line, tx, box_top - 58 - i * 15, 10, regular, _MUTED)
    caption('Код билета', tx, box_y + 36)
    draw(fit(token or '—', tw, 9), tx, box_y + 18, 9, regular, _MUTED)

    # ---------- Подвал ----------
    y = box_y - 44
    c.setStrokeColorRGB(*_LINE)
    c.line(margin, y, width - margin, y)

    y -= 18
    draw('TEPPA · поездки из Ташкента в горы Узбекистана', margin, y, 8, regular, _MUTED)
    from datetime import date as _date  # noqa: PLC0415 — локально, чтобы не тянуть вверх
    draw_right(f'Выдан {fmt_date(_date.today().isoformat())}', width - margin, y,
               8, regular, _MUTED)

    y -= 15
    draw('Билет действителен только при предъявлении QR-кода.', margin, y, 8, regular, _MUTED)

    c.showPage()
    c.save()
    return buffer.getvalue()
