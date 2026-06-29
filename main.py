"""main.py — FastAPI application for CFIUS Screener.

Milestone 1: structured screening form → deterministic jurisdiction engine →
stored result with full findings trail.

Milestone 2: plain-English intake (Claude parse + human confirmation screen) +
Claude-drafted screening memorandum + ReportLab PDF export.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from claude_intake import IntakeError, parse_deal_description
from claude_memo import MemoError, draft_memo
from ofac_checker import OFACHit, screen_entities
from config import (
    APP_TITLE,
    CRITICAL_INFRASTRUCTURE_EXAMPLES,
    DECLARATION_ASSESSMENT_DAYS,
    DEMO_BANNER,
    DEMO_MODE,
    NOTICE_INVESTIGATION_DAYS,
    NOTICE_REVIEW_DAYS,
    SENSITIVE_DATA_CATEGORIES,
    VERIFICATION_DISCLAIMER,
)
from database import SessionLocal, get_db, init_db
from jurisdiction_engine import TransactionFacts
from models import Screening
from pdf_export import render_memo_pdf
from screening_service import (
    findings_of,
    mandatory_reasons_of,
    ofac_hits_of,
    risk_score_of,
    run_and_store,
    tid_categories_of,
)
from seed_data import load_seed_data


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        load_seed_data(db)
    finally:
        db.close()
    yield


# Anchor asset paths to this file so the app works no matter what the
# process's working directory is (uvicorn from the repo, pytest from anywhere).
_HERE = Path(__file__).resolve().parent

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title=APP_TITLE, lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
templates = Jinja2Templates(directory=_HERE / "templates")

OUTCOME_LABELS = {
    "NOT_COVERED": "Not a covered transaction",
    "COVERED_VOLUNTARY": "Covered — voluntary filing available",
    "MANDATORY_DECLARATION": "Mandatory declaration required",
}


def _template(request: Request, name: str, ctx: dict) -> HTMLResponse:
    ctx.update({
        "app_title": APP_TITLE,
        "demo_mode": DEMO_MODE,
        "demo_banner": DEMO_BANNER,
        "disclaimer": VERIFICATION_DISCLAIMER,
        "outcome_labels": OUTCOME_LABELS,
    })
    return templates.TemplateResponse(request, name, ctx)


# ---------------------------------------------------------------------------
# Dashboard — recent screenings
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    screenings = (
        db.query(Screening).order_by(Screening.created_at.desc()).limit(25).all()
    )
    return _template(request, "index.html", {"screenings": screenings})


# ---------------------------------------------------------------------------
# New screening — structured fact form
# ---------------------------------------------------------------------------

@app.get("/screen", response_class=HTMLResponse)
def screen_form(request: Request):
    return _template(request, "screen_form.html", {
        "infra_examples": CRITICAL_INFRASTRUCTURE_EXAMPLES,
        "data_categories": SENSITIVE_DATA_CATEGORIES,
    })


@app.post("/screen", response_class=HTMLResponse)
def screen_submit(
    request: Request,
    db: Session = Depends(get_db),
    us_business_name: str = Form(...),
    us_business_description: str = Form(""),
    acquirer_name: str = Form(...),
    acquirer_country: str = Form(...),
    foreign_govt_ownership_pct: float = Form(0.0),
    voting_interest_pct: float = Form(0.0),
    # Unchecked HTML checkboxes are simply absent from the POST body, so each
    # bool arrives as Optional[str] ("on" when checked, None when not).
    contractual_control_rights: Optional[str] = Form(None),
    board_seat: Optional[str] = Form(None),
    board_observer: Optional[str] = Form(None),
    access_nonpublic_tech_info: Optional[str] = Form(None),
    substantive_decision_role: Optional[str] = Form(None),
    produces_critical_tech: Optional[str] = Form(None),
    export_authorization_required: Optional[str] = Form(None),
    critical_infrastructure: Optional[str] = Form(None),
    sensitive_personal_data: Optional[str] = Form(None),
):
    us_business_name = us_business_name.strip()
    acquirer_name = acquirer_name.strip()
    acquirer_country = acquirer_country.strip()
    if not (us_business_name and acquirer_name and acquirer_country):
        raise HTTPException(status_code=422, detail="Names and country are required.")

    facts = TransactionFacts(
        us_business_name=us_business_name,
        us_business_description=us_business_description.strip(),
        acquirer_name=acquirer_name,
        acquirer_country=acquirer_country,
        foreign_govt_ownership_pct=foreign_govt_ownership_pct,
        voting_interest_pct=voting_interest_pct,
        contractual_control_rights=contractual_control_rights is not None,
        board_seat=board_seat is not None,
        board_observer=board_observer is not None,
        access_nonpublic_tech_info=access_nonpublic_tech_info is not None,
        substantive_decision_role=substantive_decision_role is not None,
        produces_critical_tech=produces_critical_tech is not None,
        export_authorization_required=export_authorization_required is not None,
        critical_infrastructure=critical_infrastructure is not None,
        sensitive_personal_data=sensitive_personal_data is not None,
    )
    row = run_and_store(db, facts)
    return RedirectResponse(f"/screening/{row.id}", status_code=303)


# ---------------------------------------------------------------------------
# Screening result — determination + findings trail
# ---------------------------------------------------------------------------

@app.get("/screening/{screening_id}", response_class=HTMLResponse)
def screening_detail(
    request: Request,
    screening_id: int,
    db: Session = Depends(get_db),
    memo_error: str = Query(default=""),
    ofac_error: str = Query(default=""),
):
    row = db.get(Screening, screening_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Screening not found.")
    return _template(request, "result.html", {
        "s": row,
        "findings": findings_of(row),
        "tid_categories": tid_categories_of(row),
        "mandatory_reasons": mandatory_reasons_of(row),
        "risk_score": risk_score_of(row),
        "ofac_hits": ofac_hits_of(row),
        "declaration_days": DECLARATION_ASSESSMENT_DAYS,
        "review_days": NOTICE_REVIEW_DAYS,
        "investigation_days": NOTICE_INVESTIGATION_DAYS,
        "memo_error": memo_error,
        "ofac_error": ofac_error,
    })


# ---------------------------------------------------------------------------
# Intake — plain-English description → Claude parse → human confirm → engine
# ---------------------------------------------------------------------------

@app.get("/intake", response_class=HTMLResponse)
def intake_form(request: Request):
    return _template(request, "intake_form.html", {})


_MAX_INTAKE_CHARS = 5_000

@app.post("/intake", response_class=HTMLResponse)
@limiter.limit("10/minute")
def intake_parse(
    request: Request,
    description: str = Form(...),
):
    """Claude parses the description. Renders confirm screen — nothing is stored."""
    description = description.strip()
    if len(description) > _MAX_INTAKE_CHARS:
        return _template(request, "intake_form.html", {
            "error": f"Description too long ({len(description):,} chars). Maximum is {_MAX_INTAKE_CHARS:,} characters.",
            "description": description[:200],
        })
    try:
        proposed = parse_deal_description(description)
    except IntakeError as exc:
        return _template(request, "intake_form.html", {
            "error": str(exc),
            "description": description,
        })
    return _template(request, "intake_confirm.html", {
        "proposed": proposed,
        "description": description,
        "infra_examples": CRITICAL_INFRASTRUCTURE_EXAMPLES,
        "data_categories": SENSITIVE_DATA_CATEGORIES,
    })


@app.post("/intake/confirm", response_class=HTMLResponse)
def intake_confirm(
    request: Request,
    db: Session = Depends(get_db),
    intake_description: str = Form(""),
    us_business_name: str = Form(...),
    us_business_description: str = Form(""),
    acquirer_name: str = Form(...),
    acquirer_country: str = Form(...),
    foreign_govt_ownership_pct: float = Form(0.0),
    voting_interest_pct: float = Form(0.0),
    contractual_control_rights: Optional[str] = Form(None),
    board_seat: Optional[str] = Form(None),
    board_observer: Optional[str] = Form(None),
    access_nonpublic_tech_info: Optional[str] = Form(None),
    substantive_decision_role: Optional[str] = Form(None),
    produces_critical_tech: Optional[str] = Form(None),
    export_authorization_required: Optional[str] = Form(None),
    critical_infrastructure: Optional[str] = Form(None),
    sensitive_personal_data: Optional[str] = Form(None),
):
    """User has confirmed (or adjusted) Claude's proposed facts. Run the engine."""
    us_business_name = us_business_name.strip()
    acquirer_name = acquirer_name.strip()
    acquirer_country = acquirer_country.strip()
    if not (us_business_name and acquirer_name and acquirer_country):
        raise HTTPException(status_code=422, detail="Names and country are required.")

    facts = TransactionFacts(
        us_business_name=us_business_name,
        us_business_description=us_business_description.strip(),
        acquirer_name=acquirer_name,
        acquirer_country=acquirer_country,
        foreign_govt_ownership_pct=foreign_govt_ownership_pct,
        voting_interest_pct=voting_interest_pct,
        contractual_control_rights=contractual_control_rights is not None,
        board_seat=board_seat is not None,
        board_observer=board_observer is not None,
        access_nonpublic_tech_info=access_nonpublic_tech_info is not None,
        substantive_decision_role=substantive_decision_role is not None,
        produces_critical_tech=produces_critical_tech is not None,
        export_authorization_required=export_authorization_required is not None,
        critical_infrastructure=critical_infrastructure is not None,
        sensitive_personal_data=sensitive_personal_data is not None,
    )
    row = run_and_store(
        db, facts, intake_description=intake_description,
    )
    return RedirectResponse(f"/screening/{row.id}", status_code=303)


