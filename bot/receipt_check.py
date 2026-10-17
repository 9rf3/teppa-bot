"""Машинная проверка чека по фото — «базовая» проверка перед авто-подтверждением.

Что проверяем (без человека):
  1. фото вообще похожо на скриншот: размер, яркость, не пустой экран;
  2. дубликат — тот же файл не приходил раньше (чтобы не прикладывали один чек дважды);
  3. если установлен tesseract (OCR): сумма из чека == сумма брони,
     дата чека — сегодня/вчера, в тексте есть слова об оплате.

Вердикты:
  ok      — пройдено, через 2 минуты чек принимается сам;
  suspect — что-то не сошлось: авто-принятие блокируется, админ проверяет вручную;
  unknown — фото не скачалось/не разобралось: старое поведение (авто-принятие).

Ничего не отклоняет автоматически — сомнительный чек просто ждёт админа.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageStat

log = logging.getLogger('teppa.receipt_check')

# Узбекистан — UTC+5: «сегодня» для чека считаем по ташкентскому времени.
UZ_TZ = timezone(timedelta(hours=5))

OK = 'ok'
SUSPECT = 'suspect'
UNKNOWN = 'unknown'

_MONTHS = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
    'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10,
    'ноября': 11, 'декабря': 12,
}

# Слова, которые есть в настоящем чеке перевода.
_PAYMENT_WORDS = (
    'перевод', 'переведён', 'переведен', 'оплат', 'успешн', 'транзакц', 'комисси',
    'получател', 'сумм', 'payme', 'paycom', 'click', 'uzcard', 'humo',
    'payment', 'paid', 'completed', 'transaction', 'transfer', 'uzs',
)

_AMOUNT_RE = re.compile(r'\d[\d\s\u00a0]{2,24}\d')
_DATE_NUMERIC_RE = re.compile(r'\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b')
_DATE_ISO_RE = re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b')
_DATE_WORDS_RE = re.compile(
    r'\b(\d{1,2})\s+(' + '|'.join(_MONTHS) + r')\s+(\d{4})\b'
)


@dataclass
class CheckResult:
    """Итог машинной проверки одного чека."""

    verdict: str = UNKNOWN              # ok | suspect | unknown
    reasons: list[str] = field(default_factory=list)   # почему suspect
    checked: list[str] = field(default_factory=list)   # что успели проверить
    sha256: str = ''
    amount_found: int | None = None
    date_found: str = ''                # ISO-дата из чека, если нашлась

    @property
    def auto_accept(self) -> bool:
        """Можно ли принимать оплату автоматически через 2 минуты."""
        return self.verdict != SUSPECT


def analyze(photo: bytes | None, *, expected_amount: int | None = None,
            seen: set[str] | None = None) -> CheckResult:
    """Разбирает скриншот чека. Тяжёлая работа (PIL/OCR) — вызывать в to_thread."""
    result = CheckResult()
    if not photo:
        result.verdict = UNKNOWN
        result.reasons.append('фото не скачалось')
        return result

    result.sha256 = hashlib.sha256(photo).hexdigest()
    if seen is not None:
        if result.sha256 in seen:
            result.verdict = SUSPECT
            result.reasons.append('это фото уже присылали ранее')
            return result
        seen.add(result.sha256)

    try:
        image = Image.open(io.BytesIO(photo))
        image.load()
    except Exception:  # noqa: BLE001
        result.verdict = SUSPECT
        result.reasons.append('файл не открылся как изображение')
        return result
    image = image.convert('RGB')

    if not _image_sane(image, result):
        result.verdict = SUSPECT
        return result
    result.checked.append('фото в порядке (размер, яркость, не пустой экран)')

    text = _ocr_text(image)
    if text is None:
        result.checked.append('OCR недоступен — текст чека не проверялся')
    else:
        _check_text(text, expected_amount, result)

    result.verdict = SUSPECT if result.reasons else OK
    return result


def describe(result: CheckResult) -> str:
    """Строка для поста в группе админов."""
    if result.verdict == OK:
        detail = '; '.join(result.checked) or 'похоже на настоящий чек'
        return f'🤖 Автопроверка: ок — {detail}'
    if result.verdict == SUSPECT:
        return '⚠️ Автопроверка: ' + '; '.join(result.reasons) + ' — подтвердите вручную'
    return '🤖 Автопроверка: не выполнена (' + '; '.join(result.reasons) + ')'


# ---------- Картинка ----------


def _image_sane(image: Image.Image, result: CheckResult) -> bool:
    width, height = image.size
    if min(width, height) < 300:
        result.reasons.append(f'мелкое фото {width}×{height}')
        return False

    gray = image.convert('L')
    stat = ImageStat.Stat(gray)
    mean = stat.mean[0]
    deviation = stat.stddev[0]
    if mean < 15:
        result.reasons.append('фото почти чёрное')
        return False
    if mean > 252:
        result.reasons.append('фото почти белое — похоже на пустой экран')
        return False
    if deviation < 6:
        result.reasons.append('на фото нет содержимого (однотонный экран)')
        return False
    return True


def _ocr_text(image: Image.Image) -> str | None:
    """tesseract установлен? — возвращаем текст чека. Нет — None (проверка молча пропускается)."""
    try:
        import pytesseract  # noqa: PLC0415 — лениво: библиотеки может не быть в образе
    except Exception:  # noqa: BLE001
        return None
    for lang in ('rus+eng', 'rus', 'eng'):
        try:
            return pytesseract.image_to_string(image, lang=lang)
        except Exception:  # noqa: BLE001 — нет языкового пакета / нет самого tesseract
            continue
    return None


# ---------- Текст чека ----------


def _digits_number(raw: str) -> int | None:
    """'350 990' / '350\u00a0990' -> 350990; слишком длинное (телефон) — None."""
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if not 3 <= len(digits) <= 12:
        return None
    return int(digits)


def _find_date(text: str) -> datetime | None:
    for match in _DATE_NUMERIC_RE.finditer(text):
        try:
            parsed = datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            continue
        if 2020 <= parsed.year <= 2100:
            return parsed
    for match in _DATE_ISO_RE.finditer(text):
        try:
            parsed = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if 2020 <= parsed.year <= 2100:
            return parsed
    for match in _DATE_WORDS_RE.finditer(text):
        month = _MONTHS.get(match.group(2))
        if not month:
            continue
        try:
            parsed = datetime(int(match.group(3)), month, int(match.group(1)))
        except ValueError:
            continue
        return parsed
    return None


def _check_text(text: str, expected_amount: int | None, result: CheckResult) -> None:
    lowered = text.lower()

    # Сумма: ищем число, похожее на сумму брони.
    candidates = [n for n in (_digits_number(m.group(0)) for m in _AMOUNT_RE.finditer(text))
                  if n is not None]
    if expected_amount:
        exact = [c for c in candidates if abs(c - expected_amount) <= max(1000, expected_amount // 100)]
        if exact:
            result.amount_found = exact[0]
            result.checked.append(f'сумма {exact[0]:,} совпала'.replace(',', ' '))
        else:
            near = [c for c in candidates if expected_amount * 0.5 <= c <= expected_amount * 1.5]
            if near:
                result.amount_found = near[0]
                result.reasons.append(
                    f'сумма в чеке {near[0]:,} не совпадает с {expected_amount:,}'.replace(',', ' ')
                )

    # Дата: чек должен быть сегодня или вчера (по Ташкенту).
    found = _find_date(text)
    if found:
        today = datetime.now(UZ_TZ).date()
        if found.date() not in (today, today - timedelta(days=1)):
            result.reasons.append(
                f'дата чека {found.date().strftime("%d.%m.%Y")} — не из последних двух дней'
            )
        else:
            result.date_found = found.date().isoformat()
            result.checked.append('дата чека — сегодня/вчера')

    # Текст вообще похож на чек перевода?
    if any(word in lowered for word in _PAYMENT_WORDS):
        result.checked.append('в чеке найдены слова о переводе')
    elif lowered.strip():
        result.reasons.append('в чеке не нашлось ни слов об оплате, ни суммы')
