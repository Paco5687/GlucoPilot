"""Provider sessions: read-only over Emily's data, writable only where intended.

The intended provider-writable surfaces are exactly three: attributable review
actions (covered in test_clinical_reviews), their own Companion threads, and
care-team notes. Everything here pins the scoping rules that keep those from
leaking into each other or into Emily's records.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from server import care_notes, companion, db, functions
from server.migrations import run_migrations

pytestmark = pytest.mark.risk_critical

PROVIDER = "provider:drchen"
OTHER_PROVIDER = "provider:drpatel"


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _handle(body, actor):
    return asyncio.run(companion.handle(body, actor=actor))


class TestCompanionScoping:
    def test_threads_are_partitioned_by_actor(self, database):
        owner_thread = companion._new_thread("morning glucose question", "owner")
        provider_thread = companion._new_thread("clinical review question", PROVIDER)

        owner_ids = {t["id"] for t in _handle({"action": "threads"}, "owner")["threads"]}
        provider_ids = {t["id"] for t in _handle({"action": "threads"}, PROVIDER)["threads"]}

        assert owner_thread["id"] in owner_ids
        assert provider_thread["id"] not in owner_ids
        assert provider_thread["id"] in provider_ids
        assert owner_thread["id"] not in provider_ids

    def test_pre_provider_threads_belong_to_the_owner(self, database):
        """Rows created before the actor field existed carry none: all Emily's."""
        legacy = companion._entity("CompanionThread").create(
            {"title": "old chat", "owner_email": companion.OWNER_EMAIL}
        )
        assert legacy["id"] in {t["id"] for t in _handle({"action": "threads"}, "owner")["threads"]}
        assert legacy["id"] not in {t["id"] for t in _handle({"action": "threads"}, PROVIDER)["threads"]}

    def test_provider_cannot_read_or_delete_owner_thread(self, database):
        thread = companion._new_thread("private conversation", "owner")
        companion._entity("ChatMessage").create({
            "role": "user", "content": "private", "thread_id": thread["id"],
            "owner_email": companion.OWNER_EMAIL,
        })

        assert _handle({"action": "history", "thread_id": thread["id"]}, PROVIDER)["messages"] == []
        _handle({"action": "delete_thread", "thread_id": thread["id"]}, PROVIDER)
        assert companion._entity("CompanionThread").get(thread["id"]) is not None

        # ...and one provider cannot touch another provider's thread either.
        other = companion._new_thread("other clinician chat", OTHER_PROVIDER)
        assert _handle({"action": "history", "thread_id": other["id"]}, PROVIDER)["messages"] == []

    def test_provider_cannot_rename_owner_thread(self, database):
        thread = companion._new_thread("original title", "owner")
        _handle({"action": "rename_thread", "thread_id": thread["id"], "title": "hijacked"}, PROVIDER)
        assert companion._entity("CompanionThread").get(thread["id"])["title"] == "original title"

    def test_memories_are_owner_only(self, database):
        for action in ("memories", "add_memory", "delete_memory"):
            result = _handle({"action": action, "content": "x", "id": "y"}, PROVIDER)
            assert result.get("_status") == 403

        # The owner path is untouched.
        assert _handle({"action": "memories"}, "owner") == {"memories": []}

    def test_audience_note_only_for_providers(self):
        assert companion._audience_note("owner") == ""
        note = companion._audience_note(PROVIDER)
        assert "drchen" in note
        assert "third person" in note

    def test_provider_evidence_command_cannot_open_owner_messages(self, database):
        thread = companion._new_thread("owner thread", "owner")
        message = companion._entity("ChatMessage").create({
            "role": "assistant", "content": "reply", "thread_id": thread["id"],
            "evidence": {"statements": []}, "owner_email": companion.OWNER_EMAIL,
        })
        result = _handle(
            {"action": "evidence_command", "message_id": message["id"], "command": "show"},
            PROVIDER,
        )
        assert result.get("_status") == 404


class TestFunctionsGate:
    def test_provider_allowlist_is_exactly_the_companion(self):
        # Growing this set is a deliberate security decision, not a convenience.
        assert functions.PROVIDER_FUNCTIONS == {"companion"}


