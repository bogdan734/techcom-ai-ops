from followup.team.spec_terms import (
    contains_verbatim,
    identifiers,
    key_terms,
    limits,
    normalise_ws,
    requirement_sentences,
    sentences,
    spec_hash,
)

SPEC = """
- The tool must log every splice to `splice_log`.
- Splice loss should stay under 0.5 dB, within 48 hours of the visit.
1. Technicians need to run `followup run --dry-run` and call /messages first.
Mustard is not a requirement. Nothing else here.
"""


def test_needs_to_is_a_requirement_and_mustard_is_not():
    found = requirement_sentences(SPEC)
    assert len(found) == 3
    assert any("need to run" in sentence for sentence in found)
    assert not any("Mustard" in sentence for sentence in found)


def test_bullets_are_stripped_and_every_sentence_stays_verbatim():
    for sentence in sentences(SPEC):
        assert contains_verbatim(SPEC, sentence)
    assert sentences(SPEC)[0].startswith("The tool must")


def test_backticks_are_stripped():
    assert "splice_log" in identifiers(SPEC)
    assert not any("`" in name for name in identifiers(SPEC))


def test_flags_and_routes_are_identifiers():
    found = identifiers("Add --dry-run, call /messages and set retry_count; see TaskExtras.")
    assert found == ["--dry-run", "/messages", "retry_count", "TaskExtras"]


def test_identifiers_keep_order_and_drop_duplicates():
    assert identifiers("`a_b` then `c_d` then `a_b`") == ["a_b", "c_d"]


def test_and_or_is_not_a_route():
    assert identifiers("Use and/or wisely, 1/2 of the time.") == []


def test_limits_find_numbers_with_units_and_comparators():
    found = limits(SPEC)
    assert "under 0.5 dB" in found
    assert "within 48 hours" in found
    assert not any("run" in item for item in found)


def test_whitespace_differences_match_verbatim():
    assert contains_verbatim("must   log\n every  splice", "must log every splice")


def test_case_change_does_not_match():
    assert not contains_verbatim("Must log every splice", "must log every splice")


def test_an_empty_quote_proves_nothing():
    assert not contains_verbatim("anything", "   ")


def test_key_terms_come_from_the_quote():
    quote = "Splice loss should stay under 0.5 dB at the closeout visit"
    terms = key_terms(quote)
    assert len(terms) == 4 and "0.5" in terms
    assert all(term in normalise_ws(quote).lower() for term in terms)


def test_key_terms_drop_stopwords_and_short_words():
    assert key_terms("It is to be on a go") == ()


def test_spec_hash_ignores_whitespace_edits():
    assert spec_hash("must  log\nit") == spec_hash("must log it")
    assert spec_hash("must log it") != spec_hash("must log them")
