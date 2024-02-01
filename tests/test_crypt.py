from unittest.mock import patch, MagicMock
from motionblindsble.crypt import MotionCrypt, TimezoneNotSetException
from datetime import datetime, timezone
import pytest


class TestCrypt:
    """Test the crypt.py module."""

    def test_encrypt_decrypt(self) -> None:
        """Test encryption and decryption."""

        MotionCrypt.set_timezone("Europe/Amsterdam")

        expectedEncrypted = "244e1d963ebdc5453f43e896465b5bcf"
        expectedDecrypted = "070404020e0059b4"

        decrypted = MotionCrypt.decrypt(expectedEncrypted)
        encrypted = MotionCrypt.encrypt(decrypted)

        assert expectedDecrypted == decrypted
        assert expectedEncrypted == encrypted

    @patch("motionblindsble.crypt.datetime")
    def test_get_time(self, mock_datetime: MagicMock) -> None:
        """Test getting the time string."""

        MotionCrypt.set_timezone("Europe/Amsterdam")

        mock_datetime.datetime.now.return_value = datetime(
            year=2015,
            month=3,
            day=4,
            hour=5,
            minute=6,
            second=7,
            microsecond=999999,
            tzinfo=timezone.utc,
        )

        assert MotionCrypt.get_time() == "0f030405060703e7"

    def test_get_time_timezone_not_set(self) -> None:
        """Test getting the time string when the timezone is not set."""

        MotionCrypt.timezone = None

        with pytest.raises(TimezoneNotSetException):
            print(MotionCrypt.get_time())
