"""Deleting a folder must select only that folder and its real children."""
from __future__ import annotations

import pytest

from emailclient.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "mail.db")
    yield d
    d.conn.close()


def _seed(db, names):
    for n in names:
        db.add_account_folder(1, n)


class TestAccountFolderTree:
    def test_underscore_is_not_a_wildcard(self, db):
        _seed(db, ["Q1_2024", "Q1_2024/Invoices", "Q1-2024", "Q1-2024/Invoices",
                   "Q1x2024/Other"])
        got = db.account_folder_tree(1, "Q1_2024")
        assert set(got) == {"Q1_2024", "Q1_2024/Invoices"}

    def test_matching_is_case_sensitive(self, db):
        _seed(db, ["Projects", "Projects/A", "projects/Secret"])
        assert set(db.account_folder_tree(1, "Projects")) == {"Projects", "Projects/A"}

    def test_percent_is_not_a_wildcard(self, db):
        _seed(db, ["Sales%", "Sales%/EU", "SalesX/EU"])
        assert set(db.account_folder_tree(1, "Sales%")) == {"Sales%", "Sales%/EU"}

    def test_prefix_without_delimiter_is_not_a_child(self, db):
        _seed(db, ["Work", "Work/2024", "Workshop", "Workshop/Notes"])
        assert set(db.account_folder_tree(1, "Work")) == {"Work", "Work/2024"}

    def test_deepest_first_and_other_accounts_untouched(self, db):
        _seed(db, ["A", "A/B", "A/B/C"])
        db.add_account_folder(2, "A/B")
        got = db.account_folder_tree(1, "A")
        assert got == ["A/B/C", "A/B", "A"]

    def test_custom_delimiter(self, db):
        _seed(db, ["A", "A.B", "A/B"])
        assert set(db.account_folder_tree(1, "A", delim=".")) == {"A", "A.B"}

    def test_unknown_folder_still_returns_itself(self, db):
        assert db.account_folder_tree(1, "Nope") == ["Nope"]
