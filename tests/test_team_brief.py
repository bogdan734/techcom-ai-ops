from followup.team.brief import structure
from followup.team.spec_terms import contains_verbatim

GOOD = (
    "Splice all 48 fibers on the Elm Street trunk cable. "
    "Splice loss should stay under 0.5 dB per splice. "
    "The crew must record the OTDR trace for each splice. "
    "Photos of every closure need to be uploaded within 48 hours. "
    "The crew lead signs the closeout sheet."
)


def test_the_brief_puts_the_goal_first_and_sorts_the_rest():
    brief = structure(GOOD, due="2026-10-30")
    assert brief.goal == "Splice all 48 fibers on the Elm Street trunk cable."
    assert len(brief.requirements) == 3 and brief.other == (
        "The crew lead signs the closeout sheet.",
    )
    assert "under 0.5 dB" in brief.limits and "within 48 hours" in brief.limits
    assert brief.gaps == ()


def test_everything_in_a_brief_is_a_sentence_the_author_wrote():
    brief = structure(GOOD)
    for text in (brief.goal, *brief.requirements, *brief.other):
        assert contains_verbatim(GOOD, text)


def test_a_goal_that_is_also_a_requirement_is_not_listed_twice():
    brief = structure("The crew must install four handholes. Photos should be taken.")
    assert brief.goal == "The crew must install four handholes."
    assert brief.requirements == ("Photos should be taken.",)


def test_a_short_vague_spec_without_numbers_is_asked_about():
    gaps = structure("Fix the cable soon and make it good.", due="").gaps
    text = " | ".join(gaps)
    assert "very short" in text and "Write at least one requirement" in text
    assert "No measurable limit" in text and "'soon'" in text and "'good'" in text
    assert "No due date" in text


def test_a_due_date_is_only_asked_for_when_the_caller_knows_it_is_missing():
    assert not any("due date" in g for g in structure(GOOD).gaps)
    assert any("due date" in g for g in structure(GOOD, due="").gaps)


def test_an_engineer_task_has_to_name_what_will_be_checked_in_the_code():
    plain = "Write the exporter for the route survey. It must finish within 24 hours of the survey."
    assert any("Name the commands" in g for g in structure(plain, "engineer").gaps)
    assert not any("Name the commands" in g for g in structure(plain, "field").gaps)
    assert not any(
        "Name the commands" in g for g in structure(plain + " Use `--export-dxf`.", "engineer").gaps
    )


def test_a_very_long_sentence_is_flagged():
    long = "The crew must " + " ".join(["document"] * 45) + " everything."
    assert any("Split a long sentence" in g for g in structure(long + " Done within 3 days.").gaps)


def test_an_empty_spec_has_no_goal_and_says_so():
    brief = structure("")
    assert brief.goal == "" and brief.requirements == () and brief.gaps
