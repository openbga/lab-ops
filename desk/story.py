"""The demonstration story (DM1): a believable month at the Example Reference Laboratory.

Every request is created and worked through the audited services, step by step, at a moment in
the past: while a step runs, `django.utils.timezone.now` returns that moment, so the request, its
entries, its audit events and its service clock all carry the time the step "happened". Nothing
is rewritten afterwards (entries and audit events cannot be).

Ages and step offsets are counted in business minutes on the demonstration calendar (the clocks'
calendar), backwards from the moment the seed runs. So whenever the daily reset runs, the same
clocks come out overdue, due soon, on track, paused, met or missed; due-soon requests have
staggered horizons so the mix holds through the following day.

Synthetic data only: example organizations, TEST identifiers, a masked synthetic patient key,
no patient details.
"""

from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from clients.models import Client, ClientContact, ClientSite
from core.audit import record_event
from service_levels.business_time import add_business_seconds, business_seconds_between
from service_levels.models import BusinessCalendar
from taxonomy.models import RequestCategory
from teams.models import Team

from . import services
from .models import ComplaintDecision, EntryKind, ServiceRequest, Status

CALENDAR = "Laboratory hours"  # service_levels.seed
CAPABILITY = "view_patient_information"
CAPABILITY_HOLDER = "demo_supervisor"

# Who does what in the story.
AGENT, LEAD, TECH, MANAGER = (
    "demo_client_services",
    "demo_supervisor",
    "demo_technical",
    "demo_account_manager",
)
CLINIC_PORTAL, WATER_PORTAL = "demo_client", "demo_client_water"

