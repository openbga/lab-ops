"""Laboratory-type request form presets (data only): what each type adds to "All laboratories".

Imported at the end of presets.py, after the dataclasses and the shared fields exist, so the
circular import below is safe. The same rules apply as there: no active field holds patient
information, no laboratory policy is invented, and everything is a starting point to edit.
"""

from __future__ import annotations

from .models import FieldKind as K
from .presets import (
    DETAILS,
    PATIENT_IDENTIFIER,
    SHOWN,
    STAFF,
    FieldAddition,
    FormPreset,
    PresetCatalog,
)
from .presets import PresetField as F
from .presets import PresetItem as I  # noqa: E741
from .presets import PresetTemplate as T

# Templates two laboratory types share, defined once.
REQUISITION_CORRECTION = T(
    "requisition-correction",
    "Requisition correction",
    "requisition-information",
    client_label="Fix or complete a requisition",
    description="Missing or conflicting information on a requisition or order.",
    icon="list-check",
    instructions="The laboratory may need to contact the person who ordered the test before it "
    "can go ahead.",
    fields=(
        F(
            "document-type",
            "Which document?",
            K.CHOICE,
            required=True,
            choices=("Paper requisition", "Electronic order", "Manifest"),
        ),
        F(
            "order-reference",
            "Order, requisition or sample number",
            K.SHORT_TEXT,
            required=True,
            help_text="The number on your order, requisition, sample label or report.",
        ),
        F(
            "what-is-wrong",
            "Which field is missing or wrong?",
            K.LONG_TEXT,
            required=True,
            help_text="Name the field and what is wrong with it. Do not enter patient names or "
            "dates of birth here.",
        ),
        F("correct-value", "The correct information", K.SHORT_TEXT),
        F(
            "patient-correction",
            "Corrected patient information",
            K.SHORT_TEXT,
            help_text="A correction to a patient field, only where the laboratory asks for it.",
            patient=True,
            active=False,
        ),
        F(
            "authorized-by",
            "Authorized by (name and role)",
            K.SHORT_TEXT,
            required=True,
            help_text="The person who confirmed the correction.",
        ),
        F("callback-notes", "Callback attempts and times", K.LONG_TEXT, audience=STAFF),
        F("can-proceed", "Testing can go ahead", K.YES_NO, audience=STAFF),
    ),
)
COC_CORRECTION = T(
    "coc-correction",
    "Chain of custody correction",
    "chain-of-custody-discrepancy",
    client_label="Fix a chain of custody form",
    description="Missing or conflicting information on a chain of custody form.",
    icon="list-check",
    instructions="The laboratory may need to contact the person who sampled before testing can go "
    "ahead.",
    fields=(
        F(
            "document-reference",
            "Chain of custody or project number",
            K.SHORT_TEXT,
            required=True,
            help_text="The number printed on the form, or your project or sample number.",
        ),
        F(
            "what-is-wrong",
            "What is missing or does not match?",
            K.LONG_TEXT,
            required=True,
            help_text="Name the field, for example sample ID, date, time or preservative, and "
            "what is wrong with it.",
        ),
        F("correct-value", "The correct information", K.SHORT_TEXT, required=True),
        F("authorized-by", "Authorized by (name and role)", K.SHORT_TEXT, required=True),
        F("callback-notes", "Callback attempts and times", K.LONG_TEXT, audience=STAFF),
        F("can-proceed", "Testing can go ahead", K.YES_NO, audience=STAFF),
    ),
)

