#!/usr/bin/env python3
"""
presidio_name_audit.py

Scans PDFs for personal names using Microsoft Presidio,
deduplicates them, tiers them by confidence, and
reports every page each name appears on.

The work is split into two independent phases so the expensive part only
runs once:

    scan    -- walk the PDFs in page chunks, cache every raw detection to
               SQLite. Resumable, parallel across files, checkpointed per
               chunk.

    report  -- read the cache, deduplicate, tier, fuzzy-flag variants, and
               write the CSV + run manifest. Cheap, so thresholds can be
               retuned and re-run in seconds without rescanning.

------------------------------------------------------------------------
SETUP (one time)
------------------------------------------------------------------------
pip install presidio-analyzer pdfplumber spacy rapidfuzz numpy --break-system-packages
python -m spacy download en_core_web_lg

------------------------------------------------------------------------
USAGE
------------------------------------------------------------------------
# Typical workflow
python presidio_name_audit.py all ./pdfs --out names_report.csv

# Phase 1 -- slow, resumable. Re-run after a crash and it picks up where
# it stopped.
python presidio_name_audit.py scan ./pdfs --workers 4 --chunk-size 100

# Phase 2 -- fast. Re-run freely with different thresholds.
python presidio_name_audit.py report --out names_report.csv \
    --certain-threshold 0.85 --light-threshold 0.60

# Prepare a gazetteer from raw Census/SSA files, then scan with it:
python prepare_gazetteer.py ./raw_names --out-dir ./gazetteer
python presidio_name_audit.py scan ./pdfs \
    --gazetteer ./gazetteer/gazetteer_names.txt \
    --gazetteer-ambiguous ./gazetteer/gazetteer_ambiguous.txt

# Suppressed rows always land in names_report.suppressed.csv. To fold
# them back into the main report instead (each marked with the reason
# that fired), re-run the cheap phase -- no rescan needed:
python presidio_name_audit.py report --out names_report.csv --include-suppressed

# Starting a new project: wipe the cached detections from the old one.
# Prompts before deleting; --yes skips the prompt for scripted use.
python presidio_name_audit.py clear
python presidio_name_audit.py clear --cache-dir .name_audit_cache --yes

# Better still, give each project its own cache directory and clearing
# becomes unnecessary -- old projects stay intact and separate:
python presidio_name_audit.py all ./pdfs --cache-dir .cache_projectA

------------------------------------------------------------------------
OUTPUT
------------------------------------------------------------------------
names_report.csv
    name, tier, confidence, possible_minor, occurrence_count, file_count,
    locations, possible_duplicate_of, recognizer, minor_tier,
    minor_binding, minor_reason, suppressed, suppress_reason,
    also_reported_as

names_report.suppressed.csv
    Same columns, holding the rows this run withheld from the main
    report: OCR debris, organisations, medical eponyms and bare
    salutations. Written EVERY run, empty or not.

    Suppression is a judgement the tool makes on the reviewer's behalf,
    and a reviewer who cannot see what was withheld cannot check the
    judgement -- so nothing is ever deleted. The verdict is cached per
    hit at scan time and applied at report time, which means the rules
    can be retuned, or reversed wholesale with --include-suppressed,
    without paying for a rescan.

    Four rules, all of them deliberately timid, because each one can eat
    a real person and that is the more expensive error:

        ocr_garbage      a character that cannot occur in a name
                         survived edge-trimming (i.e. sits in the
                         span's INTERIOR), or two independent
                         orthographic tells fired together. Debris is
                         TRIMMED before it is judged, so "Sarah
                         Kowalski<dagger><pilcrow>" is reported as Sarah
                         Kowalski rather than discarded. Ng, Krk and
                         every non-Latin script are exempt from the
                         vowel and consonant-run tests, which are
                         properties of the Latin alphabet and not of
                         names.

        organization     a strong institutional token ("Council",
                         "Hospital", "LLC"), or the NER model itself
                         labelling the same characters ORGANIZATION, or
                         three weak tokens together. Weak tokens --
                         Church, Grace, Hall, Valley, Park -- never
                         suppress in ones or twos: Summer Church and
                         Grace Hall are people.

        medical_term     the span is followed by a HEAD NOUN --
                         "syndrome", "manoeuvre", "forceps",
                         "distribution". Keying on the head noun rather
                         than on a catalogue of conditions covers the
                         whole eponym space in one rule. A bare
                         "Turner", "Bell", "Graves" or "Down" is left
                         alone; only the possessive form convicts.

        salutation_only  the span is nothing but correspondence
                         furniture. Greetings, valedictions and
                         honorifics are otherwise TRIMMED, not
                         suppressed -- "Hello Sarah" reports Sarah,
                         because the recipient of a letter is a real
                         person and belongs in a PII audit. Only the
                         unambiguous words qualify; George Best survives.

        denylist         every token of the span appears in a
                         user-supplied --denylist file. Requires ALL
                         tokens, so listing "hospital" cannot take a
                         person surnamed Hospital with it.

    A name is withheld only if EVERY occurrence of it was suppressed.
    One clean sighting rescues it: "Bell" following "syndrome" on page 4
    does not erase Nurse Bell on page 9.

    also_reported_as shows what was folded into a row -- the untrimmed
    surface form, and any surname-first variant merged into it -- so
    every edit the tool made is visible without reopening the PDF.

    recognizer is "spacy", "gazetteer", or "both" -- which engine(s)
    produced the detections behind a name, so the dictionary recogniser's
    contribution (and its precision cost) is measurable. Gazetteer files
    are supplied with --gazetteer (see --help); without them the tool runs
    exactly as before.

    locations uses compressed page ranges: "fileA.pdf:p3,p17-p22; fileB.pdf:p9"

    THE GAZETTEER. Raw Census surname and SSA given-name files should be
    run through prepare_gazetteer.py before use, NOT passed to
    --gazetteer directly. As shipped they compile to ~197,000 tokens and
    emit ordinary capitalised English as PERSON: measured on 40 everyday
    phrases from documents of this kind, 37 fired ("Blood Pressure",
    "Field Trip", "Legal Guardian", "White House").

    Preparation does three things. It cuts the frequency tails, which
    handles the rare junk. It deletes English function words that are
    also attested surnames. And it emits a separate demotion list for
    tokens that are both real names and common words -- White, King,
    Green, Hill, Hall, Bell -- which cannot be deleted, because they are
    among the most common surnames in the country, and cannot be
    thresholded, because they sit at the TOP of the distribution, not in
    the tail. Pass that list to --gazetteer-ambiguous and a dictionary
    span must contain at least one match from outside it.

    Combined effect on the same probe sets: 37/40 junk phrases down to
    0/40, with recall on 27 real names rising from 25 to 26 -- the
    derived feminine surname forms find women the Census file omits.

    INVERTED NAMES. "Smith, John" is detected in table cells (by column
    header OR by column shape, so headerless rosters and columns headed
    "Last, First" no longer fall through) and in running prose -- "RE:"
    lines, index entries, semicolon-separated service lists, signature
    blocks -- none of which live in a table and none of which the flip
    path previously reached.

    Detection was only half of it. Both forms still keyed separately, so
    a child caught as "Kowalska, Zofia" in a roster column and as "Zofia
    Kowalska" in the narrative appeared twice, flagged once, with her
    occurrence count split. The two now merge into one row -- but ONLY
    when both forms were actually observed. Flipping is genuinely
    ambiguous for names plausible in either order, so a lone "Thomas,
    James" is reported exactly as it was found.

    minor_binding is the verdict: WHY this name is believed to be a
    child. One of "column", "label", "dob", "relation", or empty. It
    replaces the previous ambient-proximity flag, which asked only
    whether child-related vocabulary appeared somewhere within 150
    characters, and which measured at 2.7% precision on the reference
    corpus (147 names flagged, 4 of them actually minors).

        column    the name occupied a cell in a table column whose HEADER
                  names a child -- "Student", "Pupil", "Minor child" --
                  and did not also occupy an adult-labelled column
        label     a minor role label sits immediately before the name, or
                  a minor appositive immediately after ("..., a minor",
                  "..., age 15"), or the name is coordinated with a name
                  that is itself label-bound
        dob       a LABELLED date of birth within the narrow window
                  implies the person is currently under 18
        relation  a relational narrative cue binds the name to a child
                  ("a classmate, Kaylee Hutchens") without asserting the
                  age directly

    Column binding exists because character distance is the wrong unit of
    measurement inside a table. A roster linearises into one long stream,
    so the child in row 6 is hundreds of characters from the header that
    identifies the column, while her parent in the next cell is adjacent
    to her. pdfplumber already knows which column each cell came from;
    the previous implementation discarded that and then tried to recover
    it from proximity, which cannot be done. Reading the header instead
    separates the seven children in a free/reduced-lunch roster from the
    seven parents beside them exactly, with no ambiguity to triage.

    ADULT BINDING NOW SUPPRESSES. A name bound to an adult role or
    credential ("Case Manager:", ", RN", "Parent/Guardian" column) is not
    flagged at all, where previously it was flagged at minor_tier=low.
    That tier held 136 of the 158 flags on the reference corpus and 3.7%
    of them were minors, so it cost far more review time than it bought.

    minor_tier orders the queue: high (column / label / dob), medium
    (relation). There is no longer a low tier -- weak ambient language is
    no longer a flag, so nothing lands there.

    minor_reason lists the signals that fired, so a flag can be triaged
    without reopening the source PDF.

    possible_minor is retained as a yes/blank column for consumers that
    depend on it, and is now simply (minor_binding != "").

------------------------------------------------------------------------
SCORING NOTE (important)
------------------------------------------------------------------------
spaCy's NER does not emit real per-entity confidences, so Presidio's
spaCy engine assigns a flat default score (0.85) to every PERSON hit.
The final confidence here is a composite:

    presidio score + context boost - shape penalties

Shape penalties down-weight detections that historically need human
eyes: single-token names, ALL-CAPS/lowercase strings, tokens containing
digits. Note that penalties only re-tier a row; they never remove it.
Removing is what the suppression rules do, and they report themselves. Tune the constants near the top of the file if your corpus
skews differently (e.g. depositions full of legitimately ALL-CAPS
caption names).

names_report.manifest.json
    Per-file page counts, text-layer status, skipped/failed pages,
    timings, and the thresholds used -- an audit trail for the run.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber

# ---------------------------------------------------------------------
# Tier labels
# ---------------------------------------------------------------------
TIER_CERTAIN = "essentially_certain"
TIER_LIGHT = "light_review"
TIER_EXTENSIVE = "extensive_review"

DEFAULT_CERTAIN_THRESHOLD = 0.85
DEFAULT_LIGHT_THRESHOLD = 0.60
DEFAULT_FUZZY_THRESHOLD = 88
DEFAULT_CHUNK_SIZE = 100
DEFAULT_MODEL = "en_core_web_lg"

# Bumped whenever the text-extraction strategy changes in a way that makes
# old cached detections incomparable with new ones (offsets, unit
# boundaries, occurrence semantics). v1 = whole-page extract_text();
# v2 = table-aware extraction + truecase variants; v3 = every relocated
# occurrence of a name scored, not just the first, plus role-bound minor
# tiering; v4 = table COLUMN roles carried through to the minor verdict,
# surname-first cells re-analysed in given-name-first order, and the
# ambient proximity flag replaced by explicit bindings; v5 = span
# sanitising (OCR debris trimmed, salutations and honorifics stripped),
# suppression verdicts cached per hit, ORGANIZATION/LOCATION spans
# scanned alongside PERSON as suppressors, and surname-first detection
# extended from table cells to prose lines. Shards built by a different
# version are refused, mirroring the src_sig/chunk_size checks.
# v6 = corroboration required for NER-ORG suppression (was firing on an
# unconditional ORGANIZATION/ORG/NRP tag alone; a real person -- "Georgia
# Bell" -- was lost to a spaCy ORG misread with nothing to check it);
# table-header rows ("Last, First") excluded from surname-first flip
# detection; eponym head-noun lookahead widened from a fixed next-token
# match to a bounded search, and the possessive-eponym branches'
# asymmetric evidence requirement removed. All of scan_one_pdf's
# suppression verdicts and flipped-name detection change under this
# version; a v5 shard's cached suppress_reason/flip results predate all
# four fixes and must not be trusted without a rescan.
EXTRACTOR_VERSION = 6

# Gazetteer-only detections land at this score by default. NOTE: the work
# order said "defaulting to 0.55" and, in the same sentence, that a score
# below light_threshold (0.60) is too low and the default must land these
# hits in light_review. 0.55 fails that second criterion (0.55 < 0.60 ->
# extensive_review unless a context boost happens to fire), so the two
# halves of the instruction conflict; the tier-placement intent wins.
# 0.65 sits in light_review and can never reach essentially_certain even
# with the +0.05 context boost (0.70 < 0.85). Override with
# --gazetteer-score.
DEFAULT_GAZETTEER_SCORE = 0.65
GAZETTEER_MIN_TOKEN_MATCHES = 2
GAZETTEER_RECOGNIZER_NAME = "GazetteerNameRecognizer"

# A line is "predominantly uppercase" (and gets a truecased variant
# analysed alongside it) when at least this share of its letters are
# uppercase, with enough letters/tokens to plausibly contain a name.
UPPERCASE_LINE_RATIO = 0.75
UPPERCASE_LINE_MIN_LETTERS = 6
UPPERCASE_LINE_MIN_TOKENS = 2

# Words/phrases near a detection that make the guess more trustworthy. A
# light heuristic nudge on top of Presidio's own score, not a replacement
# for it. Multi-word phrases are matched literally within the window.
#
# NOTE: "sincerely", "regards" and "signed" were REMOVED from this set.
# They are evidence that a document is correspondence, not evidence that
# the adjacent string is a person, and they sit in the one position where
# the detected span is likeliest to be contaminated by the boilerplate
# itself ("Sincerely, Sarah" truecases into something spaCy reads as two
# given names). Boosting them rewarded exactly the false positives that
# SALUTATION_TOKENS/VALEDICTION_TOKENS now trim. They remain in
# ADULT_ROLE_LABELS, where they do useful work binding a signatory to the
# adult side.
CONTEXT_BOOST_WORDS = {
    # General / correspondence
    "mr", "mrs", "ms", "dr", "prof",
    "attn", "attention", "cc",
    # Healthcare
    "patient", "physician", "provider", "nurse", "therapist", "counselor",
    "clinician", "psychiatrist", "psychologist", "diagnosed", "treated by",
    "referred by", "admitted", "discharged", "guardian", "next of kin",
    "emergency contact", "caregiver", "case manager", "social worker",
    "attending", "examined by", "evaluated by",
    # Education
    "student", "teacher", "instructor", "professor", "principal", "iep",
    "504", "parent", "enrolled", "expelled", "suspended", "disciplinary",
    "coach", "advisor", "dean", "superintendent", "paraeducator",
    "classroom aide", "homeroom", "teacher's aide",
    # Legal / procedural
    "witness", "attorney", "esq", "plaintiff", "defendant", "deponent",
    "affiant", "petitioner", "respondent", "complainant",
    "guardian ad litem", "custodian", "minor child", "next friend",
    "notary", "court reporter", "counsel", "filed by", "sworn", "declarant",
    # High-impact additions (estates, claims, testimony)
    "decedent", "executor", "beneficiary", "trustee", "claimant",
    "testified", "interviewed by", "on behalf of",
}
CONTEXT_BOOST_AMOUNT = 0.05
CONTEXT_WINDOW = 30

# The minor window is now NARROW and deliberately so. At 150 characters
# it was picking up the whole neighbourhood: on the reference corpus the
# top false-positive drivers were "dob" (36 rows), "daughter" (31),
# "father" (17), "school nurse" (16) and "mother" (13), none of which say
# anything about the name they were attached to. 40 characters is about
# an appositive clause -- "..., a classmate, Kaylee Hutchens" -- which is
# the only narrative construction that does bind.
MINOR_CONTEXT_WINDOW = 40

# Tokens that mark an institution rather than a person. spaCy's NER
# habitually tags things like "Fabian Socialism", "Department of
# Politics" or "Johns Hospital" as PERSON.
#
# The set is split in two, because a flat list cannot tell "Stevens
# Council" from a person surnamed Church. STRONG terms are words that are
# effectively never surnames, and one is enough to suppress. WEAK terms
# are institution-flavoured words that ARE attested surnames or given
# names -- Church, Bell, Hall, Mercy, Grace, Valley -- and one alone
# proves nothing; they suppress only in company (see org_verdict).
#
# Suppression here is recorded, never destructive: suppressed rows go to
# the sidecar CSV with their reason, and --include-suppressed puts them
# back in the main report.
INSTITUTIONAL_TERMS = {
    # Government / civic
    "department", "politics", "socialism", "committee", "commission",
    "council", "agency", "bureau", "office", "authority", "administration",
    "municipality", "township", "borough", "precinct", "district",
    "directorate", "secretariat", "tribunal", "judiciary",
    # Education
    "university", "college", "academy", "schools", "schoolhouse",
    "institute", "institution", "polytechnic", "seminary", "campus",
    "faculty", "curriculum", "district's",
    # Health
    "hospital", "hospitals", "clinic", "clinics", "healthcare",
    "infirmary", "dispensary", "sanitarium", "sanatorium", "polyclinic",
    "laboratory", "laboratories", "labs", "pharmacy", "pharmaceuticals",
    "radiology", "pathology", "orthopedics", "orthopaedics", "urgent",
    "ambulatory", "outpatient", "inpatient", "hospice", "nursing",
    # Corporate / legal entity suffixes
    "incorporated", "inc", "llc", "l.l.c.", "llp", "pllc", "plc", "pc",
    "ltd", "limited", "gmbh", "s.a.", "n.v.", "corp", "corporation",
    "company", "holdings", "enterprises", "industries", "technologies",
    "solutions", "systems", "services", "consulting", "consultants",
    "partners", "associates", "affiliates", "ventures", "capital",
    # Collective nouns
    "association", "society", "federation", "consortium", "coalition",
    "syndicate", "cooperative", "alliance", "network", "conference",
    "ministry", "foundation", "endowment", "party", "chapter", "league",
    "guild", "assembly", "congregation", "diocese", "archdiocese",
}
INSTITUTIONAL_PENALTY = 0.50

# Institution-flavoured tokens that are also real personal names. These
# never suppress alone; org_verdict requires a second, independent signal
# (another weak term, a strong term, a spaCy ORGANIZATION span over the
# same characters, or an "of"/"&" construction).
INSTITUTIONAL_WEAK_TERMS = {
    "church", "chapel", "abbey", "temple", "mission", "parish", "shrine",
    "memorial", "regional", "national", "international", "general",
    "community", "county", "city", "state", "valley", "park", "ridge",
    "hill", "lake", "river", "creek", "grove", "field", "house", "home",
    "manor", "lodge", "hall", "court", "place", "center", "centre",
    "trust", "fund", "bank", "union", "board", "group", "first", "saint",
    "st", "health", "medical", "care", "family", "mercy", "grace",
    "providence", "presbyterian", "methodist", "baptist", "lutheran",
    "catholic", "episcopal", "jewish", "christian", "veterans",
    "children's", "women's", "men's", "senior", "youth",
}

# "Foo of Bar", "Foo & Bar", "Foo and Sons" -- constructions that read as
# an organisation name and corroborate a weak term.
ORG_CONSTRUCTION_RE = re.compile(
    r"\b(?:of|for|&|and\s+sons|and\s+daughters|und)\b", re.IGNORECASE)

# Presidio/spaCy entity labels that, when they cover the same characters
# as a PERSON hit, are strong evidence the PERSON label is wrong. Scanned
# alongside PERSON in the same pipeline pass, so they cost nothing extra;
# filtered against the analyzer's actual supported set at runtime, since
# label names differ across presidio-analyzer releases.
ORG_SUPPRESSOR_ENTITIES = ("ORGANIZATION", "ORG", "NRP", "LOCATION", "GPE")


# ---------------------------------------------------------------------
# Suppression
#
# DESIGN RULE: suppression is a verdict, not a deletion. Every hit is
# still scored, still cached, and still carries its page locations; a
# suppressed hit is written to <out>.suppressed.csv with the reason that
# fired, and --include-suppressed folds it back into the main report.
# Nothing is ever silently discarded, because every rule below trades
# precision for recall and all of them can eat a real person: Ng has no
# vowel, Council is an attested surname, Bell and Turner and Down and
# Graves are eponyms AND surnames, and George Best signed letters.
#
# The rules are correspondingly timid. Where a single signal could be a
# coincidence, two are required (see garbage_verdict and org_verdict).
# ---------------------------------------------------------------------
SUPPRESS_NONE = ""
SUPPRESS_GARBAGE = "ocr_garbage"
SUPPRESS_ORG = "organization"
SUPPRESS_MEDICAL = "medical_term"
SUPPRESS_SALUTATION = "salutation_only"
SUPPRESS_DENYLIST = "denylist"

SUPPRESS_REASONS = (
    SUPPRESS_GARBAGE, SUPPRESS_ORG, SUPPRESS_MEDICAL,
    SUPPRESS_SALUTATION, SUPPRESS_DENYLIST,
)

# ---------------------------------------------------------------------
# OCR garbage
#
# Characters a personal name may legitimately contain, beyond letters and
# combining marks. Everything else is layout debris, ligature failure, or
# mojibake. Note that this is a permit list of PUNCTUATION only -- every
# alphabetic character in every script is allowed, so non-Latin names are
# untouched.
# ---------------------------------------------------------------------
NAME_PUNCT_OK = set(" '’‘`-‐‑–.,")

# Unicode general categories that have no business inside a name. Sc is
# currency (the € in "Eurosymbol:///lj{]"), So/Sk are standalone symbols,
# Co is private-use (a classic OCR/font-subset artefact), Cf is invisible
# formatting, Cn is unassigned.
GARBAGE_UNICODE_CATEGORIES = {"Sc", "So", "Sk", "Co", "Cf", "Cn", "Cs"}

# ASCII that Unicode calls punctuation but that never appears in a name.
GARBAGE_ASCII = set("{}[]<>|\\/^~*#=+@_$%\"()!?;:")

GARBAGE_NONLETTER_RATIO = 0.34   # share of non-letter chars that looks broken
GARBAGE_CONSONANT_RUN = 6        # "lj{]" survives trimming as "lj"; "Schwartz" is 4
GARBAGE_REPEAT_RUN = 3           # "aaa" -- no name, "Aaliyah" has 2
GARBAGE_VOWELLESS_MIN_LEN = 5    # Ng, Ngo, Krk, Brzy all shorter than this
GARBAGE_MAX_TOKEN_LEN = 30       # concatenated-column artefacts

_VOWELS = set("aeiouyáàâäãåæéèêëíìîïóòôöõøœúùûüýÿ")

# ---------------------------------------------------------------------
# Salutations and valedictions
#
# These are TRIMMED from the front of a span, not used to suppress the
# span. The recipient of a letter is a real person whose name belongs in
# a PII audit -- "Hello Sarah" should report Sarah, not nothing. Only a
# span consisting of NOTHING BUT boilerplate is suppressed, and then only
# from the unambiguous subset below.
# ---------------------------------------------------------------------
SALUTATION_TOKENS = {
    "hello", "hi", "hey", "dear", "greetings", "salutations",
    "good morning", "good afternoon", "good evening", "good day",
    "to whom it may concern", "attention", "attn", "re", "subject",
}

VALEDICTION_TOKENS = {
    "sincerely", "regards", "best regards", "kind regards",
    "warm regards", "best wishes", "best", "warmly", "cordially",
    "respectfully", "respectfully submitted", "yours", "yours truly",
    "yours sincerely", "yours faithfully", "truly", "faithfully",
    "cheers", "thanks", "thank you", "many thanks", "gratefully",
    "signed", "sent from", "sent via", "typed by", "dictated by",
}

# Honorifics stripped from the front of a span so "Dr. Sarah Kowalski"
# and "Sarah Kowalski" collapse to one row.
HONORIFIC_TOKENS = {
    "mr", "mrs", "ms", "mx", "miss", "dr", "prof", "professor", "rev",
    "reverend", "fr", "sr", "hon", "honorable", "honourable", "sir",
    "dame", "madam", "madame", "lord", "lady", "capt", "captain", "sgt",
    "sergeant", "lt", "lieutenant", "col", "colonel", "gen", "general",
    "officer", "judge", "justice", "chief", "deputy", "coach", "nurse",
    "pastor", "rabbi", "imam", "sister", "brother",
}

# The only tokens whose bare, unaccompanied appearance justifies dropping
# a hit outright. Deliberately excludes "best", "thanks", "truly",
# "warmly", "grace", "hope" and every other valediction that is also a
# name -- George Best exists, and a report that loses him to a wordlist
# is worse than a report with one junk row in it.
SALUTATION_ONLY_SAFE = {
    "hello", "hi", "hey", "dear", "greetings", "salutations",
    "sincerely", "regards", "cordially", "respectfully", "faithfully",
    "good morning", "good afternoon", "good evening",
    "to whom it may concern", "attn", "re", "subject",
}

# ---------------------------------------------------------------------
# Medical / eponymous terms
#
# Eponymous conditions are LITERALLY named after people, so spaCy tagging
# "Klinefelter" as PERSON is not a model failure -- it is a correct
# reading of a string that happens not to denote a living data subject.
#
# The rule keys on the HEAD NOUN, not on a list of conditions: any
# capitalised token followed by "syndrome", "disease", "manoeuvre",
# "forceps" and so on is an eponym regardless of whether anyone has
# catalogued it. One rule, ~90 nouns, covers the entire eponym space
# instead of a perpetually incomplete list of 500 diagnoses.
# ---------------------------------------------------------------------
MEDICAL_HEAD_NOUNS = {
    # Conditions
    "syndrome", "syndromes", "disease", "diseases", "disorder",
    "disorders", "anomaly", "malformation", "deformity", "dystrophy",
    "atrophy", "palsy", "ataxia", "anemia", "anaemia", "encephalopathy",
    "neuropathy", "myopathy", "nephropathy", "sarcoma", "lymphoma",
    "carcinoma", "tumor", "tumour", "cyst", "fistula", "hernia",
    "fracture", "lesion", "plexus", "phenomenon", "triad", "tetralogy",
    # Findings and manoeuvres
    "sign", "signs", "test", "tests", "reflex", "reflexes", "maneuver",
    "manoeuvre", "manouver", "procedure", "operation", "incision",
    "approach", "technique", "method", "repair", "graft", "flap",
    "block", "stain", "staining", "assay", "titre", "titer",
    # Scores and instruments
    "index", "indices", "scale", "score", "scores", "classification",
    "criteria", "criterion", "staging", "stage", "grade", "grading",
    "questionnaire", "inventory", "rating", "chart", "nomogram",
    # Devices and anatomy
    "catheter", "forceps", "clamp", "retractor", "speculum", "tube",
    "drain", "shunt", "stent", "splint", "cast", "position",
    "ligament", "gland", "glands", "canal", "node", "nodes", "duct",
    "space", "triangle", "fossa", "cells", "cell", "body", "bodies",
    "fibers", "fibres", "corpuscle", "capsule", "membrane", "layer",
    "loop", "tract", "nucleus", "area", "zone", "line", "angle",
    # Science generally -- eponyms are not confined to medicine
    "law", "laws", "curve", "formula", "equation", "constant", "unit",
    "units", "factor", "coefficient", "distribution", "transform",
    "theorem", "principle", "effect", "cycle", "reaction", "process",
    "scale's", "virus", "bacillus", "bacterium", "diagram",
    # Inflammation family (-itis) -- "Hashimoto's thyroiditis" was missed
    # entirely by the original list, which had no member of this family
    # at all despite it being one of the most common eponym-adjacent
    # constructions in a chart.
    "arthritis", "dermatitis", "colitis", "gastritis", "hepatitis",
    "meningitis", "appendicitis", "bronchitis", "sinusitis",
    "tonsillitis", "pancreatitis", "thyroiditis", "nephritis",
    "cystitis", "conjunctivitis", "laryngitis", "pharyngitis", "otitis",
    "vasculitis", "myelitis", "encephalitis", "myocarditis",
    "pericarditis", "peritonitis",
    # Degeneration/condition family (-osis)
    "dermatosis", "keratosis", "fibrosis", "sclerosis", "stenosis",
    "necrosis", "thrombosis", "cirrhosis", "psychosis", "neurosis",
    "scoliosis", "kyphosis", "osteoporosis",
    # Imaging and ECG eponyms -- "Waters' view", "Towne projection",
    # "Osborn wave" name the finding/technique the same way "Apgar
    # Score" does, just in radiology and cardiology rather than exam
    # findings.
    "view", "projection", "wave", "complex", "interval", "segment",
}

# Bare eponyms with no head noun attached suppress ONLY when they carry a
# possessive ("Down's", "Crohn's") -- the possessive is what marks the
# eponymic use. A bare "Turner" or "Bell" or "Graves" or "Parkinson" is
# left alone, because each is a common surname and the corpus is full of
# real people who answer to them.
MEDICAL_EPONYMS = {
    "klinefelter", "kleinfelter", "kleinfeldter", "klinefelters",
    "down", "asperger", "aspergers", "crohn", "crohns", "parkinson",
    "parkinsons", "alzheimer", "alzheimers", "hodgkin", "hodgkins",
    "marfan", "marfans", "ehlers", "danlos", "guillain", "barre",
    "barré", "munchausen", "tourette", "tourettes", "bell", "graves",
    "addison", "addisons", "cushing", "cushings", "wilson", "wilsons",
    "paget", "pagets", "raynaud", "raynauds", "sjogren", "sjögren",
    "angelman", "prader", "willi", "turner", "turners", "apgar",
    "glasgow", "braden", "morse", "kaposi", "burkitt", "wernicke",
    "korsakoff", "creutzfeldt", "jakob", "huntington", "huntingtons",
    "lou gehrig", "charcot", "duchenne", "becker", "rett", "reye",
    "reyes", "kawasaki", "takotsubo", "hashimoto", "hashimotos",
    "bartholin", "meckel", "murphy", "homan", "homans", "babinski",
    "brudzinski", "kernig", "romberg", "trendelenburg", "heimlich",
    "epley", "valsalva", "mcburney", "cheyne", "stokes", "korotkoff",
    "foley", "yankauer", "penrose", "jackson", "pratt", "mallory",
    "weiss", "boerhaave", "zollinger", "ellison", "cushing's",
}

# How far past the end of a span to look for a head noun. Long enough for
# a possessive and a wrapped line break, short enough that the next
# sentence cannot reach back.
# How far past the end of a span to SEARCH for a head noun. Widened from
# 24 to 72 after "Asperger's or other developmental eponymous syndrome"
# went unsuppressed in a real scan: the old value assumed the head noun
# sits immediately next to the eponym ("Down's syndrome"), but real
# chart prose routinely puts several words in between. See
# _head_noun_within -- this is now a bounded SEARCH, not a fixed-offset
# match, so widening it costs a few more tokens of scanning per
# candidate, not a change in what counts as "immediately adjacent".
MEDICAL_LOOKAHEAD = 72

# Terms that ASSERT the nearby name belongs to a child. Every term in the
# old set that merely established a child was somewhere in the document
# has been removed, because that is a property of the page, not of the
# name: "mother", "father", "parent", "parents", "stepparent", "birth
# parent", "daughter", "son", "sister", "brother", "sibling", "dob",
# "date of birth", "born on", "grade", "classroom", "school nurse",
# "school district", "teacher", "principal", "household size",
# "free/reduced", "reduced lunch", "guardian 1", "guardian 2", "therapy",
# "counseling", "treatment plan", "ward", "dependent", "custody",
# "adoption", "guardianship", "paternity", "parental rights", "middle
# school", "high school", "elementary", "immunization", "vaccination".
#
# Between them those accounted for essentially the entire false-positive
# mass on the reference corpus while adding one true positive. The role
# labels among them are NOT lost -- they still bind on the adult side via
# ADULT_ROLE_LABELS, where they belong.
MINOR_CONTEXT_TERMS = {
    # Direct assertions of minority
    "minor", "minor child", "minor children", "minor's", "juvenile",
    "underage", "under the age", "unaccompanied minor", "emancipated",
    "emancipation", "foster child", "ward of the state",
    # Life stage -- these describe the person, not the document
    "newborn", "infant", "toddler", "baby", "teen", "teenage", "teenager",
    "adolescent", "pediatric", "pediatrics", "pediatrician",
    # Enrolment terms that attach to a pupil rather than to a school
    "student", "pupil", "enrollee", "kindergarten", "preschool", "pre-k",
    "daycare", "day care", "nursery school", "freshman", "sophomore",
    # Instruments that exist only for a child
    "iep", "504 plan", "individualized education", "guardian ad litem",
    "next friend", "child protective", "truancy", "report card",
    "permission slip",
    # Relational cues -- these bind narratively; see RELATION_TERMS
    "classmate", "schoolmate",
}

# The subset of MINOR_CONTEXT_TERMS that binds only by relation to some
# other child, never by direct assertion. "a classmate, Kaylee Hutchens"
# tells us Kaylee is a peer of a child; it does not state her age. Worth
# flagging, worth ranking below a direct assertion.
RELATION_TERMS = {"classmate", "schoolmate"}

# Signals strong enough to carry a flag on their own when they land
# inside the narrow window. Used for ranking only.
MINOR_STRONG_SIGNALS = {
    "minor", "minor child", "minor children", "juvenile", "foster child",
    "iep", "504 plan", "individualized education", "guardian ad litem",
    "next friend", "pediatric", "pediatrician", "pediatrics", "kindergarten",
    "preschool", "daycare", "newborn", "infant", "toddler", "adolescent",
    "teenager", "unaccompanied minor", "child protective", "truancy",
    "dob-implies-minor", "student", "pupil",
}

# ---------------------------------------------------------------------
# Table column roles. pdfplumber hands us the header row; these decide
# whether a column holds children, adults, or neither. A header matching
# both (e.g. "Student's Parent") is treated as adult, because the safe
# reading of an ambiguous header is that it does NOT license a flag --
# column binding is meant to be the high-precision path.
# ---------------------------------------------------------------------
COL_NONE = ""
COL_MINOR = "minor"
COL_ADULT = "adult"

MINOR_COLUMN_RE = re.compile(
    r"\b(?:student|students|pupil|pupils|child|children|minor|minors|"
    r"juvenile|youth|enrollee|dependent|dependents)\b", re.IGNORECASE)

ADULT_COLUMN_RE = re.compile(
    r"\b(?:parent|parents|guardian|guardians|mother|father|employee|"
    r"employees|staff|instructor|teacher|counsel|counselor|counsellor|"
    r"custodian|prescriber|prescribed|dispensed|witness|witnessed|"
    r"verified|surgeon|attending|physician|provider|clinician|nurse|"
    r"practitioner|holder|account\s+holder|subscriber|member|payer|payee|"
    r"administered|administrator|role|title|supervisor|manager|officer|"
    r"attorney|next\s+of\s+kin|emergency\s+contact|contact|caregiver|"
    r"reported\s+by|prepared\s+by|approved\s+by|signed\s+by)\b",
    re.IGNORECASE)

# Columns that hold a person's name at all -- minor or adult. Used to
# decide which cells are worth re-analysing in flipped ("Surname, Given"
# -> "Given Surname") order; see extract_page_units.
NAME_COLUMN_RE = re.compile(
    r"\b(?:name|names|last|first|middle|surname|forename|given|"
    r"family\s+name|last\s+name|first\s+name|full\s+name|legal\s+name|"
    r"student|pupil|child|children|minor|employee|patient|resident|"
    r"client|beneficiary|enrollee|applicant|claimant|recipient|"
    r"custodian|parent|guardian|member|subscriber|holder|prescriber|"
    r"witness|instructor|teacher|attending|surgeon|provider|clinician|"
    r"nurse|signatory|signer|contact|individual|person|"
    r"dispensed\s+by|administered\s+by|verified\s+by|reported\s+by)\b",
    re.IGNORECASE)

# A column with no usable header still gives itself away by shape: if
# this many of its cells parse as "Surname, Given", it is a name column
# whatever the header says (or fails to say). Headerless rosters, tables
# whose first row is a spanning title, and columns headed "Last, First"
# all fell through the header-only gate.
NAME_COLUMN_SHAPE_MIN_CELLS = 2

# Minor-binding kinds, strongest first. Reported as the minor_binding
# column so a flag can be audited without reopening the PDF.
BIND_NONE = ""
BIND_COLUMN = "column"
BIND_LABEL = "label"
BIND_DOB = "dob"
BIND_RELATION = "relation"
BIND_ORDER = {
    BIND_NONE: 0, BIND_RELATION: 1, BIND_DOB: 2,
    BIND_LABEL: 3, BIND_COLUMN: 4,
}
# BIND_TIER is defined below, next to the MINOR_TIER_* constants it maps
# onto -- those are declared further down the file.

# ---------------------------------------------------------------------
# Role binding. Proximity alone cannot tell the child from the adults
# standing next to the child -- the mother, the case manager, and the
# guardian ad litem all share the same 150 characters. These labels say
# which role the ADJACENT name occupies, which is what separates them.
#
# Note that "parent", "mother", "guardian", and "spouse" appear on the
# ADULT side: as a label immediately before a name they identify an adult,
# even though as ambient vocabulary they indicate a child is present.
# ---------------------------------------------------------------------
ADULT_ROLE_LABELS = {
    "attending", "therapist", "clinician", "counselor", "physician",
    "provider", "nurse", "case manager", "social worker", "assigned worker",
    "worker is", "reporting officer", "officer", "sgt", "sergeant", "coach",
    "athletic trainer", "trainer", "teacher", "classroom teacher",
    "instructor", "professor", "principal", "superintendent", "dean",
    "advisor", "counsel for", "counsel", "attorney", "notary",
    "guardian ad litem", "gal is", "proposed gal",
    "petitioner", "respondent", "plaintiff", "defendant", "deponent",
    "executor", "trustee", "beneficiary", "decedent", "estate of",
    "employee", "spouse", "parent", "parents", "mother", "father",
    "guardian", "emergency contact", "caregiver", "next of kin",
    "prepared by", "approved by", "billing contact", "records custodian",
    "attn", "bill to", "referring provider", "referring practice",
    "accompanied by", "signed", "sincerely", "dr", "mr", "mrs", "ms", "prof",
}

# Only labels that identify the adjacent name as a CHILD. "Patient",
# "Member", "Subject" and "Re" are age-neutral -- on this corpus they put
# every adult patient and every background-check subject into the high
# tier. Relational nouns ("son", "daughter", "sibling") are age-neutral
# too: "I reached the daughter, Doris Jean Pettigrew" is a 60-year-old.
# They remain in MINOR_CONTEXT_TERMS, so they still raise possible_minor;
# they simply no longer promote a name to high on their own.
MINOR_ROLE_LABELS = {
    "student", "participant", "minor", "minor child", "minor children",
    "child", "children", "juvenile", "youth", "foster child",
    "name of child", "pupil", "enrollee",
}

ADULT_CREDENTIALS = {
    "md", "do", "rn", "lpn", "np", "pa-c", "lcsw", "lmft", "lpc", "phd",
    "psyd", "esq", "cpa", "jd", "edd", "mba", "dds", "faap",
}

# How far either side of a name a role label still counts as binding to it.
BINDING_WINDOW_BEFORE = 45
BINDING_WINDOW_AFTER = 60

MINOR_TIER_NONE = ""
MINOR_TIER_LOW = "low"
MINOR_TIER_MEDIUM = "medium"
MINOR_TIER_HIGH = "high"
MINOR_TIER_ORDER = {
    MINOR_TIER_NONE: 0, MINOR_TIER_LOW: 1,
    MINOR_TIER_MEDIUM: 2, MINOR_TIER_HIGH: 3,
}

# Which review tier each binding lands in. MINOR_TIER_LOW is retained in
# the ordering above so shards written by v3 still sort correctly, but
# nothing emits it any more: it was the bucket for "an adult standing
# near child-related words", which is no longer a flag at all.
BIND_TIER = {
    BIND_COLUMN: MINOR_TIER_HIGH,
    BIND_LABEL: MINOR_TIER_HIGH,
    BIND_DOB: MINOR_TIER_HIGH,
    BIND_RELATION: MINOR_TIER_MEDIUM,
    BIND_NONE: MINOR_TIER_NONE,
}

# Shape penalties -- see SCORING NOTE in the module docstring. Without
# these, the flat 0.85 spaCy score puts nearly everything in one tier.
SINGLE_TOKEN_PENALTY = 0.15   # "Reyes" alone is more ambiguous than "Jon Reyes", useful for product names
CASE_ANOMALY_PENALTY = 0.10   # ALL CAPS / all lowercase often = headers, OCR noise
DIGIT_PENALTY = 0.30          # digits inside a "name" are almost always misfires

# Pages fed to spaCy's pipe() per batch inside a chunk. Distinct from
# --chunk-size, which is the checkpoint/commit granularity.
DEFAULT_BATCH_SIZE = 16

# Matched on word boundaries, not as bare substrings -- otherwise "cc"
# fires inside "account" and "ms" inside "Williams"
CONTEXT_BOOST_RE = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, CONTEXT_BOOST_WORDS))) + r")\b",
    re.IGNORECASE,
)

def _phrase_pattern(terms) -> str:
    """Allow any whitespace between the words of a multi-word term.
    pdfplumber routinely wraps "date of birth" and "middle school" across
    lines; the previous single-space literals missed every such case
    silently."""
    parts = [r"\s+".join(re.escape(w) for w in term.split())
             for term in sorted(terms)]
    return r"\b(?:" + "|".join(parts) + r")\b"


MINOR_CONTEXT_RE = re.compile(_phrase_pattern(MINOR_CONTEXT_TERMS), re.IGNORECASE)
RELATION_RE = re.compile(_phrase_pattern(RELATION_TERMS), re.IGNORECASE)

# Case-SENSITIVE by design: lowercase "gal" and "cps" in running prose are
# noise, the uppercase acronyms are not.
MINOR_ACRONYM_RE = re.compile(r"\b(?:GAL|CPS|DCFS|DCF|FERPA|COPPA|CASA)\b")

# Ages 0-17 only, so "63 years old" in a discharge summary cannot fire.
MINOR_AGE_RE = re.compile(
    r"\b(?:age[d]?\s*:?\s*)?(?<!\d)(?:[0-9]|1[0-7])(?!\d)"
    r"\s*(?:-|\s)?\s*(?:years?|yrs?|y\.?\s?o\.?|yo)\b(?:\s*old)?"
    r"|\bage[d]?\s*:?\s*(?<!\d)(?:[0-9]|1[0-7])(?!\d)\b",
    re.IGNORECASE,
)
ADULT_AGE_RE = re.compile(
    r"\bage[d]?\s*:?\s*(?<!\d)(?:1[89]|[2-9]\d)(?!\d)\b"
    r"|\b(?<!\d)(?:1[89]|[2-9]\d)(?!\d)\s*years?\s*old\b",
    re.IGNORECASE,
)

_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b|\b(19|20)(\d{2})\b")
# "11 February 1992", "September 22, 2011" -- the cue sits before the
# month name, too far back for BIRTH_CUE_LOOKBACK to see from the year.
_MONTHS = ("january|february|march|april|may|june|july|august|september"
           "|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept"
           "|oct|nov|dec")
_WRITTEN_DATE_RE = re.compile(
    r"(?:\d{1,2}\s+)?(?:" + _MONTHS + r")\.?\s+(?:\d{1,2},?\s+)?((?:19|20)\d{2})",
    re.IGNORECASE,
)

# A role label sitting immediately before a name, with optional ":" / "-"
# / "," between. Anchored to the end of the preceding text so only the
# label actually adjacent to the name binds to it.
def _label_pattern(labels) -> re.Pattern:
    alt = "|".join(
        r"\s+".join(re.escape(w) for w in label.split())
        for label in sorted(labels, key=len, reverse=True)
    )
    return re.compile(
        r"\b(?:" + alt + r")\b\s*\(?\s*\d{0,2}\s*\)?\s*[:\-,]?\s*$",
        re.IGNORECASE)


ADULT_LABEL_BEFORE_RE = _label_pattern(ADULT_ROLE_LABELS)
MINOR_LABEL_BEFORE_RE = _label_pattern(MINOR_ROLE_LABELS)
ADULT_CREDENTIAL_AFTER_RE = re.compile(
    r"^\s*[,.]?\s*(?:" + "|".join(re.escape(c) for c in sorted(ADULT_CREDENTIALS))
    + r")\b",
    re.IGNORECASE,
)
# "Persephone Nakagawa-Ubaldo, who is 12 years old", "T.M.B., a minor",
# "Ignacia Vondracek-Mbeki, sophomore, age 15".
MINOR_APPOSITIVE_RE = re.compile(
    r"^\s*[,(]?\s*(?:who\s+is\s+)?(?:a\s+)?"
    r"(?:minor|juvenile"
    r"|age[d]?\s*:?\s*(?:[0-9]|1[0-7])\b"
    r"|(?:[0-9]|1[0-7])\s*(?:years?|yrs?|y/?o)\b"
    r"|sophomore|freshman|junior|senior)",
    re.IGNORECASE,
)
# "the minor children, Tobias Banderas-Ng and Clementine Banderas-Ng" --
# the second conjunct inherits the first's binding.
COORDINATION_RE = re.compile(r"^\s*(?:,|and|&|;)\s*$")


# ---------------------------------------------------------------------
# Cache schema -- one SQLite shard per PDF, so parallel workers never
# contend for a write lock and resume state is tracked per file.
# ---------------------------------------------------------------------
SHARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_info (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS chunks_done (
    start_page INTEGER NOT NULL,
    end_page   INTEGER NOT NULL,
    PRIMARY KEY (start_page, end_page)
);
CREATE TABLE IF NOT EXISTS hits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_num   INTEGER NOT NULL,
    raw_name   TEXT NOT NULL,
    score      REAL NOT NULL,
    boosted    REAL NOT NULL,
    entity     TEXT NOT NULL,
    minor_ctx  INTEGER NOT NULL DEFAULT 0,
    minor_tier TEXT NOT NULL DEFAULT '',
    minor_reason TEXT NOT NULL DEFAULT '',
    recognizer TEXT NOT NULL DEFAULT 'spacy',
    minor_binding TEXT NOT NULL DEFAULT '',
    -- v5. suppressed is a verdict carried alongside the hit, never a
    -- reason to skip the INSERT: the report phase decides what to show,
    -- so rules can be retuned (or reversed wholesale with
    -- --include-suppressed) without paying for a rescan, and an auditor
    -- can always answer "what did you throw away, and why".
    suppressed INTEGER NOT NULL DEFAULT 0,
    suppress_reason TEXT NOT NULL DEFAULT '',
    -- The span exactly as detected, before sanitising. raw_name holds the
    -- cleaned form; this holds what the PDF actually said, so a trim can
    -- be audited or second-guessed.
    raw_surface TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS page_problems (
    page_num INTEGER PRIMARY KEY,
    reason   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hits_name ON hits(raw_name);
"""


