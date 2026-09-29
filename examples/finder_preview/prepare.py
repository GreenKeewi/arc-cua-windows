"""Build the organizer's synthetic PDF corpus; never run this inside a timed trial.

Requires reportlab (only for fixture creation). No model or desktop access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "output/pdf/finder-preview"
CLIENTS = (
    ("Birchwood Dental", "BD", "Patient booking portal", "Priya Nair", "#315D66"),
    ("Kestrel Logistics", "KL", "Fleet tracking dashboard", "Tom Hayes", "#96502F"),
    ("Marlow Architects", "MA", "Studio website refresh", "Elena Ruiz", "#645782"),
)
SOURCE_NAMES = (
    "scan_008.pdf", "download (3).pdf", "final_v2.pdf", "attachment.pdf", "document (1).pdf",
    "export_04.pdf", "untitled.pdf", "scan_012.pdf", "download.pdf", "notes_final.pdf",
    "attachment (2).pdf", "document.pdf", "scan_003.pdf", "final_FINAL.pdf", "export.pdf",
    "download (7).pdf", "scan_021.pdf", "attachment (5).pdf",
)
KINDS = (("Invoice", "Invoices", "INV"), ("Proposal", "Proposals", "PROP"),
         ("Meeting Notes", "Meeting Notes", "MTG"))


def documents() -> list[dict]:
    result = []
    for cycle in range(2):
        for client_index, (client, code, project, contact, accent) in enumerate(CLIENTS):
            for kind_index, (kind, folder, prefix) in enumerate(KINDS):
                index = len(result)
                day = 2 + client_index * 3 + kind_index * 4
                date = f"2026-{8 + cycle:02d}-{day:02d}"
                reference = f"{code}-{prefix}-{101 + cycle}"
                filename = f"{date} - {client} - {reference}.pdf"
                result.append({
                    "source": SOURCE_NAMES[index], "client": client, "project": project,
                    "contact": contact, "accent": accent, "kind": kind, "date": date,
                    "reference": reference, "cycle": cycle,
                    "destination": f"Organized/{client}/{folder}/{filename}",
                })
    return result


def render_document(doc: dict, path: Path) -> None:
    accent = colors.HexColor(doc["accent"])
    ink = colors.HexColor("#1D2C34")
    muted = colors.HexColor("#65727A")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Brand", fontName="Helvetica-Bold", fontSize=10, textColor=accent,
                              spaceAfter=23, tracking=1.5))
    styles.add(ParagraphStyle("TitleLarge", fontName="Helvetica-Bold", fontSize=28, leading=33,
                              textColor=ink, spaceAfter=12))
    styles.add(ParagraphStyle("Deck", fontSize=12, leading=17, textColor=muted, spaceAfter=25))
    styles.add(ParagraphStyle("LabelSmall", fontName="Helvetica-Bold", fontSize=8, leading=11,
                              textColor=muted, spaceAfter=4))
    styles.add(ParagraphStyle("Value", fontSize=11, leading=15, textColor=ink, spaceAfter=12))
    styles.add(ParagraphStyle("SectionLabel", fontName="Helvetica-Bold", fontSize=11,
                              leading=16, textColor=accent, spaceBefore=15, spaceAfter=7))
    styles.add(ParagraphStyle("Copy", fontSize=10.5, leading=16, textColor=ink, spaceAfter=10))

    def p(text: str, style: str = "Copy") -> Paragraph:
        return Paragraph(escape(text), styles[style])

    def field(label: str, value: str) -> list:
        return [p(label.upper(), "LabelSmall"), p(value, "Value")]

    def section(title: str, text: str) -> list:
        return [p(title, "SectionLabel"), p(text)]

    title = {"Invoice": "Invoice", "Proposal": "Project proposal", "Meeting Notes": "Workshop notes"}[doc["kind"]]
    story = [p("FIELDWORK / CLIENT SERVICES", "Brand"), p(title, "TitleLarge"), p(doc["project"], "Deck")]
    date_label = {"Invoice": "Issue date", "Proposal": "Proposal date", "Meeting Notes": "Meeting date"}[doc["kind"]]
    meta = Table([
        [field("Client", doc["client"]), field(date_label, doc["date"])],
        [field("Reference", doc["reference"]), field("Client contact", doc["contact"])],
    ], colWidths=[3.6 * inch, 2.9 * inch])
    meta.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, -1), (-1, -1), 0.7, colors.HexColor("#DCE2E4")),
    ]))
    story += [meta, Spacer(1, 15)]
    cycle = doc["cycle"]
    if doc["kind"] == "Invoice":
        rows = [
            ["SERVICE", "HOURS", "AMOUNT"],
            ["Discovery and stakeholder interviews" if cycle == 0 else "Prototype review and revisions", "12", "$1,800"],
            ["Experience design" if cycle == 0 else "Implementation handoff", "20", "$3,000"],
            ["Project coordination", "4", "$600"],
            ["TOTAL DUE (USD)", "", "$5,400"],
        ]
        table = Table(rows, colWidths=[4.4 * inch, 0.75 * inch, 1.35 * inch], rowHeights=32)
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 1), (-1, -1), 10),
            ("TEXTCOLOR", (0, 0), (-1, 0), muted), ("TEXTCOLOR", (0, 1), (-1, -1), ink),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#E5E9EB")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F5")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ]))
        story += [table, Spacer(1, 16)]
        story += section("Payment terms", f"Payment is due on 2026-{9 + cycle:02d}-01. Please include {doc['reference']} with your remittance.")
        story += section("Service period", "The charges cover the completed project phase. Scope changes are billed separately after written approval.")
    elif doc["kind"] == "Proposal":
        story += section("Objective", f"Help {doc['client']} deliver a clearer and more usable {doc['project'].lower()}. This proposal covers {'discovery and initial design' if cycle == 0 else 'the next phase of delivery and launch support'}.")
        story += section("Deliverables", "A prioritized journey map, a reviewed interaction prototype, and an implementation handoff with annotated screens and acceptance criteria.")
        story += section("Schedule and investment", "Four weeks from approval. Fixed project fee: USD 12,000. The client will nominate one reviewer and consolidate feedback after each review.")
        story += section("Dependencies", "Work begins after access to the current materials is confirmed. Any expansion of the agreed scope will be estimated before work starts.")
        story += section("Approval window", f"This proposal is valid through 2026-{9 + cycle:02d}-20. Contact {doc['contact']} to confirm the start date.")
    else:
        story += section("Attendees", f"{doc['contact']} ({doc['client']}); Jordan Ellis (Fieldwork); Priya Shah (Fieldwork).")
        story += section("Discussion", f"Reviewed the {'current journey and research findings' if cycle == 0 else 'revised prototype and launch checklist'} for {doc['project'].lower()}. The team agreed that clearer guidance at the first decision point is the highest priority.")
        story += section("Decisions", "Keep the first release focused on the core flow. Move advanced customization to a later phase. Test the updated language with five participants before final sign-off.")
        story += section("Next actions", f"{doc['contact']} will supply the remaining source content. Fieldwork will circulate the revised prototype and consolidate open questions into the review agenda.")
        story += section("Next review", f"The follow-up meeting is planned for 2026-{8 + cycle:02d}-25. These notes record the meeting date shown above.")

    def footer(canvas, _document):
        canvas.setStrokeColor(colors.HexColor("#DCE2E4"))
        canvas.line(54, 50, 558, 50)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(muted)
        canvas.drawString(54, 35, "Fictional sample document / arc-cua desktop demo")
        canvas.drawRightString(558, 35, f"{doc['reference']} / {canvas.getPageNumber():02d}")

    SimpleDocTemplate(str(path), pagesize=(612, 792), rightMargin=72, leftMargin=72,
                      topMargin=52, bottomMargin=68, title="Client working document",
                      author="Fieldwork (fictional)").build(story, onFirstPage=footer, onLaterPages=footer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = root / "source"
    source.mkdir()
    rl_config.invariant = 1
    manifest = []
    for document in documents():
        path = source / document["source"]
        render_document(document, path)
        manifest.append({**document, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (root / "manifest.json").write_text(json.dumps({"version": 1, "documents": manifest}, indent=2) + "\n")
    print(json.dumps({"corpus": str(root), "documents": len(manifest)}, indent=2))


if __name__ == "__main__":
    main()
