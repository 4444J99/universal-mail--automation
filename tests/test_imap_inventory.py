"""Offline tests for IMAPInventory (providers/imap_inventory.py).

The account-bound inventory adapter had only incidental coverage through the
higher-level core.mail_inventory receipt tests (tests/test_mail_inventory.py).
This file pins the adapter's own invariants:

  * constructor guards      — explicit provider/account required, AUTH state,
    Gmail-native extension required for provider=gmail.
  * inventory_surfaces      — strict LIST parsing (RFC 3501 attr/name slots),
    MUTF-7 mailbox decoding, \\Noselect -> not selectable, \\Trash/\\Junk ->
    junk_trash retention, retained-before-junk sort order.
  * inventory_snapshot      — UIDVALIDITY read, sorted unique UID snapshot,
    iCloud empty-folder handling (SEARCH [None] + count 0 == empty), and
    rejection of malformed/duplicate UID sets.
  * inventory_page          — cursor bounds, gmail X-GM identity required,
    UIDVALIDITY stability across pages, next_cursor/complete discipline, and
    the "page must return exactly the selected UIDs" honesty rule.
  * read_corpus_message     — exact-account binding, full-message literal
    integrity, and the raw_mime evidence digest (non-Gmail representation).

Nothing touches a socket: all wire traffic is scripted on an imaplib stand-in.
"""

import base64
import hashlib

import pytest

from core.flag_workflow import sha256_hex
from providers.imap_inventory import IMAPInventory, decode_mailbox, quote

RAW = b"Message-ID: <fixture@example.invalid>\r\nSubject: Fixture\r\n\r\nRetained evidence.\r\n"


class Conn:
    """imaplib stand-in: AUTH state, scripted LIST/UIDVALIDITY/uid traffic."""

    state = "AUTH"

    def __init__(self, capabilities=(), uidvalidity="123", list_data=(),
                 uid_log=None, counts=(b"5",)):
        self.capabilities = capabilities
        self._uidvalidity = uidvalidity
        self.list_data = list(list_data)
        self.uid_log = list(uid_log or [])
        self._counts = list(counts)
        self.calls = []
        self.selected = None

    def list(self):
        self.calls.append("LIST")
        return ("OK", self.list_data)

    def select(self, mailbox, readonly=False):
        self.calls.append(("SELECT", mailbox, bool(readonly)))
        self.selected = mailbox
        if self._counts:
            return ("OK", [self._counts.pop(0)])
        return ("OK", [b"5"])

    def response(self, name):
        if name.upper() == "UIDVALIDITY":
            return ("UIDVALIDITY", [self._uidvalidity.encode()])
        return (name.upper(), [b""])

    def uid(self, command, *args):
        self.calls.append((command.upper(), *args))
        if self.uid_log:
            return self.uid_log.pop(0)
        return ("OK", [b""])


def _adapter(caps=(), **kwargs):
    kwargs.setdefault("capabilities", caps)
    conn = Conn(**kwargs)
    return IMAPInventory(conn, account="a@example.invalid",
                         provider="icloud", host="imap.example.com"), conn


# -- constructor guards -------------------------------------------------------
def test_inventory_requires_explicit_account_and_provider():
    conn = Conn()
    with pytest.raises(ValueError, match="explicit provider/account"):
        IMAPInventory(conn, account="", provider="icloud", host="h")
    with pytest.raises(ValueError, match="explicit provider/account"):
        IMAPInventory(conn, account="a", provider="pop3", host="h")


def test_inventory_requires_authenticated_connection():
    conn = Conn()
    conn.state = "NONAUTH"
    with pytest.raises(ValueError, match="authenticated"):
        IMAPInventory(conn, account="a@example.invalid", provider="icloud", host="h")


def test_inventory_gmail_requires_native_extension():
    with pytest.raises(ValueError, match="native identity"):
        IMAPInventory(Conn(), account="a@example.invalid", provider="gmail", host="h")
    IMAPInventory(Conn(capabilities=["X-GM-EXT-1"]), account="a@example.invalid",
                  provider="gmail", host="h")  # must not raise


