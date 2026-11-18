"""Find requests by an identifier held in an external-record link.

`request_ids_for_identifier` matches non-person identifiers exactly, ignoring case, among the
requests desk.selectors.staff_requests(user) returns; usable from a GET search such as Find (D12).
Person-level keys (patient, participant, donor) are never matched here: their search is exact,
POST-only and audited, in links.services.search_person_key (page: link_records:person-search).
"""

from .services import request_ids_for_identifier, search_person_key

__all__ = ["request_ids_for_identifier", "search_person_key"]
