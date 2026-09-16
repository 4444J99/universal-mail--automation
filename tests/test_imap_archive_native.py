"""Offline tests for IMAPArchive (providers/imap_archive_native.py).

tests/test_archive_native_adapters.py already pins the happy-path dispatch and
post-observation verification. This file covers the branches that file leaves
open:

  * constructor guards      — MOVE capability required, explicit account,
    distinct Archive folder (never "inbox"), UIDSELECTICE selection health.
  * observe_archive         — provider/account/destination binding, immutable
    native identity (message_id == sha256_hex(original_native)), INBOX as the
    only legal source mailbox, source identity stability across reselect, and
    ambiguous duplicate-copy rejection.
  * dispatch/restore        — fresh-observation-changed, protected /
    human_override refusal, UIDVALIDITY drift, non-OK MOVE and MOVE exception
    both reported ambiguous (never silently "applied").

Invariant mirrored from the other adapters: the archive mutation is dispatched
ONLY when the archive preserve/release invitation is observed as unchanged AND
unprotected — otherwise status stays not_dispatched with no MOVE issued.
"""

import hashlib
import imaplib

import pytest

from core.flag_workflow import sha256_hex
from providers.imap_archive_native import IMAPArchive

RAW = b"Message-ID: <fixture@example.invalid>\r\nSubject: Fixture\r\n\r\nRetained evidence.\r\n"


def _mailbox(name):
    return name.strip('"')


class IMAP:
    """imaplib stand-in for archive (_find/select/response/MOVE) traffic.

    UIDVALIDITY is keyed by (selected mailbox, readonly) so tests can simulate
    read-only EXAMINE stability while the read-write reselect drifts.
    """

    capabilities = (b"IMAP4rev1", b"MOVE")

    def __init__(self, inbox=("7",), archive=(), uidvalidity=None, move_result="OK",
                 raise_on_move=False, select_status="OK", uidvalidity_missing=False):
        self.uids_by_folder = {"INBOX": list(inbox), "Archive": list(archive)}
        self._uidv = dict(uidvalidity or {})
        self.move_result = move_result
        self.raise_on_move = raise_on_move
        self.select_status = select_status
        self.uidvalidity_missing = uidvalidity_missing
        self.commands = []
        self.selected = None
        self.selected_readonly = None
        self.last_move = None

    def select(self, mailbox, readonly=False):
        self.selected = _mailbox(mailbox)
        self.selected_readonly = bool(readonly)
        self.commands.append(("SELECT", self.selected, bool(readonly)))
        if self.select_status != "OK":
            return self.select_status, []
        return ("OK", [str(len(self.uids_by_folder.get(self.selected, []))).encode()])

    def response(self, name):
        if self.uidvalidity_missing:
            return (name.upper(), [])
        val = self._uidv.get((self.selected, self.selected_readonly),
                             self._uidv.get((self.selected, True), "11"))
        return (name.upper(), [str(val).encode()])

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return ("OK", [" ".join(self.uids_by_folder.get(self.selected, [])).encode()])
        if command == "FETCH":
            uid = args[0]
            meta = (b"1 (UID " + uid.encode()
                    + b" RFC822.SIZE " + str(len(RAW)).encode()
                    + b" BODY[]<0.10485760>)")
            return ("OK", [(meta, RAW), b")"])
        if command == "MOVE":
            uid, target = args[0], args[1].strip('"')
            self.last_move = (self.selected, uid, target)
            if self.raise_on_move:
                raise imaplib.IMAP4.error("MOVE")
            return (self.move_result, [b"moved"])
        return ("OK", [b""])


def guard(identity):
    return {"protected": False, "human_override": False}


def _mutation(account="account@example.invalid", archive="Archive",
              msgid=None, content=None, native=None):
    native = native or {"mailbox": "INBOX", "uid": "7", "uidvalidity": "11"}
    return {"identity": {"provider": "icloud", "account": account,
                         "message_id": msgid or sha256_hex(native)},
            "before": {"rfc_message_id": "<fixture@example.invalid>",
                       "content_sha256": content or hashlib.sha256(RAW).hexdigest(),
                       "original_native": native},
            "destination": archive}


