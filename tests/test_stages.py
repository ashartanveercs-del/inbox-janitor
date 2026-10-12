import pytest

from inbox_janitor.stages import REQUIRED_STAGE, Action, Stage, effective_stage, permits


def test_stages_are_ordered_trial_file_trash():
    assert Stage.TRIAL < Stage.FILE < Stage.TRASH


@pytest.mark.parametrize(
    ("action", "stage"),
    [
        (Action.KEEP, Stage.TRIAL),
        (Action.RESCUE, Stage.TRIAL),
        (Action.LABEL, Stage.FILE),
        (Action.ARCHIVE, Stage.FILE),
        (Action.TRASH, Stage.TRASH),
    ],
)
def test_each_action_needs_its_stage(action, stage):
    assert REQUIRED_STAGE[action] is stage
    assert permits(stage, action)
    if stage > Stage.TRIAL:
        assert not permits(Stage(stage - 1), action)


def test_rescue_is_the_only_change_a_trial_may_make():
    writes = [a for a in Action if a is not Action.KEEP]
    assert [a for a in writes if permits(Stage.TRIAL, a)] == [Action.RESCUE]


def test_the_file_stage_never_trashes():
    assert not permits(Stage.FILE, Action.TRASH)


@pytest.mark.parametrize("pinned", list(Stage))
@pytest.mark.parametrize("requested", [*list(Stage), None])
def test_a_requested_stage_can_only_lower_the_pin(pinned, requested):
    got = effective_stage(pinned, requested)
    assert got <= pinned
    assert got == (pinned if requested is None else min(pinned, requested))


def test_required_stages_cannot_be_edited_at_runtime():
    with pytest.raises(TypeError):
        REQUIRED_STAGE[Action.TRASH] = Stage.TRIAL  # type: ignore[index]


@pytest.mark.parametrize(("text", "stage"), [("trial", Stage.TRIAL), (" FILE ", Stage.FILE),
                                             ("Trash", Stage.TRASH)])
def test_parse_accepts_the_three_names(text, stage):
    assert Stage.parse(text) is stage


@pytest.mark.parametrize("text", ["", "delete", "2", "max", "trash!"])
def test_parse_rejects_anything_else(text):
    with pytest.raises(ValueError, match="trial, file, trash"):
        Stage.parse(text)


def test_stage_prints_as_its_name():
    assert str(Stage.FILE) == "file"
