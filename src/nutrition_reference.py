"""Local USDA FoodData Central reference and deterministic per-weight calculation."""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Union


NumberInput = Union[Decimal, str, int, float]
MAX_GRAMS = Decimal("100000")
QUALIFIER_TOKENS = {
    "raw",
    "cooked",
    "boiled",
    "roasted",
    "fried",
    "baked",
    "steamed",
    "stewed",
    "grilled",
    "dried",
    "frozen",
    "canned",
    "сырой",
    "сырая",
    "сырое",
    "вареный",
    "вареная",
    "вареное",
    "готовый",
    "готовая",
    "жареный",
    "жареная",
    "запеченный",
    "запеченная",
    "сухой",
    "сухая",
}


class FoodReferenceError(Exception):
    """Base error for the local food reference."""


class ReferenceDataNotFoundError(FoodReferenceError):
    """Required local reference files are missing."""


class FoodNotFoundError(FoodReferenceError):
    """The requested FDC identifier is unknown."""


class InvalidAmountError(FoodReferenceError):
    """Food weight is not a finite positive number."""


class IncompleteNutrientsError(FoodReferenceError):
    """A record cannot be calculated because one or more nutrients are missing."""


@dataclass(frozen=True)
class Nutrients:
    kcal: Decimal
    protein_g: Decimal
    fat_g: Decimal
    carbs_g: Decimal


@dataclass(frozen=True)
class FoodCandidate:
    fdc_id: str
    display_name: str
    description: str
    preparation: str | None
    aliases: tuple[str, ...]
    source: str
    version: str
    url: str


@dataclass(frozen=True)
class FoodRecord:
    fdc_id: str
    display_name: str
    description: str
    preparation: str | None
    aliases: tuple[str, ...]
    source: str
    version: str
    url: str
    per_100g: Nutrients


