from emailclient import temp_files


def test_clean_leftovers_only_removes_email_copies(tmp_path):
    (tmp_path / "reader_view_abc.html").write_text("x")
    (tmp_path / "print_123.pdf").write_text("x")
    (tmp_path / "mail.db").write_text("x")
    (tmp_path / "user_dictionary.txt").write_text("x")
    assert temp_files.clean_leftovers(tmp_path) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mail.db", "user_dictionary.txt"]


def test_tracked_files_removed_at_quit(tmp_path):
    f = tmp_path / "print_1.pdf"
    f.write_text("x")
    temp_files.track(f)
    temp_files._remove_all_live()
    assert not f.exists()


def test_remove_now(tmp_path):
    f = tmp_path / "reader_view_1.html"
    f.write_text("x")
    temp_files.track(f)
    temp_files.remove(f)
    temp_files.remove(f)          # twice is fine
    assert not f.exists()
