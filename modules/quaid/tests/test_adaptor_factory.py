import pytest

from adaptors.factory import create_adapter


def test_create_adapter_error_guides_standalone_resolution():
    with pytest.raises(RuntimeError, match="standalone.*lib\\.adapter\\.get_adapter"):
        create_adapter("standalone")
