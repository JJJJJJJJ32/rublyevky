from types import SimpleNamespace

import modules.network as network
from modules.category_finder import find_funpay_category
from modules.games_list_reader import remove_game_from_list


def test_zapret_check_uses_defined_runner(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="winws.exe  1234 Console 1 12,000 K", returncode=0)

    monkeypatch.setattr(network.sys, "platform", "win32")
    monkeypatch.setattr(network, "_run", fake_run)

    assert network.is_zapret_active() is True
    assert calls == [["tasklist", "/FI", "IMAGENAME eq winws.exe"]]


def test_warp_disconnected_is_not_reported_as_connected(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "win32")
    monkeypatch.setattr(
        network,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="Status: Disconnected", returncode=0
        ),
    )

    assert network.warp_cli_status() is False


def test_warp_connected_is_reported_as_connected(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "win32")
    monkeypatch.setattr(
        network,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="Status: Connected", returncode=0
        ),
    )

    assert network.warp_cli_status() is True


def test_remove_game_matches_the_whole_name(tmp_path):
    games_file = tmp_path / "games_list.txt"
    games_file.write_text(
        "Raft\nMinecraft\nWorld of Warcraft\nRaft|12345\n",
        encoding="utf-8",
    )

    remove_game_from_list("Raft", str(games_file))

    assert games_file.read_text(encoding="utf-8").splitlines() == [
        "Minecraft",
        "World of Warcraft",
    ]


def test_known_category_id_skips_brittle_search():
    result = find_funpay_category(None, "Any game", known_id="9876")
    assert result == {"id": "9876", "type": "Указанный раздел"}