# One request: where it comes from, what it is, how long ago (business minutes) it arrived,
# and what happened since (offsets in business minutes after it arrived). Comments say the
# state the story is after at reset time, given the demonstration targets (service_levels.seed).
STORY = (
    {
        "key": "missing-result",
        "org": "EXCLIN",
        "contact": "office@clinic.example.invalid",
        "category": "missing-result",
        "channel": "phone",
        "subject": "Result not received for order ORD-TEST-000042",
        "body": "Office manager says the result for order ORD-TEST-000042 has not arrived.",
        "team": "client-services",
        "assignee": AGENT,
        "reference": "ORD-TEST-000042",
        "age": 1860,  # normal: 5 business hours left of 36, due soon
        "steps": (
            ("link", 5, AGENT, "lis", "order", "ORD-TEST-000042", "primary", True),
            ("link", 10, LEAD, "lis", "patient", "PT-TEST-00004821", "related", False),
            (
                "contact",
                60,
                AGENT,
                "Interface shows the result released; office has not received it.",
                ("to_client", "phone", "Office manager", "spoke_with"),
            ),
            (
                "reply",
                120,
                AGENT,
                "We are re-sending the result for ORD-TEST-000042 and checking the interface.",
            ),
            ("note", 600, AGENT, "Interfaces team confirms a queue backlog on the clinic's feed."),
        ),
    },
    {
        "key": "add-on",
        "org": "EXCLIN",
        "site": "MAIN",
        "category": "add-on-cancellation",
        "channel": "portal",
        "by": CLINIC_PORTAL,
        "priority": "high",
        "form": "add-on",
        "values": lambda template: {
            "change": "Add a test",
            "order-reference": "ORD-TEST-000310",
            "sample": "Accession ACC-TEST-000310, collected this morning",
            "tests": "Lipid panel",
            "new-order-placed": False,
            "requested-by": "Dr. Example, ordering provider",
            "patient-identifier": "PT-TEST-00004821",
        },
        "subject": "Add a test to accession ACC-TEST-000310",
        "body": "Please add a lipid panel to accession ACC-TEST-000310 collected this morning.",
        "age": 400,  # high: waiting on the client, paused
        "steps": (
            ("assign", 10, AGENT, "accessioning", AGENT),
            (
                "correct",
                20,
                AGENT,
                {"tests": "Lipid panel and magnesium"},
                "The office added magnesium by phone.",
            ),
            (
                "reply",
                30,
                AGENT,
                "The specimen qualifies. Please send the signed authorization for the added test.",
                Status.WAITING_CLIENT,
            ),
        ),
    },
    {
        "key": "corrected-report",
        "org": "EXCLIN",
        "contact": "office@clinic.example.invalid",
        "category": "corrected-report",
        "channel": "email",
        "priority": "high",
        "subject": "Corrected report for RPT-TEST-000310",
        "body": "The office asks why report RPT-TEST-000310 was reissued.",
        "team": "technical",
        "assignee": TECH,
        "age": 900,  # resolved within its target: met
        "steps": (
            (
                "reply",
                40,
                TECH,
                "A units error was corrected; the corrected report replaces the original.",
            ),
            ("link", 50, TECH, "lis", "report", "RPT-TEST-000310", "original", True),
            ("link", 55, TECH, "lis", "report", "RPT-TEST-000310-2", "replacement", True),
            ("resolve", 500, TECH, "Corrected report delivered and explained."),
        ),
    },
    {
        "key": "supplies",
        "org": "EXCLIN",
        "site": "MAIN",
        "category": "supply-order",
        "channel": "portal",
        "by": CLINIC_PORTAL,
        "form": "supply-order",
        "values": lambda template: {"items": _catalog_items(template, (2, 1))},
        "subject": "Requisition forms",
        "body": "We need two pads of requisition forms for the main office.",
        "age": 3000,  # closed
        "steps": (
            ("reply", 100, AGENT, "Two pads are on today's courier run."),
            (
                "correct",
                120,
                "demo_admin",
                {"shipped-on": "today", "tracking": "TRK-TEST-0042"},
                "",
            ),
            ("resolve", 1500, AGENT, "Forms delivered with the courier."),
            ("close", 1600, AGENT),
        ),
    },
    {
        "key": "supplies-follow-up",
        "org": "EXCLIN",
        "site": "MAIN",
        "category": "supply-order",
        "channel": "portal",
        "by": CLINIC_PORTAL,
        "follow_up_of": "supplies",
        "subject": "Wrong form in the requisition pads",
        "body": "The pads we received are the toxicology form, not the general one.",
        "age": 150,  # unassigned in Logistics, not answered yet: on track
        "steps": (),
    },
    {
        "key": "withdrawn-reply",
        "org": "EXCLIN",
        "contact": "office@clinic.example.invalid",
        "category": "report-delivery",
        "channel": "fax",
        "subject": "Where was report RPT-TEST-000320 sent?",
        "body": "Faxed note asks where report RPT-TEST-000320 was delivered.",
        "age": 200,  # normal: on track
        "steps": (
            ("reply", 20, AGENT, "Report RPT-TEST-000321 was delivered on Monday."),
            ("withdraw", 30, LEAD, "Named the wrong report; corrected in the next reply."),
            (
                "reply",
                40,
                AGENT,
                "Report RPT-TEST-000320 was delivered on Monday to the main office.",
            ),
        ),
    },
    {
        "key": "complaint-awaiting",
        "org": "EXCLIN",
        "contact": "office@clinic.example.invalid",
        "category": "complaint",
        "channel": "phone",
        "subject": "Results late three times this month",
        "body": "Practice manager complains that results were late three times this month.",
        "team": "client-services",
        "assignee": AGENT,
        "age": 1500,  # resolved, awaiting the supervisor's independent review
        "steps": (
            (
                "decide",
                120,
                AGENT,
                ComplaintDecision.INVESTIGATE,
                "Repeated delay for one client; look for a common cause.",
            ),
            (
                "record",
                600,
                AGENT,
                "Courier pickup moved later in the day; results missed the evening run.",
                "",  # its nonconformance takes the reference's place (nonconformance.seed)
            ),
            (
                "reply",
                610,
                AGENT,
                "We found the cause: the afternoon pickup. It is back to its usual time.",
            ),
            ("resolve", 900, AGENT, "Pickup time restored; the client was told."),
        ),
    },
    {
        "key": "billing",
        "org": "EXCLIN",
        "contact": "billing@clinic.example.invalid",
        "category": "billing-question",
        "channel": "email",
        "priority": "low",
        "subject": "Copy of invoice INV-TEST-0042",
        "body": "Billing clerk asks for a copy of invoice INV-TEST-0042.",
        "team": "billing",
        "age": 2400,  # closed
        "steps": (
            ("reply", 200, "demo_admin", "A copy of INV-TEST-0042 is in your billing portal."),
            ("resolve", 400, "demo_admin", "Copy sent."),
            ("close", 500, "demo_admin"),
        ),
    },
    {
        "key": "critical",
        "org": "EXGEN",
        "site": "ED",
        "contact": "liaison@hospital.example.invalid",
        "category": "critical-result-follow-up",
        "channel": "phone",
        "priority": "urgent",
        "subject": "Follow-up of a critical potassium call",
        "body": "Bench reports the first call attempt to the emergency department failed.",
        "team": "client-services",
        "assignee": AGENT,
        "age": 600,  # urgent, resolved within 4 hours and closed
        "steps": (
            (
                "contact",
                5,
                AGENT,
                "Reached the charge nurse; result read back and acknowledged.",
                ("to_client", "phone", "Charge nurse", "spoke_with"),
            ),
            ("resolve", 60, AGENT, "Critical value communicated on the second attempt."),
            ("close", 120, AGENT),
        ),
    },
    {
        "key": "recollection",
        "org": "EXGEN",
        "site": "DRAW",
        "contact": "courier@hospital.example.invalid",
        "category": "sample-problem",
        "channel": "phone",
        "priority": "high",
        "subject": "Hemolyzed specimen needs recollection",
        "body": "Specimen SMP-TEST-0450 arrived hemolyzed; a new draw is needed.",
        "team": "accessioning",
        "assignee": AGENT,
        "age": 900,  # high, waiting on the courier (does not pause): overdue
        "steps": (
            ("link", 10, AGENT, "lis", "sample", "SMP-TEST-0450", "primary", True),
            (
                "contact",
                30,
                AGENT,
                "Left a message for the draw station about the recollection.",
                ("to_client", "phone", "Draw station", "message_left"),
            ),
            ("status", 60, AGENT, Status.WAITING_THIRD_PARTY),
        ),
    },
    # Four pickups on the around-the-clock pickup target (24 hours), placed by their due time
    # (hours after the reset) rather than their age: one is due soon through most of the day.
    {
        "key": "pickup",
        "org": "EXGEN",
        "site": "DRAW",
        "contact": "courier@hospital.example.invalid",
        "category": "pickup",
        "channel": "phone",
        "priority": "urgent",
        "subject": "Missed afternoon pickup at the draw station",
        "body": "Courier did not stop at the outpatient draw station; six specimens waiting.",
        "team": "logistics",
        "due_in": 2,
        "steps": (),
    },
    {
        "key": "pickup-west",
        "org": "EXCLIN",
        "site": "WEST",
        "category": "pickup",
        "channel": "portal",
        "by": CLINIC_PORTAL,
        "priority": "high",
        "form": "urgent-pickup",
        "values": lambda template: {
            "ready-at": _later(hours=1),
            "container-count": 2,
            "site-contact": "Westside front desk, 555-0103",
            "labels-confirmed": True,
            "details": "STAT specimens for the afternoon clinic.",
        },
        "subject": "Urgent pickup at the Westside office",
        "body": "Two STAT specimens are packed at the Westside office.",
        "due_in": 8,
        "steps": (
            ("assign", 10, "demo_admin", "logistics", "demo_admin"),
            ("reply", 15, "demo_admin", "A courier is on the way; expect them within the hour."),
            ("correct", 20, "demo_admin", {"courier": "Route 2 (west loop)"}, ""),
            ("note", 30, "demo_admin", "Added to the next courier route."),
        ),
    },
    {
        "key": "pickup-north",
        "org": "EXWATER",
        "site": "NORTH",
        "contact": "sampler@water.example.invalid",
        "category": "pickup",
        "channel": "email",
        "subject": "Pickup of the weekly north plant samples",
        "body": "Field sampler asks for the coolers to be collected at the north plant gate.",
        "team": "logistics",
        "due_in": 14,
        "steps": (("reply", 20, "demo_admin", "The courier will collect them at the gate."),),
    },
    {
        "key": "pickup-recovery",
        "org": "EXREC",
        "site": "CLINIC",
        "contact": "program@recovery.example.invalid",
        "category": "pickup",
        "channel": "phone",
        "subject": "Pickup of collection kits at the recovery clinic",
        "body": "Program coordinator has ten collected kits ready for pickup.",
        "team": "logistics",
        "due_in": 20,
        "steps": (),
    },
    {
        "key": "requisition",
        "org": "EXREC",
        "contact": "program@recovery.example.invalid",
        "category": "requisition-information",
        "channel": "fax",
        "subject": "Requisition missing the ordering provider",
        "body": "Faxed requisition for accession ACC-TEST-000512 has no ordering provider.",
        "team": "accessioning",
        "assignee": AGENT,
        "age": 500,  # waiting on the client: paused
        "steps": (
            (
                "contact",
                30,
                AGENT,
                "Called the program coordinator; voicemail.",
                ("to_client", "phone", "Program coordinator", "message_left"),
            ),
            (
                "reply",
                60,
                AGENT,
                "Please send the ordering provider for accession ACC-TEST-000512.",
                Status.WAITING_CLIENT,
            ),
        ),
    },
    {
        "key": "ccf",
        "org": "EXFRT",
        "contact": "der@freight.example.invalid",
        "category": "custody-control-form",
        "channel": "email",
        "priority": "high",
        "subject": "Custody and control form CCF-TEST-000777 incomplete",
        "body": "The collector did not sign step 4 of CCF-TEST-000777.",
        "team": "accessioning",
        "age": 150,  # high, no reply after 2.5 business hours: first response overdue
        "steps": (("link", 10, AGENT, "lis", "custody_form", "CCF-TEST-000777", "primary", False),),
    },
    {
        "key": "mro",
        "org": "EXFRT",
        "contact": "der@freight.example.invalid",
        "category": "mro-routing",
        "channel": "phone",
        "subject": "Which medical review officer receives the results",
        "body": "Employer representative asks where verified results are routed.",
        "team": "client-services",
        "assignee": MANAGER,
        "age": 3000,  # resolved after its target: missed
        "steps": (
            ("note", 100, MANAGER, "Checking the program's MRO on file with the administrator."),
            ("resolve", 2400, MANAGER, "Confirmed the MRO of record with the program."),
        ),
    },
    {
        "key": "coc",
        "org": "EXWATER",
        "site": "NORTH",
        "category": "chain-of-custody-discrepancy",
        "channel": "portal",
        "by": WATER_PORTAL,
        "priority": "high",
        "subject": "Two samples on COC-TEST-000021 without a collection time",
        "body": "Samples 3 and 4 on COC-TEST-000021 have no collection time on the form.",
        "age": 600,  # high, back with the lab after the client's answer: on track
        "steps": (
            ("assign", 5, AGENT, "accessioning", AGENT),
            ("link", 10, AGENT, "example-lims", "custody_form", "COC-TEST-000021", "primary", True),
            ("link", 12, AGENT, "example-lims", "sample", "SMP-TEST-0021-3", "related", False),
            ("link", 14, AGENT, "example-lims", "sample", "SMP-TEST-0021-4", "related", False),
            (
                "reply",
                30,
                AGENT,
                "Please confirm the collection times of samples 3 and 4.",
                Status.WAITING_CLIENT,
            ),
            ("client_reply", 200, WATER_PORTAL, "Both were collected at 07:40 on Monday."),
            ("note", 250, AGENT, "Times recorded; holding time is still met."),
            ("reply", 300, AGENT, "Thank you. Both samples are being analysed."),
        ),
    },
    {
        "key": "bottle-kits",
        "org": "EXWATER",
        "site": "SOUTH",
        "contact": "results@water.example.invalid",
        "category": "bottle-kit-order",
        "channel": "email",
        "priority": "low",
        "subject": "Bottle kits for the south plant",
        "body": "Twelve metals kits for next week's south plant sampling.",
        "team": "logistics",
        "assignee": "demo_admin",
        "age": 900,  # low, waiting on the courier (pauses): paused; invisible to the north portal
        "steps": (
            ("reply", 60, "demo_admin", "Kits are packed and with the courier."),
            ("status", 60, "demo_admin", Status.WAITING_THIRD_PARTY),
        ),
    },
    {
        "key": "edd",
        "org": "EXWATER",
        "site": "DIST",
        "contact": "results@water.example.invalid",
        "category": "electronic-deliverable",
        "channel": "email",
        "subject": "Electronic deliverable rejected by the state portal",
        "body": "The October EDD for the distribution samples was rejected for a date format.",
        "team": "interfaces-it",
        "assignee": "demo_admin",
        "age": 2400,  # normal: past its 36 business hours, overdue
        "steps": (("reply", 100, "demo_admin", "We are regenerating the file with ISO dates."),),
    },
    {
        "key": "scope",
        "org": "EXWATER",
        "contact": "results@water.example.invalid",
        "category": "project-scope",
        "channel": "in_person",
        "priority": "low",
        "subject": "Adding PFAS to the distribution monitoring project",
        "body": "Discussed at the quarterly meeting: add PFAS to the quarterly samples.",
        "team": "technical",
        "assignee": TECH,
        "age": 4000,  # closed
        "steps": (
            ("resolve", 500, TECH, "Quote sent; scope change recorded in the LIMS project."),
            ("close", 600, TECH),
        ),
    },
    {
        "key": "coa",
        "org": "EXMFG",
        "site": "PLANT1",
        "reported_by": "Plant QA lead (caller)",
        "category": "certificate-of-analysis",
        "channel": "email",
        "subject": "Certificate of analysis for lot LOT-TEST-0099",
        "body": "QA asks whether the certificate for LOT-TEST-0099 includes heavy metals.",
        "team": "technical",
        "assignee": TECH,
        "age": 600,  # normal: on track
        "steps": (
            ("link", 10, TECH, "lis", "lot", "LOT-TEST-0099", "primary", False),
            ("reply", 60, TECH, "Heavy metals are on page 2 of the certificate."),
        ),
    },
    {
        "key": "spec",
        "org": "EXMFG",
        "site": "PLANT1",
        "reported_by": "Plant QA lead (caller)",
        "category": "batch-specification",
        "channel": "phone",
        "priority": "high",
        "subject": "Specification limit for moisture on batch LOT-TEST-0101",
        "body": "QA asks which moisture specification the lab applied to LOT-TEST-0101.",
        "team": "technical",
        "assignee": TECH,
        "age": 2000,  # resolved within its target: met
        "steps": (("resolve", 400, TECH, "Specification version 3 applied, as on the order."),),
    },
    {
        "key": "distribution",
        "org": "EXRES",
        "contact": "coordinator@research.example.invalid",
        "category": "deposit-distribution",
        "channel": "email",
        "subject": "Distribution of aliquots for study STUDY-TEST-12",
        "body": "The study coordinator asks for 20 aliquots for the approved substudy.",
        "team": "technical",
        "assignee": TECH,
        "age": 700,  # normal: on track
        "steps": (
            ("link", 10, TECH, "lis", "study", "STUDY-TEST-12", "primary", True),
            ("link", 20, LEAD, "lis", "participant", "PARTICIPANT-TEST-0007", "related", False),
            ("reply", 100, TECH, "Feasibility confirmed; shipping next week."),
        ),
    },
    {
        "key": "access",
        "org": "EXRES",
        "contact": "coordinator@research.example.invalid",
        "category": "access-approval",
        "channel": "other",
        "priority": "low",
        "subject": "Material transfer agreement status",
        "body": "Coordinator asks whether the material transfer agreement is signed.",
        "team": "client-services",
        "assignee": MANAGER,
        "age": 1200,  # waiting on the institution's office (pauses): paused
        "steps": (
            ("reply", 60, MANAGER, "The agreement is with the institution's contracts office."),
            ("status", 60, MANAGER, Status.WAITING_THIRD_PARTY),
        ),
    },
    {
        "key": "interface",
        "category": "report-delivery",
        "channel": "internal",
        "priority": "urgent",
        "subject": "Results feed to the Westside office stopped",
        "body": "Interface monitor shows no messages to the clinic's Westside feed since 06:00.",
        "team": "interfaces-it",
        "assignee": "demo_admin",
        "age": 1500,  # urgent, resolved after 4 business hours: missed, then closed
        "steps": (
            ("note", 60, "demo_admin", "Certificate on the interface host expired."),
            ("resolve", 400, "demo_admin", "Certificate renewed; queued results delivered."),
            ("close", 450, "demo_admin"),
        ),
    },
    {
        "key": "privacy",
        "reported_by": "Unknown caller",
        "category": "privacy-request",
        "channel": "phone",
        "subject": "Request for a copy of records",
        "body": "Caller asked how to obtain a copy of their records. Identity not yet verified.",
        "age": 300,  # restricted to Quality: the technical account never sees it
        "steps": (("note", 30, LEAD, "Sent the access request form; awaiting identity check."),),
    },
    {
        "key": "feedback",
        "org": "EXGEN",
        "contact": "liaison@hospital.example.invalid",
        "category": "feedback",
        "channel": "email",
        "priority": "low",
        "subject": "Thanks for the quick turnaround during the outage",
        "body": "The liaison thanks the team for the fast turnaround last weekend.",
        "team": "quality",
        "assignee": LEAD,
        "age": 2000,  # closed
        "steps": (
            ("reply", 30, LEAD, "Thank you; we passed it on to the bench."),
            ("resolve", 60, LEAD, "Shared with the team."),
            ("close", 70, LEAD),
        ),
    },
    {
        "key": "complaint-closed",
        "org": "EXWATER",
        "contact": "results@water.example.invalid",
        "category": "complaint",
        "channel": "email",
        "priority": "high",
        "subject": "Report sent to the wrong project contact",
        "body": "The utility complains that a report went to a former project contact.",
        "team": "client-services",
        "assignee": AGENT,
        "age": 3600,  # decided, recorded, reviewed independently and closed
        "steps": (
            (
                "decide",
                60,
                AGENT,
                ComplaintDecision.INVESTIGATE,
                "Possible wrong recipient; check the distribution list.",
            ),
            (
                "record",
                300,
                AGENT,
                "Distribution list still held a former contact; removed and re-sent.",
                "CA-TEST-0003",
                True,
            ),
            ("reply", 310, AGENT, "The report now goes only to your current project contacts."),
            ("resolve", 500, AGENT, "Distribution list corrected; client informed."),
            ("review", 700, LEAD, "Checked the corrected list and the re-sent report."),
            ("close", 800, AGENT),
        ),
    },
    {
        "key": "technical",
        "org": "EXWATER",
        "site": "NORTH",
        "category": "result-question",
        "channel": "portal",
        "by": WATER_PORTAL,
        "form": "result-question",
        "values": lambda template: {
            "question-kind": "Please explain a result",
            "order-reference": "SMP-TEST-0021-3",
            "test": "Lead",
            "question": "Which reporting limit applies to a diluted sample?",
        },
        "subject": "Reporting limit for a diluted sample",
        "body": "Which reporting limit applies to a diluted sample?",
        "age": 300,  # normal: on track; raised in the north plant's portal
        "steps": (
            ("assign", 10, TECH, "technical", TECH),
            ("note", 100, TECH, "Dilution factor 5; the elevated limit applies."),
            ("correct", 110, TECH, {"needs-consult": False}, ""),
            ("reply", 150, TECH, "The reporting limit is multiplied by the dilution factor (5)."),
        ),
    },
    {
        "key": "service-info",
        "org": "EXCLIN",
        "contact": "office@clinic.example.invalid",
        "category": "service-information",
        "channel": "phone",
        "priority": "low",
        "subject": "Fasting requirement for the lipid panel",
        "body": "Office asks whether the lipid panel needs fasting.",
        "team": "client-services",
        "assignee": MANAGER,
        "by": MANAGER,
        "age": 100,  # answered on the call and resolved: met
        "steps": (("resolve", 20, MANAGER, "Answered on the call: no fasting required."),),
    },
    {
        "key": "account-change",
        "org": "EXRES",
        "contact": "coordinator@research.example.invalid",
        "category": "account-change",
        "channel": "email",
        "subject": "New study coordinator for the institute",
        "body": "The institute names a new study coordinator from next month.",
        "team": "client-services",
        "assignee": MANAGER,
        "age": 400,  # normal: on track
        "steps": (("reply", 120, MANAGER, "Thank you; we will add them as a contact."),),
    },
    # Requests raised with request forms besides those above.
    {
        "key": "pickup-routine",
        "org": "EXCLIN",
        "site": "MAIN",
        "category": "pickup",
        "channel": "portal",
        "by": CLINIC_PORTAL,
        "form": "pickup",
        "values": lambda template: {
            "ready-at": _later(days=1, at_hour=10),
            "container-count": 3,
            "handling": ["Refrigerated"],
            "site-contact": "Front desk, 555-0102",
            "access-notes": "Side entrance; ring the bell.",
        },
        "reference": "ORD-TEST-000051",
        "subject": "",
        "body": "",
        "due_in": 16,
        "steps": (
            ("reply", 10, "demo_admin", "Booked on tomorrow morning's route."),
            ("correct", 15, "demo_admin", {"courier": "Route 3 (north loop)"}, ""),
        ),
    },
    {
        "key": "bottle-kits-north",
        "org": "EXWATER",
        "site": "NORTH",
        "category": "bottle-kit-order",
        "channel": "portal",
        "by": WATER_PORTAL,
        "form": "bottle-kit-order",
        "values": lambda template: {
            "items": _catalog_items(template, (4, 4, 2)),
            "project": "PROJ-TEST-0001",
            "sampling-date": _later(days=7).date(),
            "sampling-points": 6,
        },
        "subject": "",
        "body": "",
        "age": 500,  # low: shipped, waiting on the courier (pauses)
        "steps": (
            ("assign", 10, "demo_admin", "logistics", "demo_admin"),
            (
                "correct",
                120,
                "demo_admin",
                {"shipped-on": "today", "tracking": "TRK-TEST-0105"},
                "",
            ),
            ("reply", 125, "demo_admin", "Your kits are on their way; tracking is on the ticket."),
            ("status", 125, "demo_admin", Status.WAITING_THIRD_PARTY),
        ),
    },
    {
        "key": "tech-support",
        "org": "EXGEN",
        "contact": "liaison@hospital.example.invalid",
        "category": "report-delivery",
        "channel": "email",
        "form": "technical-support",
        "values": lambda template: {
            "problem": "Results missing in our system",
            "system": "Hospital record system, inbound results feed",
            "affected-count": 6,
            "example": "ORD-TEST-000088",
        },
        "subject": "Results missing in the hospital's system",
        "body": "Six released results are not showing in the hospital's record system.",
        "team": "interfaces-it",
        "assignee": "demo_admin",
        "age": 120,  # normal: on track
        "steps": (
            ("correct", 30, "demo_admin", {"interface-reference": "MSG-TEST-0042"}, ""),
            ("reply", 60, "demo_admin", "We found the stuck messages and are resending them."),
        ),
    },
    {
        "key": "status-unknown",
        "org": "EXCLIN",
        "contact": "office@clinic.example.invalid",
        "category": "result-status",
        "channel": "phone",
        "form": "result-status",
        "details_not_known": True,
        "values": lambda template: {
            "problem": "The result was released but we did not receive it",
        },
        "subject": "Result not received; order number to follow",
        "body": "Caller did not have the order number to hand; will call back.",
        "team": "client-services",
        "assignee": AGENT,
        "age": 90,  # answered on the call; details still missing
        "steps": (("correct", 20, AGENT, {"checked-in-system": True}, ""),),
    },
)


