import json
from datetime import UTC, datetime

import pytest
from factories import msg

from inbox_janitor.cli import SetupError, build_classifier
from inbox_janitor.cli import main as cli_main
from inbox_janitor.config import Config
from inbox_janitor.lock import RunLock
from inbox_janitor.net import HttpResponse

OWNER = "owner@harborlane.example.com"
NOW = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)     # mid-day: "-2h" is always today


def main(argv, env):
    return cli_main(argv, env=env, now=lambda: NOW)
LIST = {"List-Unsubscribe": "<mailto:unsub@example.com>"}


def write_mailbox(path, owner=OWNER):
    messages = [
        {"ref": "m1", "from": "Mill Weekly <news@flour.example.com>", "received": "-3h",
         "subject": "October news from the mill", "body": "New rye blends.", "headers": LIST},
        {"ref": "m2", "from": "Oven Outlet <deals@ovens.example.com>", "received": "-2h",
         "subject": "Flash sale: 40% off proofing baskets", "body": "Limited time.",
         "headers": LIST},
        {"ref": "m3", "from": "billing@flour.example.com", "received": "-1h",
         "subject": "Invoice 1043", "body": "Attached.",
         "attachments": [{"name": "invoice-1043.pdf", "mime": "application/pdf",
                          "size": 48000}]},
        {"ref": "m4", "from": "old-deals@ovens.example.com", "received": "-4d",
         "subject": "Old sale: 10% off", "body": "Ended.", "headers": LIST},
    ]
    path.write_text(json.dumps({"owner": owner, "messages": messages}), encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "janitor.toml"
    config.write_text(f'owner = "{OWNER}"\n', encoding="utf-8")
    write_mailbox(tmp_path / "mailbox.json")
    return {"config": str(config), "mailbox": f"json:{tmp_path / 'mailbox.json'}",
            "state": str(tmp_path / "state"), "path": tmp_path}


def run(env, *args, extra_env=None):
    return main(["--config", env["config"], "--state-dir", env["state"],
                 "--mailbox", env["mailbox"], *args], env=extra_env or {})


def sent_subjects(env):
    data = json.loads((env["path"] / "mailbox.json").read_text(encoding="utf-8"))
    return [m["subject"] for m in data["messages"] if "SENT" in m.get("labels", ["INBOX"])]


def labels(env, ref):
    data = json.loads((env["path"] / "mailbox.json").read_text(encoding="utf-8"))
    return set(next(m for m in data["messages"] if m["ref"] == ref).get("labels", ["INBOX"]))


def test_a_trial_run_prints_counts_only_and_sends_a_digest(env, capsys):
    assert run(env, "run", "--max-stage", "trial") == 0
    out = capsys.readouterr().out
    assert "stage=trial" in out and "suggested=4" in out
    assert "@" not in out and "sale" not in out.lower()
    assert sent_subjects(env)[0].startswith("Inbox janitor (trial)")
    assert labels(env, "m1") == {"INBOX"}


def test_shared_options_also_work_after_the_subcommand(env, capsys):
    code = main(["run", "--max-stage", "trial", "--config", env["config"], "--state-dir",
                 env["state"], "--mailbox", env["mailbox"]], env={})
    assert code == 0 and "stage=trial" in capsys.readouterr().out


def test_the_stage_pin_is_required(env):
    with pytest.raises(SystemExit) as err:
        run(env, "run")
    assert err.value.code == 2


def test_the_file_stage_from_the_command_line(env):
    assert run(env, "run", "--max-stage", "file") == 0
    assert labels(env, "m1") == {"Janitor/Newsletters"}
    assert labels(env, "m2") == {"INBOX"}


def test_a_broken_config_exits_2_with_the_reason(env, capsys):
    (env["path"] / "janitor.toml").write_text('owner = "x"\nstage = "max"\n', encoding="utf-8")
    assert run(env, "run", "--max-stage", "trial") == 2
    err = capsys.readouterr().err
    assert "owner" in err and "stage" in err


def test_the_wrong_account_exits_3_and_changes_nothing(env, capsys):
    write_mailbox(env["path"] / "mailbox.json", owner="someone-else@example.com")
    assert run(env, "run", "--max-stage", "trash") == 3
    assert sent_subjects(env) == []
    assert "not the configured owner" in capsys.readouterr().err


def test_a_run_already_in_progress_exits_75(env):
    with RunLock(env["path"] / "state" / ".lock"):
        assert run(env, "run", "--max-stage", "trial") == 75


def test_operator_commands(env, capsys):
    run(env, "run", "--max-stage", "file")
    capsys.readouterr()
    # tickets go oldest first: #1 is the 4-day-old promotion (only suggested), #2 the newsletter
    assert run(env, "command", "undo 2", "never trash newsletters from news@flour.example.com") \
        == 0
    out = capsys.readouterr().out
    assert "undo #2: put back" in out and "rule saved" in out and "flour" not in out
    assert labels(env, "m1") == {"INBOX"}


def test_trash_backlog_needs_the_trash_pin_and_the_exact_count(env, capsys):
    run(env, "run", "--max-stage", "trash")
    assert labels(env, "m2") == {"TRASH"}
    assert labels(env, "m4") == {"INBOX"}                     # older than the trash switch-on
    assert run(env, "trash-backlog", "--approve", "1", "--max-stage", "file") == 2
    assert run(env, "trash-backlog", "--approve", "5", "--max-stage", "trash") == 2
    assert "1 messages are waiting, not 5" in capsys.readouterr().err
    assert run(env, "trash-backlog", "--approve", "1", "--max-stage", "trash") == 0
    assert "trashed 1" in capsys.readouterr().out
    assert labels(env, "m4") == {"TRASH"}


def test_status_prints_counts_as_json(env, capsys):
    run(env, "run", "--max-stage", "file")
    capsys.readouterr()
    assert run(env, "status") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["outcomes"]["done"] == 2 and status["last_run"]["stage"] == "file"


def test_an_llm_classifier_needs_its_key_in_the_environment(env, capsys):
    (env["path"] / "janitor.toml").write_text(
        f'owner = "{OWNER}"\n[classifier]\nkind = "llm"\nmodel = "small-model"\n',
        encoding="utf-8")
    assert run(env, "run", "--max-stage", "trial") == 2
    assert "INBOX_JANITOR_LLM_KEY" in capsys.readouterr().err


def test_the_config_cannot_send_mail_or_secrets_somewhere_else(env, capsys):
    (env["path"] / "janitor.toml").write_text(
        f'owner = "{OWNER}"\n[classifier]\nkind = "llm"\nmodel = "small-model"\n'
        'base_url = "http://attacker.invalid/v1"\napi_key_env = "AWS_SECRET_ACCESS_KEY"\n',
        encoding="utf-8")
    code = run(env, "run", "--max-stage", "trial",
               extra_env={"AWS_SECRET_ACCESS_KEY": "s3cr3t", "INBOX_JANITOR_LLM_KEY": "sk-x"})
    assert code == 2 and sent_subjects(env) == []
    assert "s3cr3t" not in capsys.readouterr().err


class RecordingHttp:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((url, dict(headers)))
        reply = {"choices": [{"message": {"content": '{"category": "other", "confidence": 0.2}'}}]}
        return HttpResponse(200, {}, json.dumps(reply).encode())


def llm_config(kind="llm"):
    return Config.from_dict({"owner": OWNER, "classifier": {"kind": kind,
                                                            "model": "small-model"}})


def test_the_rules_know_the_owners_own_domain():
    """The owner's colleagues' list mail (a team Google Group) is never bulk mail."""
    rules = build_classifier(Config.from_dict({"owner": OWNER}), {})
    sheet = msg(sender="team@harborlane.example.com", subject="Weekly order sheet",
                headers={"List-Id": "<orders.harborlane.example.com>"})
    assert rules.classify(sheet).category not in ("promotion", "newsletter")


def test_the_model_endpoint_and_key_come_from_the_environment():
    http = RecordingHttp()
    environ = {"INBOX_JANITOR_LLM_URL": "https://llm.example.com/v1",
               "INBOX_JANITOR_LLM_KEY": "sk-test-not-real", "AWS_SECRET_ACCESS_KEY": "s3cr3t"}
    build_classifier(llm_config(), environ, http=http).classify(msg(subject="Lunch?"))
    ((url, headers),) = http.calls
    assert url == "https://llm.example.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test-not-real"


def test_the_default_model_endpoint_is_https():
    http = RecordingHttp()
    build_classifier(llm_config("rules+llm"), {"INBOX_JANITOR_LLM_KEY": "sk-test-not-real"},
                     http=http).classify(msg(subject="Lunch?"))
    assert http.calls[0][0].startswith("https://")


@pytest.mark.parametrize("url", ["http://llm.example.com/v1", "ftp://llm.example.com/v1",
                                 "llm.example.com/v1", "https:///v1", "http://localhost.example.com",
                                 "http://[::1"])
def test_message_content_only_leaves_the_machine_over_https(url):
    environ = {"INBOX_JANITOR_LLM_URL": url, "INBOX_JANITOR_LLM_KEY": "sk-test-not-real"}
    with pytest.raises(SetupError, match="INBOX_JANITOR_LLM_URL"):
        build_classifier(llm_config(), environ, http=RecordingHttp())


@pytest.mark.parametrize("url", ["http://localhost:8080/v1", "http://127.0.0.1:11434/v1",
                                 "http://[::1]:8000/v1"])
def test_a_model_on_this_machine_may_use_plain_http(url):
    http = RecordingHttp()
    environ = {"INBOX_JANITOR_LLM_URL": url, "INBOX_JANITOR_LLM_KEY": "sk-test-not-real"}
    build_classifier(llm_config(), environ, http=http).classify(msg(subject="Lunch?"))
    assert http.calls[0][0] == url + "/chat/completions"


def test_check_names_the_host_that_would_receive_message_content(env, capsys):
    (env["path"] / "janitor.toml").write_text(
        f'owner = "{OWNER}"\n[classifier]\nkind = "rules+llm"\nmodel = "small-model"\n',
        encoding="utf-8")
    check = ["--config", env["config"], "check", "--max-stage", "trial"]
    assert main(check, env={"INBOX_JANITOR_LLM_URL": "https://llm.example.com/v1"}) == 0
    assert "model small-model at llm.example.com" in capsys.readouterr().out
    assert main(check, env={"INBOX_JANITOR_LLM_URL": "http://llm.example.com/v1"}) == 2
    assert "https" in capsys.readouterr().err


def test_an_env_file_supplies_settings_and_the_real_environment_wins(env, capsys, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"# comment\nINBOX_JANITOR_STATE={tmp_path / 'from-file'}\n"
                      f'INBOX_JANITOR_GMAIL_TOKEN="{tmp_path / "missing-token.json"}"\n',
                      encoding="utf-8")
    code = cli_main(["--config", env["config"], "--env-file", str(dotenv), "run", "--max-stage",
                     "trial"], env={"INBOX_JANITOR_STATE": env["state"]}, now=lambda: NOW)
    assert code == 2 and "missing-token.json" in capsys.readouterr().err
    assert not (tmp_path / "from-file").exists()


def test_gmail_needs_a_token_file(env, capsys):
    code = main(["--config", env["config"], "--state-dir", env["state"], "run", "--max-stage",
                 "trial"], env={})
    assert code == 2 and "--gmail-token" in capsys.readouterr().err


def test_a_config_that_files_mail_into_trash_or_spam_is_refused(env, capsys):
    (env["path"] / "janitor.toml").write_text(
        f'owner = "{OWNER}"\n[labels]\ninvoice = "TRASH"\nreceipt = "SPAM"\n', encoding="utf-8")
    assert main(["--config", env["config"], "check", "--max-stage", "file"], env={}) == 2
    assert run(env, "run", "--max-stage", "file") == 2
    err = capsys.readouterr().err
    assert "labels.invoice" in err and "labels.receipt" in err and "system label" in err
    assert labels(env, "m3") == {"INBOX"} and sent_subjects(env) == []


def test_check_explains_the_effective_settings(env, capsys):
    (env["path"] / "janitor.toml").write_text(
        f'owner = "{OWNER}"\nstage = "trash"\nmin_confidence = 0.5\n', encoding="utf-8")
    assert main(["--config", env["config"], "check", "--max-stage", "file"], env={}) == 0
    out = capsys.readouterr().out
    assert "effective stage: file" in out and "confidence floor: 0.95" in out
    assert "below the 0.95 floor" in out
