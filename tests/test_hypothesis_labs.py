import asyncio
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from server import db, hypotheses, hypothesis_labs
from server.config import OWNER_EMAIL
from server.migrations import run_migrations


@pytest.fixture
def lab_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database)
    run_migrations(database)
    return database


@pytest.fixture(autouse=True)
def fresh_queue_state(monkeypatch):
    monkeypatch.setattr(hypothesis_labs, "_locks", {})
    monkeypatch.setattr(hypothesis_labs, "_failed_at", {})
    monkeypatch.setattr(hypothesis_labs, "_queue_task", None)


def _lab(name, value, collected, *, unit="", low=None, high=None, flag="", original_value=None,
         record_id="rec_synthetic", **extra):
    data = {
        "test_name": name,
        "value": value,
        "unit": unit,
        "collected_date": collected,
        "flag": flag,
        "record_id": record_id,
        "owner_email": OWNER_EMAIL,
        **extra,
    }
    if low is not None:
        data["reference_low"] = low
    if high is not None:
        data["reference_high"] = high
    if original_value is not None:
        data["original_value"] = original_value
    return db.create_entity("LabResult", data)


OWNER = {"kind": "patient", "role": "admin", "label": "Synthetic Owner"}


def _hypothesis(title="Synthetic overactive gland", evidence=None):
    return hypotheses.SqliteHypothesisRepository().create(
        hypotheses.CreateHypothesisBody(title=title, evidence=evidence or []),
        OWNER,
    )


class FakeModel:
    """Answers the triage and the test-list prompts from scripted replies."""

    def __init__(self, tests, *, lab_tests_used=True, checked_by="blood tests", fail_on=None):
        self.tests = tests
        self.lab_tests_used = lab_tests_used
        self.checked_by = checked_by
        self.fail_on = fail_on  # a prompt substring that makes the call fail
        self.calls = []

    async def __call__(self, prompt, schema, max_tokens=4000, images=None, tier="default", site="",
                       temperature=None):
        self.calls.append({"prompt": prompt, "schema": schema, "site": site, "temperature": temperature})
        await asyncio.sleep(0)  # let a concurrent lookup try to run
        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError("Local LLM unreachable")
        if schema is hypothesis_labs.TRIAGE_SCHEMA:
            return {"lab_tests_used": self.lab_tests_used, "usually_checked_by": self.checked_by}
        return {"tests": self.tests}


def _test(test, if_true="high", aliases=(), other_cause="", why="Synthetic reason."):
    return {"test": test, "aliases": list(aliases), "if_true": if_true, "other_cause": other_cause, "why": why}


def _suggest(monkeypatch, model, hypothesis_id, **kwargs):
    monkeypatch.setattr(hypothesis_labs.llm, "invoke_llm", model)
    return asyncio.run(hypothesis_labs.suggest(hypothesis_id, **kwargs))


def test_values_come_from_lab_rows_and_roles_follow_direction(lab_database, monkeypatch):
    _lab("TSH", 0.9, "2026-01-10", unit="mIU/L", low=0.4, high=4.5)
    latest_tsh = _lab("Thyroid Stimulating Hormone", 0.1, "2026-06-01", unit="mIU/L", low=0.4, high=4.5)
    _lab("Free T4", 1.1, "2026-06-01", unit="ng/dL", low=0.8, high=1.8)
    _lab("Synthetic Marker", 3.0, "2026-06-01", unit="U")  # no range, no flag
    hypothesis = _hypothesis()
    model = FakeModel([
        _test("TSH", if_true="low", aliases=["thyrotropin"]),
        _test("Free thyroxine", if_true="high", aliases=["FT4"]),
        _test("Synthetic Marker", if_true="high"),
        _test("Synthetic Receptor Antibody", if_true="positive", why="Specific for the synthetic condition."),
    ])

    result = _suggest(monkeypatch, model, hypothesis["id"])

    labs = {item["test"]: item for item in result["labs"]}
    tsh = labs["TSH"]
    # Two printed names for one test are read together; the latest draw leads.
    assert tsh["evidence"]["source_id"] == latest_tsh["id"]
    assert tsh["value"] == "0.1 mIU/L"
    assert tsh["flag"] == "low"
    assert tsh["earlier"] == [{"value": "0.9 mIU/L", "date": "2026-01-10"}]
    assert tsh["suggested_role"] == "supporting"
    assert tsh["evidence"]["source_link"]["href"] == "/api/records/file/rec_synthetic"
    assert "0.1 mIU/L on 2026-06-01" in tsh["evidence"]["summary"]
    assert labs["Free T4"]["suggested_role"] == "opposing"  # normal where high was expected
    assert labs["Synthetic Marker"]["suggested_role"] is None  # nothing to judge the value by
    assert [item["test"] for item in result["not_on_file"]] == ["Synthetic Receptor Antibody"]
    missing = result["not_on_file"][0]["evidence"]
    assert missing["role"] == "missing" and missing["source_kind"] == "missing"
    assert missing["summary"].startswith("No Synthetic Receptor Antibody result in the app's lab results.")
    # Every call is local-only and deterministic.
    assert {call["site"] for call in model.calls} == {hypothesis_labs.SITE}
    assert {call["temperature"] for call in model.calls} == {0}
    assert "0.1" not in model.calls[1]["prompt"]  # the model never sees values