def shard_path(cache_dir: Path, pdf_name: str) -> Path:
    # The sanitizer maps distinct names to the same string ("a b.pdf" and
    # "a_b.pdf" both become "a_b.pdf"), so a short hash of the original
    # name disambiguates. NOTE: this changes shard filenames -- caches
    # built before this change will be ignored and need a rescan.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", pdf_name)
    digest = hashlib.sha1(pdf_name.encode("utf-8")).hexdigest()[:8]
    return cache_dir / f"{safe}-{digest}.db"


def open_shard(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SHARD_SCHEMA)
    # Migration: shards created before the possible_minor feature lack the
    # column (CREATE TABLE IF NOT EXISTS won't add it to an existing table).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(hits)")}
    if "minor_ctx" not in cols:
        conn.execute(
            "ALTER TABLE hits ADD COLUMN minor_ctx INTEGER NOT NULL DEFAULT 0"
        )
    if "recognizer" not in cols:
        conn.execute(
            "ALTER TABLE hits ADD COLUMN recognizer TEXT NOT NULL DEFAULT 'spacy'"
        )
    # Shards predating the minor-tier work carry possible_minor but no
    # binding verdict. They migrate cleanly, but their tiers stay empty
    # until rescanned -- the tier depends on page text the cache does not
    # retain.
    # minor_binding arrived with the column-binding rewrite (extractor v4).
    # Shards from v3 migrate structurally but their bindings stay empty --
    # the verdict depends on table geometry the cache does not retain, so
    # the version guard in scan_one_pdf will require a --force rescan
    # before those rows are trusted.
    for col, ddl in (
        ("minor_tier", "ALTER TABLE hits ADD COLUMN minor_tier TEXT NOT NULL DEFAULT ''"),
        ("minor_reason", "ALTER TABLE hits ADD COLUMN minor_reason TEXT NOT NULL DEFAULT ''"),
        ("minor_binding", "ALTER TABLE hits ADD COLUMN minor_binding TEXT NOT NULL DEFAULT ''"),
        # v5 suppression columns. A migrated v4 shard reads as
        # "nothing suppressed", which is the inclusive default and the
        # correct one -- those rows were never evaluated against the new
        # rules, so claiming a verdict for them would be a lie. The
        # extractor-version guard requires --force before they are
        # trusted anyway.
        ("suppressed", "ALTER TABLE hits ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0"),
        ("suppress_reason", "ALTER TABLE hits ADD COLUMN suppress_reason TEXT NOT NULL DEFAULT ''"),
        ("raw_surface", "ALTER TABLE hits ADD COLUMN raw_surface TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in cols:
            conn.execute(ddl)
    conn.commit()
    return conn


def set_file_info(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO file_info (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_file_info(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM file_info WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


# ---------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------
def normalize_name(raw: str) -> str:
    """Collapse whitespace and strip edge punctuation so repeats merge."""
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return cleaned.strip(".,;:()[]\"'")


def dedup_key(raw: str) -> str:
    return normalize_name(raw).lower()


def _context_window(page_text: str, start: int, end: int,
                    window: int = CONTEXT_WINDOW) -> str:
    lo = max(0, start - window)
    hi = min(len(page_text), end + window)
    return page_text[lo:hi]


def context_boost(page_text: str, start: int, end: int) -> float:
    window = _context_window(page_text, start, end)
    return CONTEXT_BOOST_AMOUNT if CONTEXT_BOOST_RE.search(window) else 0.0


def normalize_context(text: str) -> str:
    """Undo hyphenated line breaks ("elemen-\\ntary") and collapse
    whitespace, so wrapped PDF text matches the same terms flat text does."""
    return re.sub(r"\s+", " ", re.sub(r"-\s*\n\s*", "", text))


BIRTH_CUE_RE = re.compile(
    r"(?:d\.?o\.?b\.?|date\s+of\s+birth|birth\s*date|birthdate|born(?:\s+on|\s+in)?"
    r"|dob)\s*[:\-]?\s*$",
    re.IGNORECASE,
)
BIRTH_CUE_LOOKBACK = 24


def _birth_years(window: str):
    """
    Years from dates that are actually LABELLED as birth dates.

    Without the cue requirement this fired on any date within 18 years of
    today, which in a production dated 2026 means essentially every
    collection date, claim date, admission date and log timestamp in the
    corpus. Measured on the test corpus: 75% of all dates were being read
    as the birth date of a minor.
    """
    years = []
    for m in _WRITTEN_DATE_RE.finditer(window):
        lead = window[max(0, m.start() - BIRTH_CUE_LOOKBACK):m.start()]
        if BIRTH_CUE_RE.search(lead):
            years.append(int(m.group(1)))
    for m in _DATE_RE.finditer(window):
        lead = window[max(0, m.start() - BIRTH_CUE_LOOKBACK):m.start()]
        if not BIRTH_CUE_RE.search(lead):
            continue
        if m.group(3):
            year = int(m.group(3))
            if year < 100:
                year += 2000 if year <= datetime.now(timezone.utc).year % 100 else 1900
        else:
            year = int(m.group(4) + m.group(5))
        years.append(year)
    return years


def dob_implies_minor(window: str, today=None) -> bool:
    """A labelled birth date that would make the person under 18 today."""
    today = today or datetime.now(timezone.utc).date()
    return any(0 <= today.year - y <= 18 for y in _birth_years(window))


def _dob_implies_adult(window: str, today=None) -> bool:
    today = today or datetime.now(timezone.utc).date()
    return any(today.year - y > 18 for y in _birth_years(window))


def minor_reasons(page_text: str, start: int, end: int) -> list:
    """
    Minor-related signals in the NARROW window around this detection; an
    empty list means no narrative signal. Surfaced as the minor_reason
    CSV column so a flag is cheap for a human to triage rather than
    opaque.

    Two things this deliberately no longer does. It no longer sweeps a
    700-character "section" for high-specificity terms: one "Elementary"
    in a school's postal address was enough to attach page:elementary to
    every name around it, including the 504 coordinator and the school
    itself. And it no longer treats relational or documentary vocabulary
    ("mother", "dob", "school nurse") as a signal, because those describe
    the document rather than the name -- see MINOR_CONTEXT_TERMS.

    Note that reasons alone never raise a flag now. They feed
    minor_verdict(), which requires an actual binding.
    """
    raw = _context_window(page_text, start, end, MINOR_CONTEXT_WINDOW)
    window = normalize_context(raw)
    hits = {m.group(0).lower() for m in MINOR_CONTEXT_RE.finditer(window)}
    hits |= {m.group(0) for m in MINOR_ACRONYM_RE.finditer(raw)}
    hits |= {"age:" + m.group(0).strip().lower()
             for m in MINOR_AGE_RE.finditer(window)}
    if dob_implies_minor(window):
        hits.add("dob-implies-minor")
    return sorted(hits)


def minor_context(page_text: str, start: int, end: int) -> bool:
    """Backwards-compatible boolean form of minor_reasons()."""
    return bool(minor_reasons(page_text, start, end))


def column_role(header_cell: str) -> str:
    """
    Whether a table column holds children, adults, or neither, from its
    header text. Ambiguity resolves to adult: a header naming both roles
    ("Student's Parent", "Guardian of Minor") must not license a column
    binding, since that binding is the high-precision path.
    """
    if not header_cell:
        return COL_NONE
    text = normalize_context(header_cell)
    if ADULT_COLUMN_RE.search(text):
        return COL_ADULT
    if MINOR_COLUMN_RE.search(text):
        return COL_MINOR
    return COL_NONE


def role_binding(page_text: str, start: int, end: int):
    """
    (adult_bound, minor_bound) for the name at [start:end], from the role
    label or credential immediately adjacent to it. This is what tells the
    child from the adults around the child: "Student: Rosalind Petrosyan"
    versus "Case manager: Denholm Okabe", both in the same 150 characters.
    """
    before = normalize_context(page_text[max(0, start - BINDING_WINDOW_BEFORE):start])
    after = normalize_context(page_text[end:end + BINDING_WINDOW_AFTER])

    adult = bool(ADULT_LABEL_BEFORE_RE.search(before)
                 or ADULT_CREDENTIAL_AFTER_RE.search(after)
                 or ADULT_AGE_RE.search(after)
                 or _dob_implies_adult(after))
    minor = bool(MINOR_LABEL_BEFORE_RE.search(before)
                 or MINOR_APPOSITIVE_RE.search(after)
                 or dob_implies_minor(after))
    return adult, minor


def _inherits_minor_binding(page_text: str, name_spans, start: int) -> bool:
    """True when this name is joined by "and"/"," to a preceding name that
    is itself minor-bound -- "the minor children, Tobias and Clementine",
    ubiquitous in captions and dependent lists."""
    for s, e in name_spans:
        if e < start and COORDINATION_RE.match(normalize_context(page_text[e:start])):
            adult, minor = role_binding(page_text, s, e)
            if minor and not adult:
                return True
    return False


def _closest_to_a_signal(page_text: str, name_spans, start: int, end: int) -> bool:
    """True when this name is the nearest detected name to some minor
    signal. Assigns each signal to one name instead of to everything in
    range, which is where most of the over-flagging came from."""
    midpoint = (start + end) // 2
    signals = [m.start() for m in MINOR_CONTEXT_RE.finditer(page_text)]
    signals += [m.start() for m in MINOR_AGE_RE.finditer(page_text)]
    for sig in signals:
        if abs(sig - midpoint) > MINOR_CONTEXT_WINDOW:
            continue
        nearest = min(name_spans, key=lambda sp: abs((sp[0] + sp[1]) // 2 - sig))
        if nearest[0] == start and nearest[1] == end:
            return True
    return False


def minor_verdict(page_text: str, name_spans, start: int, end: int,
                  reasons, col_role: str = COL_NONE) -> str:
    """
    Why -- if at all -- this name is believed to belong to a child.
    Returns one of the BIND_* constants; BIND_NONE means no flag.

    The ordering below is the whole point of the rewrite. Structure beats
    proximity, and an adult role beats ambient vocabulary:

      1. COLUMN. The cell sat in a child-labelled table column. This is
         the only signal that survives table linearisation intact, and it
         is exact -- the header states which column holds children, so
         the parents in the next column are separated with no ambiguity.
         A cell in an adult-labelled column vetoes everything else for
         that occurrence.
      2. LABEL. A minor role label immediately before the name, a minor
         appositive immediately after, or coordination with a name that
         is itself label-bound.
      3. DOB. A labelled date of birth in the narrow window implying the
         person is currently under 18, or an explicit age of 0-17.
      4. RELATION. A relational cue ("a classmate, ...") binding the name
         to some other child without asserting its own age.

    Adult binding SUPPRESSES rather than demotes. Previously a name bound
    to an adult role still carried possible_minor at minor_tier=low; that
    bucket was 136 of 158 flags on the reference corpus and 3.7% of it
    was minors. A case manager is not a lead worth a reviewer's minute.

    (The former nearest-name promotion path sat after an unconditional
    return and had therefore never executed. Rather than resurrect it,
    column binding now does the job it was written for -- assigning a
    signal to one name instead of to every name in range -- and does it
    from the document's own structure instead of from character
    distance. _closest_to_a_signal is kept for callers/tests but is no
    longer part of the verdict.)
    """
    if col_role == COL_ADULT:
        return BIND_NONE
    if col_role == COL_MINOR:
        return BIND_COLUMN

    adult, minor = role_binding(page_text, start, end)
    if not minor and _inherits_minor_binding(page_text, name_spans, start):
        minor = True
    if adult and not minor:
        return BIND_NONE
    if minor:
        return BIND_LABEL

    if not reasons:
        return BIND_NONE

    # Bounded, not line-scoped: an extracted "line" can be a whole
    # paragraph, which put a child's DOB next to every clinician in it.
    near = normalize_context(
        page_text[max(0, start - 25):min(len(page_text), end + BINDING_WINDOW_AFTER)])
    if dob_implies_minor(near) or MINOR_AGE_RE.search(near):
        return BIND_DOB

    window = normalize_context(
        _context_window(page_text, start, end, MINOR_CONTEXT_WINDOW))
    if RELATION_RE.search(window):
        return BIND_RELATION
    if any(r.startswith("age:") or r.lower() in MINOR_STRONG_SIGNALS
           for r in reasons):
        return BIND_RELATION
    return BIND_NONE


def minor_tier(page_text: str, name_spans, start: int, end: int,
               reasons, col_role: str = COL_NONE) -> str:
    """Review tier for a name, derived from its binding."""
    return BIND_TIER[
        minor_verdict(page_text, name_spans, start, end, reasons, col_role)
    ]


def shape_adjustment(name: str) -> float:
    """
    Negative adjustment for detections whose surface form suggests they
    need review. This is what spreads the otherwise-flat spaCy scores
    across the three tiers.
    """
    penalty = 0.0
    if any(ch.isdigit() for ch in name):
        penalty += DIGIT_PENALTY
    tokens = name.split()
    if len(tokens) < 2:
        penalty += SINGLE_TOKEN_PENALTY
    # Institutional denylist: "Department of Politics", "Fabian Socialism",
    # etc. Token-level, punctuation-stripped, applied once per name.
    if any(t.strip(".,;:()[]\"'").lower() in INSTITUTIONAL_TERMS for t in tokens):
        penalty += INSTITUTIONAL_PENALTY
    letters = [ch for ch in name if ch.isalpha()]
    if letters and (name == name.upper() or name == name.lower()):
        penalty += CASE_ANOMALY_PENALTY
    return -penalty


# ---------------------------------------------------------------------
# Span sanitising -- trim first, judge second
#
# Every function below trims before it judges, because the alternative
# loses real names: "Sarah Kowalski†¶" is a person with two glyphs of
# OCR debris stuck to her, not garbage, and "Hello Sarah" is a person
# with a greeting stuck to her. A rule that drops both is worse than one
# that drops neither.
# ---------------------------------------------------------------------
_NAME_TOKEN_STRIP = ".,;:'’\"()[]{}<>|/\\!?*#=+@_-"


def _char_is_garbage(ch: str) -> bool:
    """True for characters that cannot occur inside any personal name."""
    if ch.isalpha() or ch.isspace():
        return False
    if ch in NAME_PUNCT_OK:
        return False
    if ch in GARBAGE_ASCII:
        return True
    import unicodedata
    if unicodedata.combining(ch):
        return False
    cat = unicodedata.category(ch)
    if cat in GARBAGE_UNICODE_CATEGORIES:
        return True
    # Any punctuation not on the permit list: footnote daggers, pilcrows,
    # bullets, em dashes, guillemets. NAME_PUNCT_OK has already excused
    # the apostrophes, hyphens, periods and commas that names really use,
    # so whatever is left here is furniture or a ligature failure.
    return cat.startswith("P")


def _leading_phrase_len(tokens, phrases) -> int:
    """
    Length in tokens of the longest boilerplate phrase at the head of
    `tokens`, or 0. Phrases are matched longest-first so "good morning"
    beats a bare "good" and "yours truly" beats "yours".
    """
    for n in range(min(4, len(tokens)), 0, -1):
        probe = " ".join(t.lower().strip(_NAME_TOKEN_STRIP) for t in tokens[:n])
        if probe in phrases:
            return n
    return 0


def sanitize_surface(raw: str):
    """
    Clean a detected span without destroying it. Returns
    (cleaned_name, notes) where notes records what was removed, so a trim
    is auditable from the CSV rather than being a silent rewrite.

    Three passes, in order:
      1. edge debris  -- strip garbage characters from both ends of the
         span and of each token, and drop tokens left with no letters at
         all ("†", "¶", "///"). Interior debris is deliberately LEFT in
         place: it is the evidence garbage_verdict needs.
      2. boilerplate  -- strip leading salutations, valedictions and
         honorifics ("Hello Sarah" -> "Sarah", "Dr. Sarah Kowalski" ->
         "Sarah Kowalski"). Never strips the last remaining token, so a
         name that IS a valediction word survives to be judged on its
         merits rather than vanishing mid-trim.
      3. re-collapse whitespace.
    """
    notes = []
    text = re.sub(r"\s+", " ", raw or "").strip()
    if not text:
        return "", notes

    tokens = []
    for tok in text.split():
        cleaned = tok
        while cleaned and _char_is_garbage(cleaned[0]):
            cleaned = cleaned[1:]
        while cleaned and _char_is_garbage(cleaned[-1]):
            cleaned = cleaned[:-1]
        cleaned = cleaned.strip(_NAME_TOKEN_STRIP + " ")
        if not cleaned:
            if tok:
                notes.append("dropped_debris_token")
            continue
        if cleaned != tok:
            notes.append("trimmed_edge_debris")
        tokens.append(cleaned)

    # Restore a trailing period on initials, which the strip above eats
    # ("Ejike C. Okonkwo" -> "Ejike C Okonkwo" reads oddly in a report).
    tokens = [t for t in tokens if t]
    if not tokens:
        return "", sorted(set(notes))

    changed = True
    while changed and len(tokens) > 1:
        changed = False
        for phrases, note in (
            (SALUTATION_TOKENS, "stripped_salutation"),
            (VALEDICTION_TOKENS, "stripped_valediction"),
            (HONORIFIC_TOKENS, "stripped_honorific"),
        ):
            n = _leading_phrase_len(tokens, phrases)
            # `n < len(tokens)` is the safety catch: never trim a span
            # down to nothing. If every token is boilerplate the span is
            # handed intact to boilerplate_only() to decide.
            if n and n < len(tokens):
                tokens = tokens[n:]
                notes.append(note)
                changed = True
                break

    return " ".join(tokens), sorted(set(notes))


def boilerplate_only(name: str) -> bool:
    """
    True when a span consists of nothing but correspondence furniture.
    Checked against SALUTATION_ONLY_SAFE rather than the full trim sets,
    so "Best" and "Thanks" and "Grace" -- all attested surnames -- are
    never dropped on the strength of a wordlist.
    """
    toks = [t.lower().strip(_NAME_TOKEN_STRIP) for t in name.split()]
    toks = [t for t in toks if t]
    if not toks:
        return True
    if " ".join(toks) in SALUTATION_ONLY_SAFE:
        return True
    return all(t in SALUTATION_ONLY_SAFE for t in toks)


def _is_latin(token: str) -> bool:
    """
    Whether the orthographic tests below can say anything useful about a
    token. Vowel and consonant-run heuristics are properties of the Latin
    alphabet; applied to Cyrillic, Greek, Hebrew, Arabic or CJK they
    report every name as garbage.
    """
    return all(
        (not ch.isalpha()) or ("a" <= ch.lower() <= "z") or ch.lower() in _VOWELS
        for ch in token
    )


def garbage_signals(name: str):
    """
    Returns (severe, signals). `severe` means a character that cannot
    occur in any name survived edge-trimming, i.e. it sits in the
    INTERIOR of the span -- "Eurosymbol:///lj{]" keeps its colons and
    slashes and is condemned on those alone. `signals` are weaker
    orthographic tells; garbage_verdict requires two of them, because
    any one on its own has a real-name counterexample.
    """
    signals = []
    if not name:
        return True, ["empty"]

    letters = [ch for ch in name if ch.isalpha()]
    if not letters:
        return True, ["no_letters"]

    if any(_char_is_garbage(ch) for ch in name):
        return True, ["interior_garbage_char"]

    # Digits are excluded here because they have their own signal. Counting
    # them in both places let one observation convict twice, which is not
    # what "two independent signals" is supposed to mean.
    punct = sum(1 for ch in name if not ch.isalnum() and not ch.isspace())
    if punct / max(1, len(name.replace(" ", ""))) > GARBAGE_NONLETTER_RATIO:
        signals.append("punctuation_ratio")

    if any(ch.isdigit() for ch in name):
        signals.append("digits")

    if not any(len([c for c in t if c.isalpha()]) >= 2 for t in name.split()):
        signals.append("no_multiletter_token")

    for tok in name.split():
        alpha = [c for c in tok if c.isalpha()]
        if len(tok) > GARBAGE_MAX_TOKEN_LEN:
            signals.append("overlong_token")
        # Letters and digits interleaved within a single token -- "J0hn",
        # "5m1th", "Sm1th". A standalone numeric token ("Room 214") is
        # only the weaker "digits" signal, because that one does show up
        # beside real names.
        if alpha and any(c.isdigit() for c in tok):
            signals.append("alnum_mixed_token")
        if not _is_latin(tok):
            continue
        low = "".join(alpha).lower()
        if len(low) >= GARBAGE_VOWELLESS_MIN_LEN and not any(c in _VOWELS for c in low):
            signals.append("vowelless_token")
        run = best = 0
        for c in low:
            run = run + 1 if c not in _VOWELS else 0
            best = max(best, run)
        if best >= GARBAGE_CONSONANT_RUN:
            signals.append("consonant_run")
        rep = 1
        for a, b in zip(low, low[1:]):
            rep = rep + 1 if a == b else 1
            if rep >= GARBAGE_REPEAT_RUN:
                signals.append("repeated_char_run")
                break

    return False, sorted(set(signals))


def garbage_verdict(name: str) -> str:
    """Suppression reason detail for OCR debris, or "" to keep the hit."""
    severe, signals = garbage_signals(name)
    if severe:
        return ",".join(signals)
    if len(signals) >= 2:
        return ",".join(signals)
    return ""


def org_verdict(name: str, entity_overlaps=()) -> str:
    """
    Suppression reason detail for an organisation, or "".

    Two independent routes in, plus a third that is NOT independent:

      - a STRONG institutional token ("Council", "Hospital", "LLC") is
        sufficient on its own;
      - a WEAK token ("Church", "Memorial", "Valley") is sufficient on
        its own ONLY when there are three or more of them, or one plus
        an "of"/"&" construction, or one plus a LOCATION/GPE overlap.
        Never a single weak token alone -- Summer Church is a person and
        Mercy Hill is a plausible one.
      - the NER model itself labelling the same characters ORGANIZATION
        is corroboration, NOT a verdict by itself. It stacks with a
        SINGLE weak token, an org construction, or a LOCATION overlap to
        reach the same bar the weak-term route needs on its own.

    The NER-alone case was tried and cut: presidio's ORG recognizer
    tagged "Georgia Bell" -- a nurse's name, not a company -- as an
    organisation (plausibly reading "Bell" as a phone-company pattern),
    and with no corroboration requirement that alone was enough to
    erase a real person from the report. spaCy's entity types are a
    useful signal, not a verdict; NER on its own is held to exactly the
    same corroboration bar as a bare weak token, because that is what it
    is -- one piece of evidence, not two.
    """
    toks = [t.lower().strip(_NAME_TOKEN_STRIP) for t in name.split()]
    toks = [t for t in toks if t]
    strong = sorted({t for t in toks if t in INSTITUTIONAL_TERMS})
    weak = sorted({t for t in toks if t in INSTITUTIONAL_WEAK_TERMS})
    overlaps = {str(e).upper() for e in entity_overlaps}

    if strong:
        return "institutional_term:" + "+".join(strong)

    corroboration = []
    # THREE weak terms, not two. "Grace Hall", "Park Hill" and
    # "Summer Church" are all plausible people; "Mercy Valley Health"
    # and "Park Ridge Manor" are not. Two weak terms alone convict too
    # many real names, which is the wrong error to make here.
    if len(weak) >= 3:
        corroboration.append("multiple_weak_terms")
    if ORG_CONSTRUCTION_RE.search(name):
        corroboration.append("org_construction")
    if overlaps & {"LOCATION", "GPE"}:
        corroboration.append("ner_location")

    if weak and corroboration:
        return ("weak_term:" + "+".join(weak)
                + "|" + "+".join(sorted(set(corroboration))))

    hard_ner = overlaps & {"ORGANIZATION", "ORG", "NRP"}
    if hard_ner:
        # NER needs corroboration from the DICTIONARY, not from a second
        # guess by the same model. A LOCATION/GPE overlap was tried here
        # too and cut: "Georgia Bell" gets an ORGANIZATION tag on "Bell"
        # and a GPE tag on "Georgia" (it is, after all, a US state), and
        # letting one NER guess corroborate another just stacks two
        # pieces of the same model's noise into something that looks
        # like two independent signals but isn't. Only an actual weak
        # institutional TOKEN counts here.
        if weak:
            return ("ner_entity:" + "+".join(sorted(hard_ner))
                    + "|weak_term:" + "+".join(weak))
    return ""


_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?;\n]")
_WORD_RE = re.compile(r"[^\W\d_][\u2019']?[\w\u2019'\-]*", re.UNICODE)


def _head_noun_within(ctx_text: str, end: int, limit: int = MEDICAL_LOOKAHEAD):
    """
    Search a bounded window after a span for a medical/eponym head noun,
    rather than requiring one to be the very next token.

    This replaced a fixed-offset regex match after a real scan missed
    "Asperger's or other developmental eponymous syndrome": the head
    noun ("syndrome") was there, just six words further along than a
    strict next-token match could see. "Down's syndrome" has the head
    noun immediately adjacent; real chart prose is not always that
    considerate.

    The window is cut at the first sentence boundary (.!?; or newline)
    so a search starting near the end of one sentence cannot wander into
    an unrelated clause two sentences later and find a head noun that
    has nothing to do with the eponym in question.
    """
    if not ctx_text or not end:
        return None
    window = ctx_text[end:end + limit]
    m = _SENTENCE_BOUNDARY_RE.search(window)
    if m:
        window = window[:m.start()]
    for tok in _WORD_RE.findall(window):
        norm = tok.lower().strip(_NAME_TOKEN_STRIP)
        if norm in MEDICAL_HEAD_NOUNS:
            return norm
    return None


def medical_verdict(name: str, ctx_text: str = "", start: int = 0,
                    end: int = 0) -> str:
    """
    Suppression reason detail for an eponym or medical term, or "".

    Keyed on the head noun, not on a catalogue of conditions: any name
    followed (nearby, not necessarily adjacent -- see _head_noun_within)
    by "syndrome", "manoeuvre", "forceps" or "distribution" is being used
    eponymically whether or not anyone has written the condition down.
    The bare-eponym route requires a possessive, because Bell, Turner,
    Graves, Down, Wilson and Parkinson are all common surnames and this
    corpus is full of the people who bear them.
    """
    toks = [t.lower().strip(_NAME_TOKEN_STRIP) for t in name.split()]
    toks = [t for t in toks if t]
    if not toks:
        return ""

    if toks[-1] in MEDICAL_HEAD_NOUNS:
        return "head_noun:" + toks[-1]

    nxt = _head_noun_within(ctx_text, end)
    if nxt:
        return "following_head_noun:" + nxt

    # Possessive eponym embedded IN the detected span: "Down's",
    # "Crohn's", "Alzheimer's" -- the tokenizer kept the "'s" attached.
    raw_toks = [t.strip(_NAME_TOKEN_STRIP.replace("'", "").replace("’", ""))
                for t in name.split()]
    for tok in raw_toks:
        low = tok.lower()
        for suffix in ("'s", "’s", "s'", "s’"):
            if low.endswith(suffix):
                base = low[: -len(suffix)].strip(_NAME_TOKEN_STRIP)
                if base in MEDICAL_EPONYMS:
                    return "possessive_eponym:" + base

    # Possessive eponym where the tokenizer SPLIT the possessive off,
    # ending the span right before it: "Graves' disease" (span ends at
    # "Graves", next char is a bare apostrophe -- "Graves" already ends
    # in "s", so its possessive takes no trailing "s" of its own) or
    # "Wilson's" (next two chars are "'s"). Checked for a BARE
    # apostrophe as well as "'s", which a stricter earlier version of
    # this check did not do -- that omission alone was enough to miss
    # every eponym whose surname already ends in "s": Graves, Hodgkins,
    # Williams, Rivers, Jenkins.
    #
    # Deliberately requires NO head noun here. A possessive attached
    # directly to a known eponym surname is itself the evidence -- the
    # same leniency already given to the embedded-possessive branch just
    # above. An earlier version of this function required a head noun
    # within range for this branch but not the other one, which was an
    # unintentional asymmetry: it demanded MORE evidence for the more
    # common case (spaCy splits the possessive off) than for the rarer
    # one (spaCy keeps it attached). In practice this branch is now
    # usually redundant with _head_noun_within above -- "Graves' disease"
    # is already caught because "disease" is found in the search window
    # regardless of the possessive -- but it remains as a safety net for
    # an eponym with no head noun nearby at all ("ruled out Wilson's on
    # follow-up").
    if ctx_text and end and toks[-1] in MEDICAL_EPONYMS:
        if ctx_text[end:end + 1] in ("'", "’"):
            return "possessive_eponym:" + toks[-1]
    return ""


def denylist_verdict(name: str, denylist) -> str:
    """
    Suppression reason detail for a user-supplied token denylist, or "".
    Fires only when EVERY token of the span is on the list, so adding
    "hospital" cannot take "Hospital Jones" (or a person whose surname
    collides with one denied token) down with it.
    """
    if not denylist:
        return ""
    toks = [normalize_gazetteer_token(t) for t in name.split()]
    toks = [t for t in toks if t]
    if not toks:
        return ""
    if all(t in denylist for t in toks):
        return "denylist:" + "+".join(sorted(set(toks)))[:80]
    return ""


def suppress_verdict(name: str, ctx_text: str = "", start: int = 0,
                     end: int = 0, entity_overlaps=(), denylist=None):
    """
    Single entry point. Returns (reason, detail) with reason one of the
    SUPPRESS_* constants, or ("", "") to keep the hit in the main report.
    Order is by confidence in the rule, and the first match wins so the
    reported reason is the strongest one rather than the last one.
    """
    detail = garbage_verdict(name)
    if detail:
        return SUPPRESS_GARBAGE, detail
    if boilerplate_only(name):
        return SUPPRESS_SALUTATION, "boilerplate_only"
    detail = denylist_verdict(name, denylist)
    if detail:
        return SUPPRESS_DENYLIST, detail
    detail = org_verdict(name, entity_overlaps)
    if detail:
        return SUPPRESS_ORG, detail
    detail = medical_verdict(name, ctx_text, start, end)
    if detail:
        return SUPPRESS_MEDICAL, detail
    return SUPPRESS_NONE, ""


def locate_in_text(haystack: str, needle: str):
    """
    Find `needle` in `haystack`, tolerating whitespace differences (a cell
    had its internal newlines collapsed; the page text still has them).
    Returns (start, end) or None.
    """
    needle = needle.strip()
    if not needle:
        return None
    idx = haystack.find(needle)
    if idx >= 0:
        return idx, idx + len(needle)
    tokens = needle.split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    m = re.search(pattern, haystack)
    return (m.start(), m.end()) if m else None


def locate_all_in_text(haystack: str, needle: str):
    """
    Every occurrence of `needle`, not just the first. locate_in_text()
    returns the first match, so a name appearing in both a routing header
    and a "Student: ... Grade 3" row had only the header window examined
    -- silently contradicting the promise that the flag fires within
    MINOR_CONTEXT_WINDOW of ANY occurrence. Cell and truecase units relocate
    through here; prose units keep their own real offsets.
    """
    needle = needle.strip()
    if not needle:
        return []
    tokens = needle.split()
    if not tokens:
        return []
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    return [(m.start(), m.end()) for m in re.finditer(pattern, haystack)]


# ---------------------------------------------------------------------
# Truecasing -- ALL-CAPS lines get a title-cased twin unit
# ---------------------------------------------------------------------
def titlecase_preserve_length(s: str) -> str:
    """
    Character-position-preserving title-case: every alpha char maps to
    exactly one char, so a span detected in the variant maps 1:1 back to
    the original line and the reported name keeps its source casing.
    Word starts (after any non-alpha char) are uppercased -- this turns
    O'FAOLÁIN into O'Faoláin and OKONKWO-ADEYEMI into Okonkwo-Adeyemi.
    """
    out = []
    word_start = True
    for ch in s:
        if ch.isalpha():
            t = ch.upper() if word_start else ch.lower()
            if len(t) != 1:      # rare one-to-many case mappings (e.g. İ)
                t = ch
            out.append(t)
            word_start = False
        else:
            out.append(ch)
            word_start = True
    return "".join(out)


def is_predominantly_uppercase(line: str) -> bool:
    letters = [ch for ch in line if ch.isalpha()]
    if len(letters) < UPPERCASE_LINE_MIN_LETTERS:
        return False
    tokens = [t for t in line.split() if any(c.isalpha() for c in t)]
    if len(tokens) < UPPERCASE_LINE_MIN_TOKENS:
        return False
    upper = sum(1 for ch in letters if ch.isupper())
    return upper / len(letters) >= UPPERCASE_LINE_RATIO


def uppercase_variants(text: str):
    """
    Yield (titlecased_line, original_line) for each predominantly-uppercase
    line in `text`. Skips lines whose titlecased form equals the original
    (nothing new to analyse).
    """
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or not is_predominantly_uppercase(stripped):
            continue
        variant = titlecase_preserve_length(stripped)
        if variant != stripped:
            yield variant, stripped


# ---------------------------------------------------------------------
# Gazetteer -- dictionary-backed recogniser for noun-like names
# ---------------------------------------------------------------------
# spaCy's NER misses names that read as places or common nouns ("Georgia
# Bell", "Summer Church"). A dictionary of US Census surnames + SSA given
# names catches these purely by membership: runs of 2+ consecutive
# capitalised tokens where >= GAZETTEER_MIN_TOKEN_MATCHES tokens appear in
# the dictionary are emitted as PERSON at a review-tier score.
_GAZ_TOKEN_RE = re.compile(r"[^\W\d_][\w'’.\-]*", re.UNICODE)
_GAZ_GAP_RE = re.compile(r"[ \t]*")
_GAZ_STRIP = ".,;:'’\"()[]"


def normalize_gazetteer_token(tok: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", tok)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.casefold().strip(_GAZ_STRIP)


def _token_in_gazetteer(tok: str, names: frozenset) -> bool:
    norm = normalize_gazetteer_token(tok)
    if norm in names:
        return True
    # Hyphenated tokens count as matched if any component matches
    # ("Okonkwo-Adeyemi" won't be in the Census file, but "Lopez-Whitfield"
    # matches on both halves).
    if "-" in norm:
        return any(part in names for part in norm.split("-") if part)
    return False


def find_gazetteer_spans(text: str, names: frozenset,
                         min_matches: int = GAZETTEER_MIN_TOKEN_MATCHES,
                         ambiguous: frozenset = frozenset()):
    """
    Return [(start, end), ...] for runs of 2+ consecutive capitalised
    tokens (separated by spaces/tabs only -- newlines, commas and other
    punctuation break a run, so table-row neighbours cannot fuse here
    either) where at least `min_matches` tokens are dictionary members.
    Deliberately regex-free at the dictionary level: a 160k-entry deny
    list compiled into one pattern is unusable, so this tokenises and
    does set lookups instead.

    `ambiguous` holds tokens that are simultaneously real names and
    everyday English words -- White, King, Green, Hill, Hall, Bell,
    Church, Best. These are the dominant source of dictionary false
    positives, and they cannot be solved by deleting rows: White is the
    660,000-count 20th most common surname in the United States AND the
    first half of "White House". Frequency thresholds do not touch them
    either, because they sit at the TOP of the distribution, not in the
    tail.

    So they stay in the dictionary and are demoted here instead: a match
    on an ambiguous token counts toward `min_matches`, but a run made up
    ENTIRELY of ambiguous matches is rejected. At least one match must be
    a token that is a name and not also a common word.

        "Georgia Bell"  -> georgia unambiguous + bell ambiguous  -> KEEP
        "White House"   -> both ambiguous                        -> reject
        "Green Bay"     -> both ambiguous                        -> reject
        "Zofia Kowalska"-> both unambiguous                      -> KEEP

    The cost is real and worth stating: "Summer Church", the two-common-
    words person this recogniser was built to catch, is now missed by
    the dictionary. spaCy still gets a shot at her, and the org rules no
    longer suppress a lone weak term, so she survives -- but the
    dictionary alone will not find her.
    """
    spans = []
    run = []          # [(token, start, end), ...]

    def flush():
        if len(run) >= 2:
            matched = [t for t, _, _ in run if _token_in_gazetteer(t, names)]
            unambiguous = [
                t for t in matched
                if not _token_in_gazetteer(t, ambiguous)
            ] if ambiguous else matched
            if len(matched) >= min_matches and unambiguous:
                spans.append((run[0][1], run[-1][2]))
        run.clear()

    prev_end = None
    for m in _GAZ_TOKEN_RE.finditer(text):
        tok, s, e = m.group(), m.start(), m.end()
        capitalised = tok[:1].isupper()
        contiguous = (
            prev_end is not None
            and _GAZ_GAP_RE.fullmatch(text, prev_end, s) is not None
        )
        if capitalised and (not run or contiguous):
            run.append((tok, s, e))
        else:
            flush()
            if capitalised:
                run.append((tok, s, e))
        # A trailing period on a multi-letter token is a sentence boundary
        # and ends the run ("...with Summer Church. Autumn Winters..." must
        # not fuse). Initials ("Ejike C. Okonkwo") stay inside the run.
        if run and tok.endswith(".") and sum(ch.isalpha() for ch in tok) >= 3:
            flush()
        prev_end = e
    flush()
    return spans


def _iter_gazetteer_files(paths):
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and child.suffix.lower() in (".csv", ".txt"):
                    yield child
        elif p.is_file():
            yield p


def load_gazetteer(paths, cache_dir: Path, quiet: bool = False,
                   label: str = "gazetteer"):
    """
    Compile the name files into one normalised frozenset, cached on disk
    keyed by the source files' identity, so the ~160k-line parse is paid
    once per dictionary, not once per run. Handles the US Census surname
    CSV (first column = name) and SSA baby-name files ("Mary,F,7065");
    plain one-name-per-line .txt also works. Returns None -- and the tool
    runs exactly as before -- when no files are supplied or found.
    """
    if not paths:
        return None
    files = sorted(set(_iter_gazetteer_files(paths)))
    if not files:
        if not quiet:
            print(
                f"{label}: no usable files found under "
                + ", ".join(str(p) for p in paths)
                + f" -- continuing without the {label}.",
                file=sys.stderr,
            )
        return None

    sig_parts = []
    for f in files:
        st = f.stat()
        sig_parts.append(f"{f.resolve()}|{st.st_size}|{int(st.st_mtime)}")
    sig = hashlib.sha1("\n".join(sig_parts).encode("utf-8")).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    compiled = cache_dir / f"{label}-{sig}.txt"

    if compiled.is_file():
        with open(compiled, encoding="utf-8") as fh:
            return frozenset(line.rstrip("\n") for line in fh if line.strip())

    names = set()
    header_words = {"name", "surname", "rank", "count"}
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    first = line.split(",", 1)[0].strip()
                    norm = normalize_gazetteer_token(first)
                    if len(norm) < 2 or norm in header_words:
                        continue
                    if not all(ch.isalpha() or ch in "-'" for ch in norm):
                        continue
                    names.add(norm)
        except OSError as exc:
            if not quiet:
                print(f"{label}: could not read {f}: {exc}", file=sys.stderr)

    if not names:
        if not quiet:
            print(
                f"{label}: files yielded no usable tokens -- "
                f"continuing without the {label}.",
                file=sys.stderr,
            )
        return None

    tmp = compiled.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted(names)))
    tmp.replace(compiled)
    return frozenset(names)


def make_gazetteer_recognizer(names: frozenset, score: float,
                              ambiguous: frozenset = frozenset()):
    """Factory keeps the presidio import lazy, matching the existing
    pattern where presidio is only imported inside worker startup."""
    from presidio_analyzer import EntityRecognizer, RecognizerResult

    class GazetteerNameRecognizer(EntityRecognizer):
        def __init__(self):
            super().__init__(
                supported_entities=["PERSON"],
                name=GAZETTEER_RECOGNIZER_NAME,
                supported_language="en",
            )

        def load(self):
            pass

        def analyze(self, text, entities, nlp_artifacts=None):
            if entities and "PERSON" not in entities:
                return []
            return [
                RecognizerResult(
                    entity_type="PERSON", start=s, end=e, score=score,
                )
                for s, e in find_gazetteer_spans(
                    text, names, ambiguous=ambiguous)
            ]

    return GazetteerNameRecognizer()


def _result_recognizer(result) -> str:
    md = getattr(result, "recognition_metadata", None) or {}
    rec = str(md.get("recognizer_name", ""))
    return "gazetteer" if rec == GAZETTEER_RECOGNIZER_NAME else "spacy"


def resolve_overlaps(results):
    """
    Presidio can return a spaCy PERSON and a gazetteer PERSON over the
    same tokens. Keep one result per overlapping cluster -- the
    higher-scoring span -- and label it 'spacy', 'gazetteer', or 'both'
    so the report can show what the gazetteer contributes vs. costs.
    Returns [(result, label), ...].
    """
    kept = []  # mutable [result, label]
    ordered = sorted(results, key=lambda r: (r.start, -(r.end - r.start)))
    for r in ordered:
        label = _result_recognizer(r)
        for slot in kept:
            k = slot[0]
            if (k.entity_type == r.entity_type
                    and r.start < k.end and k.start < r.end):
                if r.score > k.score:
                    slot[0] = r
                if label != slot[1]:
                    slot[1] = "both"
                break
        else:
            kept.append([r, label])
    return [(s[0], s[1]) for s in kept]


PAGE_GARBAGE_RATIO = 0.30


def page_garbage_ratio(text: str) -> float:
    """
    Share of a page's non-whitespace characters that cannot occur in
    running text. A high value means OCR failed on this page and the
    names mined from it should be read with suspicion.

    Recorded in page_problems and the manifest, NOT acted on: a botched
    page usually still contains recoverable names, and skipping it
    outright would trade a handful of junk rows for a handful of real
    people. The reviewer gets told which pages to distrust instead.
    """
    import unicodedata
    body = [ch for ch in text if not ch.isspace()]
    if not body:
        return 0.0

    def broken(ch):
        # Deliberately NARROWER than _char_is_garbage: running text is
        # legitimately full of parentheses, slashes, colons and dollar
        # signs, and judging a whole page by a name's standards would
        # flag every well-OCR'd invoice in the corpus. Only symbols,
        # private-use glyphs and unassigned code points count here.
        if ch.isalnum():
            return False
        if ch in "{}|\\^~<>\u00ac\u2020\u2021\u00b6":
            return True
        return unicodedata.category(ch) in GARBAGE_UNICODE_CATEGORIES

    return sum(1 for ch in body if broken(ch)) / len(body)


def has_text_layer(pdf_path: Path) -> bool:
    """
    Cheap up-front check for a scanned/raster-only PDF. Prefers pdffonts
    (poppler); falls back to sampling pages with pdfplumber when poppler
    is not installed.
    """
    if shutil.which("pdffonts"):
        try:
            out = subprocess.run(
                ["pdffonts", str(pdf_path)],
                capture_output=True, text=True, timeout=120,
            )
            lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
            # Header occupies 2 lines; rows beyond that mean fonts exist.
            return len(lines) > 2
        except (subprocess.SubprocessError, OSError):
            pass

    try:
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            # Sample pages spread across the document. Sampling only the
            # first few pages silently skips PDFs whose scanned cover
            # sheets precede a thousand pages of perfectly good text.
            sample = sorted({round(i * (n - 1) / 7) for i in range(8)}) if n else []
            for idx in sample:
                page = pdf.pages[idx]
                try:
                    if (page.extract_text() or "").strip():
                        return True
                finally:
                    if hasattr(page, "flush_cache"):
                        page.flush_cache()
            return False
    except Exception:
        return False


# ---------------------------------------------------------------------
# Phase 1 -- scan
# ---------------------------------------------------------------------
# Unit kinds. 'prose' units are large enough to be their own context;
# 'cell' and 'truecase' units are small, so context windows come from the
# page-level text instead (see extract_page_units docstring).
UNIT_PROSE = "prose"
UNIT_CELL = "cell"
UNIT_TRUECASE = "truecase"
UNIT_FLIPPED = "flipped"

# Kinds that are variants of text analysed elsewhere on the same page,
# and so must not inflate occurrence_count when they rediscover a name
# already found in its source form.
UNIT_VARIANT_KINDS = (UNIT_TRUECASE, UNIT_FLIPPED)

# "Kalinowska, Zofia Maria" -> "Zofia Maria Kalinowska". Requires a
# capitalised token on each side and no digits, so "Room 214, Bed 12" and
# "Portland, OR 97205" cannot flip.
SURNAME_FIRST_RE = re.compile(
    r"^\s*([^\W\d_][\w'’.-]*(?:\s+[^\W\d_][\w'’.-]*){0,2})\s*,\s*"
    r"([^\W\d_][\w'’.-]*(?:\s+[^\W\d_][\w'’.-]*){0,3})\s*$",
    re.UNICODE)


# Tokens that identify a line as a column HEADER rather than a person,
# even though it syntactically matches "Surname, Given" -- "Last, First"
# is the canonical trap, but any label-only pair should be caught the
# same way. Deliberately generic (roster/form vocabulary), not limited
# to the words NAME_COLUMN_RE looks for, since a header can name the
# name column ("Last, First") or a completely unrelated column
# ("Room, Ext") that just happens to sit on a comma-joined title line.
TABLE_HEADER_WORDS = frozenset({
    "last", "first", "middle", "name", "names", "surname", "forename",
    "given", "family", "full", "legal", "maiden", "nickname",
    "department", "dept", "division", "unit", "ward", "role", "title",
    "position", "status", "badge", "ext", "extension", "phone", "email",
    "address", "room", "bed", "floor", "suite", "attendee", "attendees",
    "present", "absent", "code", "id", "number", "no", "initial",
    "initials", "sex", "gender", "dob", "age", "date", "signature",
    "notes", "comments", "remarks", "office", "location",
})


def _looks_like_header(part: str) -> bool:
    """
    True when every token on one side of a comma is column-header
    vocabulary. Checked on BOTH sides independently in flipped_name --
    "Last, First" fails because both sides are single header words;
    "Osei, Amara" passes because neither side is.
    """
    toks = [t.lower().strip(_NAME_TOKEN_STRIP) for t in part.split()]
    toks = [t for t in toks if t]
    return bool(toks) and all(t in TABLE_HEADER_WORDS for t in toks)


def flipped_name(cell_text: str):
    """
    Given-name-first rendering of a "Surname, Given" cell, or None.

    Roster and face-sheet tables write names surname-first, and spaCy's
    PERSON recogniser -- trained on running prose -- misses a fair number
    of them outright. On the reference corpus two children appeared ONLY
    in that form and were never detected at all, so no amount of
    minor-flag tuning could have reached them. Re-analysing the flipped
    string recovers the name; the cell itself is still analysed as
    written, so nothing is lost if the flip is wrong.
    """
    m = SURNAME_FIRST_RE.match(re.sub(r"\s+", " ", cell_text).strip())
    if not m:
        return None
    surname, given = m.group(1).strip(), m.group(2).strip()
    # Suffix-style tails ("Pettigrew, Jr.") are not given names.
    if given.rstrip(".").lower() in {"jr", "sr", "ii", "iii", "iv", "esq", "md",
                                     "rn", "do", "phd", "inc", "llc", "llp"}:
        return None
    # "Council, Stevens" and "Hospital, Johns" are inverted ORGANISATION
    # renderings from an index, not people. Flipping them would smuggle a
    # suppressed org back in wearing a different surface form.
    if org_verdict(f"{given} {surname}"):
        return None
    # "Last, First" -- a table's OWN column header, not a data row. It
    # matches SURNAME_FIRST_RE syntactically (it IS two comma-joined
    # capitalised-shaped tokens) and both "First" and "Last" are real,
    # if rare, census surnames, so the gazetteer will happily confirm a
    # fabricated person here unless the header vocabulary itself is
    # excluded first. Rejected when EVERY token on BOTH sides of the
    # comma is header vocabulary, so a real name that happens to share
    # one word with a header ("Rhodes, Grant" -- neither is a header
    # word; "Title, Grace" -- "Grace" isn't either) still flips fine.
    if _looks_like_header(surname) and _looks_like_header(given):
        return None
    return f"{given} {surname}"


# Labels that introduce a surname-first name in running prose, where
# there is no table and therefore no column header to read:
# "RE: Kowalska, Zofia", "Patient: Okonkwo-Adeyemi, Ejike".
PROSE_NAME_LABEL_RE = re.compile(
    r"(?:^|[\n\r])\s*(?:re|ref|reference|subject|patient|client|student|"
    r"resident|member|employee|name|insured|decedent|deponent|witness|"
    r"in\s+re|regarding)\s*[:\-]\s*(?P<value>[^\n\r]{3,80})",
    re.IGNORECASE)

# A bare line that is nothing but a surname-first name -- index entries,
# service lists, signature blocks, deposition captions. Bounded length so
# a wrapped sentence containing a comma cannot qualify.
PROSE_LINE_MAX_LEN = 60


def prose_flipped_units(text: str):
    """
    Yield given-name-first renderings of surname-first names found in
    running prose. Returns [(flipped, source_fragment), ...].

    The table path (extract_page_units) only ever saw names inside a
    table pdfplumber actually detected, which excludes every
    whitespace-aligned roster in a scanned corpus, every index, every
    "RE:" line and every signature block. Those are exactly the places
    "Smith, John" lives, so surname-first detection was reaching a small
    fraction of its targets.

    Both forms are emitted; the source text is still analysed as written,
    so a wrong flip costs a duplicate row rather than a lost name.
    """
    out = []
    seen = set()

    def offer(fragment):
        fragment = fragment.strip().rstrip(".;")
        if not fragment or fragment in seen:
            return
        flipped = flipped_name(fragment)
        # "Present: Ng, Wei" and "Attendee - Smith, John" carry a label
        # the surname-first pattern cannot match past, so retry on the
        # tail. Split on the LAST separator, since a name never contains
        # one and a label may contain several.
        if not flipped:
            for sep in (":", " - ", "\t"):
                if sep in fragment:
                    tail = fragment.rsplit(sep, 1)[1].strip()
                    flipped = flipped_name(tail)
                    if flipped:
                        fragment = tail
                        break
        if flipped and flipped != fragment and flipped not in seen:
            seen.add(flipped)
            out.append((flipped, fragment))

    for m in PROSE_NAME_LABEL_RE.finditer(text):
        offer(m.group("value"))

    for line in text.split("\n"):
        stripped = line.strip()
        if 3 <= len(stripped) <= PROSE_LINE_MAX_LEN and "," in stripped:
            offer(stripped)
            # Semicolon-separated service lists: "Ng, Wei; Okonkwo, Ejike"
            if ";" in stripped:
                for part in stripped.split(";"):
                    offer(part)

    return out


def extract_page_units(page, page_text: str):
    """
    Table-aware extraction. Returns (units, problems) where each unit is
    (analysis_text, kind, source_text, col_role):

      - one 'cell' unit per non-empty table cell, with in-cell newlines
        collapsed so a name wrapping inside its own cell rejoins, tagged
        with the role of the column it came from (see column_role);
      - one 'flipped' unit per surname-first cell in a name-bearing
        column, holding the given-name-first rendering;
      - one 'prose' unit for the page text OUTSIDE table bboxes (filtered
        so table content isn't analysed twice and occurrence counts stay
        honest);
      - one 'truecase' unit per predominantly-uppercase line, holding the
        title-cased variant; source_text is the original-cased line so
        detected spans map 1:1 back to the source casing.

    CONTEXT-WINDOW APPROACH: context_boost()/minor_context() need to see
    role labels ("Case Manager", "student") that often sit in a *nearby
    cell*, not inside the tiny cell containing the name. Rather than
    maintaining an offset map from every cell into the page string --
    brittle, because pdfplumber's extract_text() re-flows text and its
    offsets correspond to no stable source coordinates -- we keep the
    whole-page text alongside the units and relocate each detected span
    in it with a whitespace-flexible search (locate_in_text) at scoring
    time. That is cheap at realistic hit volumes, and when the span
    cannot be relocated (heavily mangled layout) the code degrades to
    windowing within the unit itself, which is exactly the pre-change
    behaviour.

    If table detection raises, the caller falls back to whole-page
    extraction for that page and records it in page_problems -- one
    malformed page must not kill a run.
    """
    units = []
    tables = page.find_tables()
    bboxes = [t.bbox for t in tables]

    for t in tables:
        rows = t.extract()
        if not rows:
            continue
        # First row is the header. Its cells decide, per column, whether
        # the values below are children, adults, or neither -- the fact
        # the old proximity approach threw away and then could not
        # reconstruct. A single-row table has no data rows, so it yields
        # no column roles.
        header = [re.sub(r"\s*\n\s*", " ", c).strip() if c else ""
                  for c in rows[0]]
        roles = [column_role(h) for h in header]
        namey = [bool(h and NAME_COLUMN_RE.search(h)) for h in header]

        # Header text is not the only evidence a column holds names, and
        # frequently not the available evidence: headerless rosters,
        # tables whose first row is a spanning title, and columns headed
        # "Last, First" (which matches no role vocabulary) all read as
        # non-name columns and so never had their cells flipped. Shape is
        # the more reliable signal -- if a column's cells parse as
        # "Surname, Given", it is a name column whatever the header says.
        width = max(len(r) for r in rows)
        while len(namey) < width:
            namey.append(False)
        while len(roles) < width:
            roles.append(COL_NONE)
        shape_hits = [0] * width
        for row in rows[1:]:
            for idx, cell in enumerate(row):
                if not cell or idx >= width:
                    continue
                if flipped_name(re.sub(r"\s*\n\s*", " ", cell).strip()):
                    shape_hits[idx] += 1
        for idx, n in enumerate(shape_hits):
            if n >= NAME_COLUMN_SHAPE_MIN_CELLS:
                namey[idx] = True

        for r_i, row in enumerate(rows):
            for idx, cell in enumerate(row):
                if not cell:
                    continue
                collapsed = re.sub(r"\s*\n\s*", " ", cell).strip()
                if not collapsed:
                    continue
                # The header row describes the column; it is not a value
                # in it. Without this a header literally reading "Student"
                # would bind itself as a child if spaCy tagged it PERSON.
                role = COL_NONE if r_i == 0 else (
                    roles[idx] if idx < len(roles) else COL_NONE)
                units.append((collapsed, UNIT_CELL, collapsed, role))
                if idx < len(namey) and namey[idx]:
                    flipped = flipped_name(collapsed)
                    if flipped and flipped != collapsed:
                        units.append((flipped, UNIT_FLIPPED, flipped, role))

    if bboxes:
        def outside_tables(obj):
            try:
                h = (obj["x0"] + obj["x1"]) / 2
                v = (obj["top"] + obj["bottom"]) / 2
            except (KeyError, TypeError):
                return True
            return not any(
                x0 <= h <= x1 and top <= v <= bottom
                for (x0, top, x1, bottom) in bboxes
            )

        prose = page.filter(outside_tables).extract_text() or ""
    else:
        prose = page_text

    if prose.strip():
        units.append((prose, UNIT_PROSE, prose, COL_NONE))

    # Truecase variants for ALL-CAPS lines, from prose and cells alike
    # (face-sheet headers like "OKONKWO-ADEYEMI, EJIKE C." live in cells).
    # A cell's truecase variant keeps that cell's column role, so an
    # ALL-CAPS name in a student column still binds by column.
    caps_sources = [(prose, COL_NONE)] + [
        (u[0], u[3]) for u in units if u[1] == UNIT_CELL
    ]
    for source, role in caps_sources:
        for variant, original in uppercase_variants(source):
            units.append((variant, UNIT_TRUECASE, original, role))

    # Surname-first names in running prose: "RE: Kowalska, Zofia", index
    # entries, signature blocks, deposition captions. None of these sit in
    # a table, so none of them reached the flip path before.
    truecased_prose = "\n".join(
        variant for variant, _ in uppercase_variants(prose)
    )
    for source in (prose, truecased_prose):
        if not source.strip():
            continue
        for flipped, _fragment in prose_flipped_units(source):
            units.append((flipped, UNIT_FLIPPED, flipped, COL_NONE))

    return units, []


def build_batch_analyzer(model_name: str, gazetteer_names=None,
                         gazetteer_score: float = DEFAULT_GAZETTEER_SCORE,
                         gazetteer_ambiguous=None):
    """
    Built inside each worker process, since a spaCy pipeline cannot be
    pickled across process boundaries. Loading the model is the expensive
    part of startup, which is why parallelism is per file rather than
    per chunk.
    """
    from presidio_analyzer import AnalyzerEngine, BatchAnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    provider = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": model_name}],
    })
    analyzer = AnalyzerEngine(
        nlp_engine=provider.create_engine(),
        supported_languages=["en"],
    )
    if gazetteer_names:
        analyzer.registry.add_recognizer(
            make_gazetteer_recognizer(
                gazetteer_names, gazetteer_score,
                frozenset(gazetteer_ambiguous or ()),
            )
        )
    return BatchAnalyzerEngine(analyzer_engine=analyzer)


def scan_one_pdf(pdf_path_str: str, cache_dir_str: str, chunk_size: int,
                 model_name: str, entities: list, force: bool,
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 gazetteer_paths=None,
                 gazetteer_score: float = DEFAULT_GAZETTEER_SCORE,
                 denylist_paths=None,
                 ambiguous_paths=None) -> dict:
    """
    Worker entry point. Processes one PDF in page chunks, committing hits
    and a checkpoint marker together per chunk so an interrupted run
    resumes cleanly at chunk granularity.
    """
    pdf_path = Path(pdf_path_str)
    cache_dir = Path(cache_dir_str)
    started = time.time()

    db_path = shard_path(cache_dir, pdf_path.name)
    if force and db_path.exists():
        db_path.unlink()

    conn = open_shard(db_path)
    page_count = 0

    try:
        set_file_info(conn, "source_name", pdf_path.name)

        # Resume markers are keyed by (start, end), so re-running with a
        # different --chunk-size would re-scan already-cached pages and
        # double-insert their hits, quietly inflating occurrence counts.
        # A PDF modified since the last scan is equally poisonous. Refuse
        # both rather than corrupt the cache.
        st = pdf_path.stat()
        src_sig = {"size": st.st_size, "mtime": int(st.st_mtime)}
        stored_sig = get_file_info(conn, "src_sig")
        stored_chunk = get_file_info(conn, "chunk_size")
        stored_ev = get_file_info(conn, "extractor_version")
        # Shards written before extractor versioning never stored the key,
        # so the src_sig pattern ("stored is not None and differs") would
        # silently accept every pre-versioning shard -- the exact mixing
        # this check exists to prevent. A missing key on a shard that has
        # completed chunks therefore means "built by extractor v1".
        has_prior_work = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM chunks_done)"
        ).fetchone()[0]
        if stored_ev is None and has_prior_work:
            stored_ev = 1
        blocker = None
        if stored_sig is not None and stored_sig != src_sig:
            blocker = "skipped_stale_cache (PDF changed since last scan; rerun with --force)"
        elif stored_chunk is not None and stored_chunk != chunk_size:
            blocker = (
                f"skipped_chunk_size_mismatch (cache built with {stored_chunk}; "
                f"rerun with --chunk-size {stored_chunk} or --force)"
            )
        elif stored_ev is not None and stored_ev != EXTRACTOR_VERSION:
            blocker = (
                f"skipped_extractor_version_mismatch (cache built by extractor "
                f"v{stored_ev}, this build is v{EXTRACTOR_VERSION}; rerun with "
                f"--force to rescan)"
            )
        if blocker:
            set_file_info(conn, "status", blocker)
            conn.commit()
            return {
                "file": pdf_path.name,
                "status": blocker,
                "page_count": get_file_info(conn, "page_count", 0),
                "pages_scanned": 0,
                "pages_empty": 0,
                "pages_failed": 0,
                "hits": 0,
                "elapsed_sec": round(time.time() - started, 1),
            }
        set_file_info(conn, "src_sig", src_sig)
        set_file_info(conn, "chunk_size", chunk_size)
        set_file_info(conn, "extractor_version", EXTRACTOR_VERSION)

        text_layer = get_file_info(conn, "has_text_layer")
        if text_layer is None:
            text_layer = has_text_layer(pdf_path)
            set_file_info(conn, "has_text_layer", text_layer)
        conn.commit()

        if not text_layer:
            set_file_info(conn, "page_count", 0)
            set_file_info(conn, "status", "skipped_no_text_layer")
            conn.commit()
            return {
                "file": pdf_path.name,
                "status": "skipped_no_text_layer",
                "page_count": 0,
                "pages_scanned": 0,
                "pages_empty": 0,
                "pages_failed": 0,
                "hits": 0,
                "elapsed_sec": round(time.time() - started, 1),
            }

        done_chunks = {
            (r[0], r[1])
            for r in conn.execute("SELECT start_page, end_page FROM chunks_done")
        }

        # The parent process already compiled (and warned about) the
        # gazetteer; workers reload from the on-disk compile cache.
        gaz_names = load_gazetteer(gazetteer_paths, cache_dir, quiet=True)
        denylist = load_gazetteer(denylist_paths, cache_dir, quiet=True,
                                  label="denylist")
        ambiguous = load_gazetteer(ambiguous_paths, cache_dir, quiet=True,
                                   label="ambiguous")
        analyzer = build_batch_analyzer(model_name, gaz_names, gazetteer_score,
                                        ambiguous)

        # Ask the pipeline for ORGANIZATION/LOCATION alongside PERSON.
        # These are never reported as names -- they are filtered out
        # below -- but a PERSON span sitting on top of an ORGANIZATION
        # span is the model contradicting itself, and that is far better
        # evidence than any word list. It costs nothing: the same spaCy
        # doc is already parsed, so the extra labels are free.
        target_entities = list(entities)
        engine = getattr(analyzer, "analyzer_engine", None)
        supported = set()
        if engine is not None:
            try:
                supported = set(engine.get_supported_entities(language="en"))
            except Exception:
                supported = set()
        scan_entities = target_entities + [
            e for e in ORG_SUPPRESSOR_ENTITIES
            if e in supported and e not in target_entities
        ]

        pages_scanned = 0
        pages_empty = 0
        pages_failed = 0
        total_hits = 0

        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            set_file_info(conn, "page_count", page_count)
            conn.commit()

            for start in range(1, page_count + 1, chunk_size):
                end = min(start + chunk_size - 1, page_count)
                if (start, end) in done_chunks:
                    continue

                # Per-unit analysis: (analysis_text, kind, source_text,
                # col_role) tuples from extract_page_units, plus the page each unit
                # came from (page attribution is unchanged: every hit still
                # records its page_num) and the page-level text for context
                # windows.
                texts = []
                unit_meta = []        # (page_num, kind, source_text, col_role)
                page_texts = {}       # page_num -> full page text
                pages_with_units = set()
                chunk_problems = []

                for page_num in range(start, end + 1):
                    page = pdf.pages[page_num - 1]
                    try:
                        text = page.extract_text() or ""
                    except Exception as exc:
                        # One malformed page must not kill a 1,000-page run.
                        chunk_problems.append(
                            (page_num, f"extract_error: {type(exc).__name__}")
                        )
                        if hasattr(page, "flush_cache"):
                            page.flush_cache()
                        continue

                    if not text.strip():
                        chunk_problems.append((page_num, "empty_text_layer"))
                        if hasattr(page, "flush_cache"):
                            page.flush_cache()
                        continue

                    # Flag, do not skip. A page whose OCR degenerated
                    # still tends to hold recoverable names, and dropping
                    # it would trade junk rows for real people. The
                    # reviewer is told which pages to distrust; the names
                    # are still extracted.
                    if page_garbage_ratio(text) > PAGE_GARBAGE_RATIO:
                        chunk_problems.append((page_num, "ocr_garbage_suspected"))

                    try:
                        units, _ = extract_page_units(page, text)
                    except Exception as exc:
                        # Table detection failed on this page: fall back to
                        # whole-page extraction (the pre-change behaviour)
                        # and record it, rather than losing the page.
                        chunk_problems.append(
                            (page_num,
                             f"table_fallback: {type(exc).__name__}")
                        )
                        units = [(text, UNIT_PROSE, text, COL_NONE)]
                        for variant, original in uppercase_variants(text):
                            units.append(
                                (variant, UNIT_TRUECASE, original, COL_NONE))
                    finally:
                        # pdfplumber caches parsed layout objects per page.
                        # Left alone, that cache grows monotonically across a
                        # 1,000-page file and defeats the point of chunking.
                        if hasattr(page, "flush_cache"):
                            page.flush_cache()

                    page_texts[page_num] = text
                    for analysis_text, kind, source_text, col_role in units:
                        texts.append(analysis_text)
                        unit_meta.append((page_num, kind, source_text, col_role))
                    if units:
                        pages_with_units.add(page_num)

                chunk_hits = []
                if texts:
                    # Batch the chunk through the pipeline in one call rather
                    # than paying per-page pipeline overhead. batch_size lets
                    # spaCy's pipe() process several pages at once; older
                    # presidio-analyzer releases lack the parameter, so fall
                    # back gracefully.
                    try:
                        results_per_page = analyzer.analyze_iterator(
                            texts=texts, language="en", entities=scan_entities,
                            batch_size=batch_size,
                        )
                    except TypeError:
                        results_per_page = analyzer.analyze_iterator(
                            texts=texts, language="en", entities=scan_entities,
                        )
                    # Two passes over the per-unit results: original-casing
                    # units first, then truecase variants, so a variant hit
                    # can be dropped when the same name was already found on
                    # the same page in its source casing (occurrence_count
                    # must not inflate).
                    resolved = []
                    for (page_num, kind, source_text, col_role), analysis_text, results in zip(
                            unit_meta, texts, results_per_page):
                        # Suppressor entities never become rows; they only
                        # testify against the PERSON spans they cover.
                        wanted = [r for r in results
                                  if r.entity_type in target_entities]
                        suppressors = [r for r in results
                                       if r.entity_type not in target_entities]
                        for r, recognizer in resolve_overlaps(wanted):
                            overlaps = sorted({
                                s.entity_type for s in suppressors
                                if s.start < r.end and r.start < s.end
                            })
                            resolved.append(
                                (page_num, kind, source_text, col_role,
                                 analysis_text, r, recognizer, overlaps)
                            )

                    # Scoring runs in two passes. The first resolves each
                    # detection to its candidate context spans; the second
                    # binds it. They stay separate because minor_verdict()
                    # needs to know where the OTHER names on the page sit,
                    # for coordination inheritance ("the minor children, X
                    # and Y") in _inherits_minor_binding.
                    pending = []
                    # id(text) -> (text, [spans]). Holding the text in the
                    # value keeps it alive, so the id stays valid.
                    span_registry = {}

                    seen_on_page = set()
                    for pass_kind in ("original", "variant"):
                        for (page_num, kind, source_text, col_role,
                             analysis_text, r, recognizer, overlaps) in resolved:
                            is_variant = kind in UNIT_VARIANT_KINDS
                            if is_variant != (pass_kind == "variant"):
                                continue
                            # Truecasing preserves char positions, so the
                            # span maps 1:1 onto the original line and the
                            # reported name keeps its source casing. A
                            # flipped unit is its own source text, so the
                            # span maps onto the reordered string.
                            raw = source_text[r.start:r.end]
                            surface = normalize_name(raw)
                            # Trim OCR debris and correspondence
                            # boilerplate BEFORE keying, so "Hello Sarah",
                            # "Dr. Sarah" and "Sarah†" all collapse onto
                            # the one Sarah instead of splitting her
                            # occurrence count three ways. The untrimmed
                            # surface is cached alongside so the edit is
                            # auditable from the CSV.
                            cleaned, _trim_notes = sanitize_surface(raw)
                            key = dedup_key(cleaned or surface)
                            if not key:
                                continue
                            # Variant units re-analyse text already seen in
                            # another form. Suppress a variant hit when the
                            # same name was found on this page in its source
                            # form, so occurrence_count stays honest.
                            if is_variant and (page_num, key) in seen_on_page:
                                continue
                            seen_on_page.add((page_num, key))
                            name = cleaned or surface

                            # Context windows need page-level text (role
                            # labels usually sit in a neighbouring cell).
                            # Prose units ARE page-scale text, so use their
                            # own offsets; for cells and truecase lines,
                            # relocate the span in the page text -- EVERY
                            # occurrence, not just the first -- and fall back
                            # to the unit itself if that fails. See
                            # extract_page_units docstring for why this was
                            # chosen over an offset map.
                            if kind == UNIT_PROSE:
                                candidates = [(analysis_text, r.start, r.end)]
                            else:
                                page_text = page_texts.get(page_num, "")
                                located = locate_all_in_text(page_text, raw)
                                candidates = [
                                    (page_text, s, e) for s, e in located
                                ] or [(source_text, r.start, r.end)]

                            for ctx_text, ctx_start, ctx_end in candidates:
                                entry = span_registry.setdefault(
                                    id(ctx_text), (ctx_text, []))
                                entry[1].append((ctx_start, ctx_end))

                            pending.append(
                                (page_num, name, r, recognizer, candidates,
                                 col_role, overlaps, surface))

                    for (page_num, name, r, recognizer, candidates, col_role,
                         overlaps, surface) in pending:
                        # Take the strongest binding found across every
                        # occurrence of this name on the page. A child named
                        # once in a roster and mentioned nowhere else must
                        # not be averaged away by the mentions that carry no
                        # binding at all.
                        boost = 0.0
                        reasons = set()
                        binding = BIND_NONE
                        for ctx_text, ctx_start, ctx_end in candidates:
                            boost = max(
                                boost, context_boost(ctx_text, ctx_start, ctx_end))
                            these = minor_reasons(ctx_text, ctx_start, ctx_end)
                            reasons.update(these)
                            spans = span_registry[id(ctx_text)][1]
                            this_bind = minor_verdict(
                                ctx_text, spans, ctx_start, ctx_end, these,
                                col_role)
                            if BIND_ORDER[this_bind] > BIND_ORDER[binding]:
                                binding = this_bind

                        # Reasons are evidence for a flag, not a flag in
                        # themselves: only report them when something
                        # actually bound, or the column becomes noise again.
                        if binding == BIND_NONE:
                            reasons = set()

                        # Suppression is decided per occurrence and the
                        # MOST FORGIVING verdict wins: one appearance that
                        # reads as a real person keeps the name in the
                        # report, even if fifty others read as an eponym.
                        # "Bell" following "syndrome" on page 4 does not
                        # erase Nurse Bell on page 9.
                        suppress_reason, suppress_detail = SUPPRESS_NONE, ""
                        probes = candidates or [("", 0, 0)]
                        for ctx_text, ctx_start, ctx_end in probes:
                            suppress_reason, suppress_detail = suppress_verdict(
                                name, ctx_text, ctx_start, ctx_end,
                                overlaps, denylist)
                            if not suppress_reason:
                                break

                        composite = float(r.score) + boost + shape_adjustment(name)
                        chunk_hits.append((
                            page_num,
                            name,
                            float(r.score),
                            max(0.0, min(1.0, composite)),
                            r.entity_type,
                            int(binding != BIND_NONE),
                            BIND_TIER[binding],
                            "; ".join(sorted(reasons))[:500],
                            recognizer,
                            binding,
                            int(bool(suppress_reason)),
                            (f"{suppress_reason}|{suppress_detail}"
                             if suppress_reason else "")[:200],
                            surface[:200],
                        ))

                # Hits, problems, and the checkpoint land in one transaction,
                # so a chunk is never recorded half-finished.
                with conn:
                    if chunk_hits:
                        conn.executemany(
                            "INSERT INTO hits "
                            "(page_num, raw_name, score, boosted, entity, "
                            "minor_ctx, minor_tier, minor_reason, recognizer, "
                            "minor_binding, suppressed, suppress_reason, "
                            "raw_surface) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            chunk_hits,
                        )
                    if chunk_problems:
                        conn.executemany(
                            "INSERT OR REPLACE INTO page_problems (page_num, reason) "
                            "VALUES (?, ?)",
                            chunk_problems,
                        )
                    conn.execute(
                        "INSERT OR IGNORE INTO chunks_done (start_page, end_page) "
                        "VALUES (?, ?)",
                        (start, end),
                    )

                # len(texts) now counts units, not pages; count pages that
                # produced at least one unit. A table_fallback page was
                # still scanned, so it is informational, not a failure.
                pages_scanned += len(pages_with_units)
                pages_empty += sum(1 for _, r in chunk_problems if r == "empty_text_layer")
                # ocr_garbage_suspected is informational like
                # table_fallback: the page was scanned and its names were
                # kept, it just needs a reviewer's suspicion.
                pages_failed += sum(
                    1 for _, r in chunk_problems
                    if r not in ("empty_text_layer", "ocr_garbage_suspected")
                    and not r.startswith("table_fallback")
                )
                total_hits += len(chunk_hits)

                n_suppressed = sum(1 for h in chunk_hits if h[10])
                print(
                    f"  [{pdf_path.name}] pages {start}-{end} of {page_count} "
                    f"-> {len(chunk_hits)} hits "
                    f"({n_suppressed} suppressed)",
                    file=sys.stderr, flush=True,
                )

        set_file_info(conn, "status", "complete")
        conn.commit()

        return {
            "file": pdf_path.name,
            "status": "complete",
            "page_count": page_count,
            "pages_scanned": pages_scanned,
            "pages_empty": pages_empty,
            "pages_failed": pages_failed,
            "hits": total_hits,
            "elapsed_sec": round(time.time() - started, 1),
        }

    except Exception as exc:
        message = f"error: {type(exc).__name__}: {exc}"
        try:
            set_file_info(conn, "status", message)
            conn.commit()
        except sqlite3.Error:
            pass
        return {
            "file": pdf_path.name,
            "status": message,
            "page_count": page_count,
            "pages_scanned": 0,
            "pages_empty": 0,
            "pages_failed": 0,
            "hits": 0,
            "elapsed_sec": round(time.time() - started, 1),
        }
    finally:
        conn.close()


def run_scan(args) -> list:
    pdf_paths = sorted(args.pdf_folder.glob("*.pdf"))
    if not pdf_paths:
        print(f"No PDFs found in {args.pdf_folder}", file=sys.stderr)
        sys.exit(1)

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    entities = [e.strip() for e in args.entities.split(",") if e.strip()]

    # Compile the gazetteer once here so (a) the "no gazetteer" warning
    # prints exactly once, and (b) workers hit the on-disk compile cache
    # instead of each re-parsing ~160k lines.
    gazetteer_paths = [str(p) for p in (getattr(args, "gazetteer", None) or [])]
    gaz_names = load_gazetteer(gazetteer_paths, args.cache_dir, quiet=False)
    if gaz_names:
        print(
            f"Gazetteer recogniser active: {len(gaz_names)} dictionary "
            f"entries, score {args.gazetteer_score}.",
            file=sys.stderr,
        )
    elif not gazetteer_paths:
        print(
            "No --gazetteer supplied; dictionary recogniser disabled "
            "(running exactly as before).",
            file=sys.stderr,
        )
    if not gaz_names:
        gazetteer_paths = []

    # Optional token denylist, compiled through the same cached path as
    # the gazetteer. This is the escape hatch for corpus-specific noise
    # the built-in rules cannot know about -- drug names in a pharmacy
    # corpus, ship names in a maritime one -- so tuning it never requires
    # editing the source.
    denylist_paths = [str(p) for p in (getattr(args, "denylist", None) or [])]
    deny_names = load_gazetteer(denylist_paths, args.cache_dir, quiet=False,
                                label="denylist")
    if deny_names:
        print(
            f"Denylist active: {len(deny_names)} tokens. A hit is suppressed "
            f"only when EVERY token of the span is listed.",
            file=sys.stderr,
        )
    else:
        denylist_paths = []

    # The ambiguous tier. Not a second dictionary -- a demotion list
    # applied to the first one. See find_gazetteer_spans for why these
    # tokens cannot simply be deleted from the gazetteer.
    ambiguous_paths = [str(p) for p in (getattr(args, "gazetteer_ambiguous", None) or [])]
    ambig_names = load_gazetteer(ambiguous_paths, args.cache_dir, quiet=False,
                                 label="ambiguous")
    if ambig_names:
        overlap = len(ambig_names & gaz_names) if gaz_names else 0
        print(
            f"Ambiguous tier active: {len(ambig_names)} tokens "
            f"({overlap} of them present in the gazetteer). A dictionary "
            f"span needs at least one match outside this list.",
            file=sys.stderr,
        )
        if gaz_names and not overlap:
            print(
                "  WARNING: no ambiguous token appears in the gazetteer. "
                "The tier will have no effect -- check the files match.",
                file=sys.stderr,
            )
    else:
        ambiguous_paths = []

    print(
        f"Scanning {len(pdf_paths)} PDF(s) with {args.workers} worker(s), "
        f"chunk size {args.chunk_size}...",
        file=sys.stderr,
    )

    summaries = []
    if args.workers <= 1:
        for p in pdf_paths:
            summaries.append(scan_one_pdf(
                str(p), str(args.cache_dir), args.chunk_size,
                args.model, entities, args.force, args.batch_size,
                gazetteer_paths, args.gazetteer_score, denylist_paths,
                ambiguous_paths,
            ))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    scan_one_pdf, str(p), str(args.cache_dir), args.chunk_size,
                    args.model, entities, args.force, args.batch_size,
                    gazetteer_paths, args.gazetteer_score, denylist_paths,
                    ambiguous_paths,
                ): p
                for p in pdf_paths
            }
            for fut in as_completed(futures):
                # scan_one_pdf catches most exceptions internally, but a
                # failure before its try block (or a worker killed by the
                # OOM reaper) would otherwise abort every remaining file.
                try:
                    summaries.append(fut.result())
                except Exception as exc:
                    summaries.append({
                        "file": futures[fut].name,
                        "status": f"worker_failed: {type(exc).__name__}: {exc}",
                        "page_count": 0, "pages_scanned": 0, "pages_empty": 0,
                        "pages_failed": 0, "hits": 0, "elapsed_sec": 0.0,
                    })

    summaries.sort(key=lambda s: s["file"])
    print("", file=sys.stderr)
    for s in summaries:
        print(
            f"{s['file']}: {s['status']} "
            f"({s['pages_scanned']}/{s['page_count']} pages, "
            f"{s['hits']} hits, {s['elapsed_sec']}s)",
            file=sys.stderr,
        )
    return summaries


