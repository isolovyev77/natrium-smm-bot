"""Детерминированный разбор ручного ввода еды.

Поддерживаются только форматы, которые бот показывает пользователю: строки с
точкой с запятой, скопированные строки черновика и последовательность из
названия, порции и четырёх значений КБЖУ. Модуль не определяет источник данных
и не дополняет отсутствующие значения.
"""

from __future__ import annotations

import html
import math
import re
from typing import Any


_NUMBER = r"[-+]?(?:\d+(?:[.,]\d+)?|nan|inf(?:inity)?)"
_HTML_TAG = re.compile(r"<[^>]*>")
_CONTEXT_LINE = re.compile(
    r"^[^,]{1,60},\s*\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2}$"
)
_DATE_OR_TIME = re.compile(
    r"(?:\b\d{1,2}\.\d{1,2}\.\d{2,4}\b|\b\d{1,2}:\d{2}\b)"
)
_ID_VALUE = re.compile(r"#\s*\d+")
_WEIGHT = re.compile(
    rf"^\s*({_NUMBER})\s*(?:г|гр|грамм|грамма|граммов)?\s*$",
    re.IGNORECASE,
)
_CALORIES = re.compile(rf"({_NUMBER})\s*ккал\b", re.IGNORECASE)
_PROTEIN = re.compile(rf"(?:^|[\s,])Б\s*({_NUMBER})(?:\s*г)?(?=\s*[,;.]|\s*$)", re.IGNORECASE)
_FAT = re.compile(rf"(?:^|[\s,])Ж\s*({_NUMBER})(?:\s*г)?(?=\s*[,;.]|\s*$)", re.IGNORECASE)
_CARBS = re.compile(rf"(?:^|[\s,])У\s*({_NUMBER})(?:\s*г)?(?=\s*[,;.]|\s*$)", re.IGNORECASE)

_IGNORED_PREFIXES = (
    "⚠️ оценка приблизительная",
    "оценка приблизительная",
    "запись попадет в статистику",
    "запись попадёт в статистику",
    "📚 по справочнику:",
)

_FIELD_LABELS = (
    ("название блюда", "Гречневые хлопья"),
    ("масса или порция", "294 г"),
    ("калорийность", "400 ккал"),
    ("белки", "Б 12"),
    ("жиры", "Ж 18"),
    ("углеводы", "У 50"),
)


