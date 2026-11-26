"""Signed access reviews (/access-review/reviews/): the reviewer, the demonstration supervisor who
holds the review capability, opens the Reviews tab and the demonstration's signed review (a grant
decided for removal and still held, and its CSV), records a staff access review on the full page
(missing decisions and a wrong password are refused, the decisions are kept), is refused and shown
the new grant when an account's grants change while the form is open, reviews client portal
accounts, uses the form and a review page on a phone in the dark theme, and staff without the
capability are refused. Synthetic demonstration data only."""

import csv
import io
import re

import pytest
from conftest import NARROW
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from helpers import field, native_select, sign_in
from playwright.sync_api import Page, expect

from audit_review.models import AccessReview

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
REVIEWER = "demo_supervisor"
REVIEWS = "/access-review/reviews/"
NEW = "/access-review/reviews/new/"
FORMER = "former_technician"
FINDINGS = "Synthetic finding: every role matches the person's job."
FOLLOW_UP = "Synthetic follow-up: ticket 0000 removes one grant."
STALE_START = "The accounts or what they hold changed after you opened this review"


def no_sideways_scroll(page: Page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "the page scrolls sideways"
    )


def open_reviews(page: Page):
    page.goto("/access-review/accounts/")
    page.get_by_role("navigation", name="Access review lists").get_by_role(
        "link", name="Reviews"
    ).click()
    expect(page).to_have_url(re.compile(r"/access-review/reviews/$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Access review: reviews")


def grants(page: Page):
    """Every grant's decision fieldset of the review form."""
    return page.locator("fieldset.vr-decision")


def grant(page: Page, username: str, label: str):
    """The decision fieldset of one grant of one account."""
    account = page.locator("li.vr-review-account").filter(has_text=username)
    expect(account).to_have_count(1)
    return account.locator("fieldset.vr-decision").filter(has_text=label)


def decide(fieldset, decision: str):
    fieldset.get_by_label(decision, exact=True).check()


def decide_all(page: Page, *, remove=()):
    """Keep every grant not yet decided, except those whose fieldsets are in `remove`."""
    for index in range(grants(page).count()):
        fieldset = grants(page).nth(index)
        if fieldset.get_by_label("Keep", exact=True).is_checked():
            continue
        if fieldset.get_by_label("Remove", exact=True).is_checked():
            continue
        decide(fieldset, "Keep")
    for fieldset in remove:
        decide(fieldset, "Remove")


def sign(page: Page, password: str):
    field(page, "Your password").fill(password)
    page.get_by_role("button", name="Sign access review", exact=True).click()


def fact(page: Page, label: str):
    return (
        page.locator("dt")
        .filter(has_text=re.compile(rf"^\s*{re.escape(label)}\s*$"))
        .locator("xpath=following-sibling::dd[1]")
    )


def panel(page: Page, title: str):
    return page.get_by_role("region", name=re.compile(rf"^{re.escape(title)}"))


def open_form(page: Page):
    page.goto(REVIEWS)
    page.get_by_role("link", name="Record an access review").first.click()
    expect(page).to_have_url(re.compile(r"/access-review/reviews/new/$"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Record an access review")
    # A full page, never the form dialog.
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    expect(grants(page).first).to_be_visible()


def test_a_reviewer_opens_the_signed_review_and_exports_its_decisions(page: Page, demo):
    seeded = AccessReview.objects.get()
    assert re.fullmatch(r"AC-\d{6}", seeded.number)
    sign_in(page, REVIEWER, demo.password)
    open_reviews(page)
    table = page.get_by_role("table", name="Access reviews")
    expect(table.locator("tbody tr")).to_have_count(1)
    page.get_by_role("link", name=seeded.number).click()
    expect(page).to_have_url(re.compile(rf"/access-review/reviews/{seeded.pk}/$"))
    expect(page.get_by_role("heading", level=1)).to_contain_text(f"Access review {seeded.number}")

    removal = panel(page, "To remove")
    expect(removal.locator("li.vr-review-removal")).to_have_count(1)
    expect(removal).to_contain_text("Technical staff role")
    expect(removal).to_contain_text(FORMER)
    expect(removal).to_contain_text("Still held")
    expect(removal).not_to_contain_text("No longer held")
    expect(page.get_by_text("Removals still held")).to_be_visible()
    expect(panel(page, "Every grant reviewed")).to_contain_text("Keep")
    expect(page.get_by_text("Matches the access review as stored.")).to_be_visible()
    expect(page.get_by_role("heading", name="Sign-off")).to_be_visible()

    with page.expect_download() as download:
        page.get_by_role("link", name="Export CSV").click()
    with open(download.value.path(), encoding="utf-8-sig") as handle:
        header, *lines = list(csv.reader(io.StringIO(handle.read())))
    assert header[:2] == ["Access review", "Person"]
    columns = {name: header.index(name) for name in ("Username", "Grant", "Decision", "Held now")}
    assert len(lines) == seeded.grants
    row = [line for line in lines if line[columns["Username"]] == FORMER]
    assert len(row) == 1 and row[0][0] == seeded.number
    assert row[0][columns["Grant"]] == "Technical staff role"
    assert row[0][columns["Decision"]] == "Remove"
    assert row[0][columns["Held now"]] == "Yes"
    kept = [line for line in lines if line[columns["Decision"]] == "Keep"]
    assert kept and all(line[columns["Held now"]] == "" for line in kept)
    assert page.console_errors == []


def test_a_reviewer_records_a_staff_access_review_and_signs_it(page: Page, demo):
    seeded = AccessReview.objects.get()
    sign_in(page, REVIEWER, demo.password)
    open_form(page)
    total = grants(page).count()
    assert total > 3
    expect(page.get_by_text(f"{total} grants", exact=False).first).to_be_visible()
    # No decision is chosen beforehand.
    expect(page.locator("fieldset.vr-decision input[type=radio]:checked")).to_have_count(0)
    expect(grant(page, FORMER, "Technical staff role")).to_be_visible()
    # Each person is a fieldset named by the person and the username.
    person = page.get_by_role("group", name=re.compile(r"\bdemo_technical$"))
    expect(person).to_have_count(1)
    expect(person.get_by_role("group", name=re.compile("^Technical staff role"))).to_have_count(1)

    # Sent with nothing decided: refused, nothing signed.
    findings = field(page, "Findings")
    findings.fill(FINDINGS)
    field(page, "Follow-up").fill(FOLLOW_UP)
    sign(page, demo.password)
    expect(page.get_by_text("Check the highlighted fields below").first).to_be_visible()
    expect(page.get_by_text("Choose Keep or Remove.")).to_have_count(total)
    expect(page.get_by_text(f"{total} grants still need a decision.")).to_be_visible()
    assert AccessReview.objects.count() == 1
    expect(field(page, "Findings")).to_have_value(FINDINGS)
    expect(field(page, "Your password")).to_have_value("")

    # Every grant decided: keep all, remove one.
    technical = grant(page, "demo_technical", "Technical staff role")
    decide_all(page, remove=[technical])
    sign(page, "not-the-password")
    expect(page.get_by_text("Incorrect password.")).to_be_visible()
    assert AccessReview.objects.count() == 1
    expect(technical.get_by_label("Remove", exact=True)).to_be_checked()
    expect(grants(page).locator("input[type=radio][value=keep]:checked")).to_have_count(total - 1)
    expect(field(page, "Findings")).to_have_value(FINDINGS)
    expect(field(page, "Your password")).to_have_value("")
    expect(page.get_by_text("Choose Keep or Remove.")).to_have_count(0)

    sign(page, demo.password)
    review = AccessReview.objects.latest("pk")
    assert AccessReview.objects.count() == 2 and review.number != seeded.number
    expect(page).to_have_url(re.compile(rf"/access-review/reviews/{review.pk}/$"))
    expect(page.get_by_role("heading", level=1)).to_contain_text(f"Access review {review.number}")
    expect(page.get_by_text(f"Access review {review.number} signed.")).to_be_visible()
    removal = panel(page, "To remove")
    expect(removal.locator("li.vr-review-removal")).to_have_count(1)
    expect(removal).to_contain_text("Technical staff role")
    expect(removal).to_contain_text("demo_technical")
    expect(removal).to_contain_text("Still held")
    expect(fact(page, "Grants")).to_have_text(str(total))
    expect(fact(page, "Decided to remove")).to_have_text("1")
    expect(fact(page, "Findings")).to_have_text(FINDINGS)
    expect(fact(page, "Follow-up")).to_have_text(FOLLOW_UP)
    expect(panel(page, "Every grant reviewed")).to_contain_text(str(total))
    expect(panel(page, "Every grant reviewed").locator("li.vr-review-item")).to_have_count(total)
    expect(page.get_by_text("Matches the access review as stored.")).to_be_visible()
    assert (review.grants, review.removals) == (total, 1)

    # The list shows the new review first.
    page.get_by_role("navigation", name="Breadcrumb").get_by_role("link", name="Reviews").click()
    table = page.get_by_role("table", name="Access reviews")
    expect(table.locator("tbody tr")).to_have_count(2)
    expect(table.locator("tbody tr").first).to_contain_text(review.number)
    expect(table.locator("tbody tr").last).to_contain_text(seeded.number)
    assert page.console_errors == []


def test_grants_that_change_while_the_form_is_open_refuse_the_signing(page: Page, demo):
    seeded = AccessReview.objects.get()
    sign_in(page, REVIEWER, demo.password)
    open_form(page)
    total = grants(page).count()

    # Someone gives an account a role after the form was drawn.
    account = User.objects.get(username="demo_client_services")
    assert not account.groups.filter(name="technical").exists()
    account.groups.add(Group.objects.get(name="technical"))

    removed = grant(page, "demo_supervisor", "Supervisor role")
    decide_all(page, remove=[removed])
    field(page, "Findings").fill(FINDINGS)
    sign(page, demo.password)

    expect(page.get_by_text(STALE_START)).to_be_visible()
    assert AccessReview.objects.count() == 1
    new = page.locator("fieldset.vr-decision").filter(has_text="New or changed")
    expect(new).to_have_count(1)
    expect(new).to_contain_text("Technical staff role")
    expect(page.locator("li.vr-review-account").filter(has=new)).to_contain_text(
        "demo_client_services"
    )
    assert grants(page).count() == total + 1
    expect(new.locator("input[type=radio]:checked")).to_have_count(0)
    # The decisions of the grants that did not change are kept, with what was typed.
    expect(grants(page).locator("input[type=radio]:checked")).to_have_count(total)
    expect(removed.get_by_label("Remove", exact=True)).to_be_checked()
    expect(field(page, "Findings")).to_have_value(FINDINGS)
    expect(field(page, "Your password")).to_have_value("")
    expect(page.get_by_text("Choose Keep or Remove.")).to_have_count(0)

    # Deciding the new grant and signing again, with the password again.
    sign(page, demo.password)
    expect(page.get_by_text("Choose Keep or Remove.")).to_have_count(1)
    expect(page.get_by_text("1 grant still needs a decision.")).to_be_visible()
    assert AccessReview.objects.count() == 1
    undecided = grants(page).filter(has_text="Choose Keep or Remove.")
    expect(undecided).to_have_count(1)
    expect(undecided).to_contain_text("Technical staff role")
    decide(undecided, "Keep")
    sign(page, demo.password)
    review = AccessReview.objects.latest("pk")
    assert review.pk != seeded.pk
    expect(page).to_have_url(re.compile(rf"/access-review/reviews/{review.pk}/$"))
    expect(fact(page, "Grants")).to_have_text(str(total + 1))
    expect(fact(page, "Decided to remove")).to_have_text("1")
    expect(panel(page, "To remove")).to_contain_text("Supervisor role")
    expect(page.get_by_text("Matches the access review as stored.")).to_be_visible()
    assert page.console_errors == []


def test_client_portal_accounts_are_chosen_and_listed_with_their_scope(page: Page, demo):
    sign_in(page, REVIEWER, demo.password)
    page.goto(NEW)
    choice = page.get_by_role("heading", name="Accounts to review").locator(
        "xpath=ancestor::*[self::section or self::div][.//form][1]"
    )
    native_select(field(choice, "Accounts to review")).select_option(label="Client portal accounts")
    expect(field(choice, "Organization")).to_be_visible()
    choice.get_by_role("button", name="Show", exact=True).click()
    expect(page).to_have_url(re.compile(r"scope=portal"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Record an access review")

    portal = grants(page)
    expect(portal.first).to_be_visible()
    count = portal.count()
    assert count >= 1
    # Every grant is the portal account, with what of its organization it may see.
    for index in range(count):
        item = portal.nth(index)
        expect(item.locator("legend")).to_contain_text("Portal account")
        expect(item.locator("legend")).to_contain_text(
            re.compile(r"Only the tickets it raised|Its organization's tickets|Tickets of ")
        )
        expect(item.get_by_label("Keep", exact=True)).not_to_be_checked()
    # Each account carries its organization.
    accounts = page.locator("li.vr-review-account")
    expect(accounts.first).to_contain_text(re.compile(r"\S"))
    assert accounts.count() == count
    expect(page.get_by_text("Staff accounts leave out").first).to_be_visible()
    assert page.console_errors == []


@pytest.mark.parametrize("theme", ["dark"])
def test_a_phone_fits_the_form_and_a_review_and_the_choices_are_usable(page: Page, demo, theme):
    page.set_viewport_size(NARROW)
    page.emulate_media(color_scheme=theme)
    sign_in(page, REVIEWER, demo.password)
    seeded = AccessReview.objects.get()

    page.goto(REVIEWS)
    expect(page.get_by_role("link", name=seeded.number).first).to_be_visible()
    no_sideways_scroll(page)

    open_form(page)
    no_sideways_scroll(page)
    first = grants(page).first
    first.scroll_into_view_if_needed()
    for decision in ("Keep", "Remove"):
        radio = first.get_by_label(decision, exact=True)
        expect(radio).to_be_visible()
        box = radio.bounding_box()
        assert box and box["width"] > 0 and box["height"] > 0
        assert box["x"] >= 0 and box["x"] + box["width"] <= NARROW["width"] + 1, decision
    # The radio can be chosen by tapping its label, and the choice sticks.
    first.get_by_text("Remove", exact=True).click()
    expect(first.get_by_label("Remove", exact=True)).to_be_checked()
    first.get_by_text("Keep", exact=True).click()
    expect(first.get_by_label("Keep", exact=True)).to_be_checked()
    expect(first.get_by_label("Remove", exact=True)).not_to_be_checked()
    for label in ("Findings", "Follow-up", "Your password"):
        control = field(page, label)
        control.scroll_into_view_if_needed()
        box = control.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= NARROW["width"] + 1, label
    submit = page.get_by_role("button", name="Sign access review", exact=True)
    submit.scroll_into_view_if_needed()
    box = submit.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= NARROW["width"] + 1
    no_sideways_scroll(page)
    page.screenshot(path="/tmp/labops-asg-form-phone.png")

    page.goto(f"{REVIEWS}{seeded.pk}/")
    expect(page.get_by_text("Matches the access review as stored.")).to_be_visible()
    expect(panel(page, "To remove")).to_contain_text("Still held")
    no_sideways_scroll(page)
    page.screenshot(path="/tmp/labops-asg-review-phone.png", full_page=True)
    assert page.console_errors == []


def test_staff_without_the_capability_are_refused_the_reviews(page: Page, demo):
    seeded = AccessReview.objects.get()
    sign_in(page, "demo_technical", demo.password)
    page.goto("/")
    expect(page.locator("nav.vr-sidenav").get_by_role("link", name="Access review")).to_have_count(
        0
    )
    for address in (REVIEWS, NEW, f"{REVIEWS}{seeded.pk}/"):
        response = page.goto(address)
        assert response is not None and response.status == 403, address
