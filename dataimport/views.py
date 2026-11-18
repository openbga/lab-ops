"""Spreadsheet import pages: transport only; dataimport.importing holds columns, preview and
import. Every page needs a client-manager role (account manager, client services, admin); the
clients services check it again on every row."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render

from clients.permissions import require_client_manager

from . import importing
from .forms import CommitForm, UploadForm


def _record_type(request, kind):
    require_client_manager(request.user)
    record_type = importing.BY_SLUG.get(kind)
    if record_type is None:
        raise Http404("No such record type.")
    return record_type


def import_title(record_type):
    return f"Import {record_type.title.lower()}"


@login_required
def index(request):
    require_client_manager(request.user)
    return render(
        request,
        "dataimport/index.html",
        {
            "page_title": "Spreadsheet import",
            "types": [
                {"spec": record_type, "position": position, "title": import_title(record_type)}
                for position, record_type in enumerate(importing.TYPES, start=1)
            ],
            "max_kib": importing.MAX_BYTES // 1024,
            "max_rows": importing.MAX_ROWS,
        },
    )


@login_required
def template(request, kind):
    record_type = _record_type(request, kind)
    response = HttpResponse(
        importing.template_bytes(record_type), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="{record_type.slug}-template.csv"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _columns(record_type):
    out = []
    for column in record_type.columns:
        requirement, format_text = record_type.describe(column)
        out.append(
            {
                "name": column.name,
                "requirement": requirement,
                "format": format_text,
                "help": column.help,
            }
        )
    return out


def _commit_form(preview, payload_bytes, reason=""):
    return CommitForm(
        initial={
            "filename": preview.filename,
            "payload": importing.encode_payload(payload_bytes),
            "state_token": preview.state_token,
            "reason": reason,
        }
    )


@login_required
def upload(request, kind):
    record_type = _record_type(request, kind)
    form = UploadForm()
    preview = commit_form = None
    step = request.POST.get("step") if request.method == "POST" else None
    if step == "commit":
        commit_form = CommitForm(request.POST)
        if commit_form.is_valid():
            cleaned = commit_form.cleaned_data
            try:
                done = importing.commit_file(
                    record_type,
                    actor=request.user,
                    data=cleaned["payload"],
                    filename=cleaned["filename"],
                    reason=cleaned["reason"],
                    state_token=cleaned["state_token"],
                )
            except ValidationError as exc:
                preview = importing.preview_file(
                    record_type,
                    actor=request.user,
                    data=cleaned["payload"],
                    filename=cleaned["filename"],
                )
                if hasattr(exc, "error_dict"):  # only the reason is missing; the preview holds
                    for name, errors in exc.message_dict.items():
                        for error in errors:
                            commit_form.add_error(
                                name if name in commit_form.fields else None, error
                            )
                else:
                    for message in exc.messages:
                        messages.error(request, message)
                    commit_form = _commit_form(preview, cleaned["payload"], cleaned["reason"])
            else:
                counts = done.counts
                if not counts["create"] and not counts["update"]:
                    messages.info(
                        request, f"{done.filename} matches the records already: nothing changed."
                    )
                    return redirect("clients:client-list")
                messages.success(
                    request,
                    f"Imported {done.filename}: {counts['create']} created, "
                    f"{counts['update']} changed, {counts['unchanged']} unchanged.",
                )
                return redirect("clients:client-list")
        else:
            for errors in commit_form.errors.values():
                for error in errors:
                    messages.error(request, f"{error} Nothing was imported.")
            commit_form = None
    elif step == "preview":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload_file = form.cleaned_data["file"]
            data = upload_file.read(importing.MAX_BYTES + 1)
            filename = upload_file.name[:200]
            if len(data) > importing.MAX_BYTES:
                form.add_error(
                    "file", f"The file is larger than {importing.MAX_BYTES // 1024} KiB."
                )
            else:
                preview = importing.preview_file(
                    record_type, actor=request.user, data=data, filename=filename
                )
                commit_form = _commit_form(preview, data)
    return render(
        request,
        "dataimport/upload.html",
        {
            "page_title": import_title(record_type),
            "spec": record_type,
            "form": form,
            "preview": preview,
            "commit_form": commit_form,
            "columns": _columns(record_type),
            "max_kib": importing.MAX_BYTES // 1024,
            "max_rows": importing.MAX_ROWS,
        },
    )