CLINICAL = FormPreset(
    "clinical",
    "Clinical (including molecular and pathology)",
    catalogs=(
        PresetCatalog(
            "supplies",
            "Supplies",
            items=(
                I("Serum separator tube, 5 mL", "box of 100", "gold top, SST, red-gray top", 10),
                I("EDTA tube, 4 mL", "box of 100", "lavender top, purple top", 10),
                I("Sodium citrate tube, 2.7 mL", "box of 100", "light blue top, coagulation", 10),
                I("Lithium heparin tube, 4 mL", "box of 100", "green top", 10),
                I("Sterile urine collection cup", "box of 100", "urine cup, specimen cup", 10),
                I("Swab with transport medium", "box of 50", "viral transport, VTM", 10),
            ),
        ),
    ),
    templates=(
        T(
            "urgent-pickup",
            "Urgent pickup",
            "pickup",
            client_label="Request an urgent pickup",
            description="A courier collects specimens that cannot wait for the routine pickup.",
            icon="exclamation-triangle",
            featured=True,
            instructions="For specimens that need collection before the routine pickup. The "
            "laboratory confirms the pickup time.",
            fields=(
                F(
                    "ready-at",
                    "Ready for pickup at",
                    K.DATETIME,
                    required=True,
                    help_text="When the specimens will be packed and ready.",
                ),
                F(
                    "container-count",
                    "Number of specimens, boxes or coolers",
                    K.NUMBER,
                    required=True,
                    min_value=1,
                    max_value=999,
                ),
                F(
                    "site-contact",
                    "Person and phone at the pickup point",
                    K.SHORT_TEXT,
                    required=True,
                ),
                F(
                    "labels-confirmed",
                    "Every specimen is labeled and packed for transport",
                    K.CONFIRMATION,
                    required=True,
                ),
                DETAILS,
                F("courier", "Courier or route", K.SHORT_TEXT, audience=STAFF),
                F("picked-up-at", "Picked up at", K.DATETIME, audience=SHOWN),
            ),
        ),
        REQUISITION_CORRECTION,
        T(
            "patient-billing-question",
            "Patient billing question",
            "patient-billing",
            client_label="Patient or insurance billing question",
            description="A charge billed to a patient or an insurer for tests you ordered.",
            icon="tag",
            fields=(
                F(
                    "question-kind",
                    "What is it about?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "A charge billed to a patient",
                        "A claim to an insurer",
                        "A statement",
                        "Something else",
                    ),
                ),
                F(
                    "order-reference",
                    "Order, requisition or sample number",
                    K.SHORT_TEXT,
                    help_text="The number on your order, requisition, sample label or report.",
                ),
                F(
                    "patient-account",
                    "Patient account or claim reference",
                    K.SHORT_TEXT,
                    help_text="The account or claim number the laboratory uses. Not a name or "
                    "date of birth.",
                    patient=True,
                    active=False,
                ),
                F("details", "Details", K.LONG_TEXT, required=True),
            ),
        ),
        T(
            "pathology-material",
            "Pathology material return or referral",
            "pathology-material",
            client_label="Return or refer blocks and slides",
            description="Ask for tissue blocks or slides to be returned or sent to another place.",
            icon="archive",
            instructions="Sending this ticket does not release anything. The laboratory checks "
            "its records and tells you what it decided.",
            fields=(
                F(
                    "case-number",
                    "Case or accession number",
                    K.SHORT_TEXT,
                    required=True,
                    help_text="The number on the pathology report.",
                ),
                F(
                    "material",
                    "Which material?",
                    K.MULTIPLE_CHOICE,
                    required=True,
                    choices=("Blocks", "Slides", "Other"),
                ),
                F(
                    "destination",
                    "Where should it go?",
                    K.LONG_TEXT,
                    required=True,
                    help_text="Name, organization and address, or say it is for return to you.",
                ),
                F("needed-by", "Needed by", K.DATE),
                F("details", "Anything else the laboratory should know", K.LONG_TEXT),
                F("released-on", "Released on", K.DATE, audience=SHOWN),
            ),
        ),
    ),
    additions=tuple(
        FieldAddition(template, (PATIENT_IDENTIFIER,))
        for template in (
            "result-status",
            "result-question",
            "add-on",
            "sample-problem",
            "report-request",
        )
    ),
)

