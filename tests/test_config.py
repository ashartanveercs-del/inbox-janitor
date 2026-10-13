from datetime import date

import pytest

from inbox_janitor.classify.rules import Rule
from inbox_janitor.config import (
    CONFIDENCE_FLOOR,
    DEFAULT_LABELS,
    DEFAULT_POLICY,
    MAX_TRASH_PER_DAY,
    Config,
    ConfigError,
    load_config,
)
from inbox_janitor.stages import Action, Stage

OWNER = "owner@harborlane.example.com"


def cfg(**over):
    return Config.from_dict({"owner": OWNER, **over})


def test_a_minimal_config_gets_safe_defaults():
    c = cfg()
    assert c.owner == OWNER
    assert c.stage is None
    assert c.min_confidence == CONFIDENCE_FLOOR == 0.95
    assert c.policy == DEFAULT_POLICY
    assert c.policy["personal"] is Action.KEEP
    assert c.trash_per_run > 0 and c.trash_per_day >= c.trash_per_run
    assert c.timezone == "UTC"
    assert c.warnings == ()


@pytest.mark.parametrize(("name", "defaults"), [
    ("policy", DEFAULT_POLICY), ("labels", DEFAULT_LABELS),
])
def test_direct_configs_get_independent_read_only_defaults(name, defaults):
    first, second = Config(owner=OWNER), Config(owner=OWNER)
    first_mapping, second_mapping = getattr(first, name), getattr(second, name)
    assert first_mapping == second_mapping == defaults
    assert first_mapping is not second_mapping
    with pytest.raises(TypeError):
        first_mapping["personal"] = "TRASH"
    assert second_mapping == defaults


@pytest.mark.parametrize("owner", [None, "", "not-an-address", 7])
def test_the_owner_address_is_required(owner):
    with pytest.raises(ConfigError, match="owner"):
        Config.from_dict({"owner": owner} if owner is not None else {})


def test_the_owner_address_is_lower_cased():
    assert cfg(owner="Owner@HarborLane.Example.com").owner == OWNER


def test_the_confidence_floor_can_be_raised():
    assert cfg(min_confidence=0.98).min_confidence == 0.98


def test_the_confidence_floor_can_never_be_lowered():
    c = cfg(min_confidence=0.6)
    assert c.min_confidence == CONFIDENCE_FLOOR
    assert any("0.95" in w for w in c.warnings)


@pytest.mark.parametrize("value", [1.5, "high", True])
def test_a_nonsense_floor_is_an_error(value):
    with pytest.raises(ConfigError, match="min_confidence"):
        cfg(min_confidence=value)


def test_the_config_may_name_a_stage():
    assert cfg(stage="file").stage is Stage.FILE


def test_an_unknown_stage_is_an_error():
    with pytest.raises(ConfigError, match="stage"):
        cfg(stage="yolo")


def test_a_partial_policy_is_merged_with_the_defaults():
    c = cfg(policy={"newsletter": "trash"})
    assert c.policy["newsletter"] is Action.TRASH
    assert c.policy["invoice"] is DEFAULT_POLICY["invoice"]


@pytest.mark.parametrize(("policy", "words"), [
    ({"memes": "trash"}, "unknown category"),
    ({"newsletter": "shred"}, "unknown action"),
    ({"newsletter": "rescue"}, "unknown action"),
    ({"personal": "trash"}, "personal"),
])
def test_bad_policies_are_refused(policy, words):
    with pytest.raises(ConfigError, match=words):
        cfg(policy=policy)


@pytest.mark.parametrize("over", [{"trash_per_run": -1}, {"trash_per_day": -5},
                                  {"trash_per_day": MAX_TRASH_PER_DAY + 1},
                                  {"trash_per_run": 10, "trash_per_day": 5},
                                  {"trash_per_run": "lots"}])
def test_caps_must_be_sane(over):
    with pytest.raises(ConfigError, match="trash_per"):
        cfg(**over)


def test_trash_after_is_a_date():
    assert cfg(trash_after="2026-10-01").trash_after == date(2026, 10, 1)
    with pytest.raises(ConfigError, match="trash_after"):
        cfg(trash_after="next tuesday")


