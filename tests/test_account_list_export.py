"""Settings → License → Export list: data shaping, DB added_at, and the xlsx writer."""
from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest

from emailclient import __version__, account_list, xlsx
from emailclient.db import Database
from emailclient.models import Account

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _acct(**kw) -> Account:
    base = dict(id=None, name="A", email="a@example.com", provider="gmail",
                imap_host="imap.gmail.com", imap_port=993,
                smtp_host="smtp.gmail.com", smtp_port=465, color="")
    base.update(kw)
    return Account(**base)


def _sheet_rows(path) -> list[list[str]]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    out = []
    for row in root.find("m:sheetData", NS):
        vals = []
        for c in row:
            t = c.find("m:is/m:t", NS)
            v = c.find("m:v", NS)
            vals.append(t.text if t is not None else (v.text if v is not None else ""))
        out.append(vals)
    return out


class TestDatabaseAddedAt:
    def test_new_account_gets_utc_timestamp(self, tmp_path):
        db = Database(tmp_path / "mail.db")
        db.add_account(_acct())
        (a,) = db.list_accounts()
        dt = datetime.fromisoformat(a.added_at)
        assert dt.tzinfo is not None
        assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 60

    def test_legacy_rows_read_as_blank(self, tmp_path):
        import sqlite3
        p = tmp_path / "legacy.db"
        con = sqlite3.connect(p)
        con.executescript(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " email TEXT NOT NULL UNIQUE, provider TEXT NOT NULL, imap_host TEXT NOT NULL,"
            " imap_port INTEGER NOT NULL, smtp_host TEXT NOT NULL, smtp_port INTEGER NOT NULL,"
            " color TEXT NOT NULL);"
            "INSERT INTO accounts VALUES (1,'Old','old@example.com','zoho','imap.zoho.com',"
            "993,'smtp.zoho.com',465,'');")
        con.commit(); con.close()
        db = Database(p)
        (a,) = db.list_accounts()
        assert a.added_at == ""
        assert account_list.added_label(a) == account_list.UNKNOWN_ADDED


class _Worker:
    def __init__(self, running): self._r = running
    def isRunning(self): return self._r


class _Engine:
    def __init__(self):
        self.auth_failed = {2}
        self.workers = {1: _Worker(True), 3: _Worker(False)}


class TestRows:
    def test_status_precedence(self):
        eng = _Engine()
        assert account_list.status_label(_acct(id=1), eng) == "Active"
        assert account_list.status_label(_acct(id=2), eng) == "Login failed"
        assert account_list.status_label(_acct(id=3), eng) == "Not syncing"
        assert account_list.status_label(_acct(id=2, paused=True), eng) == "Paused"
        assert account_list.status_label(_acct(id=9), None) == "Active"

    def test_provider_labels(self):
        assert account_list.provider_label(_acct(provider="outlook")) == "Outlook / Hotmail"
        assert account_list.provider_label(
            _acct(provider="custom", imap_host="mail.corp.local")) == "Other (IMAP) — mail.corp.local"
        assert account_list.provider_label(_acct(provider="fastmail")) == "Fastmail"

    def test_added_label_is_local_minutes(self):
        a = _acct(added_at="2026-03-05T09:30:00+00:00")
        expected = datetime(2026, 3, 5, 9, 30, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        assert account_list.added_label(a) == expected

    def test_build_rows_shape(self):
        rows = account_list.build_rows([_acct(id=1), _acct(id=2, email="b@x.com")],
                                       _Engine(), pc_name="OFFICE-PC")
        assert rows == [
            [1, "OFFICE-PC", __version__, "a@example.com", "Gmail", account_list.UNKNOWN_ADDED, "Active"],
            [2, "OFFICE-PC", __version__, "b@x.com", "Gmail", account_list.UNKNOWN_ADDED, "Login failed"],
        ]
        assert len(account_list.HEADERS) == len(rows[0]) == len(account_list.COL_WIDTHS)


class TestXlsxWriter:
    def test_roundtrip_and_escaping(self, tmp_path):
        p = tmp_path / "out.xlsx"
        xlsx.write_xlsx(p, ["#", "Name & Co"], [[1, "a<b>"], [2, "tab\there"], [3, ""]],
                        sheet_name="Accounts", col_widths=[5, 20])
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
            assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                    "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                    "xl/worksheets/sheet1.xml"} <= names
            for n in names:  # every part must be well-formed XML
                ET.fromstring(z.read(n))
            assert 'name="Accounts"' in z.read("xl/workbook.xml").decode()
        assert _sheet_rows(p) == [["#", "Name & Co"], ["1", "a<b>"], ["2", "tab\there"], ["3", ""]]

    def test_header_bold_frozen_filtered(self, tmp_path):
        p = tmp_path / "out.xlsx"
        xlsx.write_xlsx(p, ["H"], [["x"]])
        sheet = zipfile.ZipFile(p).read("xl/worksheets/sheet1.xml").decode()
        assert 'state="frozen"' in sheet
        assert '<autoFilter ref="A1:A2"/>' in sheet
        assert '<c r="A1" t="inlineStr" s="1">' in sheet

    def test_col_letters(self):
        assert [xlsx.col_letter(n) for n in (1, 26, 27, 52, 53, 702, 703)] == \
            ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"]


@pytest.mark.usefixtures("qtbot")
class TestDialogExport:
    def test_export_writes_file(self, tmp_path, monkeypatch, qtbot):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from emailclient.ui.settings_dialog import SettingsDialog
        db = Database(tmp_path / "mail.db")
        db.add_account(_acct(email="one@example.com"))
        db.add_account(_acct(email="two@example.com", provider="outlook"))
        out = tmp_path / "list.xlsx"
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(out), "")))
        shown = []
        monkeypatch.setattr(QMessageBox, "information",
                            staticmethod(lambda *a, **k: shown.append(a[2])))
        dlg = SettingsDialog(db=db, engine=None)
        qtbot.addWidget(dlg)
        dlg._export_account_list()
        rows = _sheet_rows(out)
        assert rows[0] == account_list.HEADERS
        assert [r[3] for r in rows[1:]] == ["one@example.com", "two@example.com"]
        assert rows[1][1] == account_list.computer_name()
        assert rows[1][2] == __version__
        assert rows[2][4] == "Outlook / Hotmail"
        assert shown and "2 account(s)" in shown[0]
