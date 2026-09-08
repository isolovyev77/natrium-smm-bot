"""Проверка реального преобразования времени веб-кабинета в дни перевода часов."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_web_local_time_roundtrip_and_daylight_saving_gap():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser time conversion check")
    html = (Path(__file__).parents[1] / "src/nutrition_dashboard.html").read_text()
    source = "function zonedIso(" + html.split("function zonedIso(", 1)[1].split(
        "function mealContext(", 1
    )[0]
    scenarios = [
        ["2026-09-08", "08:30", "Europe/Moscow", "2026-09-08T05:30:00.000Z"],
        ["2026-03-29", "01:30", "Europe/Paris", "2026-03-29T00:30:00.000Z"],
        ["2026-03-29", "03:30", "Europe/Paris", "2026-03-29T01:30:00.000Z"],
        ["2026-03-08", "01:30", "America/New_York", "2026-03-08T06:30:00.000Z"],
        ["2026-03-08", "03:30", "America/New_York", "2026-03-08T07:30:00.000Z"],
        ["2026-10-25", "02:30", "Europe/Paris", "2026-10-25T00:30:00.000Z"],
        ["2026-10-04", "02:45", "Australia/Lord_Howe", "2026-10-03T15:45:00.000Z"],
        ["2026-09-08", "00:15", "Pacific/Kiritimati", "2026-09-07T10:15:00.000Z"],
    ]
    script = "const fn=(" + source + ");const cases=" + json.dumps(scenarios) + ";" + """
const assert = require('node:assert/strict');
for (const [day,time,zone,expected] of cases) assert.equal(fn(day,time,zone),expected,`${day} ${time} ${zone}`);
assert.throws(() => fn('2026-03-29','02:30','Europe/Paris'), /такого местного времени нет/);
assert.throws(() => fn('2026-03-08','02:30','America/New_York'), /такого местного времени нет/);
assert.throws(() => fn('2026-10-04','02:15','Australia/Lord_Howe'), /такого местного времени нет/);
process.stdout.write('time conversion scenarios passed');
"""
    result = subprocess.run([node, "-"], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "time conversion scenarios passed"
