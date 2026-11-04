from datetime import time

import pytest

from followup.config import ConfigError
from followup.team.settings import TeamSettings
from teamkit import sample_people, team_settings, write_org


def test_no_org_file_means_no_team():
    assert TeamSettings.from_env({}) is None
    assert TeamSettings.from_env({"ORG_FILE": "  "}) is None


def test_defaults(tmp_path):
    settings = team_settings(tmp_path)
    assert settings.trust_visibility == "hidden"
    assert (settings.quiz_max_questions, settings.quiz_pass_score) == (5, 0.6)
    assert settings.calls is False
    assert settings.call_window == (time(9), time(18))


def test_unknown_visibility_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="TRUST_VISIBILITY"):
        team_settings(tmp_path, TRUST_VISIBILITY="everyone")


def test_inverted_call_window_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="CALL_WINDOW"):
        team_settings(tmp_path, CALL_WINDOW="18:00-09:00")


def test_a_malformed_number_names_the_setting(tmp_path):
    with pytest.raises(ConfigError, match="AUDIT_RATE"):
        team_settings(tmp_path, AUDIT_RATE="1.5")
    with pytest.raises(ConfigError, match="QUIZ_MAX_QUESTIONS"):
        team_settings(tmp_path, QUIZ_MAX_QUESTIONS="many")


def test_a_missing_org_file_stops_the_run(tmp_path):
    with pytest.raises(ConfigError, match="org file"):
        TeamSettings.from_env({"ORG_FILE": str(tmp_path / "nope.json")})


def test_decision_fields_see_the_window_not_the_minute(tmp_path):
    morning = team_settings(tmp_path, CALLS="true", CALL_TIME="10:00")
    noon = team_settings(tmp_path, CALLS="true", CALL_TIME="12:30")
    night = team_settings(tmp_path, CALLS="true", CALL_TIME="23:00")
    assert morning.decision_fields() == noon.decision_fields()
    assert morning.decision_fields() != night.decision_fields()


def test_decision_fields_follow_the_org(tmp_path):
    first = team_settings(tmp_path).decision_fields()
    people = sample_people()
    people[4]["name"] = "Fin Field"
    write_org(tmp_path, people)
    assert team_settings(tmp_path, people=people).decision_fields() != first
