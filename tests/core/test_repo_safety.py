import httpx
import pytest

from postpilot.core.repo_safety import SafetyResult, check_repo, finalize_links
from postpilot.sources.github import GitHubClient


@pytest.mark.parametrize(
    ("visibility", "readme_status", "license_status", "expected"),
    [
        ("private", 200, 200, ("not_public",)),
        ("internal", 200, 200, ("not_public",)),
        ("public", 404, 200, ("missing_readme",)),
        ("public", 200, 404, ("missing_license",)),
        ("public", 404, 404, ("missing_readme", "missing_license")),
        ("public", 404, 403, ("check_failed",)),
        ("public", 503, 200, ("check_failed",)),
        ("public", 200, 200, ()),
    ],
)
def test_check_repo(visibility, readme_status, license_status, expected):
    def respond(request):
        assert request.method == "GET"
        path = request.url.path
        if path.endswith("/readme"):
            return httpx.Response(readme_status)
        if path.endswith("/license"):
            return httpx.Response(license_status)
        return httpx.Response(
            200,
            json={
                "name": "demo",
                "html_url": "https://github.com/owner/demo",
                "visibility": visibility,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert check_repo(GitHubClient("owner", client=client), "demo") == SafetyResult(
            not expected, expected
        )


def test_transport_error_blocks_the_repo():
    def respond(request):
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert check_repo(GitHubClient("owner", client=client), "demo") == SafetyResult(
            False, ("check_failed",)
        )


def test_auto_fix_requires_later_review():
    def respond(request):
        pytest.fail("Auto-fix must not make a request.")

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        pytest.raises(NotImplementedError, match="reviewed in a later task"),
    ):
        check_repo(GitHubClient("owner", client=client), "demo", auto_fix_license=True)


def test_finalize_links_removes_all_forms_of_the_target_only():
    repo_url = "https://github.com/owner/demo"
    text = (
        "See <https://GitHub.com/OWNER/DEMO/> or [source](http://github.com/owner/demo/tree/main). "
        "github.com/owner/demo?tab=readme#intro //www.github.com/owner/%64emo.git "
        "https://reader@github.com/owner/demo "
        "https://github.com/owner/demo-other https://github.com/other/demo"
    )
    assert finalize_links(text, repo_url, True) == text
    assert finalize_links(text, repo_url, False) == (
        "See <> or [source]().    https://github.com/owner/demo-other https://github.com/other/demo"
    )
