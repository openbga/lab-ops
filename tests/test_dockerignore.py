"""The container image carries every application package: .dockerignore sends only listed files,
so a new app missing from it builds an image that fails to migrate (seen when the nonconformance
module was added)."""

from pathlib import Path

from django.apps import apps
from django.conf import settings

ROOT = Path(settings.BASE_DIR)


def test_every_local_app_is_sent_to_the_image():
    rules = set((ROOT / ".dockerignore").read_text().split())
    local = [
        config.name
        for config in apps.get_app_configs()
        if Path(config.path).resolve().parent == ROOT.resolve()
    ]
    assert local, "no local apps found"
    missing = [name for name in local if f"!{name}/**/*.py" not in rules]
    assert not missing, f"Add to .dockerignore: {missing}"
