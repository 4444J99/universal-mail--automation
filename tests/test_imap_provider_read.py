"""Tests for the still-uncovered IMAPProvider read/auth surface.

The archive/append clusters are covered elsewhere (test_imap_archive.py,
test_imap_append.py, test_thread_reconcile.py). This file pins the parts of
``providers/imap.py`` that had no offline coverage at all:

  * list_messages       — offset pagination moves NEWEST-first (tail of the
    server's ascending UID list), and next_page_token stops past the end.
  * get_message_details — RFC 2047 header decoding, BODY.PEEK[TEXT] snippet,
    X-GM-LABELS parsing and FLAGS -> read/starred state, honest None when the
    header fetch is rejected.
  * connect/disconnect  — real login flow behind a mocked IMAP4_SSL, idempotent
    connect, crash-safe logout.
  * _load_password      — precedence GMAIL_APP_PASSWORD > IMAP_PASS > 1Password,
    explicit password wins, no configuring means ValueError.
  * _select_mailbox / ensure_label_exists / _server_supports.

Everything runs offline against scripted fakes; nothing touches a socket.
"""

import imaplib
from unittest import mock

import pytest

from providers.imap import IMAPProvider, _decode_header_value

HEADERS_QUERY = ("(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE REPLY-TO "
                 "LIST-UNSUBSCRIBE LIST-ID PRECEDENCE)])")
SNIPPET_QUERY = "(BODY.PEEK[TEXT]<0.300>)"
LABELS_QUERY = "(X-GM-LABELS)"
FLAGS_QUERY = "(FLAGS)"


class Conn:
    """Offline imaplib stand-in: records every call, scripts uid() results."""

    def __init__(self, results=None, caps=(), select_no=(), create_status="OK",
                 raise_on_create=False):
        self.results = results or {}
        self.caps = tuple(caps)
        self.select_no = set(select_no)
        self.create_status = create_status
        self.raise_on_create = raise_on_create
        self.calls = []
        self.selected = None

    @property
    def capabilities(self):
        return self.caps

    def select(self, mailbox, readonly=False):
        self.calls.append(("SELECT", mailbox, bool(readonly)))
        if mailbox in self.select_no:
            return ("NO", [b"[NO] unknown mailbox"])
        self.selected = mailbox
        return ("OK", [b"5"])

    def create(self, mailbox):
        self.calls.append(("CREATE", mailbox))
        if self.raise_on_create:
            raise imaplib.IMAP4.error("CREATE")
        return (self.create_status, [mailbox.encode()])

    def uid(self, command, *args):
        self.calls.append((command.upper(), *args))
        return self.results.get((command.upper(), *args), ("OK", [b""]))


def _provider(gmail_ext=False, conn=None):
    p = IMAPProvider(host="imap.example.com", user="u@example.com",
                     password="x", use_gmail_extensions=gmail_ext)  # allow-secret: test literal
    if conn is not None:
        p._connection = conn
        p._current_mailbox = None if conn.selected is None else conn.selected
    return p


# -- list_messages: newest-first offset pagination ---------------------------
def _search_conn(uids_bundle, select_no=()):
    return Conn(results={("SEARCH", None, "ALL"): ("OK", [uids_bundle])},
                select_no=select_no)


def test_list_messages_takes_most_recent_first():
    conn = _search_conn(b"1 2 3 4 5")
    result = _provider(conn=conn).list_messages("ALL", limit=2)
    assert [m.id for m in result.messages] == ["4", "5"]
    assert result.next_page_token == "2"
    assert result.total_estimate == 5


def test_list_messages_second_page_resumes_at_offset():
    conn = _search_conn(b"1 2 3 4 5")
    result = _provider(conn=conn).list_messages("ALL", limit=2, page_token="2")
    assert [m.id for m in result.messages] == ["2", "3"]
    assert result.next_page_token == "4"


def test_list_messages_final_page_has_no_next_token():
    conn = _search_conn(b"1 2 3 4 5")
    result = _provider(conn=conn).list_messages("ALL", limit=2, page_token="4")
    assert [m.id for m in result.messages] == ["1"]
    assert result.next_page_token is None


def test_list_messages_empty_mailbox():
    conn = _search_conn(b"")
    result = _provider(conn=conn).list_messages()
    assert result.messages == []
    assert result.next_page_token is None
    assert result.total_estimate == 0


def test_list_messages_selects_queried_mailbox_and_search_wire():
    conn = _search_conn(b"7 5 3")
    _provider(conn=conn).list_messages("UNSEEN", limit=10, mailbox="Work")
    assert ("SELECT", "Work", False) in conn.calls
    assert ("SEARCH", None, "UNSEEN") in conn.calls