def test_suggestions_save_as_a_valid_evidence_revision(lab_database, monkeypatch):
    _lab("TSH", 0.1, "2026-06-01", unit="mIU/L", low=0.4, high=4.5)
    hypothesis = _hypothesis()
    model = FakeModel([_test("TSH", if_true="low"), _test("Synthetic Receptor Antibody", if_true="positive")])
    result = _suggest(monkeypatch, model, hypothesis["id"])

    chosen = [
        {**result["labs"][0]["evidence"], "role": result["labs"][0]["suggested_role"]},
        result["not_on_file"][0]["evidence"],
    ]
    saved = hypotheses.SqliteHypothesisRepository().revise_evidence(
        hypothesis["id"],
        hypotheses.ReviseEvidenceBody(reason="Synthetic accept", evidence=chosen),
        OWNER,
    )
    assert len(saved["evidence_by_role"]["supporting"]) == 1
    assert len(saved["evidence_by_role"]["missing"]) == 1

    again = _suggest(monkeypatch, model, hypothesis["id"])
    assert again["labs"][0]["already_added"] is True
    assert again["not_on_file"][0]["already_added"] is True


def test_matching_tolerates_spelling_and_assay_variants(lab_database, monkeypatch):
    _lab("DHEA-S", 120, "2026-03-01", unit="ug/dL", low=35, high=430)
    _lab("Hs CRP, Female", 0.2, "2026-03-01", unit="mg/L", low=0, high=1)
    _lab("Triiodothyronine (T3), Free", 2.4, "2026-03-01", unit="pg/mL", low=2.8, high=3.5)
    _lab("Free T3", 3.0, "2025-12-01", unit="pg/mL", low=2.3, high=4.2)
    hypothesis = _hypothesis()
    model = FakeModel([
        _test("DHEAS"),
        _test("C-Reactive Protein", aliases=["CRP"]),
        _test("Free T3", if_true="high"),
        _test("FT3", if_true="high"),  # the same analyte again is not repeated
        _test("Thyroid Ultrasound"),  # imaging never becomes a "not on file" lab
        _test("Lipid Panel"),
    ])

    result = _suggest(monkeypatch, model, hypothesis["id"])

    assert [item["test"] for item in result["labs"]] == ["DHEA-S", "Hs CRP, Female", "Free T3"]
    free_t3 = result["labs"][2]
    assert free_t3["draws"] == 2 and free_t3["value"] == "2.4 pg/mL"
    assert result["not_on_file"] == []


def test_other_cause_must_be_a_listed_condition_or_medication(lab_database, monkeypatch):
    _lab("TSH", 0.1, "2026-06-01", unit="mIU/L", low=0.4, high=4.5)
    _lab("Free T4", 2.5, "2026-06-01", unit="ng/dL", low=0.8, high=1.8)
    db.create_entity("Diagnosis", {"name": "Synthetic Thyroiditis", "status": "active", "owner_email": OWNER_EMAIL})
    db.create_entity("Medication", {"name": "Synthetic Thyroid Tablet", "dose": "30 mcg", "status": "active",
                                    "owner_email": OWNER_EMAIL})
    db.create_entity("Medication", {"name": "Stopped Synthetic Drug", "status": "stopped", "owner_email": OWNER_EMAIL})
    hypothesis = _hypothesis()
    model = FakeModel([
        _test("TSH", if_true="low", other_cause="Synthetic Thyroid Tablet"),
        _test("Free T4", if_true="high", other_cause="pregnancy"),
    ])

    result = _suggest(monkeypatch, model, hypothesis["id"])

    tsh, free_t4 = result["labs"]
    assert tsh["other_cause"] == "Synthetic Thyroid Tablet"
    assert tsh["suggested_role"] is None  # the medication alone could explain it
    assert "Also affected by: Synthetic Thyroid Tablet." in tsh["evidence"]["summary"]
    assert free_t4["other_cause"] == ""  # not on the lists given to the model
    assert free_t4["suggested_role"] == "supporting"
    prompt = model.calls[0]["prompt"]
    assert "Synthetic Thyroiditis" in prompt and "Synthetic Thyroid Tablet 30 mcg" in prompt
    assert "Stopped Synthetic Drug" not in prompt