def _later(*, days=0, hours=0, at_hour=None):
    """A moment after the step's own time (the patched now): for "ready at" and planned dates."""
    moment = timezone.localtime() + timedelta(days=days, hours=hours)
    if at_hour is not None:
        moment = moment.replace(hour=at_hour, minute=0, second=0, microsecond=0)
    return moment


def _catalog_items(template, quantities):
    """Item values for a form's items field: the catalog's first active items, these
    quantities."""
    field = template.fields.get(key__in=("items", "materials"), kind="items")
    items = field.catalog.items.filter(is_active=True).order_by("position", "pk")
    return {item.pk: quantity for item, quantity in zip(items, quantities, strict=False)}


@contextmanager
def happened_at(moment):
    """Run the services as if it were `moment` (see the module's docstring)."""
    with mock.patch("django.utils.timezone.now", return_value=moment):
        yield


def _calendar():
    calendar = BusinessCalendar.objects.filter(name=CALENDAR).first()
    return calendar.as_calendar() if calendar else None


def started(age_minutes, now, calendar):
    """The latest moment from which `age_minutes` of business time have passed by `now`."""
    target = age_minutes * 60
    low, high = now - timedelta(days=90), now
    for _ in range(48):
        middle = low + (high - low) / 2
        if business_seconds_between(middle, now, calendar) >= target:
            low = middle
        else:
            high = middle
    return low