TOXICOLOGY = FormPreset(
    "toxicology",
    "Toxicology",
    catalogs=(
        PresetCatalog(
            "supplies",
            "Supplies",
            items=(
                I(
                    "Urine collection kit with temperature strip",
                    "box of 25",
                    "urine cup, specimen cup",
                    10,
                ),
                I("Oral fluid collection kit", "box of 25", "saliva, swab", 10),
                I("Custody and control forms", "pad of 50", "CCF, chain of custody", 10),
                I("Tamper-evident seals", "roll of 100", "bottle seals, security seals", 10),
            ),
        ),
    ),
    templates=(
        T(
            "ccf-correction",
            "Custody and control form correction",
            "custody-control-form",
            client_label="Fix a custody and control form",
            description="Missing or conflicting information on a custody and control form.",
            icon="list-check",
            instructions="The laboratory may need to contact the collection site before testing "
            "can go ahead.",
            fields=(
                F(
                    "form-number",
                    "Form number",
                    K.SHORT_TEXT,
                    required=True,
                    help_text="The number printed on the form.",
                ),
                F(
                    "what-is-wrong",
                    "What is missing or wrong?",
                    K.LONG_TEXT,
                    required=True,
                    help_text="Name the field and what is wrong with it. Do not enter donor "
                    "names or identifiers here.",
                ),
                F("corrected-information", "The correct information", K.SHORT_TEXT, required=True),
                F(
                    "donor-identifier",
                    "Donor identifier",
                    K.SHORT_TEXT,
                    help_text="The identifier on the form, only where the laboratory asks for it.",
                    patient=True,
                    active=False,
                ),
                F("authorized-by", "Authorized by (name and role)", K.SHORT_TEXT, required=True),
                F("callback-notes", "Callback attempts and times", K.LONG_TEXT, audience=STAFF),
                F("can-proceed", "Testing can go ahead", K.YES_NO, audience=STAFF),
            ),
        ),
        REQUISITION_CORRECTION,
        T(
            "result-routing",
            "Result routing",
            "mro-routing",
            client_label="Where results go",
            description="Ask where a result was sent, or change where results are routed.",
            icon="envelope",
            instructions="Sending this ticket does not change anything. The laboratory checks "
            "your authority and tells you what it decided.",
            fields=(
                F(
                    "question-kind",
                    "What do you need?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "Where a result was sent",
                        "A result went to the wrong place",
                        "Change where results are sent",
                    ),
                ),
                F(
                    "order-reference",
                    "Order, requisition or sample number",
                    K.SHORT_TEXT,
                    required=True,
                    help_text="The number on your order, requisition, sample label or report.",
                ),
                F(
                    "destination",
                    "Where should results go?",
                    K.SHORT_TEXT,
                    help_text="A destination the laboratory has on file for your organization.",
                ),
                F("details", "Details", K.LONG_TEXT),
                F("authorized-by", "Authorized by (name and role)", K.SHORT_TEXT, required=True),
                F("verified", "Authority verified", K.YES_NO, audience=STAFF),
            ),
        ),
    ),
    additions=(
        FieldAddition(
            "result-status",
            (
                F(
                    "test-class",
                    "Test class",
                    K.CHOICE,
                    choices=("Presumptive screen", "Definitive confirmation", "Not sure"),
                ),
            ),
        ),
    ),
)

