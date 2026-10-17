"""Startup wiring tests for channel verification."""

import importlib
import sys
import types


def test_startup_verifies_each_configured_channel_before_listening(monkeypatch):
    timeline = []

    class FakeClient:
        def auth_test(self):
            timeline.append("auth")
            return {"user_id": "U_BOT", "user": "goose-lee"}

        def conversations_info(self, channel):
            timeline.append(("lookup", channel))
            names = {
                "C_LOADOUT": "loadout",
                "C_DEV": "loadout-development",
            }
            return {
                "channel": {
                    "id": channel,
                    "name": names[channel],
                    "is_private": False,
                }
            }

    app = types.SimpleNamespace(client=FakeClient())

    class FakeSocketModeHandler:
        def __init__(self, _app, _token):
            pass

        def start(self):
            timeline.append("start")

    bolt = types.ModuleType("slack_bolt")
    bolt.__path__ = []
    bolt.App = object
    adapter = types.ModuleType("slack_bolt.adapter")
    adapter.__path__ = []
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket_mode.SocketModeHandler = FakeSocketModeHandler
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter", adapter)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode", socket_mode)
    monkeypatch.delitem(sys.modules, "bot", raising=False)
    bot = importlib.import_module("bot")

    monkeypatch.setattr(bot, "SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(bot, "SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setattr(bot, "check_tokens", lambda: True)
    monkeypatch.setattr(bot, "init_db", lambda _path: None)
    monkeypatch.setattr(bot, "App", lambda token: app)
    monkeypatch.setattr(
        bot,
        "get_configured_channels",
        lambda: [("C_LOADOUT", "loadout"), ("C_DEV", "loadout-development")],
    )
    monkeypatch.setattr(
        bot,
        "set_watched_channel_verified",
        lambda channel_id, name, is_private: timeline.append(
            ("verify", channel_id, name, is_private)
        ) or True,
    )
    monkeypatch.setattr(bot, "register_handlers", lambda *_args, **_kwargs: timeline.append("register"))
    monkeypatch.setattr(bot, "print_banner", lambda *_args: None)
    monkeypatch.setattr(bot.signal, "signal", lambda *_args: None)

    bot.main()

    assert timeline == [
        "auth",
        ("lookup", "C_LOADOUT"),
        ("verify", "C_LOADOUT", "loadout", False),
        ("lookup", "C_DEV"),
        ("verify", "C_DEV", "loadout-development", False),
        "register",
        "start",
    ]