def started_for_due(spec, now):
    """The start that puts the request's resolution due time `due_in` hours after `now`, on the
    target that applies to it."""
    from service_levels.clocks import applicable_target, calendar_value

    category = RequestCategory.objects.get(key=spec["category"])
    target = applicable_target(category, spec.get("priority") or category.default_priority)
    calendar = calendar_value(target.calendar_id)
    due = now + timedelta(hours=spec["due_in"])
    low, high = now - timedelta(days=90), now
    for _ in range(48):
        middle = low + (high - low) / 2
        reached = add_business_seconds(middle, target.resolution_minutes * 60, calendar)
        if reached is not None and reached <= due:
            low = middle
        else:
            high = middle
    return low, calendar


def _when(start, minutes, calendar, now):
    moment = add_business_seconds(start, minutes * 60, calendar) or start
    return min(moment, now - timedelta(minutes=1))


def grant_capability():
    """ "Can view patient information" for demo_supervisor only, audited like an admin grant."""
    holder = User.objects.get(username=CAPABILITY_HOLDER)
    permission = Permission.objects.get(codename=CAPABILITY, content_type__app_label="accounts")
    if holder.user_permissions.filter(pk=permission.pk).exists():
        return
    holder.user_permissions.add(permission)
    record_event(
        actor=User.objects.get(username="demo_admin"),
        action="account.capability_granted",
        obj=holder,
        after={"capability": CAPABILITY},
        reason="Demonstration seed: the supervisor sees patient links",
    )


