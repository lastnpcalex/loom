from pathlib import Path


def test_loom_agent_contract_is_role_neutral():
    from loom_agent_prompt import load_loom_agent_prompt

    text = load_loom_agent_prompt()

    assert "inside Loom" in text
    assert "Do not assume the task is software engineering" in text
    assert "Claude Code backed by a local model" in text
    assert "inside the active Loom instance" in text
    assert "Do not stop, restart, kill, replace, or reconfigure either host" in text
    assert "leave that lifecycle action to the human" in text
    assert "files currently on disk are the canonical workspace state" in text
    assert "never use them as a substitute for reading the live file" in text
    assert "Destructive Git operations are allowed only after Loom presents" in text
    assert "normally end in a focused local commit" in text
    assert "A dirty worktree is not a reason" in text
    assert "Stage only the files or hunks attributable to the current task" in text
    assert "Pushing, force-pushing, rebasing shared history" in text
    assert "focused code agent" not in text.lower()


def test_claude_ordinary_sessions_append_loom_contract():
    import claude_client

    appended = claude_client._loom_append_system_prompt("Extra provider note")

    assert appended is not None
    assert "inside Loom" in appended
    assert "Extra provider note" in appended


def test_claude_special_roles_do_not_get_generic_contract():
    import claude_client

    assert claude_client._loom_append_system_prompt(
        None, backstage_parent_id=12
    ) is None
    assert claude_client._loom_append_system_prompt(
        "Operator", nrol_operator=True
    ) == "Operator"


def test_codex_ordinary_prompt_gets_loom_contract_without_agents_md(tmp_path):
    import codex_client

    prompt = codex_client._prepare_codex_prompt("run the script")

    assert "<loom_agent_contract provider=\"codex\">" in prompt
    assert "<user_task>\nrun the script\n</user_task>" in prompt
    assert not (tmp_path / "AGENTS.md").exists()


def test_codex_special_roles_keep_original_prompt():
    import codex_client

    assert codex_client._prepare_codex_prompt(
        "edit cards", backstage_parent_id=99
    ) == "edit cards"
    assert codex_client._prepare_codex_prompt(
        "audit topic", nrol_operator=True
    ) == "audit topic"


def test_agy_ordinary_prompt_gets_loom_contract_without_gemini_md(tmp_path):
    import gemini_client

    prompt = gemini_client._prepare_agy_prompt("run the script")

    assert "<loom_agent_contract provider=\"agy\">" in prompt
    assert "<user_task>\nrun the script\n</user_task>" in prompt
    assert not (tmp_path / "GEMINI.md").exists()


def test_agy_special_roles_keep_original_prompt():
    import gemini_client

    assert gemini_client._prepare_agy_prompt(
        "edit cards", backstage_parent_id=99
    ) == "edit cards"
    assert gemini_client._prepare_agy_prompt(
        "audit topic", nrol_operator=True
    ) == "audit topic"


def test_existing_operator_file_landing_still_uses_dedicated_role(tmp_path):
    import codex_client

    codex_client._ensure_operator_instructions(tmp_path)
    operator_md = (
        Path(codex_client.__file__).parent / "mcp_servers" / "nrol_ao" / "OPERATOR.md"
    ).read_text(encoding="utf-8")

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == operator_md


def test_claude_llama_sessions_append_image_warning():
    import claude_client

    normal = claude_client._loom_append_system_prompt("Extra note", use_llama=False)
    llama = claude_client._loom_append_system_prompt("Extra note", use_llama=True)

    assert "Never use file-reading tools" not in normal
    assert "Never use file-reading tools" in llama


def test_claude_session_injects_model_identity():
    import claude_client

    without = claude_client._loom_append_system_prompt("Extra note")
    with_model = claude_client._loom_append_system_prompt(
        "Extra note", use_umans=True, cc_model="umans-glm-5.2"
    )
    llama_model = claude_client._loom_append_system_prompt(
        "Extra note", use_llama=True, cc_model="qwen-27b"
    )
    api_model = claude_client._loom_append_system_prompt(
        "Extra note", cc_model="claude-opus-4-7"
    )

    # No model arg → no identity block (existing tests' default path unchanged)
    assert "Operating model" not in without
    # Umans: identifies provider + flags cache accounting
    assert "Operating model" in with_model
    assert "`umans-glm-5.2`" in with_model
    assert "Umans AI" in with_model
    # Llama: routes to local llama-server
    assert "`qwen-27b`" in llama_model
    assert "local llama-server" in llama_model
    # API: routes to Anthropic
    assert "`claude-opus-4-7`" in api_model
    assert "Anthropic API" in api_model


def test_claude_special_roles_skip_model_identity():
    import claude_client

    # nrol_operator and backstage use dedicated prompts, not the contract
    assert claude_client._loom_append_system_prompt(
        "Op", nrol_operator=True, cc_model="umans-glm-5.2"
    ) == "Op"
    assert claude_client._loom_append_system_prompt(
        None, backstage_parent_id=5, cc_model="umans-glm-5.2"
    ) is None