ENVIRONMENTAL = FormPreset(
    "environmental",
    "Environmental",
    catalogs=(
        PresetCatalog(
            "bottle-kits",
            "Bottle kits",
            "Sample bottles, coolers and forms clients order for sampling. Add the "
            "laboratory's own kits and item codes.",
            (
                I("1 L amber glass, unpreserved", "each", "amber bottle, glass", 99),
                I("500 mL plastic, unpreserved", "each", "poly bottle, plastic", 99),
                I("250 mL plastic, nitric acid preserved", "each", "metals, HNO3", 99),
                I("40 mL VOA vial, hydrochloric acid preserved", "pack of 3", "volatiles, HCl", 99),
                I(
                    "120 mL sterile bottle with sodium thiosulfate",
                    "each",
                    "bacteria, coliform",
                    99,
                ),
                I("Cooler with ice packs", "each", "shipper, insulated", 10),
                I("Chain-of-custody forms", "pad of 50", "COC, custody forms", 10),
            ),
        ),
    ),
    templates=(
        T(
            "bottle-kit-order",
            "Bottle kit order",
            "bottle-kit-order",
            client_label="Order bottle kits",
            description="Sample bottles, coolers and forms delivered before you sample.",
            icon="archive",
            featured=True,
            fields=(
                F("items", "Items", K.ITEMS, required=True, catalog="bottle-kits"),
                F(
                    "project",
                    "Project name or number",
                    K.SHORT_TEXT,
                    required=True,
                    help_text="So the laboratory can match the kits to the samples.",
                ),
                F("sampling-date", "Planned sampling date", K.DATE, required=True),
                F("sampling-points", "Number of sampling points", K.NUMBER, min_value=1),
                F("deliver-by", "Deliver by", K.DATE),
                F("delivery-notes", "Delivery notes", K.LONG_TEXT),
                F("shipped-on", "Shipped on", K.DATE, audience=SHOWN),
                F("tracking", "Tracking number", K.SHORT_TEXT, audience=SHOWN),
            ),
        ),
        COC_CORRECTION,
        T(
            "project-scope",
            "Project scope question",
            "project-scope",
            client_label="Question about a project",
            description="Analytes, methods, reporting limits or deliverables for a project.",
            icon="journal-text",
            fields=(
                F("project", "Project name or number", K.SHORT_TEXT, required=True),
                F(
                    "topic",
                    "What do you need to know?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "Analytes or methods",
                        "Reporting limits",
                        "Sample containers or preservatives",
                        "Deliverables or reporting format",
                        "Something else",
                    ),
                ),
                F("question", "Your question", K.LONG_TEXT, required=True),
                F("needed-by", "Answer needed by", K.DATE),
            ),
        ),
        T(
            "edd-problem",
            "Electronic deliverable problem",
            "electronic-deliverable",
            client_label="Problem with a data file",
            description="An electronic data file that is missing, will not load or looks wrong.",
            icon="file-earmark-arrow-up",
            fields=(
                F(
                    "problem",
                    "What is wrong?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "The file did not arrive",
                        "The file will not load",
                        "Values or fields look wrong",
                        "We need a different format",
                    ),
                ),
                F("project", "Project name or number", K.SHORT_TEXT, required=True),
                F("report", "Report number and version", K.SHORT_TEXT),
                F("format", "File format or the system you load it into", K.SHORT_TEXT),
                F("error", "Error message or what looks wrong", K.LONG_TEXT, required=True),
                F("interface-reference", "File or message reference", K.SHORT_TEXT, audience=STAFF),
            ),
        ),
    ),
)

FOOD_MANUFACTURING = FormPreset(
    "food-manufacturing",
    "Food and manufacturing quality control",
    catalogs=(
        PresetCatalog(
            "supplies",
            "Supplies",
            items=(
                I("Sterile sample bags", "box of 100", "sample bag, sterile bag", 10),
                I("Sterile sample jars", "case of 50", "sample cup, jar", 10),
                I("Surface swab kits", "box of 50", "environmental swab, sponge", 10),
                I("Insulated shipper with gel packs", "each", "cooler, styrofoam", 10),
            ),
        ),
    ),
    templates=(
        T(
            "batch-query",
            "Batch or specification query",
            "batch-specification",
            client_label="Question about a batch",
            description="A lot, batch or specification you have a question or concern about.",
            icon="search",
            fields=(
                F("product", "Product or material", K.SHORT_TEXT, required=True),
                F("lot", "Lot or batch", K.SHORT_TEXT, required=True),
                F("production-date", "Production date", K.DATE),
                F(
                    "specification",
                    "Specification and version",
                    K.SHORT_TEXT,
                    help_text="The specification or test method the result is compared with.",
                ),
                F("issue", "Suspected issue", K.LONG_TEXT, required=True),
                F(
                    "certificate",
                    "Affected certificate of analysis or report",
                    K.SHORT_TEXT,
                    help_text="The number and version, if there is one.",
                ),
                F("quality-case", "Quality-system case reference", K.SHORT_TEXT, audience=STAFF),
            ),
        ),
        T(
            "coa-question",
            "Certificate of analysis question",
            "certificate-of-analysis",
            client_label="Question about a certificate",
            description="A certificate of analysis you need a copy of, explained or corrected.",
            icon="file-earmark-text",
            fields=(
                F(
                    "question-kind",
                    "What do you need?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "A copy or resend",
                        "An explanation of a value",
                        "A correction",
                        "A different format",
                    ),
                ),
                F("certificate", "Certificate number and version", K.SHORT_TEXT, required=True),
                F("lot", "Lot or batch", K.SHORT_TEXT),
                F("question", "Your question", K.LONG_TEXT, required=True),
                F("needs-review", "Needs review by a qualified person", K.YES_NO, audience=STAFF),
            ),
        ),
        COC_CORRECTION,
    ),
)