def _fresh(request):
    return ServiceRequest.objects.get(pk=request.pk)


def _step(request, step, users):
    from links.seed import seed_request_link

    action, _minute, who, *args = step
    actor = users[who]
    request = _fresh(request)
    revision = request.revision
    if action in ("reply", "note"):
        body, *status = args
        services.add_entry(
            actor=actor,
            request=request,
            kind=EntryKind.REPLY if action == "reply" else EntryKind.NOTE,
            body=body,
            expected_revision=revision,
            new_status=status[0] if status else None,
        )
    elif action == "client_reply":
        services.add_entry(
            actor=actor,
            request=request,
            kind=EntryKind.REPLY,
            body=args[0],
            expected_revision=revision,
        )
    elif action == "contact":
        body, (direction, channel, with_whom, outcome) = args
        services.add_entry(
            actor=actor,
            request=request,
            kind=EntryKind.CONTACT,
            body=body,
            expected_revision=revision,
            contact={
                "direction": direction,
                "channel": channel,
                "with_whom": with_whom,
                "outcome": outcome,
            },
        )
    elif action == "assign":
        team, assignee = args
        services.assign(
            actor=actor,
            request=request,
            expected_revision=revision,
            team=Team.objects.get(slug=team),
            assignee=users[assignee] if assignee else None,
        )
    elif action == "status":
        services.change_status(
            actor=actor, request=request, expected_revision=revision, status=args[0]
        )
    elif action == "resolve":
        services.change_status(
            actor=actor,
            request=request,
            expected_revision=revision,
            status=Status.RESOLVED,
            summary=args[0],
        )
    elif action == "close":
        services.change_status(
            actor=actor, request=request, expected_revision=revision, status=Status.CLOSED
        )
    elif action == "decide":
        decision, rationale = args
        if not request.is_complaint:  # a category set up without the complaint flag
            services.set_complaint(
                actor=actor,
                request=request,
                expected_revision=revision,
                is_complaint=True,
                reason="The caller made a complaint.",
            )
            request = _fresh(request)
            revision = request.revision
        services.decide_complaint(
            actor=actor,
            request=request,
            expected_revision=revision,
            decision=decision,
            rationale=rationale,
        )
    elif action == "record":
        findings, reference, *communicated = args
        services.update_complaint(
            actor=actor,
            request=request,
            expected_revision=revision,
            findings=findings,
            corrective_action_reference=reference,
            outcome_communicated_on=timezone.localdate() if communicated else None,
            outcome_communicated_how="By email to the project manager" if communicated else "",
        )
    elif action == "review":
        services.review_complaint(
            actor=actor, request=request, expected_revision=revision, note=args[0]
        )
    elif action == "correct":
        from request_forms import definition
        from request_forms.models import FieldValue, FilledForm
        from request_forms.services import correct_values

        values, reason = args
        values = {
            key: timezone.localdate() if value == "today" else value
            for key, value in values.items()
        }
        current = {
            row.key: row.pk
            for row in FieldValue.objects.filter(
                filled_form__request=request, key__in=values, superseded_at__isnull=True
            )
        }
        filled = FilledForm.objects.select_related("template").get(request=request)
        correct_values(
            actor=actor,
            request=request,
            expected_definition=definition.read_for_correction(filled).token,
            values=values,
            reason=reason,
            expected={key: current.get(key) for key in values},
        )
    elif action == "withdraw":
        reply = request.entries.filter(kind=EntryKind.REPLY).order_by("-created_at").first()
        services.withdraw_reply(
            actor=actor, entry=reply, reason=args[0], expected_revision=revision
        )
    elif action == "link":
        system, record_type, identifier, role, shown = args
        seed_request_link(
            request,
            system_key=system,
            record_type=record_type,
            identifier=identifier,
            actor=actor,
            role=role,
            shown_to_client=shown,
        )
    else:  # pragma: no cover - a typo in STORY
        raise ValueError(f"Unknown story step {action!r}.")