def parse_manual_items(text: str) -> list[dict[str, Any]]:
    """Разобрать ручной ввод в прежний список словарей meal item.

    Неоднозначный текст отклоняется. Для нескольких блюд нужна явная граница:
    новая bullet-строка, пустая строка, новая semicolon-строка или полный блок
    из шести строк.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Нужно указать хотя бы одно блюдо")

    approximate_hint = "оценка приблизительная" in text.casefold()
    lines = _clean_lines(text)
    nonblank = [line for line in lines if line]
    if not nonblank:
        raise ValueError("Нужно указать хотя бы одно блюдо")

    if any(";" in line for line in nonblank):
        if any(";" not in line for line in nonblank):
            raise ValueError(
                "Неясно, где заканчивается блюдо. Каждое блюдо укажите отдельной строкой"
            )
        return [
            _parse_semicolon_line(line, approximate_hint=approximate_hint)
            for line in nonblank
        ]

    if any(line.startswith("•") for line in nonblank):
        blocks = _bullet_blocks(lines)
    else:
        blocks = _blank_line_blocks(lines)

    items: list[dict[str, Any]] = []
    for block in blocks:
        items.extend(_parse_block(block, approximate_hint=approximate_hint))
    if not items:
        raise ValueError("Нужно указать хотя бы одно блюдо")
    return items


def _clean_lines(text: str) -> list[str]:
    result: list[str] = []
    for raw in text.splitlines():
        line = html.unescape(_HTML_TAG.sub("", raw)).strip()
        folded = line.casefold()
        if "черновик #" in folded and folded.lstrip("🍽️ ").startswith("черновик #"):
            continue
        if _CONTEXT_LINE.fullmatch(line):
            continue
        if any(folded.startswith(prefix) for prefix in _IGNORED_PREFIXES):
            continue
        result.append(line)
    return result


def _bullet_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if not line:
            continue
        if line.startswith("•"):
            if current:
                blocks.append(current)
            first = line.removeprefix("•").strip()
            current = [first] if first else []
            continue
        if current is None:
            raise ValueError(
                "Неясно, где начинается блюдо. Начните каждое блюдо с символа •"
            )
        current.append(line)
    if current:
        blocks.append(current)
    if not blocks:
        raise ValueError("После символа • нужно указать блюдо")
    return blocks


def _blank_line_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _parse_block(
    lines: list[str],
    *,
    approximate_hint: bool,
) -> list[dict[str, Any]]:
    joined = " ".join(lines)
    if len(lines) >= 6 and len(lines) % 6 == 0:
        return [
            _parse_positional(lines[index:index + 6], approximate_hint=approximate_hint)
            for index in range(0, len(lines), 6)
        ]
    labeled = any(marker in joined.casefold() for marker in ("ккал", "б ", "ж ", "у "))
    if labeled:
        if len(_CALORIES.findall(joined)) > 1:
            raise ValueError(
                "Неясно, где заканчивается блюдо. Разделите блюда пустой строкой или символом •"
            )
        return [_parse_labeled(joined, approximate_hint=approximate_hint)]

    return [_parse_positional(lines, approximate_hint=approximate_hint)]


def _parse_semicolon_line(
    line: str,
    *,
    approximate_hint: bool,
) -> dict[str, Any]:
    parts = [part.strip() for part in line.split(";")]
    if len(parts) < 6:
        _raise_missing_field(len(parts))
    if len(parts) > 6:
        raise ValueError(
            "В строке слишком много разделителей. Пример: Название;294;400;12;18;50"
        )
    return _build_item(*parts, approximate_hint=approximate_hint)


def _parse_positional(
    lines: list[str],
    *,
    approximate_hint: bool,
) -> dict[str, Any]:
    if len(lines) < 6:
        _raise_missing_field(len(lines))
    if len(lines) > 6:
        raise ValueError(
            "Неясно, где заканчивается блюдо. Используйте 6 строк на блюдо или разделите блюда пустой строкой"
        )
    return _build_item(*lines, approximate_hint=approximate_hint)


def _parse_labeled(
    value: str,
    *,
    approximate_hint: bool,
) -> dict[str, Any]:
    if ":" not in value:
        raise ValueError("Не указана масса или порция. Пример: Название, 294 г: 400 ккал")
    head, nutrition = value.split(":", 1)
    name, portion = _split_name_portion(head)

    calories = _extract_labeled(_CALORIES, nutrition, "калорийность", "400 ккал")
    protein = _extract_labeled(_PROTEIN, nutrition, "белки", "Б 12")
    fat = _extract_labeled(_FAT, nutrition, "жиры", "Ж 18")
    carbs = _extract_labeled(_CARBS, nutrition, "углеводы", "У 50")
    residue = nutrition
    for pattern in (_CALORIES, _PROTEIN, _FAT, _CARBS):
        residue = pattern.sub("", residue)
    residue = residue.strip(" \t,;.")
    if residue:
        raise ValueError(
            f"Не удалось однозначно разобрать текст после КБЖУ: {residue[:80]}. "
            "Укажите дополнительное блюдо отдельной строкой"
        )
    return _build_item(
        name,
        portion,
        calories,
        protein,
        fat,
        carbs,
        approximate_hint=approximate_hint,
    )


def _split_name_portion(head: str) -> tuple[str, str]:
    numeric = re.fullmatch(
        rf"(?P<name>.+?),\s*(?P<portion>{_NUMBER}\s*"
        r"(?:г|гр|грамм|грамма|граммов)?)\s*",
        head,
        re.IGNORECASE,
    )
    if numeric:
        return numeric.group("name").strip(" ,"), numeric.group("portion").strip()
    textual = re.fullmatch(r"(?P<name>.+),\s+(?P<portion>.+)", head)
    if textual:
        return textual.group("name").strip(" ,"), textual.group("portion").strip(" ,")
    raise ValueError("Не указана масса или порция. Пример: Название, 294 г")


def _extract_labeled(
    pattern: re.Pattern[str],
    text: str,
    label: str,
    example: str,
) -> str:
    matches = pattern.findall(text)
    if not matches:
        raise ValueError(f"Не указаны {label}. Пример: {example}")
    if len(matches) > 1:
        raise ValueError(f"Поле {label} указано несколько раз")
    return matches[0]


def _build_item(
    name: str,
    portion_raw: str,
    calories_raw: str,
    protein_raw: str,
    fat_raw: str,
    carbs_raw: str,
    *,
    approximate_hint: bool,
) -> dict[str, Any]:
    name = name.strip(" ,")
    if not name:
        raise ValueError("Не указано название блюда. Пример: Гречневые хлопья")
    if _DATE_OR_TIME.fullmatch(name) or _ID_VALUE.fullmatch(name):
        raise ValueError("Не указано название блюда. Пример: Гречневые хлопья")

    weight, portion_text = _parse_portion(portion_raw)
    approximate = approximate_hint or weight is None
    return {
        "name": name[:200],
        "weight_g": weight,
        "portion_text": portion_text[:200],
        "calories": _parse_number(calories_raw, "калорийность", "400 ккал"),
        "protein_g": _parse_number(protein_raw, "белки", "Б 12"),
        "fat_g": _parse_number(fat_raw, "жиры", "Ж 18"),
        "carbs_g": _parse_number(carbs_raw, "углеводы", "У 50"),
        "approximate": approximate,
    }


def _parse_portion(value: str) -> tuple[float | None, str]:
    cleaned = value.strip(" ,:")
    if not cleaned:
        raise ValueError("Не указана масса или порция. Пример: 294 г")
    if _DATE_OR_TIME.search(cleaned) or _ID_VALUE.search(cleaned):
        raise ValueError("Вместо даты или ID укажите массу либо порцию. Пример: 294 г")
    match = _WEIGHT.fullmatch(cleaned)
    if match:
        weight = _parse_number(match.group(1), "масса", "294 г")
        if weight <= 0:
            raise ValueError("Масса должна быть больше нуля. Пример: 294 г")
        return weight, ""
    if any(character.isalpha() for character in cleaned):
        return None, cleaned
    raise ValueError("Массу укажите числом в граммах или опишите порцию. Пример: 294 г")


def _parse_number(value: str, label: str, example: str) -> float:
    cleaned = str(value).strip(" \t,;:").casefold()
    if label == "калорийность" and cleaned.endswith("ккал"):
        cleaned = cleaned[:-4].strip()
    elif label in {"белки", "жиры", "углеводы"}:
        prefixes = {"белки": "б", "жиры": "ж", "углеводы": "у"}
        if cleaned.startswith(prefixes[label]):
            cleaned = cleaned[1:].strip()
        if cleaned.endswith("г"):
            cleaned = cleaned[:-1].strip()
    cleaned = cleaned.strip(" \t,;:").replace(",", ".")
    if not re.fullmatch(_NUMBER, cleaned, re.IGNORECASE):
        raise ValueError(f"Поле {label} укажите числом. Пример: {example}")
    number = float(cleaned)
    if not math.isfinite(number):
        raise ValueError(f"Поле {label} должно быть конечным числом. Пример: {example}")
    if number < 0:
        raise ValueError(f"Поле {label} не может быть отрицательным. Пример: {example}")
    return number


def _raise_missing_field(part_count: int) -> None:
    index = max(0, min(part_count, len(_FIELD_LABELS) - 1))
    label, example = _FIELD_LABELS[index]
    if label == "масса или порция":
        raise ValueError(f"Не указана {label}. Пример: {example}")
    if label == "калорийность":
        raise ValueError(f"Не указана {label}. Пример: {example}")
    raise ValueError(f"Не указаны {label}. Пример: {example}")
