"""{% load attachment_files %}: the files of an entry as an audience may see them.

{% entry_files entry "staff" user as shown %}   shown.files and shown.supervisor
    (entries prefetched by attachments.selectors.with_files)
{{ attachment.size|file_size }}            "350 KB"
"""

from django import template

from attachments.limits import human_size

register = template.Library()


def _supervisor(user):
    if not hasattr(user, "_vr_attachment_supervisor"):
        from desk.permissions import is_supervisor

        user._vr_attachment_supervisor = bool(user.is_authenticated) and is_supervisor(user)
    return user._vr_attachment_supervisor


@register.simple_tag
def entry_files(entry, audience, user):
    """{"files": [...], "supervisor": bool}. The portal never gets a withdrawn file, nor the
    supervisor flag."""
    files = list(entry.attachments.all())
    if audience == "portal":
        return {"files": [item for item in files if item.withdrawn_at is None], "supervisor": False}
    return {"files": files, "supervisor": _supervisor(user)}


@register.filter
def file_size(size):
    return human_size(int(size or 0))