def _create(spec, users, by_key):
    client = Client.objects.filter(account_number=spec["org"]).first() if "org" in spec else None
    site = ClientSite.objects.get(client=client, code=spec["site"]) if spec.get("site") else None
    contact = (
        ClientContact.objects.get(client=client, email=spec["contact"])
        if spec.get("contact")
        else None
    )
    portal = spec["channel"] == "portal"
    actor = users[spec.get("by", AGENT)]
    common = dict(
        first_entry_kind=EntryKind.REPLY if portal else EntryKind.NOTE,
        channel=spec["channel"],
        client=client,
        site=site,
        contact=None if portal else contact,
        reported_by=spec.get("reported_by", ""),
        priority=spec.get("priority"),
        team=None if portal or "team" not in spec else Team.objects.get(slug=spec["team"]),
        assignee=None if portal or "assignee" not in spec else users[spec["assignee"]],
        client_reference=spec.get("reference", ""),
        follow_up_of=_fresh(by_key[spec["follow_up_of"]]) if "follow_up_of" in spec else None,
    )
    if "form" in spec:
        from request_forms import definition
        from request_forms.models import RequestTemplate
        from request_forms.services import create_request_with_form

        template = RequestTemplate.objects.get(key=spec["form"], is_active=True)
        return create_request_with_form(
            actor=actor,
            template=template,
            expected_definition=definition.token(template),
            values=spec["values"](template),
            details_not_known=spec.get("details_not_known", False),
            subject=spec["subject"],
            first_entry_body=spec["body"],
            **common,
        )
    return services.create_request(
        actor=actor,
        category=RequestCategory.objects.get(key=spec["category"]),
        subject=spec["subject"],
        first_entry_body=spec["body"],
        **common,
    )


