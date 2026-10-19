import io

import pandas as pd
import pytest

from forestmerge import composition as C
from forestmerge.config import load_species
from forestmerge.pipeline import export_xlsx, load_sample, result_json, solve
from forestmerge.settings import find_sample
from forestmerge.stand import Stand

pytestmark = pytest.mark.skipif(find_sample()["network"] is None, reason="нет файлов образца 5_x")


@pytest.fixture(scope="module")
def sample():
    return load_sample()


@pytest.fixture(scope="module")
def result(sample):
    return solve(sample)


def test_composition_roundtrip():
    assert C.parse("5Е3Б2ОС+С") == [("Е", 0.5), ("Б", 0.3), ("ОС", 0.2), ("С", C.PLUS_SHARE)]
    assert C.build({"Е": 0.62, "Б": 0.3, "ОС": 0.06, "С": 0.02}) == "6Е3Б1ОС+С"
    assert C.build({"Е": 1.0}) == "10Е"
    assert C.build({"С": 0.505, "Е": 0.495}) in ("5С5Е", "5Е5С")
    assert C.units_diff({"Е": 0.5, "Б": 0.5}, {"Е": 0.7, "Б": 0.3}) == pytest.approx(2.0)


def test_inconsistent_species_stock_uses_shares(sample):
    # у выдела 2 в образце запас «по породе» у ели равен запасу всего яруса
    st = Stand.from_taxation(sample.taxation, "2")
    ss = st.species_stock_ha()
    assert sum(ss.values()) == pytest.approx(100, abs=1)
    assert ss["100200"] == pytest.approx(80, abs=1)


def test_no_small_vydels_after(result):
    assert result.metrics["n_small_after"] == 0


def test_area_and_stock_balance(result):
    m = result.metrics
    assert abs(m["area_diff_ha"]) < 0.01
    # расхождение допускается только из-за техосколков (правка контура лесосеки)
    assert abs(m["stock_balance_m3"]) < 0.005 * m["stock_before_m3"]


def test_every_decision_explained(result):
    assert result.decisions
    for d in result.decisions:
        assert d["explanation"]
        if d["type"] == "merge":
            assert "п.196" in d["explanation"] and d["target_out_vid"]


def test_deterministic(sample, result):
    r2 = solve(sample)
    assert r2.out_vid == result.out_vid


def test_override_keep(sample, result):
    d = next(d for d in result.decisions if d["type"] == "merge")
    r = solve(sample, overrides={d["fragment"]: "keep"})
    dd = next(x for x in r.decisions if x.get("fragment") == d["fragment"])
    assert dd["type"] == "keep" and "user_override" in dd["flags"]


def test_rank1_smaller_minimum(sample, result):
    r1 = solve(sample, {"rank": 1})
    assert r1.metrics["n_merges"] <= result.metrics["n_merges"]
    assert r1.metrics["n_small_after"] == 0


def test_exports(result, sample):
    data = export_xlsx(result)
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
    assert list(sheets)[:3] == ["Общие", "Ярус насаждения", "Подрост"]
    js = result_json(result, sample)
    assert {"before", "after", "fragments", "lesoseki"} <= set(js["layers"])


def test_species_book():
    sp = load_species()
    assert sp.abbr("100200") == "Е"
    assert sp.age_group("100100", 90) == "спелые"
    assert sp.age_group("302600", 15) == "молодняки"
