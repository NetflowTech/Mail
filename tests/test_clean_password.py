from emailclient.credentials import clean_password


def test_app_password_loses_spaces():
    assert clean_password("abcd efgh ijkl mnop") == "abcdefghijklmnop"
    assert clean_password("abcd efgh ijkl mnop ") == "abcdefghijklmnop"


def test_google_server_always_loses_spaces():
    assert clean_password("ab cd", "imap.gmail.com") == "abcd"


def test_real_password_keeps_its_spaces():
    assert clean_password(" my pass word ", "imap.zoho.com") == " my pass word "
    assert clean_password("pa ss") == "pa ss"


def test_copy_paste_artifacts_removed():
    assert clean_password("secret\n") == "secret"
    assert clean_password("\tsecret\r\n") == "secret"
    assert clean_password("pa ss") == "pa ss"
