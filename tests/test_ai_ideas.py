"""Разбор идей товаров: ответ модели бывает в обёртке, обрезанным или неполным."""

import json

import pytest

from modules import ai_generator


def _idea(title, points=("пункт",)):
    return {"title": title, "type": "гайд", "content_points": list(points)}


class TestParseIdeasJson:
    def test_plain_array(self):
        text = json.dumps([_idea("Маршрут фарма"), _idea("Билд танка")])

        ideas = ai_generator._parse_ideas_json(text)

        assert [item["title"] for item in ideas] == ["Маршрут фарма", "Билд танка"]

    def test_json_in_code_fence_and_prose(self):
        text = (
            "Конечно, вот идеи:\n```json\n"
            + json.dumps([_idea("Секретные места")])
            + "\n```\nГотово."
        )

        ideas = ai_generator._parse_ideas_json(text)

        assert len(ideas) == 1
        assert ideas[0]["title"] == "Секретные места"

    def test_truncated_array_keeps_complete_items(self):
        text = '[{"title": "Первая", "content_points": ["а", "б"]}, {"title": "Вторая", "content_poi'

        ideas = ai_generator._parse_ideas_json(text)

        assert [item["title"] for item in ideas] == ["Первая"]

    def test_objects_without_title_are_skipped(self):
        text = '[{"type": "гайд", "content_points": ["а"]}, {"title": "Норм"}]'

        ideas = ai_generator._parse_ideas_json(text)

        assert [item["title"] for item in ideas] == ["Норм"]

    def test_points_with_junk_are_cleaned(self):
        text = '[{"title": "Идея", "content_points": ["а", 5, "", "б"]}]'

        ideas = ai_generator._parse_ideas_json(text)

        assert ideas[0]["content_points"] == ["а", "б"]

    def test_refusal_text_gives_nothing(self):
        assert ai_generator._parse_ideas_json("Извините, я не могу помочь.") == []


class TestGenerateProductIdeas:
    def _patch(self, monkeypatch, answers):
        calls = []

        def fake_ask(messages, **kwargs):
            calls.append(messages)
            return answers[len(calls) - 1]

        monkeypatch.setattr(ai_generator, "_ask", fake_ask)
        return calls

    def test_full_answer_is_returned(self, monkeypatch):
        ideas = [_idea(f"Идея {number}") for number in range(1, 13)]
        calls = self._patch(monkeypatch, [json.dumps(ideas, ensure_ascii=False)])

        result = ai_generator.ai_generate_product_ideas("Тест")

        assert len(result) == 12
        assert len(calls) == 1

    def test_short_answer_is_retried_and_best_kept(self, monkeypatch):
        first = [_idea(f"Идея {number}") for number in range(1, 5)]
        second = "Не могу придумать."
        self._patch(monkeypatch, [json.dumps(first, ensure_ascii=False), second])

        result = ai_generator.ai_generate_product_ideas("Тест")

        assert [item["title"] for item in result] == [f"Идея {number}" for number in range(1, 5)]

    def test_refusal_in_both_attempts_gives_empty_list(self, monkeypatch):
        errors = []
        self._patch(monkeypatch, ["Не могу.", "Всё ещё не могу."])
        monkeypatch.setattr(
            ai_generator.logger, "log_error", lambda message: errors.append(message)
        )

        assert ai_generator.ai_generate_product_ideas("Тест") == []
        assert any("без единой идеи" in message for message in errors)