BIOREPOSITORY = FormPreset(
    "biorepository",
    "Biorepository",
    catalogs=(
        PresetCatalog(
            "repository-materials",
            "Repository materials",
            "Materials researchers request from the repository. Add the repository's own "
            "holdings and item codes.",
            (
                I("Serum aliquot, 0.5 mL", "aliquot", "serum", 999),
                I("EDTA plasma aliquot, 0.5 mL", "aliquot", "plasma", 999),
                I("Whole blood aliquot", "aliquot", "blood", 999),
                I("DNA extract", "tube", "genomic DNA, nucleic acid", 999),
                I("FFPE section on slide", "slide", "tissue section, paraffin", 999),
            ),
        ),
    ),
    templates=(
        T(
            "material-request",
            "Material request",
            "deposit-distribution",
            client_label="Request repository materials",
            description="Ask the repository to release materials for a study.",
            icon="archive",
            featured=True,
            instructions="Sending this ticket does not approve a release. The repository "
            "checks the approvals and tells you what it decided.",
            fields=(
                F(
                    "investigator",
                    "Principal investigator and institution",
                    K.SHORT_TEXT,
                    required=True,
                ),
                F("study-title", "Study title", K.SHORT_TEXT, required=True),
                F("rationale", "Rationale", K.LONG_TEXT, required=True),
                F("ethics-number", "Ethics approval number", K.SHORT_TEXT),
                F("ethics-date", "Ethics approval date", K.DATE),
                F("mta-reference", "Material transfer agreement reference", K.SHORT_TEXT),
                F(
                    "materials",
                    "Materials",
                    K.ITEMS,
                    required=True,
                    catalog="repository-materials",
                ),
                F(
                    "selection-criteria",
                    "Selection criteria",
                    K.LONG_TEXT,
                    help_text="For example age range, sex, diagnosis or annotations needed. Do "
                    "not include donor identifiers.",
                ),
                F(
                    "training-confirmed",
                    "Everyone handling the materials has completed the required training",
                    K.CONFIRMATION,
                    required=True,
                ),
                F("shipping-contact", "Shipping contact", K.SHORT_TEXT, required=True),
                F("shipping-address", "Shipping address", K.LONG_TEXT, required=True),
                F(
                    "released-codes",
                    "Donor or participant codes released",
                    K.SHORT_TEXT,
                    audience=STAFF,
                    patient=True,
                    active=False,
                ),
            ),
        ),
        T(
            "access-status",
            "Access status",
            "access-approval",
            client_label="Ask about an access approval",
            description="The status of a request for access to repository materials or data.",
            icon="key",
            fields=(
                F(
                    "request-reference",
                    "Request or study reference",
                    K.SHORT_TEXT,
                    required=True,
                    help_text="The number the repository gave you, or your study title.",
                ),
                F(
                    "question-kind",
                    "What do you need to know?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "Where the approval stands",
                        "Which documents are missing",
                        "Change to an approved request",
                    ),
                ),
                F("details", "Details", K.LONG_TEXT),
                F("approval-status", "Approval status", K.SHORT_TEXT, audience=SHOWN),
            ),
        ),
        T(
            "shipment-question",
            "Shipment question",
            "shipment-question",
            client_label="Question about a shipment",
            description="A shipment of materials that is late, damaged or does not match.",
            icon="inbox",
            fields=(
                F(
                    "problem",
                    "What is the problem?",
                    K.CHOICE,
                    required=True,
                    choices=(
                        "It has not arrived",
                        "It arrived damaged or thawed",
                        "The contents do not match the request",
                        "Something else",
                    ),
                ),
                F(
                    "shipment-reference",
                    "Request or tracking number",
                    K.SHORT_TEXT,
                    required=True,
                ),
                F("received-on", "Received on", K.DATE),
                F("details", "Details", K.LONG_TEXT, required=True),
                F("tracking", "Tracking number", K.SHORT_TEXT, audience=SHOWN),
            ),
        ),
    ),
)

TYPE_PRESETS = (CLINICAL, TOXICOLOGY, ENVIRONMENTAL, FOOD_MANUFACTURING, BIOREPOSITORY)
