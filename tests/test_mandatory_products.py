import json

import config
from modules import mandatory_products


def _all_titles():
    return [product["title"] for product in config.MANDATORY_PRODUCTS]


def test_all_three_mandatory_products_are_kept(monkeypatch):
    calls = []

    def fake_ai(messages, **kwargs):
        calls.append(messages[0]["content"])
        return json.dumps({
            "keep": ["pc_optimization", "ping", "hwid"],
            "replace": [],
        })

    monkeypatch.setattr(mandatory_products.ai_client, "ai_ask", fake_ai)
    result = mandatory_products.select_mandatory_products("PC online game", "PC")

    assert len(result) == 3
    assert [item["title"] for item in result] == _all_titles()
    assert len(calls) == 1


def test_one_inappropriate_product_is_replaced(monkeypatch, tmp_path):
    replacement = {
        "title": "МАРШРУТЫ ФАРМА В ТЕСТОВОЙ ИГРЕ",
        "type": "гайд",
        "content_points": ["Локации", "Порядок действий"],
    }

    monkeypatch.setattr(
        mandatory_products.ai_client,
        "ai_ask",
        lambda messages, **kwargs: json.dumps({
            "keep": ["pc_optimization", "hwid"],
            "replace": [{
                "product_id": "ping",
                "reason": "игра не использует сетевой режим",
                "replacement": replacement,
            }],
        }),
    )
    monkeypatch.setattr(config, "LOGS_DIR", str(tmp_path))

    result = mandatory_products.select_mandatory_products("PC online game", "PC")

    assert len(result) == 3
    assert result[1] == replacement
    assert result[0]["title"] == _all_titles()[0]
    assert result[2]["title"] == _all_titles()[2]
    log_text = (tmp_path / "mandatory_products.jsonl").read_text(encoding="utf-8")
    assert "игра не использует сетевой режим" in log_text


def test_obvious_mobile_offline_game_uses_filter_without_ai(monkeypatch):
    calls = []

    def fail_if_called(*args, **kwargs):
        calls.append(True)
        raise AssertionError("для очевидной мобильной офлайн-игры ИИ не нужен")

    monkeypatch.setattr(mandatory_products.ai_client, "ai_ask", fail_if_called)
    signals = mandatory_products.quick_game_check("Mobile offline game", "Android")
    result = mandatory_products.select_mandatory_products("Mobile offline game", "Android")

    assert signals["pc"] is False
    assert signals["online"] is False
    assert signals["anti_cheat"] is False
    assert len(result) == 3
    assert calls == []
    assert all(item["title"] not in _all_titles() for item in result)


def test_main_keeps_three_mandatory_plus_twelve_ideas(monkeypatch):
    import main

    mandatory = [
        {"title": "Обязательный 1", "type": "гайд", "content_points": []},
        {"title": "Замена 2", "type": "гайд", "content_points": []},
        {"title": "Обязательный 3", "type": "гайд", "content_points": []},
    ]
    ideas = [
        {"title": f"Идея {index}", "type": "гайд", "content_points": []}
        for index in range(20)
    ]
    monkeypatch.setattr(main, "select_mandatory_products", lambda *args: mandatory)

    result = main.build_final_ideas("Тестовая игра", ideas, "PC")

    assert len(result) == 15
    assert result[:3] == mandatory
    assert result[3:] == ideas[:12]