# ---------------------------------------------------------------------------
# Memo — generate via Claude, download as PDF
# ---------------------------------------------------------------------------

@app.post("/screening/{screening_id}/memo")
def generate_memo(screening_id: int, db: Session = Depends(get_db)):
    row = db.get(Screening, screening_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Screening not found.")
    try:
        row.memo_text = draft_memo(row)
        db.commit()
    except MemoError:
        return RedirectResponse(
            f"/screening/{screening_id}?memo_error=1", status_code=303
        )
    return RedirectResponse(f"/screening/{screening_id}", status_code=303)


@app.get("/screening/{screening_id}/memo.pdf")
def memo_pdf(screening_id: int, db: Session = Depends(get_db)):
    row = db.get(Screening, screening_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Screening not found.")
    if not row.memo_text:
        raise HTTPException(status_code=404, detail="No memo generated yet.")
    pdf_bytes = render_memo_pdf(row, row.memo_text)
    filename = f"cfius_memo_{screening_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# OFAC SDN screening — on-demand, acquirer name only
# ---------------------------------------------------------------------------

@app.post("/screening/{screening_id}/ofac-screen")
def ofac_screen(screening_id: int, db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    import json

    row = db.get(Screening, screening_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Screening not found.")
    try:
        hits = screen_entities([row.acquirer_name])
        row.ofac_hits_json = json.dumps([h._asdict() for h in hits])
        row.ofac_checked_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        return RedirectResponse(
            f"/screening/{screening_id}?ofac_error=1", status_code=303
        )
    return RedirectResponse(f"/screening/{screening_id}", status_code=303)


# ---------------------------------------------------------------------------
# JSON health/stats
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def api_stats(db: Session = Depends(get_db)):
    total = db.query(Screening).count()
    by_outcome = {
        outcome: db.query(Screening).filter(Screening.outcome == outcome).count()
        for outcome in OUTCOME_LABELS
    }
    return JSONResponse({"status": "ok", "screenings": total, "by_outcome": by_outcome})
