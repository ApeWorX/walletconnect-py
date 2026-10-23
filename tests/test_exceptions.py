import pytest

from walletconnect.constants import PROJECT_ID_ENV_VAR
from walletconnect.exceptions import (
    USER_REJECTION_CODES,
    MissingProjectIdError,
    SdkError,
    SessionRequestExpiredError,
    UnsupportedMethodsError,
    UserDisconnectedError,
    UserRejectedError,
    WalletConnectError,
)


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        (5000, UserRejectedError),
        (5101, UnsupportedMethodsError),
        (6000, UserDisconnectedError),
        (8000, SessionRequestExpiredError),
    ],
)
def test_from_code_specific(code, cls):
    error = SdkError.from_code(code, "nope")
    assert type(error) is cls
    assert error.code == code
    assert error.message == "nope"
    assert isinstance(error, WalletConnectError)


def test_from_code_unknown():
    error = SdkError.from_code(424242, "custom", data={"x": 1})
    assert type(error) is SdkError
    assert error.code == 424242
    assert error.to_json() == {"code": 424242, "message": "custom", "data": {"x": 1}}
    # Class attribute is not clobbered.
    assert SdkError.code == 0


def test_default_message_and_json():
    error = UserRejectedError()
    assert str(error) == "[5000] User rejected"
    assert error.to_json() == {"code": 5000, "message": "User rejected"}


def test_duplicate_code_rejected():
    with pytest.raises(TypeError, match="Duplicate"):

        class _DupeError(SdkError):
            code = 5000


def test_user_rejection_codes():
    assert {4001, 5000} <= USER_REJECTION_CODES
    assert 6000 not in USER_REJECTION_CODES


def test_missing_project_id_message():
    message = str(MissingProjectIdError())
    assert PROJECT_ID_ENV_VAR in message
    assert "cloud.reown.com" in message
