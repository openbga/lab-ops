"""Phone summaries for consumers of the documented label, value and remove_url chip format.

Adopted from the fourth confirmation (.local/review-history/audit-review-confirm4.md). The chip
contract now names each chip's field (includes/grid_toolbar.html, `chips`); a chip built without
its name counts as the search when its value is the search text, as before this change. The two
cases that strip the name are that documented limit (strict xfails); every shipped list passes
named chips (core.grids.active_filters)."""

import re

import pytest
from django.template import Context, Template

from teams.models import Team


@pytest.mark.parametrize(
    ("term", "separate_search"),
    [
        pytest.param("courier", False, id="both-chips"),
        pytest.param(
            "all",
            False,
            id="literal-all",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Documented contract: a chip without its field's name counts as the "
                "search when its value is the search text (includes/grid_toolbar.html).",
            ),
        ),
        pytest.param(
            "courier",
            True,
            id="search-supplied-separately",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Documented contract: a chip without its field's name counts as the "
                "search when its value is the search text (includes/grid_toolbar.html).",
            ),
        ),
    ],
)
def test_legacy_team_chip_keeps_an_independent_search_in_the_phone_summary(
    client, staff, term, separate_search
):
    team = Team.objects.create(name=term, slug="confirmation-team")
    client.force_login(staff("confirmation-reader", "technical"))
    path = "/requests/"
    page = client.get(path, {"view": "all", "team": team.pk, "q": term})
    assert page.status_code == 200
    form = page.context["filter_form"]
    assert form.is_valid()
    assert (form.cleaned_data["team"], form.cleaned_data["q"]) == (team, term)

    # Use the documented fields for each chip supplied by the list.
    chips = [
        {key: chip[key] for key in ("label", "value", "remove_url")}
        for chip in page.context["active_filters"]
    ]
    team_chip = next(chip for chip in chips if chip["label"] == "Team")
    after = client.get(path + team_chip["remove_url"])
    assert after.status_code == 200
    remaining = after.context["filter_form"]
    assert remaining.is_valid()
    assert (remaining.cleaned_data["team"], remaining.cleaned_data["q"]) == (None, term)

    if separate_search:
        # This caller supplies structured filters as chips and free text through the q argument.
        chips = [team_chip]
    toolbar = Template("{% load ui %}{% grid_toolbar label='Filter tickets' chips=chips q=q %}")
    html = toolbar.render(
        Context({"request": page.wsgi_request, "filter_form": form, "chips": chips, "q": term})
    )
    count = re.search(r'<span class="vr-filter-count">(\d+)<span', html)
    summary = re.search(r"<span data-vr-filter-state>([^<]*)</span>", html)
    assert count and summary
    expected = {f"Team: {term}", f"Search: {term}"}
    assert (int(count[1]), set(summary[1].split(" · "))) == (2, expected)