def switch_on_patient_identifier():
    """The add-on form's patient identifier field, which the presets ship inactive, switched on
    with a reason, as a laboratory that asks for it would."""
    from request_forms.models import TemplateField
    from request_forms.services import update_field

    field = TemplateField.objects.filter(template__key="add-on", key="patient-identifier").first()
    if field is not None and not field.is_active:
        update_field(
            actor=User.objects.get(username="demo_admin"),
            field_id=field.pk,
            expected_revision=field.revision,
            reason="Demonstration: the example laboratory asks for the patient identifier on "
            "add-on requests. Synthetic data only.",
            is_active=True,
        )


def tell():
    """Seed the story once (an existing request means it has been told)."""
    if ServiceRequest.objects.exists():
        return
    grant_capability()
    switch_on_patient_identifier()
    usernames = {AGENT, LEAD, TECH, MANAGER, CLINIC_PORTAL, WATER_PORTAL, "demo_admin"}
    users = User.objects.in_bulk(list(usernames), field_name="username")
    calendar = _calendar()
    now = timezone.now()
    by_key = {}
    for spec in STORY:
        if "due_in" in spec:
            start, step_calendar = started_for_due(spec, now)
        else:
            start, step_calendar = started(spec["age"], now, calendar), calendar
        with transaction.atomic(), happened_at(start):
            by_key[spec["key"]] = _create(spec, users, by_key)
        for step in spec["steps"]:
            with transaction.atomic(), happened_at(_when(start, step[1], step_calendar, now)):
                _step(by_key[spec["key"]], step, users)
    return by_key