@dataclass(frozen=True)
class CalculatedFood:
    fdc_id: str
    display_name: str
    description: str
    preparation: str | None
    grams: Decimal
    per_100g: Nutrients
    totals: Nutrients
    source: str
    version: str
    url: str
    calculation_method: str = "reference"


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def _decimal(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def _fdc_id(value: int | str) -> str:
    if isinstance(value, bool):
        raise FoodNotFoundError("Неизвестный идентификатор продукта USDA")
    normalized = str(value).strip()
    if not re.fullmatch(r"[1-9]\d*", normalized):
        raise FoodNotFoundError("Неизвестный идентификатор продукта USDA")
    return normalized


def _candidate(record: FoodRecord) -> FoodCandidate:
    return FoodCandidate(
        fdc_id=record.fdc_id,
        display_name=record.display_name,
        description=record.description,
        preparation=record.preparation,
        aliases=record.aliases,
        source=record.source,
        version=record.version,
        url=record.url,
    )


class FoodReference:
    """Read-only local catalog built from pinned USDA FoodData Central releases."""

    def __init__(self, resource_dir: str | Path | None = None):
        if resource_dir is None:
            resource_dir = Path(__file__).resolve().parents[1] / "resources" / "nutrition"
        self.resource_dir = Path(resource_dir)
        catalog_path = self.resource_dir / "catalog.csv"
        aliases_path = self.resource_dir / "aliases.json"
        provenance_path = self.resource_dir / "provenance.json"
        missing = [path.name for path in (catalog_path, aliases_path, provenance_path) if not path.is_file()]
        if missing:
            raise ReferenceDataNotFoundError(
                "Локальный справочник питания не найден: {}".format(", ".join(missing))
            )

        with provenance_path.open("r", encoding="utf-8") as stream:
            self.provenance = json.load(stream)
        with aliases_path.open("r", encoding="utf-8") as stream:
            aliases_payload = json.load(stream)

        self._records: dict[str, FoodRecord] = {}
        self._incomplete_ids: set[str] = set()
        self._search_values: dict[str, tuple[str, ...]] = {}
        with catalog_path.open("r", encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                fdc_id = row.get("fdc_id", "").strip()
                aliases = tuple(value for value in row.get("aliases", "").split("|") if value)
                nutrient_values = {
                    name: _decimal(row.get(name, ""))
                    for name in ("kcal", "protein_g", "fat_g", "carbs_g")
                }
                if not fdc_id or any(value is None for value in nutrient_values.values()):
                    if fdc_id:
                        self._incomplete_ids.add(fdc_id)
                    continue
                record = FoodRecord(
                    fdc_id=fdc_id,
                    display_name=row["display_name"],
                    description=row["description"],
                    preparation=row.get("preparation") or None,
                    aliases=aliases,
                    source=row["source"],
                    version=row["version"],
                    url=row["url"],
                    per_100g=Nutrients(**nutrient_values),
                )
                self._records[fdc_id] = record
                self._search_values[fdc_id] = tuple(
                    _normalize(value)
                    for value in (record.display_name, record.description, *record.aliases)
                    if value
                )

        self._alias_targets: dict[str, tuple[str, ...]] = {}
        for entry in aliases_payload.get("aliases", []):
            alias = _normalize(str(entry.get("alias", "")))
            targets = tuple(str(value) for value in entry.get("fdc_ids", ()))
            if alias and targets:
                self._alias_targets[alias] = targets

    def search(self, query: str, limit: int = 5) -> list[FoodCandidate]:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit должен быть целым числом")
        if limit <= 0:
            return []
        normalized = _normalize(str(query or ""))
        if not normalized:
            return []

        result_ids = []
        for fdc_id in self._alias_targets.get(normalized, ()):
            if fdc_id in self._records and fdc_id not in result_ids:
                result_ids.append(fdc_id)

        query_tokens = normalized.split()
        numeric_tokens = {token for token in query_tokens if token.isdigit()}
        qualifier_tokens = {token for token in query_tokens if token in QUALIFIER_TOKENS}
        subject_tokens = [
            token
            for token in query_tokens
            if token not in numeric_tokens and token not in qualifier_tokens
        ]
        scored = []
        for fdc_id, values in self._search_values.items():
            if fdc_id in result_ids:
                continue
            token_set = set(" ".join(values).split())
            if numeric_tokens and not numeric_tokens.issubset(token_set):
                continue
            if qualifier_tokens and not qualifier_tokens.issubset(token_set):
                continue
            if subject_tokens and not any(token in token_set for token in subject_tokens):
                continue
            if normalized in values:
                score = 100
            elif any(value.startswith(normalized) for value in values):
                score = 85
            elif all(token in token_set for token in query_tokens):
                score = 70 + len(query_tokens)
            elif normalized in " ".join(values):
                score = 60
            else:
                matches = sum(token in token_set for token in query_tokens)
                if matches == 0:
                    continue
                score = matches
            scored.append((-score, self._records[fdc_id].description.casefold(), int(fdc_id), fdc_id))
        scored.sort()
        result_ids.extend(item[-1] for item in scored)
        return [_candidate(self._records[fdc_id]) for fdc_id in result_ids[:limit]]

    def get(self, fdc_id: int | str) -> FoodRecord:
        normalized = _fdc_id(fdc_id)
        if normalized in self._incomplete_ids:
            raise IncompleteNutrientsError(
                "Для продукта USDA {} нет полного набора КБЖУ".format(normalized)
            )
        try:
            return self._records[normalized]
        except KeyError as error:
            raise FoodNotFoundError(
                "Продукт USDA {} не найден в локальном справочнике".format(normalized)
            ) from error

    def calculate(self, fdc_id: int | str, grams: NumberInput) -> CalculatedFood:
        if isinstance(grams, bool):
            raise InvalidAmountError("Масса должна быть конечным положительным числом")
        try:
            amount = Decimal(str(grams).strip())
        except (InvalidOperation, ValueError, AttributeError) as error:
            raise InvalidAmountError("Масса должна быть конечным положительным числом") from error
        if not amount.is_finite() or amount <= 0 or amount > MAX_GRAMS:
            raise InvalidAmountError("Масса должна быть конечным положительным числом")

        record = self.get(fdc_id)
        factor = amount / Decimal("100")

        def scaled(value: Decimal) -> Decimal:
            try:
                return (value * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except InvalidOperation as error:
                raise InvalidAmountError(
                    "Масса должна быть конечным положительным числом"
                ) from error

        totals = Nutrients(
            kcal=scaled(record.per_100g.kcal),
            protein_g=scaled(record.per_100g.protein_g),
            fat_g=scaled(record.per_100g.fat_g),
            carbs_g=scaled(record.per_100g.carbs_g),
        )
        return CalculatedFood(
            fdc_id=record.fdc_id,
            display_name=record.display_name,
            description=record.description,
            preparation=record.preparation,
            grams=amount,
            per_100g=record.per_100g,
            totals=totals,
            source=record.source,
            version=record.version,
            url=record.url,
        )
