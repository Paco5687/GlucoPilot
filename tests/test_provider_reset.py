"""Security-question password reset: all three answers, hashed, throttled.

With no email round-trip, the throttle is the only brake on guessing — these
tests pin that it engages, that answers are never stored in the clear, and
that a partial guess is worthless.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import HTTPException

from server import auth, db
from server.migrations import run_migrations

pytestmark = pytest.mark.risk_critical

QUESTIONS = [
    {"question": "Name of my first clinic?", "answer": "Riverside"},
    {"question": "Street I grew up on?", "answer": "Maple Ave"},
    {"question": "First concert I attended?", "answer": "The Eagles"},
]


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    token, _ = auth.create_provider_invite()
    auth.accept_provider_invite(token, "dr-reset", "Original-Pass-1", QUESTIONS)
    return path


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _reset(username, answers, password):
    return asyncio.run(auth.provider_reset(_FakeRequest({
        "username": username, "answers": answers, "password": password,
    })))


def test_answers_are_stored_hashed_never_plaintext(database):
    provider = auth.load_providers()[0]
    stored = json.dumps(provider["security_questions"])
    assert "Riverside" not in stored
    assert "Maple" not in stored
    assert all(pair["answer_hash"].startswith("$argon2") for pair in provider["security_questions"])


def test_correct_answers_reset_the_password(database):
    # Case and spacing must not matter — people don't remember formatting.
    _reset("DR-RESET", ["  riverside ", "maple  ave", "the eagles"], "Brand-New-Pass-1")

    provider = auth.load_providers()[0]
    assert auth.verify_password("Brand-New-Pass-1", provider["password_hash"])
    assert not auth.verify_password("Original-Pass-1", provider["password_hash"])


def test_two_of_three_is_worthless(database):
    with pytest.raises(HTTPException) as denied:
        _reset("dr-reset", ["Riverside", "Maple Ave", "wrong guess"], "Attacker-Pass-1")
    assert denied.value.status_code == 400
    assert auth.verify_password("Original-Pass-1", auth.load_providers()[0]["password_hash"])


def test_lockout_engages_after_repeated_failures(database):
    for _ in range(auth.RESET_MAX_FAILS):
        with pytest.raises(HTTPException):
            _reset("dr-reset", ["a", "b", "c"], "Attacker-Pass-1")

    # Even the CORRECT answers are refused while locked.
    with pytest.raises(HTTPException) as locked:
        _reset("dr-reset", ["Riverside", "Maple Ave", "The Eagles"], "Brand-New-Pass-1")
    assert locked.value.status_code == 429

    # After the lockout expires, a legitimate reset works and clears the state.
    throttle = auth._reset_throttle()
    throttle["dr-reset"]["locked_until"] = int(time.time()) - 1
    auth.set_setting("provider_reset_throttle", json.dumps(throttle))
    _reset("dr-reset", ["Riverside", "Maple Ave", "The Eagles"], "Brand-New-Pass-1")
    assert auth._reset_throttle() == {}


def test_unknown_user_gets_the_same_generic_error(database):
    with pytest.raises(HTTPException) as unknown:
        _reset("nobody", ["a", "b", "c"], "Whatever-Pass-1")
    assert unknown.value.status_code == 400
    assert "don't match" in unknown.value.detail


def test_questions_endpoint_reports_unavailable_without_questions(database):
    auth.save_providers([{"username": "legacy-bill", "password_hash": "x"}])
    assert auth.provider_reset_questions("legacy-bill") == {"questions": None}
    assert auth.provider_reset_questions("nobody") == {"questions": None}

    with pytest.raises(HTTPException):
        _reset("legacy-bill", ["a", "b", "c"], "Whatever-Pass-1")


def test_signup_rejects_weak_question_sets(database):
    token, _ = auth.create_provider_invite()
    cases = [
        QUESTIONS[:2],                                             # too few
        [*QUESTIONS[:2], {"question": "Short?", "answer": "ok"}],  # question too short
        [*QUESTIONS[:2], {"question": "A question long enough?", "answer": "x"}],  # answer too short
        [QUESTIONS[0], QUESTIONS[0], QUESTIONS[1]],                # duplicates
        None,                                                      # missing entirely
    ]
    for questions in cases:
        with pytest.raises(HTTPException):
            auth.accept_provider_invite(token, "dr-weak", "Long-Enough-1", questions)
    # None of the failures burned the invite.
    assert len(auth.load_invites()) == 1


def test_rekeying_questions_requires_current_password(database):
    new_questions = [
        {"question": "My medical school city?", "answer": "Boston"},
        {"question": "My first pet's name?", "answer": "Comet"},
        {"question": "My residency hospital?", "answer": "St. Mary"},
    ]

    class _SessionRequest(_FakeRequest):
        session = {"logged_in": True, "role": "provider", "provider_name": "dr-reset"}

    with pytest.raises(HTTPException) as wrong:
        asyncio.run(auth.provider_set_questions(_SessionRequest({
            "current_password": "not-the-password", "questions": new_questions,
        })))
    assert wrong.value.status_code == 403

    asyncio.run(auth.provider_set_questions(_SessionRequest({
        "current_password": "Original-Pass-1", "questions": new_questions,
    })))
    stored = auth.load_providers()[0]["security_questions"]
    assert [pair["question"] for pair in stored] == [pair["question"] for pair in new_questions]
