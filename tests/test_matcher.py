import logging

import pytest

from core.matcher import (
    EmployerCandidate,
    community_for_place,
    dedupe_hash,
    is_numbered_company,
    match_employer,
    name_score,
    name_variants,
    normalize_name,
    normalize_place,
    normalize_title,
    split_operating_name,
)


# --- normalization -----------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("Vale Canada Limited", "vale canada"),
    ("Boulangerie Café Ltée", "boulangerie cafe"),
    ("ACME Corp.", "acme"),
    ("ACME Corporation Inc.", "acme"),
    ("Smith & Sons Co.", "smith and sons"),
    ("Les Entreprises Léger Enr.", "entreprises leger"),
    ("The Home Depot of Canada Inc.", "home depot of canada"),
    ("Tim Hortons", "tim hortons"),
    ("Co-op Atlantic", "co op atlantic"),       # 'co' only stripped at the end
    ("Inco Mining", "inco mining"),
    ("Kal Tire Ltd", "kal tire"),
    ("Dépanneur Côté S.E.N.C.", "depanneur cote"),
    ("  Northern   Lights   LLC ", "northern lights"),
    ("", ""),
    (None, ""),
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_split_operating_name():
    assert split_operating_name("1234567 Ontario Inc. o/a Tim Hortons") == (
        "1234567 Ontario Inc.", "Tim Hortons")
    assert split_operating_name("9876543 Canada Ltée faisant affaire sous Resto Chez Nous")[1] == (
        "Resto Chez Nous")
    assert split_operating_name("Plain Name Ltd.") == ("Plain Name Ltd.", None)


def test_name_variants_and_numbered_companies():
    v = name_variants("1234567 Ontario Inc. o/a Tim Hortons")
    assert v == {"1234567 ontario", "tim hortons"}
    assert is_numbered_company("1234567 ontario")
    assert is_numbered_company("0987654 b c")
    assert not is_numbered_company("7 eleven")
    # Near-identical numbered companies must NOT fuzzy-match.
    assert name_score({"1234567 ontario"}, {"1234568 ontario"}) == 0.0
    assert name_score({"1234567 ontario"}, {"1234567 ontario"}) == 100.0


def test_normalize_place():
    assert normalize_place("Sault Ste. Marie") == "sault sainte marie"
    assert normalize_place("Fort St. John") == normalize_place("Ft. St John")
    assert normalize_place("Lamèque") == "lameque"


def test_community_for_place():
    assert community_for_place("Sudbury", "ON") == "sudbury"
    assert community_for_place("Lively") == "sudbury"
    assert community_for_place("Salmon Arm", "BC") == "north-okanagan-shuswap"
    assert community_for_place("Ft St John") == "peace-liard"
    assert community_for_place("Toronto", "ON") is None
    assert community_for_place("Sudbury", "BC") is None          # province mismatch
    assert community_for_place(None, "MB", "R7A 1A1") == "brandon"  # urban FSA
    assert community_for_place(None, "ON", "P0M 1A0") is None       # rural FSA ambiguous


# --- matching ----------------------------------------------------------------
@pytest.fixture()
def candidates():
    return [
        EmployerCandidate.build(1, "Vale Canada Limited", ["sudbury"]),
        EmployerCandidate.build(2, "1234567 Ontario Inc. o/a Tim Hortons", ["timmins"]),
        EmployerCandidate.build(3, "Kal Tire Ltd.", ["brandon", "moose-jaw"]),
        EmployerCandidate.build(4, "Boulangerie St-Pierre Ltée", ["st-pierre-jolys"]),
        EmployerCandidate.build(5, "Northern Heating Services", ["thunder-bay"]),
    ]


def test_exact_match_with_suffix_differences(candidates):
    r = match_employer("VALE CANADA LTD", "Copper Cliff", candidates, province="ON")
    assert r.is_match and r.employer_id == 1 and r.community_slug == "sudbury"
    assert r.score >= 85


def test_match_via_operating_name(candidates):
    r = match_employer("Tim Hortons", "Timmins", candidates)
    assert r.is_match and r.employer_id == 2


def test_accent_and_word_order_insensitive(candidates):
    r = match_employer("Boulangerie St Pierre", "St-Pierre-Jolys", candidates)
    assert r.is_match and r.employer_id == 4
    r = match_employer("Tire Kal", "Moose Jaw", candidates)
    assert r.is_match and r.employer_id == 3


def test_geo_check_is_mandatory(candidates):
    # Perfect name, wrong community -> never linked.
    r = match_employer("Vale Canada Limited", "Thunder Bay", candidates)
    assert r.status == "unmatched" and r.reason == "geo_mismatch"
    assert r.employer_id is None
    # Unknown city -> no community -> unmatched.
    r = match_employer("Vale Canada Limited", "Toronto", candidates)
    assert r.status == "unmatched"


def test_borderline_flagged_for_review(candidates, caplog):
    # "northern heating" vs "northern heating services" scores ~78 (75 <= s < 85)
    with caplog.at_level(logging.WARNING, logger="core.matcher"):
        r = match_employer("Northern Heating", "Thunder Bay", candidates)
    assert 75 <= r.score < 85
    assert r.status == "needs_review" and r.reason == "borderline_score"
    assert r.employer_id == 5
    assert "Manual review" in caplog.text


def test_low_score_unmatched(candidates):
    r = match_employer("Completely Different Business", "Sudbury", candidates)
    assert r.status == "unmatched" and r.reason == "no_candidate"


def test_ambiguous_tie_goes_to_review():
    cands = [
        EmployerCandidate.build(10, "Northern Lights Inc.", ["sudbury"]),
        EmployerCandidate.build(11, "Northern Lights Ltd.", ["sudbury"]),
    ]
    r = match_employer("Northern Lights", "Sudbury", cands)
    assert r.status == "needs_review" and r.reason == "ambiguous"


def test_threshold_boundaries(candidates):
    r = match_employer("Vale Canada", "Sudbury", candidates, threshold=100.0)
    assert r.is_match  # exact after normalization == 100
    r = match_employer("Vale Canad", "Sudbury", candidates, threshold=100.0)
    assert r.status == "needs_review"


# --- dedupe ------------------------------------------------------------------
def test_normalize_title_and_dedupe_hash():
    assert normalize_title("Cook - Full Time (Permanent)") == "cook"
    assert normalize_title("Cuisinier/Cuisinière, temps plein") == "cuisinier cuisiniere"
    a = dedupe_hash(1, "Cook - Full Time", "Sault Ste. Marie")
    b = dedupe_hash(1, "COOK", "Sault Ste Marie")
    assert a == b and len(a) == 64
    assert dedupe_hash(2, "Cook", "Sault Ste Marie") != a
