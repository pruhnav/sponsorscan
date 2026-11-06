"""Telegram alerts, with Telegram replaced by a fake. Nothing touches the network."""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "send_telegram", REPO / "scripts" / "send_telegram.py")
telegram = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telegram)

TOKEN = "123456:secret-token-value"


class FakeTelegram:
    """Records calls and answers like the Bot API."""

    def __init__(self, errors=None, updates=None):
        self.calls, self.errors, self.updates = [], errors or {}, updates or []

    def __call__(self, token, method, payload=None, attempts=3):
        self.calls.append((method, payload or {}))
        if method in self.errors:
            raise telegram.TelegramError(telegram.explain(method, *self.errors[method]))
        return {"getMe": {"username": "sponsorscan_bot"},
                "getChat": {"first_name": "Alex"},
                "getUpdates": self.updates}.get(method, {"message_id": 1})


@pytest.fixture
def fake(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(telegram, "call", fake)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    return fake


def write_csv(path, rows):
    fields = ["company", "title", "location", "url", "resume_fit_score", "lca_certified"]
    lines = [",".join(fields)] + [",".join(str(row.get(f, "")) for f in fields) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_no_new_jobs_sends_nothing_and_succeeds(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("NEW_JOBS_CSV", str(write_csv(tmp_path / "new.csv", [])))
    assert telegram.main([]) == 0
    assert fake.calls == []


def test_new_jobs_go_out_as_one_message_with_open_buttons(fake, tmp_path, monkeypatch, capsys):
    rows = [{"company": "Benchling", "title": "Research Engineer", "location": "San Francisco",
             "url": "https://jobs.example/1", "resume_fit_score": "62", "lca_certified": "14"},
            {"company": "Ramp", "title": "Engineer <Platform>", "url": "javascript:alert(1)"}]
    monkeypatch.setenv("NEW_JOBS_CSV", str(write_csv(tmp_path / "new.csv", rows)))
    monkeypatch.setenv("SPONSORSCAN_DB", str(tmp_path / "absent.db"))
    assert telegram.main([]) == 0
    (method, payload), = fake.calls
    assert method == "sendMessage" and payload["chat_id"] == "42"
    text = payload["text"]
    assert "2 new job matches" in text and "14 certified H-1B filings" in text
    assert '<a href="https://jobs.example/1">Research Engineer</a>' in text
    # Job text is escaped, and a non-web link is never made clickable.
    assert "Engineer &lt;Platform&gt;" in text and "javascript:" not in text
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "Open: Benchling", "url": "https://jobs.example/1"}]]
    assert TOKEN not in capsys.readouterr().out


def test_long_digests_split_under_the_length_limit():
    rows = [{"company": "C" * 40, "title": "T" * 200, "url": "https://x.example/" + "p" * 300}] * 15
    messages = telegram.build_messages(rows, "Alex", "48", [])
    assert len(messages) > 1
    assert all(len(message) <= telegram.MAX_MESSAGE for message in messages)


def test_check_confirms_token_and_chat_without_sending(fake, capsys):
    assert telegram.main(["--check"]) == 0
    assert [method for method, _ in fake.calls] == ["getMe", "getChat"]
    assert "Nothing was sent" in capsys.readouterr().out


def test_find_chat_prints_ids_from_recent_messages(monkeypatch, capsys):
    updates = [{"message": {"chat": {"id": 987654, "first_name": "Alex"}}}]
    monkeypatch.setattr(telegram, "call", FakeTelegram(updates=updates))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert telegram.main(["--find-chat"]) == 0
    assert "TELEGRAM_CHAT_ID=987654  (Alex)" in capsys.readouterr().out


@pytest.mark.parametrize("method,error,advice", [
    ("getMe", (401, "Unauthorized"), "@BotFather"),
    ("getChat", (400, "Bad Request: chat not found"), "--find-chat"),
])
def test_failures_say_what_to_do(monkeypatch, capsys, method, error, advice):
    monkeypatch.setattr(telegram, "call", FakeTelegram(errors={method: error}))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert telegram.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert advice in err and TOKEN not in err


def test_missing_token_is_a_clear_failure(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert telegram.main(["--check"]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
