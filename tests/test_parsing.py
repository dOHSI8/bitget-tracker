import main


def test_parse_side():
    assert main._parse_side({"side": "short"}) == "short"
    assert main._parse_side({"holdSide": "buy"}) == "long"
    assert main._parse_side({"directionType": 1}) == "short"
    assert main._parse_side({}) == "long"


def test_fv_skips_blank_and_bad_values():
    assert main._fv("a", "b", src={"a": "", "b": "1.5"}) == 1.5
    assert main._fv("a", src={"a": "x"}, default=7) == 7