# ---------------------------------------------------------------------
# Phase 2 -- report
# ---------------------------------------------------------------------
def tier_for(confidence: float, certain: float, light: float) -> str:
    if confidence >= certain:
        return TIER_CERTAIN
    if confidence >= light:
        return TIER_LIGHT
    return TIER_EXTENSIVE


def load_cache(cache_dir: Path):
    """
    Merge every shard into one record set:
        key -> {display, max_confidence, locations: [(file, page), ...]}
    Also returns per-file stats for the manifest.
    """
    records = defaultdict(
        lambda: {
            "display": "",
            "max_confidence": 0.0,
            "locations": [],
            "possible_minor": False,
            "minor_tier": MINOR_TIER_NONE,
            "minor_binding": BIND_NONE,
            "minor_reasons": set(),
            "recognizers": set(),
            # A record is suppressed only when EVERY occurrence of it was
            # suppressed. One clean sighting rescues the name: a string
            # that reads as an eponym on page 4 and as a nurse on page 9
            # belongs in the report.
            "hit_count": 0,
            "suppressed_hits": 0,
            "suppress_reasons": set(),
            "surfaces": set(),
            "merged_forms": set(),
        }
    )
    file_stats = []

    shards = sorted(cache_dir.glob("*.db"))
    if not shards:
        print(
            f"No cache shards found in {cache_dir}. Run the scan phase first.",
            file=sys.stderr,
        )
        sys.exit(1)

    for shard in shards:
        conn = sqlite3.connect(shard)
        try:
            pdf_name = get_file_info(conn, "source_name") or shard.stem
            status = get_file_info(conn, "status", "unknown")
            page_count = get_file_info(conn, "page_count", 0)

            problem_counts = {
                reason: n
                for reason, n in conn.execute(
                    "SELECT reason, COUNT(*) FROM page_problems GROUP BY reason"
                )
            }
            chunks = conn.execute("SELECT COUNT(*) FROM chunks_done").fetchone()[0]
            hit_count = conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0]

            # Shards written before the possible_minor feature lack the
            # column; this reader opens shards directly (no open_shard
            # migration), so select defensively.
            hit_cols = {row[1] for row in conn.execute("PRAGMA table_info(hits)")}
            minor_expr = "minor_ctx" if "minor_ctx" in hit_cols else "0"
            recog_expr = "recognizer" if "recognizer" in hit_cols else "'spacy'"
            tier_expr = "minor_tier" if "minor_tier" in hit_cols else "''"
            reason_expr = "minor_reason" if "minor_reason" in hit_cols else "''"
            bind_expr = "minor_binding" if "minor_binding" in hit_cols else "''"
            supp_expr = "suppressed" if "suppressed" in hit_cols else "0"
            supp_reason_expr = (
                "suppress_reason" if "suppress_reason" in hit_cols else "''")
            surface_expr = "raw_surface" if "raw_surface" in hit_cols else "''"

            for (page_num, raw_name, boosted, minor, tier, reason,
                 recognizer, binding, suppressed, suppress_reason,
                 surface) in conn.execute(
                f"SELECT page_num, raw_name, boosted, {minor_expr}, "
                f"{tier_expr}, {reason_expr}, {recog_expr}, {bind_expr}, "
                f"{supp_expr}, {supp_reason_expr}, {surface_expr} FROM hits"
            ):
                key = dedup_key(raw_name)
                if not key:
                    continue
                rec = records[key]
                # Prefer the longest surface form seen as the display name --
                # "Jonathan A. Reyes" over a later bare "Reyes".
                if len(raw_name) > len(rec["display"]):
                    rec["display"] = raw_name
                if boosted > rec["max_confidence"]:
                    rec["max_confidence"] = boosted
                if minor:
                    rec["possible_minor"] = True
                # Strongest binding seen anywhere wins, and reasons union
                # across occurrences: a child named once in fifty thousand
                # pages must not be averaged away by the pages where the
                # same string happens to belong to an adult.
                if BIND_ORDER.get(binding, 0) > BIND_ORDER[rec["minor_binding"]]:
                    rec["minor_binding"] = binding
                if MINOR_TIER_ORDER.get(tier, 0) > MINOR_TIER_ORDER[rec["minor_tier"]]:
                    rec["minor_tier"] = tier
                if reason:
                    rec["minor_reasons"].update(reason.split("; "))
                rec["recognizers"].add(recognizer)
                rec["locations"].append((pdf_name, page_num))
                rec["hit_count"] += 1
                if suppressed:
                    rec["suppressed_hits"] += 1
                    if suppress_reason:
                        rec["suppress_reasons"].add(suppress_reason)
                if surface and normalize_name(surface) != raw_name:
                    rec["surfaces"].add(surface)

            file_stats.append({
                "file": pdf_name,
                "status": status,
                "page_count": page_count,
                "chunks_completed": chunks,
                "raw_hits": hit_count,
                "page_problems": problem_counts,
            })
        finally:
            conn.close()

    merge_inverted_records(records)
    return records, file_stats