def test_inventory_identity_echoes_account():
    adapter, _ = _adapter()
    assert adapter.identity() == {"provider": "icloud", "account": "a@example.invalid",
                                  "host": "imap.example.com", "transport": "imap",
                                  "authenticated": True}


# -- MUTF-7 mailbox names and quoting ----------------------------------------
def test_decode_mailbox_utf7_basics():
    assert decode_mailbox("plain") == "plain"
    assert decode_mailbox("&-") == "&"                       # literal ampersand
    assert decode_mailbox("Personal &- Work") == "Personal & Work"
    assert decode_mailbox("&ZeVnLIqe-") == "日本語"


def test_quote_escapes_and_rejects_control_bytes():
    assert quote("Work") == '"Work"'
    assert quote('a"b') == '"a\\"b"'
    assert quote("a\\b") == '"a\\\\b"'
    for bad in ("\r\n", "\x00", "a\rb"):
        with pytest.raises(ValueError, match="invalid IMAP mailbox"):
            quote(bad)


# -- inventory_surfaces: strict LIST parsing ---------------------------------
_LIST_DATA = [
    b'(\\HasNoChildren \\Trash) "/" "Trash"',
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
    b'(\\HasChildren) "/" "Work"',
    b'(\\HasNoChildren) "/" "Junk Email"',
    b'(\\HasNoChildren) "/" "&ZeVnLIqe-"',
    b'(\\HasNoChildren) "/" "[Gmail] &ZeVnLIqe-"',
]


def test_inventory_surfaces_sorts_retained_first_and_flags_junk_trash():
    adapter, _ = _adapter(list_data=_LIST_DATA)
    surfaces = adapter.inventory_surfaces()
    assert [s["id"] for s in surfaces] == [
        "&ZeVnLIqe-", "INBOX", "Work", "[Gmail]", "[Gmail] &ZeVnLIqe-",
        "Junk Email", "Trash",
    ]
    by_id = {s["id"]: s for s in surfaces}
    assert by_id["&ZeVnLIqe-"]["name"] == "日本語"           # MUTF-7 decoded
    assert by_id["[Gmail] &ZeVnLIqe-"]["name"] == "[Gmail] 日本語"
    assert by_id["[Gmail]"]["selectable"] is False          # \\Noselect
    assert by_id["INBOX"]["selectable"] is True
    assert by_id["Trash"]["retention_class"] == "junk_trash"
    assert by_id["Junk Email"]["retention_class"] == "junk_trash"
    assert by_id["Work"]["retention_class"] == "retained"


def test_inventory_surfaces_rejects_malformed_list_lines():
    adapter, _ = _adapter(list_data=[b"THIS IS NOT A LIST LINE"])
    with pytest.raises(ValueError, match="malformed IMAP LIST"):
        adapter.inventory_surfaces()


def test_inventory_surfaces_literal_bytes_only():
    adapter, _ = _adapter(list_data=[12345])
    with pytest.raises(ValueError, match="unsupported IMAP LIST literal"):
        adapter.inventory_surfaces()


# -- inventory_snapshot: UIDVALIDITY + sorted unique UID set ------------------
def test_inventory_snapshot_sorts_valid_set():
    adapter, _ = _adapter(uidvalidity="77", uid_log=[("OK", [b"5 1 4 2"])])
    snapshot = adapter.inventory_snapshot({"id": "INBOX"})
    assert snapshot == {"uidvalidity": "77", "uids": ["1", "2", "4", "5"],
                        "boundary": "retained_uid_set_at_scan_start"}


def test_inventory_snapshot_rejects_non_digit_or_duplicate_uids():
    for bad in (b"1 x 3", b"1 1 2", b"-1 2"):
        adapter, _ = _adapter(uid_log=[("OK", [bad])])
        with pytest.raises(ValueError, match="invalid UID"):
            adapter.inventory_snapshot({"id": "INBOX"})


def test_inventory_snapshot_failed_search_is_runtime_error():
    adapter, _ = _adapter(uid_log=[("NO", [b"[NO] -bad"])])
    with pytest.raises(RuntimeError, match="UID SEARCH"):
        adapter.inventory_snapshot({"id": "INBOX"})


