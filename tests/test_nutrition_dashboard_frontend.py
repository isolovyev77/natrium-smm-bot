import json
import re
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


HTML_PATH = Path(__file__).parents[1] / "src" / "nutrition_dashboard.html"


class _StructureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.scripts = []
        self._script = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        if tag == "script" and not values.get("src"):
            self._script = []

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None


class NutritionDashboardFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML_PATH.read_text(encoding="utf-8")
        cls.parser = _StructureParser()
        cls.parser.feed(cls.html)
        cls.script = "\n".join(cls.parser.scripts)

    def test_structure_has_unique_ids_and_two_role_modes(self):
        self.assertEqual(len(self.parser.ids), len(set(self.parser.ids)))
        self.assertIn("Мой дневник", self.html)
        self.assertIn("Кабинет тренера", self.html)
        for view in ("today", "dynamics", "plan", "profile"):
            self.assertIn(f'data-view="{view}"', self.html)

    def test_uses_v3_routes_and_raw_bounded_xlsx(self):
        for route in (
            "/api/session",
            "/api/me/day",
            "/api/me/summary",
            "/api/me/profile",
            "/api/me/water",
            "/api/me/weight",
            "/api/me/meals/drafts",
            "/api/reference/calculate",
            "/api/me/reminders",
            "/api/trainer/reminders",
            "/api/norms-plan/template.xlsx",
            "/norms-plan/xlsx/preview",
            "/norms-plan/xlsx/commit",
            "/api/export",
        ):
            self.assertIn(route, self.script)
        self.assertIn("file.size>2097152", self.script)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            self.script,
        )
        self.assertNotIn("FormData();body.append", self.script)

    def test_user_text_is_added_with_text_content(self):
        self.assertIn("textContent=String(x)", self.script)
        self.assertNotIn(".innerHTML", self.html)
        self.assertNotIn("document.write", self.html)
        self.assertNotIn("eval(", self.html)

    def test_dates_use_server_today_and_utc_calendar_math(self):
        self.assertIn("state.session.today", self.script)
        self.assertIn("c.local_today||c.today?.date", self.script)
        self.assertIn("Date.UTC", self.script)
        self.assertIn("getUTCDay", self.script)
        self.assertNotIn("T12:00:00", self.script)
        self.assertIn("zonedIso", self.script)
        self.assertIn("readableMoment", self.script)

    def test_failed_dialog_mutations_keep_fields_and_show_error(self):
        self.assertIn('id="dialog-status"', self.html)
        self.assertGreaterEqual(self.script.count("dialogStatus(x.message)"), 4)
        self.assertIn('"If-Match":String(version)', self.script)
        self.assertIn("Предпросмотр итогов", self.script)
        self.assertIn("if(!form.dataset.key){form.dataset.key=idempotency()", self.script)
        self.assertIn("button.disabled=true", self.script)

    def test_stale_meal_has_explicit_conflict_resolution(self):
        self.assertIn("showMealConflict", self.script)
        self.assertIn("Применить мои изменения поверх новых", self.script)
        self.assertIn("Принять версию сервера", self.script)
        self.assertIn("Ваши поля сохранены", self.script)
        self.assertIn("version=fresh.version", self.script)
        self.assertIn("Подтвердить актуальный черновик", self.script)
        self.assertIn("Вернуться без подтверждения", self.script)
        self.assertIn('api(path(state.mode==="trainer"?"meal":"selfMeal"', self.script)

    def test_all_versioned_forms_offer_fresh_server_choice(self):
        for text in (
            "Применить мои настройки поверх новых",
            "Принять настройки сервера",
            "Применить мои данные поверх новых",
            "Принять профиль сервера",
            "Отменить актуальную запись",
            "Оставить актуальную запись",
        ):
            self.assertIn(text, self.script)
        self.assertIn('await api(path(water?"waterEntry":"weightEntry"', self.script)

    def test_uncertain_retry_keeps_key_only_for_same_payload(self):
        self.assertIn("mutationFingerprint", self.script)
        self.assertIn("form.dataset.keyPayload&&form.dataset.keyPayload!==fingerprint", self.script)
        self.assertIn("Вернуть отправленные данные", self.script)
        self.assertIn("restoreMutationPayload", self.script)
        self.assertIn("error.status&&error.status<500", self.script)

    def test_meal_type_is_localized_in_confirmation_preview(self):
        self.assertIn("mealTypeLabel(current.meal_type)", self.script)
        self.assertNotIn('el("h3",meal.meal_type||"Приём пищи")', self.script)

    def test_manual_meal_requires_values_instead_of_coercing_blanks_to_zero(self):
        self.assertIn("if(f.required)i.required=true", self.script)
        self.assertIn('name:"calories",type:"number",min:"0",step:"0.1",required:true', self.script)
        self.assertIn('name:"weight_g",type:"number",min:"0.1",step:"0.1",required:true', self.script)
        self.assertNotIn('weight_g:d.weight_g===""?null:Number(d.weight_g)', self.script)

    def test_guided_repeat_comments_and_empty_day_navigation_are_present(self):
        for route in (
            "/api/me/trainer-comments?limit=20",
            "/api/me/meals/recent?limit=10",
            "/api/me/meals/{meal}/repeat",
        ):
            self.assertIn(route, self.script)
        for text in (
            "Последние комментарии тренера",
            "Перейти к записи",
            "Первый шаг на этот день",
            "Последние приёмы",
            "Посмотреть неделю",
            "Продолжить к подтверждению",
            "До подтверждения черновик не входит",
        ):
            self.assertIn(text, self.script)
        self.assertIn("state.session.today", self.script)
        self.assertIn("repeatMealDialog", self.script)
        self.assertIn("formMutation(f,key=>api(path(\"repeatMeal\"", self.script)

    def test_food_check_preserves_and_explains_provenance(self):
        for text in (
            "Проверка еды",
            "ИИ, приблизительно",
            "Справочник",
            "Вручную",
            "Масса:",
            "Порция:",
            "Оценка ИИ приблизительна",
            "Значения введены вручную и не проверяются автоматически",
        ):
            self.assertIn(text, self.script)
        self.assertIn("id:i.id", self.script)
        self.assertIn("item_${index}_portion_text", self.script)

    def test_mass_change_recalculates_manual_and_reference_macros(self):
        start = self.script.index("const mealMacroKeys=")
        end = self.script.index("function foodCheck", start)
        helpers = self.script[start:end]
        cases = [
            {
                "weight_g": 150,
                "calories": 400,
                "protein_g": 12,
                "fat_g": 18,
                "carbs_g": 50,
                "calculation_method": "ai",
                "approximate": True,
            },
            {
                "weight_g": 100,
                "calculation_method": "reference",
                "reference_kcal_per_100g": 89,
                "reference_protein_per_100g": 1.1,
                "reference_fat_per_100g": 0.3,
                "reference_carbs_per_100g": 22.8,
            },
        ]
        program = (
            helpers
            + "\nconsole.log(JSON.stringify(["
            + f"mealMacrosForWeight({json.dumps(cases[0])},294),"
            + f"mealMacrosForWeight({json.dumps(cases[1])},150)"
            + "]));"
        )
        result = subprocess.run(
            ["node", "-e", program], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manual, reference = json.loads(result.stdout)
        self.assertEqual(
            manual,
            {"calories": 784, "protein_g": 23.52, "fat_g": 35.28, "carbs_g": 98},
        )
        self.assertEqual(
            reference,
            {"calories": 133.5, "protein_g": 1.65, "fat_g": 0.45, "carbs_g": 34.2},
        )
        self.assertIn("manuallyEditedMacros", self.script)
        self.assertIn('readOnly:reference', self.script)
        self.assertIn("исходной записи не указана масса", self.script)

    def test_profile_timezone_refreshes_session_without_moving_historical_date(self):
        self.assertIn("wasToday=state.date===state.session.today", self.script)
        self.assertIn('const latest=await api(path("session"))', self.script)
        self.assertIn("if(wasToday)state.date=latest.today||state.date", self.script)

    def test_authenticated_binary_resources_do_not_use_direct_image_or_template_urls(self):
        self.assertIn('api(path("photo",{meal:mealId}),{blob:true})', self.script)
        self.assertIn('api(path("template"),{blob:true})', self.script)
        self.assertIn("URL.createObjectURL", self.script)
        self.assertNotIn('img.src=path("photo"', self.script)

    def test_charts_preserve_missing_values_and_render_point_markers(self):
        self.assertIn("x!==null&&x!==undefined", self.script)
        self.assertIn('document.createElementNS(ns,"circle")', self.script)
        self.assertIn("water_norm", self.script)
        self.assertIn('chart("Вес"', self.script)
        self.assertIn("display_name||f.name", self.script)

    def test_capabilities_gate_optional_actions(self):
        for capability in (
            "self_dashboard",
            "trainer_dashboard",
            "edit_own_diary",
            "manage_norms",
            "xlsx_plan",
            "export_csv",
            "export_xlsx",
            "print_view",
            "meal_photo",
            "reminder_preferences",
            "trainer_reminders",
        ):
            self.assertIn(capability, self.script)
        self.assertNotIn("demo", self.html.lower())
        self.assertNotIn("lorem", self.html.lower())

    def test_photo_and_description_open_verified_bot(self):
        self.assertIn('bot.href="https://t.me/natrium_smm_bot"', self.script)
        self.assertIn('el("a","Открыть бота"', self.script)

    def test_inline_javascript_parses(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
            handle.write(self.script)
            handle.flush()
            result = subprocess.run(
                ["node", "--check", handle.name],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_forbidden_em_dash_is_absent(self):
        self.assertNotIn("\u2014", self.html)


if __name__ == "__main__":
    unittest.main()
