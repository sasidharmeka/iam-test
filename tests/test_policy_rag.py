from iam_agent.policy_rag import PolicyStore


def test_table_names_retrieve_matching_policy_terms(tmp_path) -> None:
    (tmp_path / "policy.md").write_text(
        "# IAM Policy\n\n## Shadow access\nShadow access requires review.",
        encoding="utf-8",
    )
    store = PolicyStore(tmp_path)

    chunks = store.retrieve("shadow_access_events")

    assert chunks
    assert "Shadow access" in chunks[0].text