def merge_inverted_records(records) -> int:
    """
    Collapse "Smith, John" into "John Smith" -- but only when BOTH forms
    were actually observed in the corpus.

    Detecting the inverted form was never the whole problem. Even with
    both forms detected, dedup_key() gave them separate rows, separate
    occurrence counts and separate minor bindings, so a child caught as
    "Kowalska, Zofia" in a roster column (binding=column) and as "Zofia
    Kowalska" in the narrative (binding=none) appeared twice, flagged
    once. flag_possible_duplicates() noticed the pair -- token_sort_ratio
    scores it about 96 -- but only ever advised; nothing merged.

    The both-forms-observed condition is what keeps this safe. Flipping
    is genuinely ambiguous for names plausible in either order ("Thomas,
    James"), so a lone surname-first string is left exactly as it is and
    reported under its own name. Only a demonstrated pair merges, and the
    surface form that was folded in is reported in `also_reported_as`, so
    a merge can be undone by eye.

    Returns the number of records merged away.
    """
    merged = 0
    for key in list(records.keys()):
        if key not in records:          # already consumed by a prior merge
            continue
        display = records[key]["display"] or key
        flipped = flipped_name(display)
        if not flipped:
            continue
        target = dedup_key(flipped)
        if target == key or target not in records:
            continue

        src = records.pop(key)
        dst = records[target]
        dst["locations"].extend(src["locations"])
        dst["max_confidence"] = max(dst["max_confidence"], src["max_confidence"])
        dst["possible_minor"] = dst["possible_minor"] or src["possible_minor"]
        if BIND_ORDER.get(src["minor_binding"], 0) > BIND_ORDER.get(
                dst["minor_binding"], 0):
            dst["minor_binding"] = src["minor_binding"]
        if MINOR_TIER_ORDER.get(src["minor_tier"], 0) > MINOR_TIER_ORDER.get(
                dst["minor_tier"], 0):
            dst["minor_tier"] = src["minor_tier"]
        dst["minor_reasons"].update(src["minor_reasons"])
        dst["recognizers"].update(src["recognizers"])
        dst["surfaces"].update(src["surfaces"])
        dst["merged_forms"].update(src["merged_forms"])
        dst["merged_forms"].add(src["display"] or key)
        dst["hit_count"] += src["hit_count"]
        dst["suppressed_hits"] += src["suppressed_hits"]
        dst["suppress_reasons"].update(src["suppress_reasons"])
        # Keep the given-name-first form as the display name even when the
        # surname-first surface string is longer: a report is read by
        # humans, and "John Smith" is the form they will search for.
        merged += 1
    return merged


