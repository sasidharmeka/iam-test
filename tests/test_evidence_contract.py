from iam_agent.evidence import USER_SCOPED_TABLES


def test_bigquery_tables_are_fixed_and_match_pipeline_outputs() -> None:
    assert len(USER_SCOPED_TABLES) == len(set(USER_SCOPED_TABLES))
    assert "risk_scores" in USER_SCOPED_TABLES
    assert "identity_timelines" in USER_SCOPED_TABLES
    assert "shadow_access_events" in USER_SCOPED_TABLES
    assert "action_queue" not in USER_SCOPED_TABLES