def _adapter(conn, account="account@example.invalid", archive="Archive",
             g=guard):
    return IMAPArchive(conn, account=account, archive_mailbox=archive, guard=g)


# -- constructor guards -------------------------------------------------------
def test_archive_requires_moved_capability():
    conn = IMAP()
    conn.capabilities = (b"IMAP4rev1",)
    with pytest.raises(ValueError, match="UID MOVE"):
        IMAPArchive(conn, account="a@example.invalid", archive_mailbox="Archive",
                    guard=guard)


def test_archive_requires_account_and_distinct_archive():
    conn = IMAP()
    with pytest.raises(ValueError, match="explicit account"):
        IMAPArchive(conn, account="", archive_mailbox="Archive", guard=guard)
    with pytest.raises(ValueError, match="explicit account"):
        IMAPArchive(conn, account="a@example.invalid", archive_mailbox="Inbox",
                    guard=guard)  # case-insensitive "inbox" refuse


def test_archive_requires_healthy_archive_selection():
    conn = IMAP(select_status="NO")
    with pytest.raises(RuntimeError, match="selection failed"):
        IMAPArchive(conn, account="a@example.invalid", archive_mailbox="Archive",
                    guard=guard)
    conn = IMAP(uidvalidity_missing=True)
    with pytest.raises(RuntimeError, match="UIDVALIDITY unavailable"):
        IMAPArchive(conn, account="a@example.invalid", archive_mailbox="Archive",
                    guard=guard)


# -- observe_archive: binding and identity checks -----------------------------
def test_observe_archive_rejects_provider_or_account_mismatch():
    conn = IMAP()
    adapter = _adapter(conn)
    mutation = _mutation()
    mutation["identity"]["provider"] = "pop3"
    with pytest.raises(ValueError, match="account mismatch"):
        adapter.observe_archive(mutation)
    mutation["identity"]["provider"] = "icloud"
    mutation["identity"]["account"] = "other@example.invalid"
    with pytest.raises(ValueError, match="account mismatch"):
        adapter.observe_archive(mutation)


def test_observe_archive_rejects_destination_mismatch():
    adapter = _adapter(IMAP())
    mutation = _mutation(archive="Other")
    mutation["destination"] = "Other"
    with pytest.raises(ValueError, match="destination mismatch"):
        adapter.observe_archive(mutation)


def test_observe_archive_requires_immutable_native_identity():
    adapter = _adapter(IMAP())
    mutation = _mutation()
    mutation["identity"]["message_id"] = "stale"
    with pytest.raises(ValueError, match="native identity mismatch"):
        adapter.observe_archive(mutation)
    mutation = _mutation(native={"mailbox": "Work", "uid": "7", "uidvalidity": "11"})
    mutation["identity"]["message_id"] = sha256_hex(mutation["before"]["original_native"])
    with pytest.raises(ValueError, match="native identity mismatch"):
        adapter.observe_archive(mutation)


def test_observe_archive_requires_full_content_and_rfc_identity():
    adapter = _adapter(IMAP())
    mutation = _mutation()
    mutation["before"]["rfc_message_id"] = ""
    with pytest.raises(ValueError, match="full content"):
        adapter.observe_archive(mutation)
    mutation = _mutation()
    mutation["before"]["content_sha256"] = ""
    with pytest.raises(ValueError, match="full content"):
        adapter.observe_archive(mutation)


def test_observe_archive_rejects_source_uid_drift():
    # The source found on this scan must carry the SAME uid/uidvalidity as the
    # observed native — a change means the mailbox was rebuilt underneath us.
    mutation = _mutation(native={"mailbox": "INBOX", "uid": "7", "uidvalidity": "11"})
    mutation["identity"]["message_id"] = sha256_hex(mutation["before"]["original_native"])
    conn = IMAP(inbox=("8",))                      # scanning yields uid 8, not 7
    with pytest.raises(ValueError, match="source identity changed"):
        _adapter(conn).observe_archive(mutation)


