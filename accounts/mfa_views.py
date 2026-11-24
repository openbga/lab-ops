"""The person's own two-factor pages that change their sign-in factors: allauth's pages, with the
change, its audit event and the email notice to the account committed together.

allauth adds an authenticator app (Activate), issues new recovery codes (Generate) and removes the
app with its recovery codes (Deactivate). Its Activate and Generate send their signal and message
once the change has committed; its Deactivate is not atomic on its own and sends its
`authenticator_removed` signals as it removes, which now happens inside this transaction, so a
receiver of that signal can see a removal that then rolls back (nothing in this application
receives it; allauth's own pages behaved the same before). These views run allauth's own form_valid
inside one transaction that first locks the account's row, so two changes of one account's
factors take turns, and decide again from what the account has under that lock: an app added
meanwhile (Activate goes to Deactivate, as allauth does before the form), an app removed meanwhile
(Deactivate changes nothing; Generate is refused, as its form refuses an account without one).
After allauth's flow returns, in the same transaction, they record:

- the audit event against the account, by the person: `account.authenticator_added`,
  `account.recovery_codes_replaced` or `account.authenticator_removed` (the action the account
  administration's removal records), with each factor's id, type and times: never a secret, a
  code or a seed;
- one email notice to the account (accounts.security_notices) with every event of the change:
  adding an app that issues recovery codes with it is one notice, worded as the app added.

A change that is refused or rolls back records neither. allauth's reauthentication requirement
(its view decorators, and its flows' own check, which raises inside the transaction and so rolls
it back), its forms, messages and redirects are unchanged. config/urls.py routes allauth's paths
and URL names here, before allauth's own URLs.
"""

from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes import views as recovery_code_views
from allauth.mfa.recovery_codes.internal.flows import can_generate_recovery_codes
from allauth.mfa.totp import views as totp_views
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse

from core.audit import record_event

from . import security_notices
from .models import User

TOTP = Authenticator.Type.TOTP
RECOVERY_CODES = Authenticator.Type.RECOVERY_CODES
REASON = "Two-factor authentication page; secrets and codes not recorded"


def _lock_account(request):
    """The person's account row, locked until the change commits (allauth's own Generate locks it
    too, after this)."""
    return User.objects.select_for_update().get(pk=request.user.pk)


def _factors(account, types):
    """What the audit event records of the account's factors of these types: ids, types and
    times."""
    return [
        {
            "authenticator_id": factor.pk,
            "type": factor.type,
            "created_at": factor.created_at.isoformat(),
            "last_used_at": factor.last_used_at.isoformat() if factor.last_used_at else None,
        }
        for factor in Authenticator.objects.filter(user_id=account.pk, type__in=types).order_by(
            "pk"
        )
    ]


class ActivateTOTPView(totp_views.ActivateTOTPView):
    def form_valid(self, form):
        with transaction.atomic():
            account = _lock_account(self.request)
            if Authenticator.objects.filter(user_id=account.pk, type=TOTP).exists():
                return HttpResponseRedirect(reverse("mfa_deactivate_totp"))
            response = super().form_valid(form)
            codes = self.did_generate_recovery_codes
            added = _factors(account, [TOTP, RECOVERY_CODES] if codes else [TOTP])
            record_event(
                actor=self.request.user,
                action="account.authenticator_added",
                obj=account,
                after={"authenticators": added},
                reason=REASON,
            )
            events = [security_notices.ADDED]
            if codes:
                events.append(security_notices.CODES_REPLACED)
            security_notices.record(
                account, events, by="self", types=[factor["type"] for factor in added]
            )
        return response


class DeactivateTOTPView(totp_views.DeactivateTOTPView):
    def form_valid(self, form):
        with transaction.atomic():
            account = _lock_account(self.request)
            before = _factors(account, [TOTP, RECOVERY_CODES])
            if self.authenticator.pk not in {factor["authenticator_id"] for factor in before}:
                return HttpResponseRedirect(self.get_success_url())  # removed meanwhile
            response = super().form_valid(form)
            left = set(
                Authenticator.objects.filter(user_id=account.pk).values_list("pk", flat=True)
            )
            removed = [factor for factor in before if factor["authenticator_id"] not in left]
            record_event(
                actor=self.request.user,
                action="account.authenticator_removed",
                obj=account,
                before={"authenticators": removed},
                reason=REASON,
            )
            security_notices.record(
                account,
                [security_notices.REMOVED],
                by="self",
                types=[factor["type"] for factor in removed],
            )
        return response


class GenerateRecoveryCodesView(recovery_code_views.GenerateRecoveryCodesView):
    def form_valid(self, form):
        with transaction.atomic():
            account = _lock_account(self.request)
            if not can_generate_recovery_codes(account):  # the app was removed meanwhile
                refused = get_adapter().validation_error("cannot_generate_recovery_codes")
                form.add_error(None, refused)  # as allauth's form refuses it
                return self.form_invalid(form)
            before = _factors(account, [RECOVERY_CODES])
            response = super().form_valid(form)
            record_event(
                actor=self.request.user,
                action="account.recovery_codes_replaced",
                obj=account,
                before={"authenticators": before},
                after={"authenticators": _factors(account, [RECOVERY_CODES])},
                reason=REASON,
            )
            security_notices.record(
                account, [security_notices.CODES_REPLACED], by="self", types=[RECOVERY_CODES]
            )
        return response


activate_totp = ActivateTOTPView.as_view()
deactivate_totp = DeactivateTOTPView.as_view()
generate_recovery_codes = GenerateRecoveryCodesView.as_view()
