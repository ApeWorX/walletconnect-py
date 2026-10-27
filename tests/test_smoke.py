import walletconnect


def test_import():
    assert walletconnect.__name__ == "walletconnect"


def test_version():
    assert isinstance(walletconnect.__version__, str)
    assert walletconnect.__version__ != ""
