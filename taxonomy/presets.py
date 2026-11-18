"""Ticket category presets (T2): the catalogue in `docs/product.md` §Ticket taxonomy and presets.

Data only. Loading (`taxonomy.services.load_presets`) is additive and keyed by these stable
keys: loading a preset twice, or two presets that share a category, changes nothing that is
already there. Presets create the teams they name when missing and set no service targets.

The default team of each category is a starting point. The laboratory can change its routing,
rename it or deactivate it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Teams presets create when missing, by stable slug.
TEAMS = {
    "client-services": "Client services",
    "accessioning": "Accessioning",
    "technical": "Technical",
    "billing": "Billing",
    "logistics": "Logistics",
    "interfaces-it": "Interfaces and IT",
    "quality": "Quality",
}
DEFAULT_TEAM = "client-services"

# Groups (the first level), in display order.
GROUPS = {
    "status-results": "Status and results",
    "orders-samples": "Orders and samples",
    "reports-delivery": "Reports and delivery",
    "account-services": "Account and services",
    "billing": "Billing",
    "supplies-logistics": "Supplies and logistics",
    "service-quality": "Service quality",
}


@dataclass(frozen=True)
class PresetCategory:
    key: str
    name: str
    group: str
    team: str
    priority: str = "normal"
    client_may_raise: bool = True
    is_complaint: bool = False
    is_restricted: bool = False


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    description: str
    categories: tuple[PresetCategory, ...]


C = PresetCategory

# Categories that more than one laboratory-type preset adds; one definition each, shared by key.
_REQUISITION = C(
    "requisition-information",
    "Missing or conflicting requisition information",
    "orders-samples",
    "accessioning",
)
_CHAIN_OF_CUSTODY = C(
    "chain-of-custody-discrepancy", "Chain-of-custody discrepancy", "orders-samples", "accessioning"
)

PRESETS = {
    preset.key: preset
    for preset in (
        Preset(
            "all",
            "All laboratories",
            "The categories every laboratory's client-services desk handles. Always loaded.",
            (
                C("result-status", "Order or result status", "status-results", "client-services"),
                C(
                    "missing-result",
                    "Missing result or report",
                    "status-results",
                    "client-services",
                ),
                C("result-question", "Result or report question", "status-results", "technical"),
                C(
                    "add-on-cancellation",
                    "Add-on, cancellation or scope change",
                    "orders-samples",
                    "accessioning",
                ),
                C(
                    "sample-problem",
                    "Sample problem or recollection",
                    "orders-samples",
                    "accessioning",
                ),
                C("corrected-report", "Corrected report", "reports-delivery", "technical"),
                C(
                    "report-amendment",
                    "Report amendment or reissue",
                    "reports-delivery",
                    "technical",
                ),
                C(
                    "report-delivery",
                    "Report delivery or portal problem",
                    "reports-delivery",
                    "interfaces-it",
                ),
                C(
                    "account-change",
                    "Account, site or contact change",
                    "account-services",
                    "client-services",
                ),
                C(
                    "service-information",
                    "Test, method or service information",
                    "account-services",
                    "client-services",
                ),
                C("billing-question", "Invoice or billing question", "billing", "billing"),
                C("supply-order", "Supply or kit order", "supplies-logistics", "logistics"),
                C("pickup", "Pickup or missed pickup", "supplies-logistics", "logistics"),
                C("turnaround-delay", "Turnaround delay", "service-quality", "client-services"),
                C("complaint", "Complaint", "service-quality", "quality", is_complaint=True),
                C("feedback", "Feedback", "service-quality", "quality"),
                C("other", "Other", "service-quality", "client-services"),
            ),
        ),
        Preset(
            "clinical",
            "Clinical (including molecular and pathology)",
            "Requisitions, critical-result follow-up, patient billing, privacy requests and "
            "pathology material.",
            (
                _REQUISITION,
                C(
                    "critical-result-follow-up",
                    "Critical or urgent result follow-up",
                    "status-results",
                    "technical",
                    priority="urgent",
                    client_may_raise=False,
                ),
                C(
                    "patient-billing",
                    "Patient billing or insurance question",
                    "billing",
                    "billing",
                ),
                C(
                    "privacy-request",
                    "Privacy request",
                    "service-quality",
                    "quality",
                    client_may_raise=False,
                    is_restricted=True,
                ),
                C(
                    "pathology-material",
                    "Pathology material return or referral",
                    "orders-samples",
                    "technical",
                ),
            ),
        ),
        Preset(
            "toxicology",
            "Toxicology",
            "Requisitions, custody and control forms, and medical review officer routing.",
            (
                _REQUISITION,
                C(
                    "custody-control-form",
                    "Custody and control form discrepancy",
                    "orders-samples",
                    "accessioning",
                ),
                C(
                    "mro-routing",
                    "Medical review officer or employer routing question",
                    "reports-delivery",
                    "client-services",
                ),
            ),
        ),
        Preset(
            "environmental",
            "Environmental",
            "Chain of custody, bottle kits, project scope and electronic deliverables.",
            (
                _CHAIN_OF_CUSTODY,
                C("bottle-kit-order", "Bottle kit order", "supplies-logistics", "logistics"),
                C(
                    "project-scope",
                    "Project scope, methods or reporting limits",
                    "account-services",
                    "technical",
                ),
                C(
                    "electronic-deliverable",
                    "Electronic deliverable problem",
                    "reports-delivery",
                    "interfaces-it",
                ),
            ),
        ),
        Preset(
            "food-manufacturing",
            "Food and manufacturing quality control",
            "Chain of custody, batch and specification queries, and certificates of analysis.",
            (
                _CHAIN_OF_CUSTODY,
                C(
                    "batch-specification",
                    "Batch, lot or specification query",
                    "status-results",
                    "technical",
                ),
                C(
                    "certificate-of-analysis",
                    "Certificate of analysis question",
                    "reports-delivery",
                    "technical",
                ),
            ),
        ),
        Preset(
            "biorepository",
            "Biorepository",
            "Deposits and distributions, access approvals and shipments.",
            (
                C(
                    "deposit-distribution",
                    "Deposit or distribution request",
                    "orders-samples",
                    "client-services",
                ),
                C(
                    "access-approval",
                    "Access approval or agreement status",
                    "account-services",
                    "client-services",
                ),
                C("shipment-question", "Shipment question", "supplies-logistics", "logistics"),
            ),
        ),
    )
}
ALWAYS = "all"
