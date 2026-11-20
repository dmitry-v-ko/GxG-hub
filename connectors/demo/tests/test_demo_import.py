import demo_connector


def test_demo_is_importable() -> None:
    assert demo_connector.__version__ == "0.0.0"
