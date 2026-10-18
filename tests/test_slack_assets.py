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


def test_generate_quote_always_attaches_an_image(monkeypatch):
    monkeypatch.setattr(slack_assets, "_quote_history", lambda: [])
    monkeypatch.setattr(slack_assets, "_remember_quote", lambda text: None)
    assert slack_assets.generate_quote()["image"]


def test_goose_quote_uses_a_duck_clip(monkeypatch):
    monkeypatch.setattr(slack_assets, "_quote_history", lambda: [])
    monkeypatch.setattr(slack_assets, "_remember_quote", lambda text: None)
    monkeypatch.setattr(slack_assets, "_pick_quote_author", lambda: "Goose Lee")
    monkeypatch.setattr(slack_assets, "GOOSE_IMAGES", ["quotes/goose_typing.gif"])
    assert slack_assets.generate_quote()["image"] == "quotes/goose_typing.gif"


def test_sifu_quote_uses_an_unpaired_still(monkeypatch):
    monkeypatch.setattr(slack_assets, "_quote_history", lambda: [])
    monkeypatch.setattr(slack_assets, "_remember_quote", lambda text: None)
    monkeypatch.setattr(slack_assets, "_pick_quote_author", lambda: "Sifu Cat")
    monkeypatch.setattr(slack_assets, "_compose_quote", lambda: ("Stay lazy.", "wise"))
    monkeypatch.setattr(slack_assets, "WISE_IMAGES", ["quotes/paw_tzu_wise.png"])
    assert slack_assets.generate_quote()["image"] == "quotes/paw_tzu_wise.png"


def test_food_quote_prefers_a_sassy_still(monkeypatch):
    monkeypatch.setattr(slack_assets, "_quote_history", lambda: [])
    monkeypatch.setattr(slack_assets, "_remember_quote", lambda text: None)
    monkeypatch.setattr(slack_assets, "_pick_quote_author", lambda: "Sifu Cat")
    monkeypatch.setattr(
        slack_assets, "_compose_quote", lambda: ("The last dumpling forgives.", "wise")
    )
    monkeypatch.setattr(slack_assets, "SASSY_IMAGES", ["quotes/pai_mei_smirk.png"])
    assert slack_assets.generate_quote()["image"] == "quotes/pai_mei_smirk.png"


def test_generated_quotes_are_never_reused(monkeypatch):
    state = {}
    monkeypatch.setattr(
        slack_assets, "get_state", lambda key, default="": state.get(key, default)
    )
    monkeypatch.setattr(
        slack_assets, "set_state", lambda key, value: state.__setitem__(key, value)
    )
    quotes = [slack_assets.generate_quote()["text"] for _ in range(30)]
    assert len(set(quotes)) == len(quotes)
    assert slack_assets._quote_history()


def test_every_served_image_exists():
    images = set(slack_assets.WISE_IMAGES + slack_assets.SASSY_IMAGES + slack_assets.GOOSE_IMAGES)
    images.update(moment[0] for _, moment in slack_assets._RAGE_RULES)
    images.update({"victory/waddle.gif"})
    for image in images:
        assert (slack_assets.ASSETS_DIR / image).is_file(), image


def test_format_quote():
    assert slack_assets.format_quote({"text": "Stay lazy.", "who": "Sifu Cat"}) == '"Stay lazy." - Sifu Cat'


@pytest.mark.parametrize(
    "text,image",
    [
        ("msg is terrible", "rage/msg_sacred.gif"),
        ("boba is overrated", "rage/food_slander.gif"),
        ("bruce lee is trash", "rage/bruce_lee.gif"),
        ("goose show me your nunchaku", "rage/nunchaku_fail.gif"),
        ("kung fu is fake", "rage/bruce_lee.gif"),
        ("hot pot is overrated", "rage/food_slander.gif"),
        ("the ci failed again", "quotes/shifu_deep_sigh.png"),
        ("emotional damage", "quotes/shifu_frustrated.png"),
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
        "generate_quote",
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
        "generate_quote",
        lambda: {"text": "Stay lazy.", "who": "Sifu Cat", "image": "quotes/x.png"},
    )
    uploaded = []
    monkeypatch.setattr(
        handlers.slack_assets,
        "upload_asset",
        lambda *a, **k: uploaded.append((a, k)) or True,
    )
    monkeypatch.setattr(handlers, "_claim_reply_trigger", lambda channel, ts: True)
    monkeypatch.setattr(handlers.slack_assets, "allow_quote", lambda channel: True)

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


def test_rage_detector_ignores_negated_slander():
    assert slack_assets.detect_moment("boba is not bad at all") is None
    assert slack_assets.detect_moment("MSG is not bad") is None
    assert slack_assets.detect_moment("the noodles are not terrible") is None


def test_rage_detector_still_fires_through_ngl():
    assert slack_assets.detect_moment("not gonna lie, boba is bad") is not None


def test_quote_cooldown_blocks_back_to_back_requests(monkeypatch):
    store = {}
    monkeypatch.setattr(slack_assets, "get_state", lambda k, d="": store.get(k, d))
    monkeypatch.setattr(slack_assets, "set_state", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(slack_assets, "_QUOTE_COOLDOWN_SECONDS", 20.0)
    assert slack_assets.allow_quote("C1") is True
    assert slack_assets.allow_quote("C1") is False
    assert slack_assets.allow_quote("C2") is True

def test_quote_request_ignores_ordinary_quote_talk():
    assert slack_assets.is_quote_request("tell me a quote") is True
    assert slack_assets.is_quote_request("get the quote for the API") is False
    assert slack_assets.is_quote_request("fetch the quote from the vendor") is False
    assert slack_assets.is_quote_request("hit the quote button please") is False


def test_food_plurals_still_pick_sassy(monkeypatch):
    monkeypatch.setattr(slack_assets, "SASSY_IMAGES", ["sassy.png"])
    monkeypatch.setattr(slack_assets, "WISE_IMAGES", ["wise.png"])
    assert slack_assets.pick_quote_image("A plate of dumplings.", "wise", "Sifu Cat") == "sassy.png"
