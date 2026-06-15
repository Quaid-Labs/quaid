from lib.session_text import parse_message_pairs


def test_parse_message_pairs_preserves_plain_user_assistant_roles():
    pairs = parse_message_pairs("User: alpha\nAssistant: beta")

    assert pairs == [{"user_text": "alpha", "assistant_text": "beta"}]


def test_parse_message_pairs_accepts_subagent_role_prefixes():
    pairs = parse_message_pairs("Subagent/User: alpha\nSubagent/Assistant: beta")

    assert pairs == [{"user_text": "alpha", "assistant_text": "beta"}]


def test_parse_message_pairs_accepts_unicode_agent_label_role_prefixes():
    pairs = parse_message_pairs("調査員/User: 会議は三時\n調査員/Assistant: 了解")

    assert pairs == [{"user_text": "会議は三時", "assistant_text": "了解"}]


def test_parse_message_pairs_does_not_guess_localized_role_labels():
    transcript = "用户: 会議は三時\n助手: 了解"

    assert parse_message_pairs(transcript) == [{"user_text": transcript, "assistant_text": ""}]
