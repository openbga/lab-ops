"""Default browser policy and explicit response overrides: no page can be framed."""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from core.security_headers import (
    DEFAULT_CONTENT_SECURITY_POLICY,
    DEFAULT_PERMISSIONS_POLICY,
    SecurityHeadersMiddleware,
)


@pytest.mark.parametrize("status", [200, 302, 403, 413, 500])
def test_default_headers_apply_without_reading_body(status):
    request = RequestFactory().post("/synthetic/", data=b"synthetic", content_type="text/plain")
    response = SecurityHeadersMiddleware(lambda r: HttpResponse(status=status))(request)
    assert response["Content-Security-Policy"] == DEFAULT_CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == DEFAULT_PERMISSIONS_POLICY
    assert not request._read_started
    assert "unsafe-inline" not in response["Content-Security-Policy"]
    assert "unsafe-eval" not in response["Content-Security-Policy"]


def test_explicit_response_policies_are_preserved():
    response = HttpResponse(
        headers={
            "Content-Security-Policy": "default-src 'none'",
            "Permissions-Policy": "camera=(self)",
        }
    )
    result = SecurityHeadersMiddleware(lambda r: response)(RequestFactory().get("/"))
    assert result["Content-Security-Policy"] == "default-src 'none'"
    assert result["Permissions-Policy"] == "camera=(self)"


@pytest.mark.parametrize("status", [200, 403, 409, 503])
def test_no_page_can_be_framed_even_when_a_view_names_a_frame_exception(status):
    request = RequestFactory().get("/synthetic/preview/")
    response = SecurityHeadersMiddleware(
        lambda r: HttpResponse(status=status, headers={"X-Frame-Options": "SAMEORIGIN"})
    )(request)
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"]
    assert "frame-ancestors 'self'" not in response["Content-Security-Policy"]
    assert "object-src 'none'" in response["Content-Security-Policy"]


@override_settings(CONTENT_SECURITY_POLICY="default-src 'none'; frame-ancestors 'none'")
def test_deployer_policy_is_used_verbatim():
    result = SecurityHeadersMiddleware(lambda r: HttpResponse())(RequestFactory().get("/"))
    assert result["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_real_middleware_adds_headers_to_early_request_limit_refusals(client, settings):
    response = client.post(
        "/accounts/login/",
        data=b"x",
        content_type="text/plain",
        CONTENT_LENGTH=str(settings.MAX_REQUEST_BODY_BYTES + 1),
    )
    assert response.status_code == 413
    assert response["Content-Security-Policy"] == DEFAULT_CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == DEFAULT_PERMISSIONS_POLICY
    assert response["X-Request-ID"]


@pytest.mark.django_db
def test_a_real_page_is_refused_to_every_frame(client):
    response = client.get("/accounts/login/")
    assert response["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"]


def test_static_files_are_same_origin_only_and_cannot_be_framed(settings):
    # WhiteNoise answers before the clickjacking middleware, so it adds the header itself.
    headers = {}
    settings.WHITENOISE_ADD_HEADERS_FUNCTION(headers, "css/app.css", "/static/x")
    assert headers == {"X-Frame-Options": "DENY"}
    assert settings.WHITENOISE_ALLOW_ALL_ORIGINS is False


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/", "/clients/", "/portal/", "/lab/profile/"])
def test_the_workspace_requires_sign_in(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/")