def flag_possible_duplicates(records, threshold: int, enabled: bool):
    """
    Flag likely variants of one person ("J. Martinez" vs "Jose Martinez")
    for a reviewer to confirm. Deliberately advisory -- merging these
    automatically risks conflating two different people.

    Uses rapidfuzz's cdist in row blocks so memory stays bounded even with
    tens of thousands of unique names.
    """
    if not enabled or len(records) < 2:
        return {}

    from rapidfuzz import fuzz, process
    import numpy as np

    keys = list(records.keys())
    n = len(keys)
    flags = defaultdict(set)
    block = 1000

    for i in range(0, n, block):
        rows = keys[i:i + block]
        matrix = process.cdist(
            rows, keys,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=threshold,
            dtype=np.uint8,
            workers=-1,
        )
        for local_idx, row in enumerate(matrix):
            global_idx = i + local_idx
            for col_idx in np.nonzero(row)[0]:
                col_idx = int(col_idx)
                if col_idx == global_idx:
                    continue
                flags[keys[global_idx]].add(records[keys[col_idx]]["display"])

    return flags


def compress_page_list(pages) -> str:
    """[3, 4, 5, 9] -> 'p3-p5,p9'. Keeps location cells readable and, for
    names on hundreds of pages, under Excel's 32,767-character cell cap."""
    pages = sorted(set(pages))
    parts = []
    run_start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        parts.append(f"p{run_start}" if run_start == prev else f"p{run_start}-p{prev}")
        run_start = prev = p
    parts.append(f"p{run_start}" if run_start == prev else f"p{run_start}-p{prev}")
    return ",".join(parts)


