#!/usr/bin/env python3
"""Build the local nutrition reference from pinned USDA FoodData Central CSV releases."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import shutil
import tempfile
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path


DOWNLOAD_PAGE = "https://fdc.nal.usda.gov/download-datasets/"
FOOD_URL = "https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients"
DATASETS = (
    {
        "key": "foundation",
        "source": "USDA FoodData Central Foundation Foods",
        "version": "2026-04-30",
        "url": "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_csv_2026-04-30.zip",
        "sha256": "70457ee9d9342f43bda2010318c85f04210c689fdeb9cd2da4c513b0e8dbc655",
        "id_file": "foundation_food.csv",
        "energy_ids": ("2048", "2047", "1008"),
    },
    {
        "key": "sr_legacy",
        "source": "USDA FoodData Central SR Legacy",
        "version": "2018-04",
        "url": "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip",
        "sha256": "b80817294b8850530aaedf2e515c02593b1824f763a0ff356e5c2081643e6fd0",
        "id_file": "sr_legacy_food.csv",
        "energy_ids": ("1008",),
    },
)
MACRO_IDS = {
    "protein_g": "1003",
    "fat_g": "1004",
    "carbs_g": "1005",
}
CATALOG_FIELDS = (
    "fdc_id",
    "source",
    "version",
    "url",
    "description",
    "display_name",
    "preparation",
    "aliases",
    "kcal",
    "protein_g",
    "fat_g",
    "carbs_g",
    "energy_nutrient_id",
)
PREPARATION_RE = re.compile(
    r"\b(raw|cooked|boiled|roasted|fried|baked|steamed|stewed|grilled|dried|"
    r"frozen|canned|toasted|smoked|broiled|braised|poached|scrambled)\b",
    re.IGNORECASE,
)


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "natrium-nutrition-reference/1"})
    with urllib.request.urlopen(request, timeout=90) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def zip_member(archive: zipfile.ZipFile, basename: str) -> str:
    matches = [name for name in archive.namelist() if name.rsplit("/", 1)[-1] == basename]
    if len(matches) != 1:
        raise ValueError("archive must contain exactly one {}".format(basename))
    return matches[0]


def csv_rows(archive: zipfile.ZipFile, basename: str):
    raw = archive.open(zip_member(archive, basename))
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    return text, csv.DictReader(text)


def finite_nonnegative(value: str) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def preparation(description: str) -> str:
    parts = [part.strip() for part in description.split(",")]
    selected = [part for part in parts[1:] if PREPARATION_RE.search(part)]
    return ", ".join(selected)


def load_aliases(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if payload.get("schema_version") != 1 or not isinstance(payload.get("aliases"), list):
        raise ValueError("unsupported aliases schema")
    display_names = payload.get("display_names", {})
    if not isinstance(display_names, dict):
        raise ValueError("display_names must be an object")
    by_id = defaultdict(list)
    seen = {}
    for entry in payload["aliases"]:
        alias = str(entry["alias"]).strip()
        ids = [str(value) for value in entry["fdc_ids"]]
        key = normalize(alias)
        if not key or not ids:
            raise ValueError("alias and fdc_ids must be non-empty")
        if key in seen and seen[key] != ids:
            raise ValueError("normalized alias has conflicting targets: {}".format(alias))
        seen[key] = ids
        for fdc_id in ids:
            if alias not in by_id[fdc_id]:
                by_id[fdc_id].append(alias)
    return {str(key): str(value) for key, value in display_names.items()}, by_id


def import_dataset(dataset, archive_path: Path):
    observed_hash = sha256_file(archive_path)
    if observed_hash != dataset["sha256"]:
        raise ValueError(
            "SHA-256 mismatch for {}: expected {}, got {}".format(
                dataset["key"], dataset["sha256"], observed_hash
            )
        )

    with zipfile.ZipFile(archive_path) as archive:
        text, rows = csv_rows(archive, dataset["id_file"])
        try:
            selected_ids = {row["fdc_id"] for row in rows}
        finally:
            text.close()

        foods = {}
        text, rows = csv_rows(archive, "food.csv")
        try:
            for row in rows:
                fdc_id = row["fdc_id"]
                if fdc_id in selected_ids:
                    foods[fdc_id] = row["description"].strip()
        finally:
            text.close()

        nutrient_ids = set(MACRO_IDS.values()) | set(dataset["energy_ids"])
        nutrients = defaultdict(dict)
        text, rows = csv_rows(archive, "food_nutrient.csv")
        try:
            for row in rows:
                fdc_id = row["fdc_id"]
                nutrient_id = row["nutrient_id"]
                if fdc_id in selected_ids and nutrient_id in nutrient_ids and row.get("amount"):
                    nutrients[fdc_id][nutrient_id] = row["amount"].strip()
        finally:
            text.close()

    records = []
    incomplete = 0
    for fdc_id in sorted(selected_ids, key=int):
        values = nutrients.get(fdc_id, {})
        energy_id = next((value for value in dataset["energy_ids"] if value in values), None)
        required = [energy_id] + list(MACRO_IDS.values()) if energy_id else []
        if not required or any(not finite_nonnegative(values.get(key, "")) for key in required):
            incomplete += 1
            continue
        records.append(
            {
                "fdc_id": fdc_id,
                "source": dataset["source"],
                "version": dataset["version"],
                "url": FOOD_URL.format(fdc_id=fdc_id),
                "description": foods[fdc_id],
                "preparation": preparation(foods[fdc_id]),
                "kcal": values[energy_id],
                "protein_g": values[MACRO_IDS["protein_g"]],
                "fat_g": values[MACRO_IDS["fat_g"]],
                "carbs_g": values[MACRO_IDS["carbs_g"]],
                "energy_nutrient_id": energy_id,
            }
        )
    return records, {
        "key": dataset["key"],
        "source": dataset["source"],
        "version": dataset["version"],
        "download_url": dataset["url"],
        "sha256": observed_hash,
        "selected_foods": len(selected_ids),
        "complete_foods": len(records),
        "incomplete_skipped": incomplete,
        "energy_preference": list(dataset["energy_ids"]),
    }


def build(archives, output_dir: Path, aliases_path: Path) -> None:
    display_names, aliases_by_id = load_aliases(aliases_path)
    records = []
    provenance = []
    for dataset in DATASETS:
        imported, metadata = import_dataset(dataset, archives[dataset["key"]])
        records.extend(imported)
        provenance.append(metadata)

    ids = {record["fdc_id"] for record in records}
    unknown_alias_ids = sorted((set(display_names) | set(aliases_by_id)) - ids, key=int)
    if unknown_alias_ids:
        raise ValueError("aliases reference missing or incomplete IDs: {}".format(unknown_alias_ids))

    for record in records:
        fdc_id = record["fdc_id"]
        record["display_name"] = display_names.get(fdc_id, record["description"])
        record["aliases"] = "|".join(aliases_by_id.get(fdc_id, ()))
    records.sort(key=lambda row: (row["source"], row["description"].casefold(), int(row["fdc_id"])))

    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "catalog.csv"
    with catalog_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CATALOG_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)

    provenance_payload = {
        "schema_version": 1,
        "catalog_records": len(records),
        "source_page": DOWNLOAD_PAGE,
        "nutrients_per_100g": {
            "protein_g": {"nutrient_id": "1003", "unit": "G"},
            "fat_g": {"nutrient_id": "1004", "unit": "G"},
            "carbs_g": {"nutrient_id": "1005", "unit": "G"},
            "kcal": {
                "unit": "KCAL",
                "foundation_preference": [
                    {"nutrient_id": "2048", "name": "Energy (Atwater Specific Factors)"},
                    {"nutrient_id": "2047", "name": "Energy (Atwater General Factors)"},
                    {"nutrient_id": "1008", "name": "Energy"}
                ],
                "sr_legacy": {"nutrient_id": "1008", "name": "Energy"},
            },
        },
        "calculation": "per_100g * grams / 100",
        "datasets": provenance,
    }
    with (output_dir / "provenance.json").open("w", encoding="utf-8") as stream:
        json.dump(provenance_payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--foundation-zip", type=Path)
    parser.add_argument("--sr-legacy-zip", type=Path)
    parser.add_argument("--aliases", type=Path, default=Path("resources/nutrition/aliases.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("resources/nutrition"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    supplied = {
        "foundation": args.foundation_zip,
        "sr_legacy": args.sr_legacy_zip,
    }
    with tempfile.TemporaryDirectory(prefix="usda-nutrition-") as temporary:
        temporary_dir = Path(temporary)
        archives = {}
        for dataset in DATASETS:
            archive = supplied[dataset["key"]]
            if archive is None:
                archive = temporary_dir / (dataset["key"] + ".zip")
                download(dataset["url"], archive)
            archives[dataset["key"]] = archive
        build(archives, args.output_dir, args.aliases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
