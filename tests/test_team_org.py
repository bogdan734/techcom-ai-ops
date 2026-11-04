import pytest

from conftest import raw_task
from followup.config import ConfigError
from followup.normalize import normalise_task
from followup.team.extras import load_extras
from followup.team.org import (
    ROLE_CAPABILITIES,
    UNROUTED,
    Capability,
    Org,
    Role,
)
from teamkit import sample_people


def people(*extra, drop=()):
    return [p for p in sample_people() if p["id"] not in drop] + list(extra)


def test_reports_to_cycle_is_refused():
    loop = [
        {"id": "x", "name": "X", "role": "manager", "department": "a", "reports_to": "y"},
        {"id": "y", "name": "Y", "role": "manager", "department": "a", "reports_to": "x"},
    ]
    with pytest.raises(ConfigError, match="cycle"):
        Org.from_records(people(*loop))


def test_two_bosses_are_refused():
    second = {"id": "bo2", "name": "Bo Two", "role": "boss", "department": "management"}
    with pytest.raises(ConfigError, match="exactly one boss"):
        Org.from_records(people(second))


def test_underscore_department_is_refused():
    bad = {"id": "z", "name": "Z", "role": "field", "department": "_unrouted", "reports_to": "max"}
    with pytest.raises(ConfigError, match="underscore"):
        Org.from_records(people(bad))


def test_chain_above_is_manager_senior_boss():
    org = Org.from_records(sample_people())
    assert [p.id for p in org.chain_above("eve")] == ["max", "sid", "bea"]
    assert org.chain_above("bea") == ()


def test_below_excludes_self():
    org = Org.from_records(sample_people())
    assert org.below("sid") == {"max", "eve", "finn", "sue"}
    assert org.below("eve") == frozenset()


def test_unknown_owner_routes_to_unrouted():
    org = Org.from_records(sample_people())
    assert org.department_of("Eve Engineer") == "engineering"
    assert org.department_of("Nobody Known") == UNROUTED
    assert org.department_of(None) == UNROUTED


def test_mailboxes_have_no_capabilities():
    assert ROLE_CAPABILITIES[Role.PROJECT_MANAGER] == frozenset()
    assert ROLE_CAPABILITIES[Role.WAREHOUSE] == frozenset()
    assert Capability.SEE_TRUST not in ROLE_CAPABILITIES[Role.ENGINEER]


def test_a_person_without_reports_to_must_be_the_boss():
    loose = {"id": "w", "name": "W", "role": "field", "department": "a"}
    with pytest.raises(ConfigError, match="no reports_to"):
        Org.from_records(people(loose))


def test_consent_without_a_phone_is_refused():
    odd = {"id": "w", "name": "W", "role": "field", "department": "a", "reports_to": "max"}
    with pytest.raises(ConfigError, match="no phone"):
        Org.from_records(people({**odd, "call_consent": True}))


def test_digest_changes_with_the_people():
    first = Org.from_records(sample_people())
    other = Org.from_records(
        people({"id": "w", "name": "W", "role": "field", "department": "a", "reports_to": "max"})
    )
    assert first.digest != other.digest
    assert first.digest == Org.from_records(list(reversed(sample_people()))).digest


def test_assigned_by_without_issue_capability_is_refused():
    org = Org.from_records(sample_people())
    with pytest.raises(ConfigError, match="cannot issue"):
        load_extras([raw_task(spec="Do it.", assigned_by="Finn Field")], org)


def test_assigned_by_resolves_a_name_or_an_id_to_the_id():
    org = Org.from_records(sample_people())
    by_name = load_extras([raw_task(spec="x", assigned_by="Max Manager")], org)
    by_id = load_extras([raw_task(spec="x", assigned_by="max")], org)
    assert by_name["TC-2001"].assigned_by == by_id["TC-2001"].assigned_by == "max"


def test_conflicting_duplicate_extras_are_refused():
    org = Org.from_records(sample_people())
    twice = [raw_task(spec="one"), raw_task(spec="two")]
    with pytest.raises(ConfigError, match="different spec fields"):
        load_extras(twice, org)


def test_identical_duplicates_and_records_without_extras_are_fine():
    org = Org.from_records(sample_people())
    records = [raw_task(spec="one"), raw_task(spec="one"), raw_task(task_id="TC-2002")]
    assert set(load_extras(records, org)) == {"TC-2001"}


def test_a_code_path_cannot_leave_the_code_root():
    org = Org.from_records(sample_people())
    for bad in ("/etc/passwd", "../secrets", "a/../../b"):
        with pytest.raises(ConfigError, match="code_path"):
            load_extras([raw_task(code_path=bad)], org)


def test_extras_leave_the_normalised_task_unchanged():
    plain = raw_task()
    rich = raw_task(spec="Must log.", assigned_by="max", code_path="src", created="2026-10-20")
    assert normalise_task(rich) == normalise_task(plain)
