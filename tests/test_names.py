"""Public boards must never ping members, and names must never be ambiguous."""

import re

import pytest

import crumbs
import names

MENTION = re.compile(r"<[@!#]")


class FakeClient:
    def __init__(self, users):
        self.users = users
        self.calls = 0

    def users_info(self, user):
        self.calls += 1
        if user not in self.users:
            raise RuntimeError("user_not_found")
        return {"ok": True, "user": self.users[user]}


def _u(display, real, name="h", **extra):
    return {"name": name, "profile": {"display_name": display, "real_name": real}, **extra}


@pytest.fixture(autouse=True)
def _fresh_cache():
    names.clear_cache()
    yield
    names.clear_cache()


def _entry(uid, crumbs_=10.0):
    return {"user_id": uid, "level": 2, "title": "Paddler", "crumbs": crumbs_, "week_crumbs": crumbs_}


def test_boards_contain_no_mentions():
    client = FakeClient({"U1": _u("Ana", "Ana Ruiz"), "U2": _u("Bo", "Bo Lee")})
    entries = [_entry("U1"), _entry("U2")]
    labels = names.board_labels(client, ["U1", "U2"])
    for text in (
        crumbs.format_pond_leaderboard(entries, names=labels),
        crumbs.format_all_time(entries, names=labels),
        crumbs.format_weekly_report("2026-W45", entries, names=labels),
    ):
        assert not MENTION.search(text)
        assert "Ana" in text and "Bo" in text


def test_formatters_never_mention_even_without_names():
    text = crumbs.format_pond_leaderboard([_entry("U0123ABCD")])
    assert "<@" not in text
    assert "ABCD" in text


def test_falls_back_to_real_name_then_handle():
    client = FakeClient({"U1": _u("", "Ana Ruiz"), "U2": _u("", "", name="bolee")})
    labels = names.board_labels(client, ["U1", "U2"])
    assert labels == {"U1": "Ana Ruiz", "U2": "bolee"}


def test_duplicate_display_names_get_real_names():
    client = FakeClient({"U1": _u("Sam", "Sam Park"), "U2": _u("sam", "Samir Khan")})
    labels = names.board_labels(client, ["U1", "U2"])
    assert labels["U1"] == "Sam (Sam Park)"
    assert labels["U2"] == "sam (Samir Khan)"


def test_full_copycats_get_id_tail():
    client = FakeClient({"U1AAAA": _u("Sam", "Sam Park"), "U2BBBB": _u("Sam", "Sam Park")})
    labels = names.board_labels(client, ["U1AAAA", "U2BBBB"])
    assert labels["U1AAAA"] != labels["U2BBBB"]
    assert labels["U1AAAA"].endswith("AAAA") and labels["U2BBBB"].endswith("BBBB")


def test_invisible_characters_and_spacing_do_not_fake_uniqueness():
    client = FakeClient({"U1AAAA": _u("Sam", "Sam Park"), "U2BBBB": _u("S​am ", "Sam Park")})
    labels = names.board_labels(client, ["U1AAAA", "U2BBBB"])
    assert labels["U1AAAA"] != labels["U2BBBB"]


def test_member_cannot_pose_as_the_bot():
    client = FakeClient({
        "UBOT": _u("Goose Lee", "Goose Lee", is_bot=True),
        "U1": _u("goose lee", "Mallory Q"),
    })
    labels = names.board_labels(client, ["U1"], reserved_ids=["UBOT"])
    assert labels["U1"] == "goose lee (Mallory Q)"


def test_names_cannot_inject_mentions_or_links():
    client = FakeClient({"U1": _u("<!channel> <@U999>", "x")})
    label = names.board_labels(client, ["U1"])["U1"]
    assert "<" not in label and ">" not in label


def test_lookup_failure_and_deleted_users():
    client = FakeClient({"U2": _u("Old", "Old Name", deleted=True)})
    labels = names.board_labels(client, ["U1XYZ1", "U2"])
    assert labels["U1XYZ1"] == names.fallback_label("U1XYZ1")
    assert labels["U2"] == "former member"


def test_profiles_are_cached():
    client = FakeClient({"U1": _u("Ana", "Ana Ruiz")})
    names.board_labels(client, ["U1"])
    names.board_labels(client, ["U1"])
    assert client.calls == 1


def test_no_client_still_safe():
    assert names.board_labels(None, ["U1ABCD"]) == {"U1ABCD": names.fallback_label("U1ABCD")}


def test_big_cold_board_uses_one_bulk_fetch():
    users = {f"U{i:04d}": _u(f"P{i}", f"Person {i}") for i in range(40)}

    class BulkClient(FakeClient):
        list_calls = 0

        def users_list(self, limit=200, cursor=None):
            BulkClient.list_calls += 1
            return {"members": [dict(v, id=k) for k, v in self.users.items()], "response_metadata": {}}

    client = BulkClient(users)
    labels = names.board_labels(client, list(users))
    assert BulkClient.list_calls == 1 and client.calls == 0
    assert labels["U0007"] == "P7"