def test_list_messages_search_failure_is_runtime_error():
    conn = Conn(results={("SEARCH", None, "ALL"): ("NO", [b"[NO] bad search"])})
    with pytest.raises(RuntimeError, match="search failed"):
        _provider(conn=conn).list_messages()


# -- get_message_details: header decode, snippet, labels, flags --------------
HEADER_FIELDS = (b"From: Alice <alice@example.invalid>\r\n"
                 b"Subject: =?UTF-8?Q?Quarterly_plan?=\r\n"
                 b"Reply-To: replies@example.invalid\r\n"
                 b"List-Unsubscribe: <https://unsub.example.invalid>\r\n"
                 b"List-Id: announce.example.invalid\r\n"
                 b"Precedence: bulk\r\n\r\n")


def _details_conn(gmail_ext):
    results = {
        ("FETCH", "1", HEADERS_QUERY): ("OK", [(b"BODY[HEADER.FIELDS]", HEADER_FIELDS)]),
        ("FETCH", "1", SNIPPET_QUERY): ("OK", [(b"BODY[TEXT]<0> {5}", b"Hello")]),
    }
    if gmail_ext:
        results[("FETCH", "1", LABELS_QUERY)] = (
            "OK", [(b"", b'(\\Starred \\Inbox "Work/Dev")')])
    results[("FETCH", "1", FLAGS_QUERY)] = ("OK", [(b"", b"(\\Seen \\Flagged)")])
    return Conn(results=results)


def test_get_message_details_gmail_parses_headers_labels_and_flags():
    conn = _details_conn(gmail_ext=True)
    msg = _provider(gmail_ext=True, conn=conn).get_message_details("1")
    assert msg.sender == "Alice <alice@example.invalid>"
    assert msg.subject == "Quarterly plan"
    assert msg.snippet == "Hello"
    assert msg.labels == {"\\Starred", "\\Inbox", "Work/Dev"}
    assert msg.is_read is True
    assert msg.is_starred is True
    assert msg.headers["reply-to"] == "replies@example.invalid"
    assert msg.headers["precedence"] == "bulk"


def test_get_message_details_standard_imap_starred_from_flags_only():
    conn = _details_conn(gmail_ext=False)
    msg = _provider(conn=conn).get_message_details("1")
    assert msg.labels == set()              # no X-GM-LABELS fetch on standard IMAP
    assert msg.is_starred is True           # derived from FLAGS \\Flagged
    assert msg.is_read is True


def test_get_message_details_rejected_header_fetch_is_none():
    for rejected in (("NO", [b"[NO]"]), ("OK", [None]), ("OK", [])):
        conn = Conn(results={("FETCH", "9", HEADERS_QUERY): rejected})
        assert _provider(conn=conn).get_message_details("9") is None


def test_decode_header_value_handles_plain_rfc2047_and_empty():
    assert _decode_header_value("") == ""
    assert _decode_header_value("plain subject") == "plain subject"
    assert _decode_header_value("=?UTF-8?Q?Quarterly_plan?=") == "Quarterly plan"
    assert _decode_header_value("=?UTF-8?B?SGVsbG8gV29ybGQ=?=") == "Hello World"


# -- connect / disconnect: full login flow behind a mocked SSL ----------------
class FakeIMAP4SSL:
    instances = []

    def __init__(self, host, *, port=None, ssl_context=None):
        self.host, self.port, self.ssl_context = host, port, ssl_context
        self.logged_in = None
        self.logged_out = False
        FakeIMAP4SSL.instances.append(self)

    def login(self, user, password):
        self.logged_in = (user, password)  # allow-secret: test literal
        return ("OK", [b"OK"])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"BYE"])


@pytest.fixture(autouse=True)
def _fresh_ssl_instances():
    FakeIMAP4SSL.instances.clear()
    yield
    FakeIMAP4SSL.instances.clear()


def test_connect_logs_in_and_binds_connection():
    p = IMAPProvider(host="imap.example.com", user="u@example.com",
                     password="secret", port=993)  # allow-secret: test literal
    with mock.patch("imaplib.IMAP4_SSL", FakeIMAP4SSL):
        p.connect()
    assert len(FakeIMAP4SSL.instances) == 1
    inst = FakeIMAP4SSL.instances[0]
    assert (inst.host, inst.port) == ("imap.example.com", 993)
    assert inst.ssl_context is not None
    assert inst.logged_in == ("u@example.com", "secret")


