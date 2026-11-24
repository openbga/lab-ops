"""The Security settings page (the admin role): the laboratory's security and privacy contacts,
its session limits, who must have a second sign-in factor, what protects sign-in, sessions and
patient information as read from the running deployment (core.security_settings), and the recent
changes to the contacts, the limits and the second-factor rule.

The contacts are edited through the laboratory profile's contact form, which returns here when
opened from this page; the session limits and the second-factor rule through their own forms
below, which return here too.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from accounts.roles import ROLE_LABELS, require_role

from . import lab_profile, second_factor, security_settings, session_limits
from .concurrency import one_reading, reading_account
from .lab_profile_forms import SecondFactorRuleForm, SessionLimitsForm
from .lab_profile_views import (
    CONTACT_ACTION,
    HISTORY_LIMIT,
    _service_errors,
    contact_label,
    menu_reading,
)
from .models import AuditEvent, Lab, SessionLimit


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
def security_settings_page(request):
    # One reading for the role check and the whole page: the access decision, the contacts,
    # their history and the counts describe one state. The page is drawn after the reading, so
    # what drawing writes (What's new, preferences) is not done inside it.
    with one_reading():
        reader = reading_account(request)
        require_role(reader, *security_settings.SECURITY_ROLES)
        roles = menu_reading(request, reader)
        lab = Lab.objects.filter(pk=1).first()
        groups = security_settings.in_force()
        rule = second_factor.rule()
        covered, missing = second_factor.counts(rule)
        context = {
            "page_title": "Security settings",
            "contacts": lab_profile.contacts(),
            "limits": session_limits.limits(),
            "can_edit_limits": session_limits.can_manage(reader),
            "second_factor": rule_rows(rule),
            "second_factor_covered": covered,
            "second_factor_missing": missing,
            "can_edit_second_factor": second_factor.can_manage(reader),
            # The contact form needs the laboratory row; without it there is nothing to edit.
            "can_edit_contacts": lab is not None and lab_profile.can_manage(reader),
            # The page header names the laboratory from this reading too.
            "lab": lab,
            "groups": groups,
            "toc": [
                ("contacts-heading", "Security and privacy contacts"),
                ("limits-heading", "Session limits"),
                ("second-factor-heading", "Second sign-in factor"),
                *[(f"security-{group.key}-heading", group.title) for group in groups],
                ("about-heading", "About this page"),
                ("history-heading", "History"),
            ],
            "history": history(),
            **roles,
        }
    return render(request, "core/security_settings.html", context)


def limits_label(event):
    """Which limits a lab.session_limits_changed event changed and how, from the event alone:
    "Staff: 15 minutes without activity (was 30 minutes); longest session 8 hours (was 12
    hours)"."""
    before, after = event.before or {}, event.after or {}
    audience = after.get("audience") or before.get("audience", "")
    label = dict(SessionLimit.Audience.choices).get(audience, "")
    parts = []
    for name, text, unit in (
        ("inactivity_minutes", "{} without activity", 60),
        ("maximum_hours", "longest session {}", 3600),
    ):
        new, old = after.get(name), before.get(name)
        if not isinstance(new, int):
            continue
        part = text.format(security_settings.duration_text(new * unit))
        if isinstance(old, int) and old != new:
            part += f" (was {security_settings.duration_text(old * unit)})"
        parts.append(part)
    return ": ".join(part for part in (label, "; ".join(parts)) if part)


def rule_rows(rule):
    """The second-factor rule's parts as the panel lists them: (label, the status shown when the
    part requires a factor, or "" when it does not): "Required", or "Required: Supervisor,
    Technical staff" for the roles chosen."""
    chosen = second_factor.values(rule)
    rows = []
    for name in second_factor.FIELDS:
        label = second_factor.PART_LABELS[name]
        if name == "roles":
            names = [ROLE_LABELS[role] for role in chosen["roles"]]
            rows.append((label, f"Required: {', '.join(names)}" if names else ""))
        else:
            rows.append((label, "Required" if chosen[name] else ""))
    return rows


def rule_label(event):
    """What a lab.second_factor_rule_changed event set, from the event alone: "Required of: every
    staff account; client portal accounts (was: nobody)"."""
    before, after = event.before or {}, event.after or {}
    now = second_factor.summary(after)
    was = second_factor.summary(before)
    text = f"Required of: {now[0].lower() + now[1:]}"
    if was != now:
        text += f" (was: {was[0].lower() + was[1:]})"
    return text


def history(limit=HISTORY_LIMIT):
    """The newest changes to the security and privacy contacts, the session limits and the
    second-factor rule, newest first, each with `detail` naming what changed."""
    events = list(
        AuditEvent.objects.filter(
            action__in=(CONTACT_ACTION, session_limits.ACTION, second_factor.ACTION)
        )
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )
    for event in events:
        if event.action == CONTACT_ACTION:
            event.detail = contact_label(event)
        elif event.action == second_factor.ACTION:
            event.detail = rule_label(event)
        else:
            event.detail = limits_label(event)
    return events


@login_required
def session_limits_edit(request, audience):
    """Set the staff or the client portal session limits (the admin role), with a reason. The
    title, the initial values and the revision the form carries all come from one reading of the
    limits; a change saved in between is refused with the input kept."""
    require_role(request.user, *session_limits.ROLES)
    if audience not in SessionLimit.Audience.values:
        raise Http404("No such session limits.")
    limit = next(row for row in session_limits.limits() if row.audience == audience)
    label = limit.get_audience_display()
    form = SessionLimitsForm(
        request.POST if request.method == "POST" else None,
        initial={**session_limits.values(limit), "expected_revision": limit.revision},
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, changed = session_limits.save_limits(
                actor=request.user,
                audience=audience,
                new={name: form.cleaned_data[name] for name in session_limits.FIELDS},
                expected_revision=form.cleaned_data["expected_revision"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            _service_errors(form, exc)
        else:
            if changed:
                messages.success(request, f"{label} session limits saved.")
            else:
                messages.info(request, "No changes to save.")
            return redirect("security-settings")
    return render(
        request,
        "core/session_limits_form.html",
        {
            "page_title": f"Edit {label.lower()} session limits",
            "form": form,
            "audience_label": label,
            "cancel_url": reverse("security-settings"),
        },
    )


@login_required
def second_factor_edit(request):
    """Set who must have a second sign-in factor (the admin role), with a reason. The initial
    values and the revision the form carries come from one reading of the rule; a change saved in
    between is refused with the input kept."""
    require_role(request.user, *second_factor.MANAGER_ROLES)
    rule = second_factor.rule()
    form = SecondFactorRuleForm(
        request.POST if request.method == "POST" else None,
        initial={**second_factor.values(rule), "expected_revision": rule.revision},
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, changed = second_factor.save_rule(
                actor=request.user,
                new={name: form.cleaned_data[name] for name in second_factor.FIELDS},
                expected_revision=form.cleaned_data["expected_revision"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            _service_errors(form, exc)
        else:
            if changed:
                messages.success(request, "Second sign-in factor rule saved.")
            else:
                messages.info(request, "No changes to save.")
            return redirect("security-settings")
    return render(
        request,
        "core/second_factor_form.html",
        {
            "page_title": "Edit second sign-in factor rule",
            "form": form,
            "cancel_url": reverse("security-settings"),
        },
    )
