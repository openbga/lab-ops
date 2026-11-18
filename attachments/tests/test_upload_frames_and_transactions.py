"""Image limits across frames and attachment ownership across transactions."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.db import connections, transaction
from PIL import Image

from attachments import inspection
from attachments.inspection import inspect_bytes
from attachments.models import Attachment
from attachments.services import attach_to_new_request
from desk import services as desk
from desk.models import ServiceRequest


class RollBack(Exception):
    pass


@pytest.fixture
def new_request(agent, category, clinic):
    def create():
        return desk.create_request(
            actor=agent,
            category=category,
            subject="Synthetic",
            first_entry_kind="note",
            first_entry_body="Synthetic.",
            channel="fax",
            client=clinic,
        )

    return create


# "created in this transaction" across savepoints, nested atomics and connections


@pytest.mark.django_db(transaction=True)
def test_creation_marker_expires_after_rolled_back_savepoint(agent, new_request):
    with transaction.atomic():
        with pytest.raises(RollBack), transaction.atomic():
            record = new_request()
            assert desk.created_in_current_transaction(record)
            raise RollBack
        assert not ServiceRequest.objects.filter(pk=record.pk).exists()
        alive = desk.created_in_current_transaction(record)
        with pytest.raises(ValidationError, match="never afterwards"):
            attach_to_new_request(agent, record, [inspect_bytes("late.txt", b"ordinary text")])
    with transaction.atomic():
        alive_after_commit = desk.created_in_current_transaction(record)
    assert not alive and not alive_after_commit
    assert not Attachment.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_creation_marker_is_local_to_database_connection(new_request):
    def observes_marker(record):
        try:
            with transaction.atomic():
                return desk.created_in_current_transaction(record)
        finally:
            connections.close_all()

    with transaction.atomic():
        record = new_request()
        assert desk.created_in_current_transaction(record)
        with ThreadPoolExecutor(max_workers=1) as worker:
            assert not worker.submit(observes_marker, record).result()


@pytest.mark.django_db(transaction=True)
def test_creation_marker_survives_successful_sibling_savepoints(agent, new_request):
    with transaction.atomic():
        with transaction.atomic():
            record = new_request()
        with transaction.atomic():
            files = attach_to_new_request(agent, record, [inspect_bytes("review.txt", b"fixture")])
            assert len(files) == 1
    with transaction.atomic():
        assert not desk.created_in_current_transaction(record)
    assert Attachment.objects.get().request == record


@pytest.mark.django_db(transaction=True)
def test_a_later_request_in_the_same_transaction_does_not_revive_an_undone_one(new_request):
    with transaction.atomic():
        with pytest.raises(RollBack), transaction.atomic():
            undone = new_request()
            raise RollBack
        kept = new_request()
        assert desk.created_in_current_transaction(kept)
        assert not desk.created_in_current_transaction(undone)


# limits on every page or frame, and a bound on their number


def frames(fmt, sizes, mode="L", **options):
    first, *rest = (Image.new(mode, size, "white") for size in sizes)
    data = BytesIO()
    first.save(data, format=fmt, save_all=True, append_images=rest, **options)
    return data.getvalue()


def test_image_limits_apply_to_each_page(monkeypatch):
    monkeypatch.setattr(inspection, "MAX_SIDE", 4)
    with pytest.raises(ValidationError, match="too large in pixels"):
        inspect_bytes("two-pages.tif", frames("TIFF", [(3, 3), (5, 5)]))
    inspect_bytes("two-pages.tif", frames("TIFF", [(3, 3), (4, 4)]))


def test_a_multi_picture_jpeg_is_checked_picture_by_picture(monkeypatch):
    monkeypatch.setattr(inspection, "MAX_SIDE", 40)
    with pytest.raises(ValidationError, match="too large in pixels"):
        inspect_bytes("pair.jpg", frames("MPO", [(30, 20), (50, 40)], mode="RGB"))
    inspect_bytes("pair.jpg", frames("MPO", [(30, 20), (40, 40)], mode="RGB"))


@pytest.mark.parametrize("fmt", ["TIFF", "GIF", "WEBP", "PNG"])
def test_the_number_of_frames_is_bounded(monkeypatch, fmt):
    monkeypatch.setattr(inspection, "MAX_FRAMES", 3)
    # Frames that differ, so an animated format keeps them all.
    many = BytesIO()
    first, *rest = (Image.new("RGB", (8, 8), (shade, 0, 0)) for shade in (0, 60, 120, 180))
    first.save(many, format=fmt, save_all=True, append_images=rest)
    with pytest.raises(ValidationError, match="at most 3 pages or frames"):
        inspect_bytes(f"many.{fmt.lower()}", many.getvalue())
    few = BytesIO()
    first.save(few, format=fmt, save_all=True, append_images=rest[:2])
    inspect_bytes(f"few.{fmt.lower()}", few.getvalue())


def test_pixels_of_all_frames_together_are_bounded(monkeypatch):
    monkeypatch.setattr(inspection, "MAX_TOTAL_DECODED_PIXELS", 100)
    with pytest.raises(ValidationError, match="too large in pixels"):
        inspect_bytes("pages.tif", frames("TIFF", [(8, 8), (8, 8)]))
    inspect_bytes("pages.tif", frames("TIFF", [(8, 8), (6, 6)]))


def test_a_damaged_later_page_is_refused():
    whole = frames("TIFF", [(8, 8), (64, 64)])
    with Image.open(BytesIO(whole)) as image:
        image.seek(1)
        second_page_strip = image.tile[0][2]
    damaged = whole[:second_page_strip] + whole[second_page_strip + 1024 :]
    with pytest.raises(ValidationError):
        inspect_bytes("damaged.tif", damaged)