def test_connect_is_idempotent():
    p = IMAPProvider(host="h", user="u", password="s")  # allow-secret: test literal
    with mock.patch("imaplib.IMAP4_SSL", FakeIMAP4SSL):
        p.connect()
        p.connect()
    assert len(FakeIMAP4SSL.instances) == 1


def test_disconnect_logs_out_and_resets_state():
    p = IMAPProvider(host="h", user="u", password="s")  # allow-secret: test literal
    with mock.patch("imaplib.IMAP4_SSL", FakeIMAP4SSL):
        p.connect()
        p._current_mailbox = "INBOX"
    p.disconnect()
    assert p._connection is None
    assert p._current_mailbox is None
    assert FakeIMAP4SSL.instances[0].logged_out is True


def test_disconnect_without_connection_is_safe():
    p = IMAPProvider(host="h", user="u", password="s")  # allow-secret: test literal
    p.disconnect()  # must not raise


def test_connect_requires_user(monkeypatch):
    monkeypatch.delenv("IMAP_USER", raising=False)
    monkeypatch.delenv("GMAIL_USER", raising=False)
    p = IMAPProvider(host="h", password="s")  # allow-secret: test literal
    with pytest.raises(ValueError, match="IMAP_USER"):
        p.connect()


# -- _load_password: precedence chain ----------------------------------------
def _password_provider():
    return IMAPProvider(host="imap.example.com", user="u@example.com")


def test_load_password_explicit_wins_over_env(monkeypatch):
    p = _password_provider()
    p._password = "explicit"  # allow-secret: test literal
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "env")
    assert p._load_password() == "explicit"
    p._password = None
    assert p._load_password() == "env"


def test_load_password_prefers_gmail_app_password_over_stale_imap_pass(monkeypatch):
    # The 2026-07-23 dark-feed bug: a stale IMAP_PASS shadowed the valid app
    # password. GMAIL_APP_PASSWORD (the organ's canonical hydrated name) must win.
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app")
    monkeypatch.setenv("IMAP_PASS", "stale")
    assert _password_provider()._load_password() == "app"


def test_load_password_falls_back_to_imap_pass(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("IMAP_PASS", "imap-pass")
    assert _password_provider()._load_password() == "imap-pass"


def test_load_password_uses_1password_cli(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("IMAP_PASS", raising=False)
    monkeypatch.setenv("OP_ACCOUNT", "my.1password.com")
    monkeypatch.setenv("OP_ITEM", "mail")
    monkeypatch.setenv("OP_FIELD", "password")
    monkeypatch.setattr("providers.imap.subprocess.check_output",
                        lambda *a, **k: "op-secret\n")  # allow-secret: test literal
    assert _password_provider()._load_password() == "op-secret"


def test_load_password_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("IMAP_PASS", raising=False)
    monkeypatch.delenv("OP_ACCOUNT", raising=False)
    monkeypatch.delenv("OP_ITEM", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        _password_provider()._load_password()


# -- _select_mailbox / _server_supports / ensure_label_exists ----------------
def test_select_mailbox_tracks_selected_and_skips_repeat_select():
    conn = Conn()
    p = _provider(conn=conn)
    p._select_mailbox("INBOX")
    p._select_mailbox("INBOX")
    assert conn.calls.count(("SELECT", "INBOX", False)) == 1
    p._select_mailbox("Work")
    assert conn.calls.count(("SELECT", "Work", False)) == 1


def test_select_mailbox_no_is_runtime_error():
    conn = Conn(select_no=("INBOX",))
    with pytest.raises(RuntimeError, match="select mailbox"):
        _provider(conn=conn)._select_mailbox("INBOX")


def test_server_supports_matches_advertised_capability_exactly():
    conn = Conn(caps=("IMAP4rev1", "MOVE", "UIDPLUS"))
    p = _provider(conn=conn)
    assert p._server_supports("MOVE") is True
    assert p._server_supports("uidplus") is True            # case-insensitive
    assert p._server_supports("IDLE") is False
    assert _provider()._server_supports("MOVE") is False    # no connection


def test_ensure_label_exists_creates_once_and_accepts_no():
    conn = Conn()
    p = _provider(conn=conn)
    assert p.ensure_label_exists("Work") == "Work"
    assert p.ensure_label_exists("Work") == "Work"
    assert conn.calls.count(("CREATE", "Work")) == 1        # deduped in memory
    conn2 = Conn(create_status="NO")
    p2 = _provider(conn=conn2)
    p2.ensure_label_exists("Work")
    assert conn2.calls.count(("CREATE", "Work")) == 1       # NO = already exists


def test_ensure_label_exists_survives_server_error():
    conn = Conn(raise_on_create=True)
    assert _provider(conn=conn).ensure_label_exists("Work") == "Work"