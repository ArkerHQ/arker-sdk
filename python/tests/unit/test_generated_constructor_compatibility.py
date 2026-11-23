import dataclasses

import pytest

from arker.generated import api_models as models


@pytest.mark.parametrize(
    ("model_name", "previous_last"),
    [
        ("ForkRequest1", "command"),
        ("ForkRequest2", "command"),
        ("ForkRequest3", "command"),
        ("ForkRequest4", "command"),
        ("ForkRequest5", "command"),
        ("ForkRequest6", "command"),
        ("PatchVmRequest", "policies"),
        ("Vm", "pool_id"),
    ],
)
def test_autoscaling_appends_after_existing_optional_parameters(model_name: str, previous_last: str) -> None:
    fields = [field.name for field in dataclasses.fields(getattr(models, model_name))]
    assert fields.index(previous_last) < fields.index("resources_autoscaling")


def test_patch_vm_keeps_its_positional_description() -> None:
    request = models.PatchVmRequest("workspace description")
    assert request.description == "workspace description"
    assert request.resources_autoscaling is None
