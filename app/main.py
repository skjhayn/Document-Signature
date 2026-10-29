"""YPEF Partner Document Signing Tool — Streamlit application."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import streamlit as st

from app.components.signature_pad import signature_pad
from app.config import (
    AUDIT_CSV_PATH,
    FUNDS,
    KIND_FUND,
    KIND_LETTERHEAD_FOOTER,
    KIND_LETTERHEAD_HEADER,
    KIND_SIGNATURE,
    LETTERHEAD_KINDS,
    MAX_PDF_PAGES,
    MAX_UPLOAD_BYTES,
    OUTPUT_DIR,
    PARTNERS,
    STAMPS_DIR,
    asset_path,
    ensure_data_dirs,
    get_settings,
)
from app.services.audit_service import (
    append_audit_record,
    read_audit_log,
    record_email_status,
    verify_audit_log_integrity,
)
from app.services.email_service import send_partner_notification
from app.services.letterhead import (
    DEFAULT_PAGE_SETUP,
    content_box,
    default_letterhead_boxes,
    ensure_letterhead_assets,
    ingest_upload,
    render_letterhead_strips,
)
from app.services.pdf_service import (
    EncryptedPDFError,
    PlacementRect,
    apply_signatures,
    clamp_display_box,
    default_display_box,
    display_box_to_placement,
    image_to_data_url,
    looks_like_placeholder_signature,
    overlapping_reserved,
    overlapping_text,
    document_page_sizes,
    page_count,
    page_size_label,
    page_text_rects,
    png_file_to_data_url,
    render_page_for_display,
    save_pdf,
    sha256_bytes,
    signature_aspect_ratio,
)

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 60
INSECURE_DEFAULT_PASSWORD = "change-me"

IST = timezone(timedelta(hours=5, minutes=30))


@st.cache_resource
def _get_login_state() -> dict:
    return {"lock": threading.Lock(), "failed_attempts": 0, "locked_until": 0.0}


st.set_page_config(
    page_title="YPEF Document Signing",
    page_icon="✍️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _init_session_state() -> None:
    defaults = {
        "authenticated": False,
        "step": 1,
        "pdf_bytes": None,
        "pdf_filename": "",
        "pdf_hash": "",
        "partner": None,
        "page_number": 1,
        "placement": None,
        "signed_pdf_bytes": None,
        "signed_filename": "",
        "log_id": "",
        "email_status": "",
        "upload_key": 0,
        "show_audit_log": False,
        "display_box": None,
        "stamps": [],
        "active_stamp_id": "",
        "source_pdf_bytes": None,
        "converted_pdf_bytes": None,
        "page_setup": DEFAULT_PAGE_SETUP,
        "letterhead_fund": "",
        "converted": False,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _reset_workflow() -> None:
    upload_key = st.session_state.get("upload_key", 0) + 1
    for key in (
        "step",
        "pdf_bytes",
        "pdf_filename",
        "pdf_hash",
        "partner",
        "page_number",
        "placement",
        "signed_pdf_bytes",
        "signed_filename",
        "log_id",
        "email_status",
        "display_box",
        "stamps",
        "active_stamp_id",
        "source_pdf_bytes",
        "converted_pdf_bytes",
        "page_setup",
        "letterhead_fund",
        "converted",
    ):
        st.session_state.pop(key, None)
    _init_session_state()
    st.session_state["step"] = 1
    st.session_state["upload_key"] = upload_key


def _password_matches(given: str, expected: str) -> bool:
    given_digest = hashlib.sha256(given.encode("utf-8")).hexdigest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    return hmac.compare_digest(given_digest, expected_digest)


def _safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    cleaned = re.sub(r"[^\w\-]+", "_", stem)
    return (cleaned[:80] or "document").strip("_")


def _inject_layout_css() -> None:
    st.markdown(
        """
        <style>
          .block-container { max-width: 1280px; padding-top: 1.4rem; }
          iframe[title="signature_pad"] {
            border: 0;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _login_gate() -> bool:
    settings = get_settings()
    if st.session_state.authenticated:
        return True

    st.title("YPEF Partner Document Signing")
    st.caption("Authorized operators only")

    if settings.app_password == INSECURE_DEFAULT_PASSWORD:
        st.error(
            "APP_PASSWORD is still set to the insecure default (`change-me`). "
            "Set a real password in `.env` locally, or in Streamlit Cloud secrets."
        )
        return False

    login_state = _get_login_state()
    with login_state["lock"]:
        locked_until = login_state["locked_until"]
    now = time.monotonic()
    if now < locked_until:
        remaining = int(locked_until - now)
        st.error(f"Too many failed attempts. Try again in {remaining}s.")
        return False

    with st.form("login_form"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            if _password_matches(password, settings.app_password):
                with login_state["lock"]:
                    login_state["failed_attempts"] = 0
                st.session_state.authenticated = True
                st.rerun()
            else:
                with login_state["lock"]:
                    login_state["failed_attempts"] += 1
                    attempts = login_state["failed_attempts"]
                    if attempts >= LOGIN_MAX_ATTEMPTS:
                        login_state["locked_until"] = time.monotonic() + LOGIN_LOCKOUT_SECONDS
                        login_state["failed_attempts"] = 0
                if attempts >= LOGIN_MAX_ATTEMPTS:
                    st.error(f"Too many failed attempts. Locked for {LOGIN_LOCKOUT_SECONDS}s.")
                else:
                    remaining_tries = LOGIN_MAX_ATTEMPTS - attempts
                    st.error(
                        f"Invalid password. {remaining_tries} attempt(s) remaining before lockout."
                    )
    return False


def _sidebar() -> None:
    settings = get_settings()
    with st.sidebar:
        st.header("Session")
        st.write(f"**Operator:** {settings.operator_name}")
        st.write(f"**Step:** {st.session_state.step} of 4")
        if st.button("Start new document", use_container_width=True):
            _reset_workflow()
            st.rerun()

        st.divider()
        st.subheader("Audit log")
        log_rows = read_audit_log()
        st.metric("Total signing events", len(log_rows))
        if log_rows:
            intact, detail = verify_audit_log_integrity()
            if intact:
                st.caption(f"Integrity verified — {detail}")
            else:
                st.error(f"Audit log integrity check FAILED: {detail}")
        if st.button("View audit log", use_container_width=True):
            st.session_state.show_audit_log = True
            st.rerun()

        st.divider()
        if settings.email_enabled:
            st.success("Email notifications enabled")
        else:
            st.info("Email disabled (configure SMTP in `.env`)")


def _audit_log_view() -> None:
    st.title("Audit Log")
    st.caption(str(AUDIT_CSV_PATH))

    if st.button("← Back to signing"):
        st.session_state.show_audit_log = False
        st.rerun()

    rows = read_audit_log()
    intact, detail = verify_audit_log_integrity()

    col1, col2 = st.columns(2)
    col1.metric("Total signing events", len(rows))
    if intact:
        col2.success(f"Integrity verified — {detail}")
    else:
        col2.error(f"Integrity check FAILED — {detail}")

    if not rows:
        st.info("No signing events recorded yet.")
        return

    st.dataframe(rows, use_container_width=True, hide_index=True)


def _missing_assets() -> list[str]:
    missing = []
    for name in PARTNERS:
        if not asset_path(KIND_SIGNATURE, name).exists():
            missing.append(f"signature: {name}")
    for name in FUNDS:
        if not asset_path(KIND_FUND, name).exists():
            missing.append(f"fund stamp: {name}")
    return missing


def _placeholder_partners() -> list[str]:
    names = []
    for name in PARTNERS:
        path = asset_path(KIND_SIGNATURE, name)
        if path.exists() and looks_like_placeholder_signature(path):
            names.append(name)
    return names


def _stamp_label(stamp: dict) -> str:
    kind = stamp.get("kind")
    if kind == KIND_FUND:
        prefix = "Fund stamp"
    elif kind == KIND_LETTERHEAD_HEADER:
        prefix = "Letterhead header"
    elif kind == KIND_LETTERHEAD_FOOTER:
        prefix = "Letterhead footer"
    else:
        prefix = "Signature"
    return f"{prefix} · {stamp['name']}"


def _is_letterhead(stamp: dict) -> bool:
    return stamp.get("kind") in LETTERHEAD_KINDS


def _clamp_mode(stamp: dict) -> str:
    return "band" if _is_letterhead(stamp) else "stamp"


def _stamp_bounds(stamp: dict, render) -> dict[str, float] | None:
    if _is_letterhead(stamp):
        return None
    return _display_body_bounds(render)


def _step_upload() -> None:
    st.subheader("1. Upload document")
    uploaded = st.file_uploader(
        "Select a Word (.docx, .doc) or PDF document",
        type=["pdf", "docx", "doc", "docm"],
        key=f"uploader_{st.session_state.upload_key}",
    )
    if uploaded is None:
        st.info("Upload a partnership document (PDF or Word) to begin.")
        return

    source_bytes = uploaded.getvalue()
    if not source_bytes:
        st.error("The uploaded file is empty.")
        return

    if len(source_bytes) > MAX_UPLOAD_BYTES:
        st.error(
            f"This file is {len(source_bytes) / (1024 * 1024):.1f} MB. "
            f"The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
        return

    try:
        pdf_bytes = ingest_upload(uploaded.name, source_bytes)
        total_pages = page_count(pdf_bytes)
    except EncryptedPDFError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.error(f"Could not convert this file to A4: {exc}")
        return

    if total_pages < 1:
        st.error("This document has no pages.")
        return

    if total_pages > MAX_PDF_PAGES:
        st.error(
            f"This document has {total_pages} pages. The tool accepts up to "
            f"{MAX_PDF_PAGES} pages."
        )
        return

    st.session_state.source_pdf_bytes = source_bytes
    st.session_state.converted_pdf_bytes = pdf_bytes
    st.session_state.pdf_bytes = pdf_bytes
    st.session_state.pdf_filename = uploaded.name
    st.session_state.pdf_hash = sha256_bytes(source_bytes)
    st.session_state.page_setup = "A4 (top 3.75 cm / bottom 2.75 cm)"
    st.session_state.converted = True

    st.success(
        f"Uploaded **{uploaded.name}** and converted to A4 "
        f"({total_pages} page{'s' if total_pages != 1 else ''})"
    )
    sizes = document_page_sizes(pdf_bytes)
    unique_sizes = list(dict.fromkeys(sizes))
    st.caption("Page size after conversion: " + "; ".join(unique_sizes))
    st.caption("Paper: A4 · Margin top 3.75 cm · Margin bottom 2.75 cm")
    st.caption(f"SHA-256 (source file): `{st.session_state.pdf_hash}`")

    if st.button("Continue to placement →", type="primary"):
        st.session_state.letterhead_fund = ""
        st.session_state.stamps = []
        st.session_state.step = 2
        st.rerun()


def _new_stamp(kind: str, name: str, page_number: int, render, index: int) -> dict:
    aspect = signature_aspect_ratio(asset_path(kind, name))
    width_frac = 0.16 if kind == KIND_FUND else 0.18
    box = default_display_box(
        render.image_width,
        render.image_height,
        aspect,
        width_frac=width_frac,
        index=index,
    )
    box = clamp_display_box(
        box["x"],
        box["y"],
        box["w"],
        box["h"],
        render.image_width,
        render.image_height,
        aspect,
        bounds=_display_body_bounds(render),
        mode="stamp",
    )
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "name": name,
        "page_number": int(page_number),
        "box": box,
    }


def _stamp_aspect(stamp: dict) -> float:
    return signature_aspect_ratio(asset_path(stamp["kind"], stamp["name"]))


def _display_body_bounds(render) -> dict[str, float]:
    body = content_box(render.pdf_width, render.pdf_height)
    sx = render.image_width / render.pdf_width
    sy = render.image_height / render.pdf_height
    return {
        "x0": body.x0 * sx,
        "y0": body.y0 * sy,
        "x1": body.x1 * sx,
        "y1": body.y1 * sy,
    }


def _stamp_placement(stamp: dict, render) -> object:
    aspect = _stamp_aspect(stamp)
    box = clamp_display_box(
        stamp["box"]["x"],
        stamp["box"]["y"],
        stamp["box"]["w"],
        stamp["box"]["h"],
        render.image_width,
        render.image_height,
        aspect,
        bounds=_stamp_bounds(stamp, render),
        mode=_clamp_mode(stamp),
    )
    stamp["box"] = box
    page_number = stamp["page_number"] if stamp["page_number"] else 1
    return display_box_to_placement(box, page_number, render)


def _stamp_overlap_hits(pdf_bytes: bytes, stamp: dict, render) -> list:
    placement = _stamp_placement(stamp, render)
    page_number = stamp["page_number"] if stamp["page_number"] else render.page_number
    hits = overlapping_text(placement, page_text_rects(pdf_bytes, page_number))
    if not _is_letterhead(stamp):
        hits.extend(
            overlapping_reserved(placement, render.pdf_width, render.pdf_height)
        )
    return hits


def _letterhead_stamps(fund: str, render) -> list[dict]:
    render_letterhead_strips(fund)
    header_box, footer_box = default_letterhead_boxes(
        render.pdf_width,
        render.pdf_height,
        render.image_width,
        render.image_height,
    )
    return [
        {
            "id": "letterhead-header",
            "kind": KIND_LETTERHEAD_HEADER,
            "name": fund,
            "page_number": 0,
            "box": header_box,
        },
        {
            "id": "letterhead-footer",
            "kind": KIND_LETTERHEAD_FOOTER,
            "name": fund,
            "page_number": 0,
            "box": footer_box,
        },
    ]


def _step_place() -> None:
    st.subheader("2. Place signatures, fund stamps and letterhead")
    if not st.session_state.get("converted"):
        st.warning("Upload a Word or PDF document first. It is converted to A4 automatically.")
        st.session_state.step = 1
        st.rerun()
        return
    ensure_letterhead_assets()
    pdf_bytes = st.session_state.pdf_bytes
    if not pdf_bytes:
        st.warning("Upload a PDF first.")
        st.session_state.step = 1
        st.rerun()
        return

    missing = _missing_assets()
    if missing:
        st.error("Missing image files for: " + ", ".join(missing))
        st.caption(
            f"Partner signatures: `{Path('data/signatures').resolve()}` · "
            f"Fund stamps: `{STAMPS_DIR.resolve()}`"
        )
        return

    placeholders = _placeholder_partners()
    if placeholders:
        st.warning(
            "Placeholder signature images are still in use for: "
            + ", ".join(placeholders)
            + ". Replace them in `data/signatures` before production signing."
        )

    try:
        total_pages = page_count(pdf_bytes)
    except EncryptedPDFError as exc:
        st.error(str(exc))
        return

    page_number = st.number_input(
        "Page",
        min_value=1,
        max_value=total_pages,
        value=int(st.session_state.page_number),
        step=1,
    )
    st.session_state.page_number = int(page_number)
    render = render_page_for_display(pdf_bytes, page_number)
    st.caption(
        f"Page setup: {st.session_state.page_setup} · "
        f"{page_size_label(render.pdf_width, render.pdf_height, render.rotation)}"
    )
    st.caption(
        "Drag, select, and drop the letterhead header and footer to line them up. "
        "Partner signatures and fund stamps stay in the body of the page."
    )

    pickers = st.columns(3)
    with pickers[0]:
        partner = st.selectbox("Partner signature", list(PARTNERS.keys()))
        if st.button("Add partner signature", use_container_width=True):
            added = _new_stamp(
                KIND_SIGNATURE, partner, page_number, render, len(st.session_state.stamps)
            )
            st.session_state.stamps.append(added)
            st.session_state.active_stamp_id = added["id"]
            st.rerun()
    with pickers[1]:
        fund = st.selectbox("Fund-wise stamp", list(FUNDS.keys()))
        if st.button("Add fund stamp", use_container_width=True):
            added = _new_stamp(
                KIND_FUND, fund, page_number, render, len(st.session_state.stamps)
            )
            st.session_state.stamps.append(added)
            st.session_state.active_stamp_id = added["id"]
            st.rerun()
    with pickers[2]:
        letter_options = ["None"] + list(FUNDS.keys())
        current = st.session_state.letterhead_fund or "None"
        letter_choice = st.selectbox(
            "Letterhead",
            letter_options,
            index=letter_options.index(current) if current in letter_options else 0,
        )
        if st.button("Apply letterhead", use_container_width=True):
            name = "" if letter_choice == "None" else letter_choice
            others = [
                item for item in st.session_state.stamps if not _is_letterhead(item)
            ]
            if name:
                others.extend(_letterhead_stamps(name, render))
            st.session_state.stamps = others
            st.session_state.letterhead_fund = name
            st.session_state.pdf_bytes = st.session_state.converted_pdf_bytes
            st.rerun()

    if st.button("Remove selected stamp"):
        stamps = [
            item
            for item in st.session_state.stamps
            if item["id"] != st.session_state.active_stamp_id
        ]
        st.session_state.stamps = stamps
        st.session_state.active_stamp_id = stamps[0]["id"] if stamps else ""
        if not any(_is_letterhead(item) for item in stamps):
            st.session_state.letterhead_fund = ""
        st.rerun()

    page_stamps = [
        item
        for item in st.session_state.stamps
        if item["page_number"] in (0, page_number)
    ]
    overlap_by_id = {
        item["id"]: bool(_stamp_overlap_hits(pdf_bytes, item, render))
        for item in page_stamps
    }

    pad_stamps = []
    for item in page_stamps:
        aspect = _stamp_aspect(item)
        box = clamp_display_box(
            item["box"]["x"],
            item["box"]["y"],
            item["box"]["w"],
            item["box"]["h"],
            render.image_width,
            render.image_height,
            aspect,
            bounds=_stamp_bounds(item, render),
            mode=_clamp_mode(item),
        )
        item["box"] = box
        pad_stamps.append(
            {
                "id": item["id"],
                "x": box["x"],
                "y": box["y"],
                "w": box["w"],
                "h": box["h"],
                "aspect": aspect,
                "sig_image": png_file_to_data_url(asset_path(item["kind"], item["name"])),
                "label": _stamp_label(item),
                "overlap": overlap_by_id.get(item["id"], False),
                "full_span": _is_letterhead(item),
            }
        )

    if not page_stamps:
        st.info("Add a partner signature, a fund stamp, or a letterhead, then drag it into place.")
    result = signature_pad(
        page_image=image_to_data_url(render.image, fmt="JPEG"),
        width=render.image_width,
        height=render.image_height,
        stamps=pad_stamps,
        active_id=st.session_state.active_stamp_id,
        key=f"pad_{st.session_state.upload_key}_{page_number}_{st.session_state.letterhead_fund}",
        hint=(
            "Drag each overlay. Select it, then drop it where it belongs. "
            "Letterhead header and footer can be moved freely. "
            "Red outline means the stamp is sitting on text."
        ),
    )
    if isinstance(result, dict) and "id" in result:
        for item in st.session_state.stamps:
            if item["id"] != result["id"]:
                continue
            aspect = _stamp_aspect(item)
            next_box = clamp_display_box(
                result["x"],
                result["y"],
                result["w"],
                result["h"],
                render.image_width,
                render.image_height,
                aspect,
                bounds=_stamp_bounds(item, render),
                mode=_clamp_mode(item),
            )
            if (
                abs(next_box["x"] - item["box"]["x"]) > 0.2
                or abs(next_box["y"] - item["box"]["y"]) > 0.2
                or abs(next_box["w"] - item["box"]["w"]) > 0.2
                or abs(next_box["h"] - item["box"]["h"]) > 0.2
            ):
                item["box"] = next_box
                st.session_state.active_stamp_id = item["id"]
                st.rerun()

    st.markdown("**Signatures in this document**")
    if not st.session_state.stamps:
        st.caption("Add at least one signature.")
    for item in st.session_state.stamps:
        page_render = (
            render
            if item["page_number"] in (0, page_number)
            else render_page_for_display(pdf_bytes, item["page_number"])
        )
        hits = _stamp_overlap_hits(pdf_bytes, item, page_render)
        status = "overlaps text" if hits else "clear of text"
        cols = st.columns([4, 1])
        page_note = "all pages" if _is_letterhead(item) else f"page {item['page_number']}"
        label = f"{_stamp_label(item)} · {page_note} · {status}"
        if cols[0].button(label, key=f"jump_{item['id']}"):
            st.session_state.active_stamp_id = item["id"]
            if item["page_number"]:
                st.session_state.page_number = item["page_number"]
            st.rerun()
        if hits:
            cols[1].error("Move")
        else:
            cols[1].success("OK")

    overlap_count = 0
    for item in st.session_state.stamps:
        page_render = (
            render
            if item["page_number"] in (0, page_number)
            else render_page_for_display(pdf_bytes, item["page_number"])
        )
        if _stamp_overlap_hits(pdf_bytes, item, page_render):
            overlap_count += 1

    if overlap_count:
        st.error(
            f"{overlap_count} signature(s) sit on existing text. "
            "Move or shrink them onto a blank signature line before confirming."
        )

    can_continue = overlap_count == 0 and (
        bool(st.session_state.stamps) or bool(st.session_state.letterhead_fund)
    )
    if st.button("Continue to confirmation →", type="primary", disabled=not can_continue):
        st.session_state.step = 3
        st.rerun()


def _resolved_stamps(pdf_bytes: bytes) -> list[tuple[dict, object, Path]]:
    resolved = []
    total = page_count(pdf_bytes)
    first_render = render_page_for_display(pdf_bytes, 1)
    for item in st.session_state.stamps:
        path = asset_path(item["kind"], item["name"])
        if _is_letterhead(item):
            base = _stamp_placement(item, first_render)
            for page_number in range(1, total + 1):
                placement = PlacementRect(
                    page_number=page_number,
                    x0=base.x0,
                    y0=base.y0,
                    x1=base.x1,
                    y1=base.y1,
                )
                resolved.append((item, placement, path))
            continue
        render = render_page_for_display(pdf_bytes, item["page_number"])
        placement = _stamp_placement(item, render)
        resolved.append((item, placement, path))
    return resolved


def _audit_items(resolved: list[tuple[dict, object, Path]]) -> list[tuple[dict, object, Path]]:
    """One audit row per overlay, not one per page for letterhead bands."""
    seen_letterhead = set()
    items = []
    for item, placement, path in resolved:
        if _is_letterhead(item):
            key = (item["kind"], item["name"])
            if key in seen_letterhead:
                continue
            seen_letterhead.add(key)
        items.append((item, placement, path))
    return items


def _step_confirm() -> None:
    st.subheader("3. Confirm & sign")
    pdf_bytes = st.session_state.pdf_bytes
    if not pdf_bytes or (
        not st.session_state.stamps and not st.session_state.letterhead_fund
    ):
        st.warning("Add a signature, fund stamp, or letterhead before confirming.")
        st.session_state.step = 2
        st.rerun()
        return

    settings = get_settings()
    resolved = _resolved_stamps(pdf_bytes)
    blocked = False
    for item, placement, _path in resolved:
        hits = overlapping_text(
            placement, page_text_rects(pdf_bytes, item["page_number"])
        )
        if hits:
            blocked = True
            break

    if blocked:
        st.error("A signature still overlaps text. Go back and move it.")
        if st.button("← Back to placement"):
            st.session_state.step = 2
            st.rerun()
        return

    st.markdown("Review every stamp and letterhead band before finalizing.")
    summary = st.container(border=True)
    with summary:
        cols = st.columns(2)
        cols[0].markdown(f"**Document:** {st.session_state.pdf_filename}")
        cols[0].markdown(f"**Stamps:** {len(resolved)}")
        cols[0].markdown(f"**Operator:** {settings.operator_name}")
        names = ", ".join(sorted({_stamp_label(item) for item, _, _ in resolved}))
        pages = ", ".join(str(item["page_number"]) for item, _, _ in resolved)
        cols[1].markdown(f"**Applied:** {names or '—'}")
        cols[1].markdown(f"**Pages:** {pages or 'all'}")
        cols[1].markdown(
            f"**Letterhead:** {st.session_state.letterhead_fund or 'None'}"
        )
        cols[1].markdown(f"**Page setup:** {st.session_state.page_setup}")
        cols[1].markdown(f"**Document hash:** `{st.session_state.pdf_hash[:16]}…`")

    for item, placement, path in resolved:
        st.caption(
            f"{_stamp_label(item)} on page {item['page_number']} · `{placement.format_position()}`"
        )
    timestamp_label = datetime.now(IST).strftime("Signed %Y-%m-%d %H:%M IST")
    signed_preview = apply_signatures(
        pdf_bytes,
        [(path, placement) for _item, placement, path in resolved],
        timestamp=timestamp_label,
    )
    preview_pages = sorted(
        {
            item["page_number"]
            for item, _, _ in resolved
            if not _is_letterhead(item)
        }
    )
    if st.session_state.letterhead_fund and 1 not in preview_pages:
        preview_pages = [1, *preview_pages]
    if not preview_pages:
        preview_pages = [1]
    for page_number in preview_pages:
        preview = render_page_for_display(signed_preview, page_number)
        st.image(
            preview.image,
            caption=f"Confirmation preview — page {page_number}",
            width=preview.image_width,
        )

    confirm = st.checkbox(
        "I confirm every signature is on a blank line and does not cover text.",
        value=False,
    )
    col_back, col_sign = st.columns(2)
    with col_back:
        if st.button("← Back to placement"):
            st.session_state.step = 2
            st.rerun()
    with col_sign:
        if st.button("Confirm & apply signatures", type="primary", disabled=not confirm):
            _finalize_signing()


def _finalize_signing() -> None:
    settings = get_settings()
    pdf_bytes = st.session_state.pdf_bytes
    assert pdf_bytes
    resolved = _resolved_stamps(pdf_bytes) if st.session_state.stamps else []

    for item, placement, _path in resolved:
        if overlapping_text(placement, page_text_rects(pdf_bytes, item["page_number"])):
            st.error(
                f"{_stamp_label(item)} on page {item['page_number']} overlaps text. "
                "Signing was aborted."
            )
            return

    try:
        timestamp_label = datetime.now(IST).strftime("Signed %Y-%m-%d %H:%M IST")
        signed_bytes = apply_signatures(
            pdf_bytes,
            [(path, placement) for _item, placement, path in resolved],
            timestamp=timestamp_label,
        )
    except Exception as exc:
        st.error(f"Failed to apply the signature: {exc}")
        return

    stem = _safe_stem(st.session_state.pdf_filename)
    timestamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    signed_filename = f"{stem}_signed_{timestamp}.pdf"
    log_ids: list[str] = []
    email_notes: list[str] = []
    emailed: set[str] = set()

    fund_stamp_names = ", ".join(
        sorted(
            {
                stamp["name"]
                for stamp in st.session_state.stamps
                if stamp.get("kind") == KIND_FUND
            }
        )
    )
    letterhead_name = st.session_state.letterhead_fund or ""
    page_setup_name = st.session_state.page_setup

    audit_items = _audit_items(resolved)
    if not audit_items:
        audit_items = [
            (
                {"kind": KIND_LETTERHEAD_HEADER, "name": letterhead_name or "letterhead"},
                PlacementRect(page_number=1, x0=0, y0=0, x1=0, y1=0),
                Path("."),
            )
        ]

    try:
        for item, placement, _path in audit_items:
            log_id = str(uuid.uuid4())
            append_audit_record(
                log_id=log_id,
                operator=settings.operator_name,
                document_filename=st.session_state.pdf_filename,
                document_hash=st.session_state.pdf_hash,
                partner_signed=_stamp_label(item),
                placement=placement,
                output_filename=signed_filename,
                email_status="pending",
                fund_stamp=fund_stamp_names,
                letterhead=letterhead_name,
                page_setup=page_setup_name,
                signed_timestamp=timestamp_label,
            )
            log_ids.append(log_id)
            if item.get("kind") != KIND_SIGNATURE:
                record_email_status(log_id, "skipped (fund stamp)")
                continue
            if item["name"] in emailed:
                record_email_status(log_id, "skipped (already notified this partner)")
                continue
            status = send_partner_notification(
                settings,
                partner_name=item["name"],
                partner_email=PARTNERS[item["name"]]["email"],
                document_filename=st.session_state.pdf_filename,
                operator_name=settings.operator_name,
                log_id=log_id,
                attachment_bytes=signed_bytes,
                attachment_filename=signed_filename,
            )
            record_email_status(log_id, status)
            email_notes.append(f"{item['name']}: {status}")
            emailed.add(item["name"])
    except Exception as exc:
        st.error(
            "Could not write the audit log, so the signing action was aborted. "
            f"Error: {exc}"
        )
        return

    try:
        save_pdf(signed_bytes, OUTPUT_DIR / signed_filename)
    except Exception as exc:
        st.error(
            f"Audit entries were recorded, but saving the signed PDF failed: {exc}."
        )
        return

    st.session_state.signed_pdf_bytes = signed_bytes
    st.session_state.signed_filename = signed_filename
    st.session_state.log_id = ", ".join(log_ids)
    st.session_state.email_status = "; ".join(email_notes) or "skipped"
    st.session_state.step = 4
    st.rerun()


def _step_complete() -> None:
    st.subheader("4. Download signed document")
    signed_bytes = st.session_state.signed_pdf_bytes
    if not signed_bytes:
        st.warning("No signed document available.")
        st.session_state.step = 1
        st.rerun()
        return

    st.success("Document signed successfully.")
    st.markdown(
        f"- **Audit ID:** `{st.session_state.log_id}`\n"
        f"- **Output file:** `{st.session_state.signed_filename}`\n"
        f"- **Email status:** {st.session_state.email_status}"
    )

    st.download_button(
        label="Download signed PDF",
        data=signed_bytes,
        file_name=st.session_state.signed_filename,
        mime="application/pdf",
        type="primary",
        use_container_width=True,
    )

    st.caption(f"Saved copy: `{OUTPUT_DIR / st.session_state.signed_filename}`")

    if st.session_state.email_status and "failed" in st.session_state.email_status:
        st.warning("At least one partner email failed. Audit log records the failure.")

    if st.button("Sign another document"):
        _reset_workflow()
        st.rerun()


@st.cache_resource
def _bootstrap() -> bool:
    ensure_data_dirs()
    ensure_letterhead_assets()
    return True


def main() -> None:
    _bootstrap()
    _inject_layout_css()
    _init_session_state()

    if not _login_gate():
        return

    _sidebar()

    if st.session_state.show_audit_log:
        _audit_log_view()
        return

    st.title("YPEF Partner Document Signing")
    st.caption("Upload → Place any number of signatures → Confirm → Download")

    steps = {
        1: _step_upload,
        2: _step_place,
        3: _step_confirm,
        4: _step_complete,
    }
    steps[st.session_state.step]()


if __name__ == "__main__":
    main()
