from config.communities import (
    COMMUNITIES,
    Program,
    communities_for_program,
    community_program_pairs,
    get_community,
)


def test_counts():
    assert len(COMMUNITIES) == 18
    assert len(community_program_pairs()) == 20
    assert len(communities_for_program(Program.FCIP)) == 6
    assert len(communities_for_program("RCIP")) == 14


def test_dual_program_communities():
    dual = {c.slug for c in COMMUNITIES if c.is_dual_program}
    assert dual == {"sudbury", "timmins"}


def test_slugs_unique_and_provinces_valid():
    slugs = [c.slug for c in COMMUNITIES]
    assert len(slugs) == len(set(slugs))
    assert {c.province for c in COMMUNITIES} <= {"NB", "NS", "ON", "MB", "SK", "AB", "BC"}
    assert all(c.municipalities for c in COMMUNITIES)


def test_get_community():
    assert get_community("superior-east").municipalities[0] == "Wawa"