def csv_safe(text: str) -> str:
    """Neutralize spreadsheet formula injection: a 'name' extracted from a
    hostile or garbled PDF could begin with =, +, -, or @ and execute as a
    formula when the CSV is opened in Excel."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def write_report(records, flags, file_stats, args, scan_summaries):
    rows = []
    for key, rec in records.items():
        conf = rec["max_confidence"]
        by_file = defaultdict(list)
        for fn, pg in rec["locations"]:
            by_file[fn].append(pg)
        recogs = rec.get("recognizers") or {"spacy"}
        if "both" in recogs or len(recogs - {"both"}) > 1:
            recognizer = "both"
        else:
            recognizer = next(iter(recogs))
        # Suppressed only if every occurrence was suppressed. See the
        # note on the records defaultdict in load_cache.
        hits = rec.get("hit_count", 0)
        suppressed = bool(hits) and rec.get("suppressed_hits", 0) >= hits
        rows.append({
            "name": csv_safe(rec["display"]),
            "tier": tier_for(conf, args.certain_threshold, args.light_threshold),
            "confidence": round(conf, 3),
            "possible_minor": "yes" if rec["possible_minor"] else "",
            "minor_tier": rec["minor_tier"],
            "minor_binding": rec["minor_binding"],
            "minor_reason": "; ".join(sorted(rec["minor_reasons"]))[:500],
            "occurrence_count": len(rec["locations"]),
            "file_count": len(by_file),
            "locations": "; ".join(
                f"{fn}:{compress_page_list(pgs)}" for fn, pgs in sorted(by_file.items())
            ),
            "possible_duplicate_of": ", ".join(
                csv_safe(d) for d in sorted(flags.get(key, ()))
            ),
            "recognizer": recognizer,
            "suppressed": "yes" if suppressed else "",
            "suppress_reason": "; ".join(sorted(rec.get("suppress_reasons", ())))[:300]
                               if suppressed else "",
            "also_reported_as": ", ".join(
                csv_safe(v) for v in sorted(
                    set(rec.get("merged_forms", ())) | set(rec.get("surfaces", ()))
                )
            )[:500],
        })

    tier_order = {TIER_CERTAIN: 0, TIER_LIGHT: 1, TIER_EXTENSIVE: 2}
    rows.sort(key=lambda r: (tier_order[r["tier"]], r["name"].lower()))

    include_suppressed = getattr(args, "include_suppressed", False)
    if include_suppressed:
        main_rows, held_rows = rows, []
    else:
        main_rows = [r for r in rows if not r["suppressed"]]
        held_rows = [r for r in rows if r["suppressed"]]

    fieldnames = [
        "name", "tier", "confidence", "possible_minor",
        "occurrence_count", "file_count", "locations",
        "possible_duplicate_of", "recognizer",
        "minor_tier", "minor_binding", "minor_reason",
        # v5 columns, appended last so existing positional consumers of
        # the CSV keep working.
        "suppressed", "suppress_reason", "also_reported_as",
    ]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(main_rows)

    # The sidecar is written unconditionally, even when empty. Suppression
    # is a judgement call this tool makes on the reviewer's behalf, and a
    # reviewer who cannot see what was withheld cannot check the judgement
    # -- so the withheld rows ship with the report, with their reasons, in
    # the same directory, every run.
    suppressed_path = args.out.with_suffix(".suppressed.csv")
    with open(suppressed_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(held_rows)

    tier_counts = defaultdict(int)
    recognizer_counts = defaultdict(int)
    for r in main_rows:
        tier_counts[r["tier"]] += 1
        recognizer_counts[r["recognizer"]] += 1

    suppress_counts = defaultdict(int)
    for r in held_rows:
        suppress_counts[r["suppress_reason"].split("|", 1)[0] or "unknown"] += 1

    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "output_csv": str(args.out),
        "thresholds": {
            "certain": args.certain_threshold,
            "light": args.light_threshold,
            "fuzzy_similarity": args.fuzzy_threshold,
            "fuzzy_enabled": not args.no_fuzzy,
        },
        "suppressed_csv": str(args.out.with_suffix(".suppressed.csv")),
        "suppression": {
            "include_suppressed": include_suppressed,
            "names_withheld": len(held_rows),
            "by_reason": dict(suppress_counts),
            "occurrences_withheld": sum(r["occurrence_count"] for r in held_rows),
            "possible_minor_withheld": sum(
                1 for r in held_rows if r["possible_minor"]),
        },
        "totals": {
            "unique_names": len(main_rows),
            "total_occurrences": sum(r["occurrence_count"] for r in main_rows),
            "possible_minor_names": sum(
                1 for r in main_rows if r["possible_minor"]),
            "by_minor_tier": {
                t: sum(1 for r in main_rows if r["minor_tier"] == t)
                for t in (MINOR_TIER_HIGH, MINOR_TIER_MEDIUM, MINOR_TIER_LOW)
            },
            "by_minor_binding": {
                b: sum(1 for r in main_rows if r["minor_binding"] == b)
                for b in (BIND_COLUMN, BIND_LABEL, BIND_DOB, BIND_RELATION)
            },
            "pages_in_corpus": sum(fs["page_count"] for fs in file_stats),
            "by_tier": dict(tier_counts),
            "by_recognizer": dict(recognizer_counts),
        },
        "files": file_stats,
        "scan_summaries": scan_summaries,
    }

    manifest_path = args.out.with_suffix(".manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    minor_high = sum(1 for r in main_rows if r["minor_tier"] == MINOR_TIER_HIGH)
    minor_any = sum(1 for r in main_rows if r["possible_minor"])
    bind_counts = ", ".join(
        f"{b}={sum(1 for r in main_rows if r['minor_binding'] == b)}"
        for b in (BIND_COLUMN, BIND_LABEL, BIND_DOB, BIND_RELATION)
    )
    supp_line = ", ".join(
        f"{k}={v}" for k, v in sorted(suppress_counts.items())
    ) or "none"
    minor_withheld = sum(1 for r in held_rows if r["possible_minor"])
    print(
        f"\n{len(main_rows)} unique names -> {args.out}\n"
        f"  {TIER_CERTAIN}: {tier_counts[TIER_CERTAIN]}\n"
        f"  {TIER_LIGHT}: {tier_counts[TIER_LIGHT]}\n"
        f"  {TIER_EXTENSIVE}: {tier_counts[TIER_EXTENSIVE]}\n"
        f"possible_minor: {minor_any} ({minor_high} at minor_tier=high; "
        f"start review there)\n"
        f"  bindings: {bind_counts}\n"
        f"{len(held_rows)} name(s) withheld -> {suppressed_path}\n"
        f"  reasons: {supp_line}\n"
        + (f"  WARNING: {minor_withheld} withheld row(s) carried a "
           f"possible_minor flag -- read the sidecar before filing.\n"
           if minor_withheld else "")
        + f"Manifest -> {manifest_path}",
        file=sys.stderr,
    )


def run_report(args, scan_summaries=None):
    records, file_stats = load_cache(args.cache_dir)
    flags = flag_possible_duplicates(records, args.fuzzy_threshold, not args.no_fuzzy)
    write_report(records, flags, file_stats, args, scan_summaries or [])


# ---------------------------------------------------------------------
# Clear -- wipe cached detections when starting a new project
# ---------------------------------------------------------------------
def run_clear(args):
    """
    Delete every cache shard under --cache-dir. Deliberately surgical:
    only *.db files (plus their SQLite journal leftovers) are removed,
    never an arbitrary directory tree, so a mistyped --cache-dir can't
    vaporize unrelated files. Prompts for confirmation unless --yes.
    """
    cache_dir = args.cache_dir
    if not cache_dir.is_dir():
        print(f"Nothing to clear: {cache_dir} does not exist.", file=sys.stderr)
        return

    shards = sorted(cache_dir.glob("*.db"))
    if not shards:
        print(f"Nothing to clear: no cache shards in {cache_dir}.", file=sys.stderr)
        return

    # Show what is about to be deleted -- the source PDF each shard came
    # from and how many cached detections it holds -- so the confirmation
    # is informed rather than a blind y/N.
    print(f"{len(shards)} cache shard(s) in {cache_dir}:", file=sys.stderr)
    total_hits = 0
    for s in shards:
        label = s.name
        try:
            conn = sqlite3.connect(s)
            src = get_file_info(conn, "source_name")
            hits = conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
            conn.close()
            total_hits += hits
            if src:
                label = f"{src} ({hits} cached detections)"
        except sqlite3.Error:
            label = f"{s.name} (unreadable -- will still be deleted)"
        print(f"  - {label}", file=sys.stderr)

    if not args.yes:
        reply = input(
            f"Delete these {len(shards)} shard(s) "
            f"({total_hits} cached detections)? [y/N] "
        ).strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted; nothing deleted.", file=sys.stderr)
            return

    for s in shards:
        s.unlink()
        # SQLite side files from interrupted runs
        for suffix in ("-wal", "-shm", "-journal"):
            side = s.with_name(s.name + suffix)
            if side.exists():
                side.unlink()

    try:
        cache_dir.rmdir()  # succeeds only if now empty; harmless otherwise
        removed_dir = True
    except OSError:
        removed_dir = False

    print(
        f"Cleared {len(shards)} shard(s)"
        + (f"; removed empty {cache_dir}" if removed_dir else "")
        + ". Ready for a fresh scan.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def add_scan_args(p):
    p.add_argument("pdf_folder", type=Path, help="Folder containing the PDFs")
    p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                   help="Pages per batch/checkpoint (default: %(default)s)")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help="Pages fed to spaCy's pipe() at once within a chunk "
                        "(default: %(default)s)")
    p.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1),
                   help="Parallel processes, one PDF each (default: %(default)s)")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="spaCy model (default: %(default)s)")
    p.add_argument("--entities", default="PERSON",
                   help="Comma-separated Presidio entity types (default: %(default)s)")
    p.add_argument("--force", action="store_true",
                   help="Discard existing cache and rescan from scratch")
    p.add_argument("--gazetteer", action="append", type=Path, default=None,
                   metavar="PATH",
                   help="Name dictionary file (US Census surnames CSV, SSA "
                        "given-name file, or one-name-per-line .txt) or a "
                        "directory of such files. Repeatable. Omit to run "
                        "without the dictionary recogniser.")
    p.add_argument("--gazetteer-ambiguous", action="append", type=Path,
                   default=None, metavar="PATH",
                   help="File of tokens that are both real names and common "
                        "English words (White, King, Green, Hill, Bell, "
                        "Church). These STAY in the gazetteer -- they are "
                        "among the most common surnames in the country -- but "
                        "a dictionary span made entirely of them is rejected, "
                        "so 'White House' and 'Green Bay' stop firing while "
                        "'Georgia Bell' still does. Generate with "
                        "prepare_gazetteer.py.")
    p.add_argument("--denylist", action="append", type=Path, default=None,
                   metavar="PATH",
                   help="Token denylist file (one token per line, or CSV "
                        "with the token in the first column) or a directory "
                        "of such files. Repeatable. A hit is suppressed only "
                        "when EVERY token of the span appears in the list, so "
                        "listing 'hospital' cannot take a person surnamed "
                        "Hospital with it. Use for corpus-specific noise the "
                        "built-in rules cannot know about -- drug names, "
                        "product lines, vessel names.")
    p.add_argument("--gazetteer-score", type=float,
                   default=DEFAULT_GAZETTEER_SCORE,
                   help="Score for gazetteer-only detections "
                        "(default: %(default)s, which lands them in "
                        "light_review and can never reach "
                        "essentially_certain)")


def add_report_args(p):
    p.add_argument("--out", type=Path, default=Path("names_report.csv"))
    p.add_argument("--certain-threshold", type=float, default=DEFAULT_CERTAIN_THRESHOLD)
    p.add_argument("--light-threshold", type=float, default=DEFAULT_LIGHT_THRESHOLD)
    p.add_argument("--fuzzy-threshold", type=int, default=DEFAULT_FUZZY_THRESHOLD,
                   help="Similarity 0-100 for duplicate flagging (default: %(default)s)")
    p.add_argument("--no-fuzzy", action="store_true",
                   help="Skip duplicate flagging entirely")
    p.add_argument("--include-suppressed", action="store_true",
                   help="Put suppressed rows (OCR debris, organizations, "
                        "medical eponyms, bare salutations) back into the "
                        "main CSV, marked with the reason that fired. They "
                        "are ALWAYS written to <out>.suppressed.csv either "
                        "way; this flag only decides whether they also "
                        "appear in the main report.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit a PDF corpus for person names via Presidio.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Phase 1: extract detections to cache")
    add_scan_args(p_scan)
    p_scan.add_argument("--cache-dir", type=Path, default=Path(".name_audit_cache"))

    p_report = sub.add_parser("report", help="Phase 2: build the CSV from cache")
    add_report_args(p_report)
    p_report.add_argument("--cache-dir", type=Path, default=Path(".name_audit_cache"))

    p_all = sub.add_parser("all", help="Run both phases back to back")
    add_scan_args(p_all)
    add_report_args(p_all)
    p_all.add_argument("--cache-dir", type=Path, default=Path(".name_audit_cache"))

    p_clear = sub.add_parser(
        "clear",
        help="Delete all cached detections (e.g. when starting a new project)",
    )
    p_clear.add_argument("--cache-dir", type=Path, default=Path(".name_audit_cache"))
    p_clear.add_argument("--yes", action="store_true",
                         help="Skip the confirmation prompt")

    return parser


def main():
    args = build_parser().parse_args()

    if args.command == "scan":
        run_scan(args)
    elif args.command == "report":
        run_report(args)
    elif args.command == "clear":
        run_clear(args)
    elif args.command == "all":
        summaries = run_scan(args)
        run_report(args, summaries)


if __name__ == "__main__":
    main()