def test_clinical_diagnoses_get_no_lab_list_unless_asked(lab_database, monkeypatch):
    _lab("TSH", 2.0, "2026-06-01", unit="mIU/L", low=0.4, high=4.5)
    hypothesis = _hypothesis("Synthetic clinical syndrome")
    model = FakeModel([_test("TSH", if_true="any")], lab_tests_used=False, checked_by="examination")

    result = _suggest(monkeypatch, model, hypothesis["id"])
    assert result["lab_tests_used"] is False
    assert result["usually_checked_by"] == "examination"
    assert result["labs"] == [] and len(model.calls) == 1

    anyway = _suggest(monkeypatch, model, hypothesis["id"], skip_triage=True)
    assert anyway["lab_tests_used"] is True
    assert [item["test"] for item in anyway["labs"]] == ["TSH"]
    # The stored triage answer is reused; only the test list was asked for.
    assert [call["schema"] for call in model.calls] == [hypothesis_labs.TRIAGE_SCHEMA, hypothesis_labs.TESTS_SCHEMA]


def test_censored_values_keep_their_sign(lab_database, monkeypatch):
    _lab("Synthetic Antibody", 1.0, "2026-06-01", unit="IU/mL", original_value="<1.0", high=5)
    _lab("Synthetic Pending Marker", 4.0, "2026-06-01", unit="U", original_value="", low=1, high=10)
    hypothesis = _hypothesis()
    model = FakeModel([_test("Synthetic Antibody", if_true="positive"), _test("Synthetic Pending Marker")])

    result = _suggest(monkeypatch, model, hypothesis["id"])

    antibody, marker = result["labs"]
    assert antibody["value"] == "<1.0 IU/mL"
    assert antibody["suggested_role"] == "opposing"
    assert marker["value"] == "4 U"  # an empty original value never blanks the number


@pytest.mark.parametrize(
    ("if_true", "flag", "role"),
    [
        ("high", "high", "supporting"),
        ("high", "normal", "opposing"),
        ("high", "low", "opposing"),
        ("low", "l", "supporting"),
        ("positive", "high", "supporting"),
        ("positive", "within_range", "opposing"),
        ("negative", "normal", "supporting"),
        ("negative", "abnormal", "opposing"),
        ("high", "abnormal", None),
        ("high", "needs_improvement", None),
        ("high", "", None),
        ("any", "high", None),
    ],
)
def test_suggested_role(if_true, flag, role):
    assert hypothesis_labs.suggested_role(if_true, flag) == role


def _session(secret, role):
    payload = base64.b64encode(json.dumps({"logged_in": True, "role": role}).encode())
    return TimestampSigner(secret).sign(payload).decode()


def test_only_the_owner_can_request_suggestions(lab_database, monkeypatch):
    hypothesis = _hypothesis()
    monkeypatch.setattr(hypothesis_labs.llm, "invoke_llm", FakeModel([], lab_tests_used=False))
    secret = "synthetic-lab-suggestion-secret"
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secret)
    app.include_router(hypothesis_labs.router)
    client = TestClient(app)

    client.cookies.set("session", _session(secret, "provider"))
    assert client.post(f"/api/hypotheses/{hypothesis['id']}/lab-suggestions").status_code == 403

    client.cookies.set("session", _session(secret, "admin"))
    response = client.post(f"/api/hypotheses/{hypothesis['id']}/lab-suggestions")
    assert response.status_code == 200
    assert response.json()["lab_tests_used"] is False
    assert client.post("/api/hypotheses/hyp_missing/lab-suggestions").status_code == 404


