"""Tests for the rare visual assets: bad sifu quotes and reaction clips."""

from unittest.mock import MagicMock

import pytest

import handlers
import slack_assets


def test_quote_requests_are_detected():
    assert slack_assets.is_quote_request("goose, tell me a quote")
    assert slack_assets.is_quote_request("give me some ancient wisdom")
    assert slack_assets.is_quote_request("drop a sifu quote")
    assert slack_assets.is_quote_request("quote")
    assert slack_assets.is_quote_request("quote please")
    assert slack_assets.is_quote_request("wisdom")
    assert not slack_assets.is_quote_request("how is the build going")
    assert not slack_assets.is_quote_request("goose me")


def test_pick_quote_always_attaches_an_image():
    assert slack_assets.pick_quote()["image"]


def test_goose_quote_uses_a_duck_clip(monkeypatch):
    monkeypatch.setattr(
        slack_assets, "QUOTES", [{"text": "Stay lazy.", "who": "Goose Lee", "image": None}]
    )
    monkeypatch.setattr(slack_assets, "GOOSE_IMAGES", ["quotes/goose_typing.gif"])
    assert slack_assets.pick_quote()["image"] == "quotes/goose_typing.gif"


def test_sifu_quote_uses_an_unpaired_still(monkeypatch):
    monkeypatch.setattr(
        slack_assets, "QUOTES", [{"text": "Stay lazy.", "who": "Sifu Cat", "image": None}]
    )
    monkeypatch.setattr(slack_assets, "SIFU_IMAGES", ["quotes/paw_tzu_wise.png"])
    assert slack_assets.pick_quote()["image"] == "quotes/paw_tzu_wise.png"


def test_format_quote():
    assert slack_assets.format_quote({"text": "Stay lazy.", "who": "Sifu Cat"}) == '"Stay lazy." - Sifu Cat'


@pytest.mark.parametrize(
    "text,image",
    [
        ("msg is terrible", "rage/msg_sacred.gif"),
        ("boba is overrated", "rage/food_slander.gif"),
        ("bruce lee is trash", "rage/bruce_lee.gif"),
        ("goose show me your nunchaku", "rage/nunchaku_fail.gif"),
        ("the ci failed again", "rage/failure.gif"),
        ("emotional damage", "rage/emotional_damage.gif"),
        ("the build passed", "victory/waddle.gif"),
        ("I shipped it", "victory/waddle.gif"),
    ],
)
def test_detect_moment(text, image):
    moment = slack_assets.detect_moment(text)
    assert moment is not None
    assert moment["image"] == image


def test_detect_moment_ignores_normal_chat():
    assert slack_assets.detect_moment("how was your day") is None
    assert slack_assets.detect_moment("the noodles were great") is None