def test_inventory_snapshot_icloud_empty_folder_is_empty_not_error():
    # iCloud omits the untagged SEARCH payload for an empty folder: OK + [None]
    # together with EXAMINE count 0 must be treated as an empty snapshot.
    adapter, conn = _adapter(uidvalidity="1", uid_log=[("OK", [None])],
                             counts=(b"0",))
    assert adapter.inventory_snapshot({"id": "INBOX"})["uids"] == []
    assert conn.calls[-1][0] == "SEARCH"


# -- inventory_page: cursor discipline + gmail identity -----------------------
_GMAIL_META_5 = (b"5 (UID 5 X-GM-MSGID 1005 X-GM-THRID 2005 FLAGS (\\Seen))",
                 b"From: a@b.invalid\r\nSubject: first\r\n"
                 b"Message-ID: <m5@x>\r\nDate: Fri, 1 Jan 2026 00:00:00 +0000\r\n\r\n")
_GMAIL_META_6 = (b"6 (UID 6 X-GM-MSGID 1006 X-GM-THRID 2006 FLAGS ())",
                 b"From: c@d.invalid\r\nSubject: second\r\nMessage-ID: <m6@x>\r\n\r\n")


def _page_adapter(gmail=False, meta_items=(_GMAIL_META_5,), **kwargs):
    if "capabilities" not in kwargs:
        kwargs["capabilities"] = ["X-GM-EXT-1"] if gmail else []
    if "uid_log" not in kwargs:
        kwargs["uid_log"] = [("OK", list(meta_items))]
    if "uidvalidity" not in kwargs:
        kwargs["uidvalidity"] = "123"
    return _adapter(**kwargs)


def test_inventory_page_gmail_identity_and_paging_discipline():
    adapter, conn = _page_adapter(gmail=True, meta_items=(_GMAIL_META_5, _GMAIL_META_6))
    surface = {"id": "INBOX", "retention_class": "retained"}
    snapshot = {"uidvalidity": "123", "uids": ["5", "6", "7"]}
    page = adapter.inventory_page(surface, snapshot, cursor=None, limit=2)
    assert page["next_cursor"] == 2 and page["complete"] is False
    first = page["messages"][0]["identity"]
    assert first["provider"] == "icloud"
    assert first["account"] == "a@example.invalid"
    assert first["message_id"] == "1005"
    assert first["evidence_digest"] == sha256_hex(
        {"headers": _GMAIL_META_5[1].decode("utf-8", errors="replace")})
    assert page["messages"][1]["identity"]["message_id"] == "1006"
    assert page["messages"][0]["native"] == {"mailbox": "INBOX", "uidvalidity": "123",
                                             "uid": "5"}
    assert page["messages"][0]["memberships"] == ["INBOX"]
    assert page["messages"][0]["retention_class"] == "retained"
    assert page["messages"][0]["thread_id"] == "2005"
    assert page["messages"][0]["rfc_message_id"] == "<m5@x>"
    assert ("FETCH", "5,6",
            "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER] "
            "X-GM-MSGID X-GM-THRID X-GM-LABELS)") in conn.calls


def test_inventory_page_final_page_is_complete():
    adapter, _ = _page_adapter(gmail=True)
    page = adapter.inventory_page({"id": "INBOX", "retention_class": "retained"},
                                  {"uidvalidity": "123", "uids": ["5"]}, None, 10)
    assert page["complete"] is True and page["next_cursor"] is None


def test_inventory_page_gmail_missing_global_identity_is_value_error():
    meta = (b"5 (UID 5 FLAGS ())", b"Subject: x\r\n\r\n")
    adapter, _ = _page_adapter(gmail=True, meta_items=(meta,))
    with pytest.raises(ValueError, match="Gmail global identity"):
        adapter.inventory_page({"id": "INBOX"}, {"uidvalidity": "123",
                                                 "uids": ["5"]}, None, 10)


def test_inventory_page_rejects_invalid_cursor():
    adapter, _ = _page_adapter(gmail=True)
    snapshot = {"uidvalidity": "123", "uids": ["5", "6"]}
    for bad in ("1", -1, 3, 2.5):
        with pytest.raises(ValueError, match="continuation"):
            adapter.inventory_page({"id": "INBOX"}, snapshot, bad, 10)