def test_observe_archive_rejects_duplicate_archive_copies():
    conn = IMAP(archive=("9", "10"))
    mutation = _mutation()
    with pytest.raises(ValueError, match="duplicate copies"):
        _adapter(conn).observe_archive(mutation)


def test_observe_archive_requires_preserved_message():
    conn = IMAP(inbox=(), archive=())
    with pytest.raises(RuntimeError, match="preservation unavailable"):
        _adapter(conn).observe_archive(_mutation())


def test_observe_archive_reports_server_confirmed_presence():
    adapter = _adapter(IMAP())
    observed = adapter.observe_archive(_mutation())
    assert observed["server_confirmed"] is True
    assert observed["message_present"] is True
    assert observed["in_inbox"] is True
    assert observed["mailboxes"] == ["INBOX"]
    assert observed["archive_destination"] == "Archive"
    assert observed["protected"] is False and observed["human_override"] is False


# -- dispatch / restore: only unchanged-and-unprotected observations move -----
def test_dispatch_archive_moves_to_archive_when_unchanged():
    conn = IMAP()
    adapter = _adapter(conn)
    mutation = _mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.dispatch_archive(mutation)["status"] == "applied"
    assert conn.last_move == ("INBOX", "7", "Archive")


def test_restore_archive_moves_back_to_inbox():
    conn = IMAP(inbox=(), archive=("9",))
    adapter = _adapter(conn)
    mutation = _mutation(native={"mailbox": "INBOX", "uid": "9", "uidvalidity": "22"})
    mutation["identity"]["message_id"] = sha256_hex(mutation["before"]["original_native"])
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.restore_archive(mutation, mutation["before"])["status"] == "applied"
    assert conn.last_move == ("Archive", "9", "INBOX")


def test_dispatch_refuses_when_fresh_observation_changed():
    conn = IMAP()
    adapter = _adapter(conn)
    mutation = _mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    # expected no longer matches the live observation -> refuse, no MOVE
    result = adapter._move(mutation, {"not": "the observation"}, "Archive")
    assert result == {"status": "not_dispatched", "reason": "fresh_observation_changed"}
    assert not any(cmd == "MOVE" for cmd, *_ in conn.commands)


def test_dispatch_refuses_protected_observation():
    conn = IMAP()

    def protect(identity):
        return {"protected": True, "human_override": False}

    adapter = _adapter(conn, g=protect)
    mutation = _mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.dispatch_archive(mutation)["status"] == "not_dispatched"
    assert conn.last_move is None


def test_dispatch_refuses_human_override():
    conn = IMAP()

    def override(identity):
        return {"protected": False, "human_override": True}

    adapter = _adapter(conn, g=override)
    mutation = _mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.dispatch_archive(mutation)["status"] == "not_dispatched"
    assert conn.last_move is None


def test_dispatch_refuses_when_uidvalidity_drifted_at_reselect():
    # Observe reads read-only EXAMINE (uidvalidity "11"); the dispatch reselect
    # is read-write and reads "99" -> the mailbox changed under us, refuse.
    conn = IMAP(uidvalidity={("INBOX", True): "11", ("INBOX", False): "99",
                             ("Archive", True): "22"})
    adapter = _adapter(conn)
    mutation = _mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    result = adapter.dispatch_archive(mutation)
    assert result == {"status": "not_dispatched", "reason": "uidvalidity_changed"}
    assert conn.last_move is None


def test_dispatch_reports_ambiguous_when_move_rejected():
    conn = IMAP(move_result=("NO", [b"[NO] archive read-only"]))
    adapter = _adapter(conn)
    mutation = _mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.dispatch_archive(mutation)["status"] == "ambiguous"


def test_dispatch_reports_ambiguous_when_move_raises():
    conn = IMAP(raise_on_move=True)
    adapter = _adapter(conn)
    mutation = _mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.dispatch_archive(mutation)["status"] == "ambiguous"


def test_archive_search_ambiguity_beyond_twenty_is_runtime_error():
    conn = IMAP(inbox=tuple(str(i) for i in range(1, 22)))
    with pytest.raises(RuntimeError, match="ambiguous"):
        _adapter(conn).observe_archive(_mutation())