from arker.generated.api_models import PatchVmRequest


def test_patch_vm_request_preserves_positional_description() -> None:
    assert PatchVmRequest("nightly").description == "nightly"