class TestCareNotes:
    def _create(self, actor, monkeypatch, **fields):
        monkeypatch.setattr(care_notes, "session_actor", lambda request: actor)
        monkeypatch.setattr(
            care_notes, "session_role",
            lambda request: "admin" if actor == "owner" else "provider",
        )
        body = care_notes.NoteBody(**{"kind": "protocol", "title": "t", "body": "b", **fields})
        return care_notes.create_note(None, body)["note"]

    def _as(self, actor, monkeypatch):
        monkeypatch.setattr(care_notes, "session_actor", lambda request: actor)
        monkeypatch.setattr(
            care_notes, "session_role",
            lambda request: "admin" if actor == "owner" else "provider",
        )

    def test_notes_are_attributed_and_shared(self, database, monkeypatch):
        note = self._create(PROVIDER, monkeypatch, title="Overnight basal protocol")
        assert note["author"] == PROVIDER
        assert note["author_name"] == "drchen"

        # Everyone signed in sees every note.
        self._as("owner", monkeypatch)
        listed = care_notes.list_notes(None)
        assert [n["id"] for n in listed["notes"]] == [note["id"]]

    def test_only_the_author_edits(self, database, monkeypatch):
        note = self._create(PROVIDER, monkeypatch, title="Protocol v1")

        self._as("owner", monkeypatch)
        with pytest.raises(HTTPException) as denied:
            care_notes.update_note(note["id"], None, care_notes.NoteBody(title="edited"))
        assert denied.value.status_code == 403

        self._as(OTHER_PROVIDER, monkeypatch)
        with pytest.raises(HTTPException):
            care_notes.update_note(note["id"], None, care_notes.NoteBody(title="edited"))

        self._as(PROVIDER, monkeypatch)
        care_notes.update_note(note["id"], None, care_notes.NoteBody(kind="protocol", title="Protocol v2"))
        assert care_notes._get_note(note["id"])["title"] == "Protocol v2"

    def test_owner_can_delete_any_note_but_providers_only_their_own(self, database, monkeypatch):
        provider_note = self._create(PROVIDER, monkeypatch, title="theirs")
        owner_note = self._create("owner", monkeypatch, title="hers")

        self._as(PROVIDER, monkeypatch)
        with pytest.raises(HTTPException) as denied:
            care_notes.delete_note(owner_note["id"], None)
        assert denied.value.status_code == 403
        care_notes.delete_note(provider_note["id"], None)

        # The owner sweeps up anything — it is her record.
        another = self._create(OTHER_PROVIDER, monkeypatch, title="stale protocol")
        self._as("owner", monkeypatch)
        care_notes.delete_note(another["id"], None)
        assert care_notes._list() == [care_notes._get_note(owner_note["id"])]

    def test_unknown_kinds_collapse_to_note_and_empty_is_rejected(self, database, monkeypatch):
        note = self._create(PROVIDER, monkeypatch, kind="sinister_kind", title="x")
        assert note["kind"] == "note"

        self._as(PROVIDER, monkeypatch)
        with pytest.raises(HTTPException) as empty:
            care_notes.create_note(None, care_notes.NoteBody(title="  ", body="  "))
        assert empty.value.status_code == 400

    def test_report_block_carries_author_and_kind(self, database, monkeypatch):
        self._create(PROVIDER, monkeypatch, kind="prescription", title="Levothyroxine timing",
                     body="Take on an empty stomach.", pinned=True)
        block = care_notes.report_block()
        assert block[0]["author"] == "drchen"
        assert block[0]["kind"] == "prescription"
        assert block[0]["pinned"] is True


class TestQualityTierFlag:
    def test_threads_advertise_quality_tier_only_when_configured(self, database, monkeypatch):
        monkeypatch.setattr(companion, "config_value", lambda name, default="": "")
        assert _handle({"action": "threads"}, "owner")["quality_tier_available"] is False

        monkeypatch.setattr(companion, "config_value", lambda name, default="": {
            "quality_llm_url": "unix:///run/glucopilot/ollama.sock",
            "quality_llm_model": "gemma3:27b",
        }.get(name, default))
        assert _handle({"action": "threads"}, "owner")["quality_tier_available"] is True
