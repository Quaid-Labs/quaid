from lib.domain_text import normalize_domain_id


def test_normalize_domain_id_projects_aliases_to_project():
    assert normalize_domain_id("projects") == "project"
    assert normalize_domain_id("PROJECTS") == "project"


def test_normalize_domain_id_family_aliases_to_personal():
    assert normalize_domain_id("family") == "personal"
    assert normalize_domain_id("families") == "personal"