def test_inventory_page_uidvalidity_change_is_runtime_error():
    adapter, _ = _page_adapter(gmail=True, uidvalidity="999")
    with pytest.raises(RuntimeError, match="UIDVALIDITY changed"):
        adapter.inventory_page({"id": "INBOX", "retention_class": "retained"},
                               {"uidvalidity": "123", "uids": ["5"]}, None, 10)


def test_inventory_page_missing_message_is_runtime_error():
    # The snapshot-UID set is authoritative: a page that returns FEWER messages
    # than it selected must fail rather than silently advance the cursor.
    adapter, _ = _page_adapter(gmail=True, meta_items=(_GMAIL_META_5,))
    with pytest.raises(RuntimeError, match="missing or duplicated"):
        adapter.inventory_page({"id": "INBOX", "retention_class": "retained"},
                               {"uidvalidity": "123", "uids": ["5", "6"]}, None, 10)


def test_inventory_page_failed_fetch_is_runtime_error():
    adapter, conn = _adapter(uidvalidity="123", capabilities=["X-GM-EXT-1"],
                             uid_log=[("NO", [b""])])
    surface = {"id": "INBOX", "retention_class": "retained"}
    with pytest.raises(RuntimeError, match="FETCH failed"):
        adapter.inventory_page(surface, {"uidvalidity": "123", "uids": ["5"]}, None, 10)


# -- read_corpus_message: exact-account full-message read ---------------------
def _identity():
    header, sep, _ = RAW.partition(b"\r\n\r\n")
    return {"account": "a@example.invalid", "provider": "icloud",
            "message_id": "x",
            "evidence_digest": sha256_hex({"headers": (header + sep).decode()})}


def _corpus_item():
    return {"provenance": [{"identity": _identity(),
                            "native": {"mailbox": "Inbox", "uid": "1",
                                       "uidvalidity": "123"}}]}


def _read_adapter(uid_log=None, uidvalidity="123"):
    if uid_log is None:
        meta = (b"1 (UID 1 RFC822.SIZE " + str(len(RAW)).encode()
                + b" BODY[]<0.10485760> FLAGS ())")
        uid_log = [("OK", [(meta, RAW)])]
    return _adapter(uidvalidity=uidvalidity, uid_log=uid_log)[0]


def test_read_corpus_message_raw_mime_representation():
    adapter = _read_adapter()
    result = adapter.read_corpus_message(_corpus_item())
    assert result["complete"] is True
    assert result["header_representation"] == "raw_mime"
    assert result["raw_rfc822_base64"] == base64.b64encode(RAW).decode()
    assert result["raw_sha256"] == hashlib.sha256(RAW).hexdigest()
    assert result["native_headers_sha256"] == _identity()["evidence_digest"]


def test_read_corpus_message_rejects_research_account_mismatch():
    adapter = _read_adapter()
    item = _corpus_item()
    item["provenance"][0]["identity"]["account"] = "other@example.invalid"
    with pytest.raises(ValueError, match="account mismatch"):
        adapter.read_corpus_message(item)


def test_read_corpus_message_rejects_uidvalidity_change():
    adapter = _read_adapter(uidvalidity="999")
    with pytest.raises(RuntimeError, match="UIDVALIDITY changed"):
        adapter.read_corpus_message(_corpus_item())


def test_read_corpus_message_rejects_incomplete_literal():
    # RFC822.SIZE must equal the returned octet count for a non-Gmail raw MIME.
    meta = (b"1 (UID 1 RFC822.SIZE " + str(len(RAW) + 4).encode()
            + b" BODY[]<0.10485760> FLAGS ())")
    adapter = _read_adapter(uid_log=[("OK", [(meta, RAW)])])
    with pytest.raises(RuntimeError, match="incomplete or oversized"):
        adapter.read_corpus_message(_corpus_item())


def test_read_corpus_message_rejects_missing_boundary():
    bad = b"no-rfc-822-boundary-here"
    meta = (b"1 (UID 1 RFC822.SIZE " + str(len(bad)).encode()
            + b" BODY[]<0.10485760> FLAGS ())")
    adapter = _read_adapter(uid_log=[("OK", [(meta, bad)])])
    with pytest.raises(ValueError, match="RFC header boundary"):
        adapter.read_corpus_message(_corpus_item())