def test_allow_asset_respects_cooldown(monkeypatch):
    state = {}
    monkeypatch.setattr(slack_assets, "get_state", lambda key, default="": state.get(key, default))
    monkeypatch.setattr(slack_assets, "set_state", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(
        slack_assets, "ASSETS", {"enabled": True, "cooldown_seconds": 900, "chance": 1.0}
    )
    clock = {"now": 1000.0}
    monkeypatch.setattr(slack_assets.time, "time", lambda: clock["now"])

    assert slack_assets.allow_asset("C1") is True
    assert slack_assets.allow_asset("C1") is False
    clock["now"] = 2000.0
    assert slack_assets.allow_asset("C1") is True


def test_allow_asset_respects_chance_and_disable(monkeypatch):
    monkeypatch.setattr(slack_assets, "get_state", lambda key, default="": default)
    monkeypatch.setattr(slack_assets, "set_state", lambda key, value: None)
    monkeypatch.setattr(
        slack_assets, "ASSETS", {"enabled": True, "cooldown_seconds": 0, "chance": 0.0}
    )
    assert slack_assets.allow_asset("C1") is False

    monkeypatch.setattr(slack_assets, "ASSETS", {"enabled": False, "chance": 1.0})
    assert slack_assets.allow_asset("C1") is False


def test_upload_asset_uses_files_upload_v2(monkeypatch, tmp_path):
    monkeypatch.setattr(slack_assets, "ASSETS_DIR", tmp_path)
    (tmp_path / "x.gif").write_bytes(b"GIF89a")
    client = MagicMock()

    assert slack_assets.upload_asset(client, "C1", "T1", "x.gif", initial_comment="hi") is True
    kwargs = client.files_upload_v2.call_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["thread_ts"] == "T1"
    assert kwargs["initial_comment"] == "hi"


def test_upload_asset_missing_file_falls_back_to_url(monkeypatch, tmp_path):
    monkeypatch.setattr(slack_assets, "ASSETS_DIR", tmp_path)
    client = MagicMock()
    assert slack_assets.upload_asset(client, "C1", "T1", "nope.gif") is True
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["blocks"][0]["image_url"].endswith("/assets/goose/nope.gif")


def test_upload_asset_falls_back_to_url_when_upload_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(slack_assets, "ASSETS_DIR", tmp_path)
    (tmp_path / "x.gif").write_bytes(b"GIF89a")
    client = MagicMock()
    client.files_upload_v2.side_effect = RuntimeError("missing_scope")

    assert slack_assets.upload_asset(client, "C1", "T1", "x.gif", "hi") is True
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["thread_ts"] == "T1"
    assert kwargs["text"] == "hi"
    assert kwargs["blocks"][0]["type"] == "image"
    assert kwargs["blocks"][0]["image_url"].endswith("/assets/goose/x.gif")
    assert kwargs["blocks"][1]["elements"][0]["text"] == "hi"


def test_quote_request_falls_back_to_text(monkeypatch):
    monkeypatch.setattr(handlers, "ASSETS", {"enabled": True})
    monkeypatch.setattr(handlers.slack_assets, "is_quote_request", lambda text: True)
    monkeypatch.setattr(
        handlers.slack_assets,
        "pick_quote",
        lambda: {"text": "Stay lazy.", "who": "Sifu Cat", "image": "quotes/x.png"},
    )
    monkeypatch.setattr(handlers.slack_assets, "upload_asset", lambda *a, **k: False)
    monkeypatch.setattr(handlers, "_claim_reply_trigger", lambda channel, ts: True)
    said = []
    monkeypatch.setattr(handlers, "_safe_say", lambda say, **kwargs: said.append(kwargs["text"]))

    handled = handlers._maybe_send_asset(
        MagicMock(), MagicMock(), "C1", "tell me a quote", "T1", "M1"
    )
    assert handled is True
    assert said == ['"Stay lazy." - Sifu Cat']


def test_quote_request_uploads_its_image(monkeypatch):
    monkeypatch.setattr(handlers, "ASSETS", {"enabled": True})
    monkeypatch.setattr(handlers.slack_assets, "is_quote_request", lambda text: True)
    monkeypatch.setattr(
        handlers.slack_assets,
        "pick_quote",
        lambda: {"text": "Stay lazy.", "who": "Sifu Cat", "image": "quotes/x.png"},
    )
    uploaded = []
    monkeypatch.setattr(
        handlers.slack_assets,
        "upload_asset",
        lambda *a, **k: uploaded.append((a, k)) or True,
    )
    monkeypatch.setattr(handlers, "_claim_reply_trigger", lambda channel, ts: True)

    assert handlers._maybe_send_asset(MagicMock(), MagicMock(), "C1", "tell me a quote", "T1", "M1")
    assert uploaded and uploaded[0][1]["initial_comment"] == '"Stay lazy." - Sifu Cat'


def test_rage_moment_uploads_when_allowed(monkeypatch):
    monkeypatch.setattr(handlers, "ASSETS", {"enabled": True})
    monkeypatch.setattr(handlers.slack_assets, "is_quote_request", lambda text: False)
    monkeypatch.setattr(
        handlers.slack_assets,
        "detect_moment",
        lambda text: {"image": "rage/x.gif", "line": "aiya", "kind": "rage"},
    )
    monkeypatch.setattr(handlers.slack_assets, "allow_asset", lambda channel: True)
    monkeypatch.setattr(handlers, "_claim_reply_trigger", lambda channel, ts: True)
    uploaded = []
    monkeypatch.setattr(
        handlers.slack_assets,
        "upload_asset",
        lambda *a, **k: uploaded.append((a, k)) or True,
    )

    assert handlers._maybe_send_asset(MagicMock(), MagicMock(), "C1", "msg is bad", "T1", "M1")
    assert uploaded and uploaded[0][1]["initial_comment"] == "aiya"


def test_rage_moment_respects_cooldown(monkeypatch):
    monkeypatch.setattr(handlers, "ASSETS", {"enabled": True})
    monkeypatch.setattr(handlers.slack_assets, "is_quote_request", lambda text: False)
    monkeypatch.setattr(
        handlers.slack_assets,
        "detect_moment",
        lambda text: {"image": "rage/x.gif", "line": "aiya", "kind": "rage"},
    )
    monkeypatch.setattr(handlers.slack_assets, "allow_asset", lambda channel: False)
    upload = MagicMock()
    monkeypatch.setattr(handlers.slack_assets, "upload_asset", upload)

    assert handlers._maybe_send_asset(MagicMock(), MagicMock(), "C1", "msg is bad", "T1", "M1") is False
    upload.assert_not_called()


def test_normal_text_is_not_handled(monkeypatch):
    monkeypatch.setattr(handlers, "ASSETS", {"enabled": True})
    monkeypatch.setattr(handlers.slack_assets, "is_quote_request", lambda text: False)
    monkeypatch.setattr(handlers.slack_assets, "detect_moment", lambda text: None)
    assert handlers._maybe_send_asset(MagicMock(), MagicMock(), "C1", "hello", "T1", "M1") is False