def test_stored_answers_are_reused_with_current_lab_rows(lab_database, monkeypatch):
    _lab("TSH", 2.0, "2026-01-10", unit="mIU/L", low=0.4, high=4.5)
    hypothesis = _hypothesis()
    model = FakeModel([_test("TSH", if_true="low"), _test("Free T4")])

    first = _suggest(monkeypatch, model, hypothesis["id"])
    assert len(model.calls) == 2 and first["looked_up_at"]
    assert first["labs"][0]["value"] == "2 mIU/L"
    assert [item["test"] for item in first["not_on_file"]] == ["Free T4"]

    # Results uploaded after the lookup show up without asking the model again.
    _lab("TSH", 0.1, "2026-06-01", unit="mIU/L", low=0.4, high=4.5)
    _lab("Free T4", 2.5, "2026-06-01", unit="ng/dL", low=0.8, high=1.8)
    second = _suggest(monkeypatch, model, hypothesis["id"])

    assert len(model.calls) == 2
    labs = {item["test"]: item for item in second["labs"]}
    assert labs["TSH"]["value"] == "0.1 mIU/L"
    assert labs["TSH"]["earlier"] == [{"value": "2 mIU/L", "date": "2026-01-10"}]
    assert labs["Free T4"]["value"] == "2.5 ng/dL"
    assert second["not_on_file"] == []


def test_new_medication_or_model_asks_again(lab_database, monkeypatch):
    db.set_config_value("llm_provider", "local")
    db.set_config_value("local_llm_model", "synthetic-model-a")
    hypothesis = _hypothesis()
    model = FakeModel([_test("TSH")])

    _suggest(monkeypatch, model, hypothesis["id"])
    _suggest(monkeypatch, model, hypothesis["id"])
    assert len(model.calls) == 2

    db.create_entity("Medication", {"name": "Synthetic Tablet", "status": "active", "owner_email": OWNER_EMAIL})
    _suggest(monkeypatch, model, hypothesis["id"])
    assert len(model.calls) == 4
    assert "Synthetic Tablet" in model.calls[2]["prompt"]

    db.set_config_value("local_llm_model", "synthetic-model-b")
    _suggest(monkeypatch, model, hypothesis["id"])
    assert len(model.calls) == 6


def test_concurrent_lookups_share_one_model_answer(lab_database, monkeypatch):
    hypothesis = _hypothesis()
    model = FakeModel([_test("Synthetic Receptor Antibody", if_true="positive")])
    monkeypatch.setattr(hypothesis_labs.llm, "invoke_llm", model)

    async def both():
        return await asyncio.gather(
            hypothesis_labs.suggest(hypothesis["id"]), hypothesis_labs.suggest(hypothesis["id"])
        )

    panel, queue = asyncio.run(both())
    assert len(model.calls) == 2
    assert panel["not_on_file"] == queue["not_on_file"]


def test_queue_looks_up_each_open_hypothesis_once(lab_database, monkeypatch):
    from_panel = _hypothesis("Synthetic panel question")
    waiting = _hypothesis("Synthetic waiting question")
    archived = _hypothesis("Synthetic archived question")
    model = FakeModel([_test("TSH")])
    monkeypatch.setattr(hypothesis_labs.llm, "invoke_llm", model)
    asyncio.run(hypothesis_labs.suggest(archived["id"]))
    hypotheses.SqliteHypothesisRepository().transition(
        archived["id"], hypotheses.TransitionBody(status="archived", reason="Synthetic duplicate"), OWNER
    )
    asyncio.run(hypothesis_labs.suggest(from_panel["id"]))
    model.calls.clear()

    assert asyncio.run(hypothesis_labs.run_queue()) == {"done": 1, "failed": 0}
    assert len(model.calls) == 2
    assert "Synthetic waiting question" in model.calls[0]["prompt"]
    assert db.get_setting(hypothesis_labs.STORE_PREFIX + waiting["id"])
    # A hypothesis that is no longer open loses its stored answers.
    assert db.get_setting(hypothesis_labs.STORE_PREFIX + archived["id"]) == ""
    assert db.get_setting(hypothesis_labs.STORE_PREFIX + from_panel["id"])

    assert asyncio.run(hypothesis_labs.run_queue()) == {"done": 0, "failed": 0}
    assert len(model.calls) == 2


