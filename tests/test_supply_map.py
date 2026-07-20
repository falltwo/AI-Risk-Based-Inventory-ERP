import math


def test_wrap_text_treats_missing_pandas_value_as_empty_text():
    from frontend.components.supply_map import _wrap_text

    assert _wrap_text(math.nan, 45) == ""


def test_wrap_text_wraps_regular_summary():
    from frontend.components.supply_map import _wrap_text

    assert _wrap_text("abcdef", 3) == "abc<br>def"