def test_the_ledger_must_outlive_the_lookback_window():
    """Otherwise a message could fall out of the ledger while still being selected, and be
    processed - and acted on - a second time."""
    with pytest.raises(ConfigError, match="keep_days"):
        cfg(lookback_days=30, keep_days=20)


def test_an_unknown_timezone_is_an_error():
    with pytest.raises(ConfigError, match="timezone"):
        cfg(timezone="Mars/Olympus_Mons")


@pytest.mark.parametrize("name", ["TRASH", "SPAM", "Spam", " trash ", "INBOX", "UNREAD",
                                  "STARRED", "IMPORTANT", "SENT", "DRAFT", "CHAT",
                                  "CATEGORY_PROMOTIONS", "category_social"])
def test_a_label_can_never_be_a_system_label(name):
    """A label is something filing adds. 'TRASH' or 'SPAM' would turn filing (stage file)
    into deleting, past the trash caps and the backlog approval."""
    with pytest.raises(ConfigError, match="system label"):
        cfg(labels={"invoice": name})


def test_ordinary_label_names_are_accepted():
    assert cfg(labels={"invoice": "Bills/Trash-day"}).label_for("invoice") == "Bills/Trash-day"


def test_trusted_senders_must_be_addresses_or_domains():
    assert cfg(trusted_senders=["billing@flour.example.com", "@bank.example.com"]) \
        .trusted_senders == ("billing@flour.example.com", "@bank.example.com")
    with pytest.raises(ConfigError, match="trusted_senders"):
        cfg(trusted_senders=["everyone"])


def test_operator_rules_are_parsed():
    c = cfg(rules=[{"category": "receipt", "confidence": 0.99, "senders": ["@pos.example.com"]}])
    assert c.rules == (Rule("receipt", 0.99, senders=("@pos.example.com",)),)


def test_a_bad_operator_rule_is_an_error():
    with pytest.raises(ConfigError, match="rules"):
        cfg(rules=[{"category": "receipt", "confidence": 0.99}])


def test_classifier_settings():
    c = cfg(classifier={"kind": "rules+llm", "model": "small-model"})
    assert (c.classifier.kind, c.classifier.model) == ("rules+llm", "small-model")
    with pytest.raises(ConfigError, match="classifier"):
        cfg(classifier={"kind": "crystal-ball"})
    with pytest.raises(ConfigError, match="model"):
        cfg(classifier={"kind": "llm"})


@pytest.mark.parametrize(("key", "value"), [("base_url", "http://attacker.invalid/v1"),
                                            ("api_key_env", "AWS_SECRET_ACCESS_KEY")])
def test_the_config_cannot_choose_where_mail_goes_or_which_secret_is_sent(key, value):
    """The endpoint receives message content and the key: both are the operator's, set in the
    environment. An agent that can edit janitor.toml must not be able to aim them."""
    with pytest.raises(ConfigError, match="environment"):
        cfg(classifier={"kind": "llm", "model": "small-model", key: value})


def test_every_problem_is_reported_at_once():
    with pytest.raises(ConfigError) as err:
        Config.from_dict({"owner": "nope", "stage": "yolo", "trash_per_run": -1})
    text = str(err.value)
    assert "owner" in text and "stage" in text and "trash_per_run" in text


def test_unknown_keys_are_refused_so_typos_do_not_pass_silently():
    with pytest.raises(ConfigError, match="trash_per_dya"):
        cfg(trash_per_dya=5)


def test_load_config_reads_toml(tmp_path):
    path = tmp_path / "janitor.toml"
    path.write_text('owner = "owner@harborlane.example.com"\nstage = "file"\n\n'
                    '[policy]\nnewsletter = "label"\n', encoding="utf-8")
    c = load_config(path)
    assert c.stage is Stage.FILE and c.policy["newsletter"] is Action.LABEL


def test_load_config_explains_a_broken_file(tmp_path):
    path = tmp_path / "janitor.toml"
    path.write_text("owner = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(path)