def test_a_failed_lookup_waits_before_the_queue_tries_again(lab_database, monkeypatch):
    _hypothesis("Synthetic answerable question")
    unreachable = _hypothesis("Synthetic unreachable question")
    model = FakeModel([_test("TSH")], fail_on="Synthetic unreachable question")
    monkeypatch.setattr(hypothesis_labs.llm, "invoke_llm", model)

    assert asyncio.run(hypothesis_labs.run_queue()) == {"done": 1, "failed": 1}
    calls = len(model.calls)
    assert asyncio.run(hypothesis_labs.run_queue()) == {"done": 0, "failed": 0}
    assert len(model.calls) == calls

    hypothesis_labs._failed_at[unreachable["id"]] -= hypothesis_labs.RETRY_AFTER + 1
    model.fail_on = None
    assert asyncio.run(hypothesis_labs.run_queue()) == {"done": 1, "failed": 0}


def test_start_queue_runs_in_the_background_only_when_needed(lab_database, monkeypatch):
    model = FakeModel([_test("TSH")])
    monkeypatch.setattr(hypothesis_labs.llm, "invoke_llm", model)

    async def scenario():
        assert hypothesis_labs.start_queue() is False  # nothing open
        _hypothesis()
        assert hypothesis_labs.start_queue() is True
        assert hypothesis_labs.queue_running()
        assert hypothesis_labs.start_queue() is False  # already running
        await hypothesis_labs._queue_task
        assert not hypothesis_labs.queue_running()
        assert hypothesis_labs.start_queue() is False  # everything looked up

    asyncio.run(scenario())
    assert len(model.calls) == 2


def test_scheduler_starts_the_queue_without_waiting_for_it(monkeypatch):
    from server import scheduler

    last = {key: float("inf") for key in scheduler._last_run}
    last["hypothesis_labs"] = float("-inf")
    monkeypatch.setattr(scheduler, "_last_run", last)
    monkeypatch.setattr(scheduler, "WEEKLY_ANALYSES", ())
    started = []
    monkeypatch.setattr(scheduler.hypothesis_labs, "start_queue", lambda: started.append(True) or True)

    asyncio.run(scheduler._tick())

    assert started == [True]


def test_status_counts_labs_not_yet_in_evidence(lab_database, monkeypatch):
    _lab("TSH", 0.1, "2026-06-01", unit="mIU/L", low=0.4, high=4.5)
    _lab("Free T4", 2.5, "2026-06-01", unit="ng/dL", low=0.8, high=1.8)
    looked_up = _hypothesis("Synthetic looked-up question")
    waiting = _hypothesis("Synthetic waiting question")
    clinical = _hypothesis("Synthetic clinical question")
    model = FakeModel([_test("TSH", if_true="low"), _test("Free T4"), _test("Synthetic Receptor Antibody")])
    result = _suggest(monkeypatch, model, looked_up["id"])
    tsh = next(item for item in result["labs"] if item["test"] == "TSH")
    hypotheses.SqliteHypothesisRepository().revise_evidence(
        looked_up["id"],
        hypotheses.ReviseEvidenceBody(reason="Synthetic accept", evidence=[{**tsh["evidence"], "role": "supporting"}]),
        OWNER,
    )
    _suggest(monkeypatch, FakeModel([], lab_tests_used=False, checked_by="examination"), clinical["id"])
    started = []
    monkeypatch.setattr(hypothesis_labs, "start_queue", lambda: started.append(True) or True)

    secret = "synthetic-lab-status-secret"
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secret)
    app.include_router(hypothesis_labs.router)
    client = TestClient(app)

    client.cookies.set("session", _session(secret, "provider"))
    assert client.get("/api/hypothesis-labs").status_code == 403
    assert client.post("/api/hypothesis-labs/queue").status_code == 403
    assert started == []

    client.cookies.set("session", _session(secret, "admin"))
    body = client.get("/api/hypothesis-labs").json()
    assert body["running"] is False
    assert body["pending"] == [waiting["id"]]
    ready = body["ready"][looked_up["id"]]
    assert (ready["lab_tests_used"], ready["labs"], ready["not_on_file"]) == (True, 1, 1)  # TSH is saved
    assert ready["looked_up_at"]
    assert body["ready"][clinical["id"]]["lab_tests_used"] is False
    assert body["ready"][clinical["id"]]["labs"] == 0

    assert client.post("/api/hypothesis-labs/queue").json()["pending"] == [waiting["id"]]
    assert started == [True]
