"""Anonymous search for a patient's family (no login): what is available near the patient, and how to get it.

Privacy rules (threat model #2, ADR 020):
- the caller's location is rounded to 1 decimal (~11 km) before any query, so a stranger cannot probe street by street;
- donors are only ever COUNTED, per fixed distance band; counts below MIN_SHOWN (0 included) are hidden, and the
  total is hidden whenever any band is, so a result never points at one identifiable person; no id, name or
  coordinate of a donor is returned;
- reaching donors goes through a hospital (an emergency request), never through this page;
- rate-limited per IP: generous for a worried family, useless for mapping the whole state.
"""

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query

from deps import Config, Now, Session, public_rate_limit
from domain import BloodGroup, compatible_groups, eligibility_cutoffs
from modules.inventory import service as inventory
from modules.organizations import service as organizations
from repository import DISTANCE_BANDS, eligible_donor_counts_by_band

from .schemas import DonorBand, DonorsNearby, NearbyBank

MIN_SHOWN = 5                       # k-anonymity style floor: never show a count small enough to identify someone
router = APIRouter(prefix="/public", tags=["public search"], dependencies=[Depends(public_rate_limit)])

Latitude = Query(ge=-90, le=90)
Longitude = Query(ge=-180, le=180)


def coarse(value: float) -> float:
    return round(value, 1)          # ~11 km: enough to find what is near, too coarse to probe a street


@router.get("/nearest-banks", response_model=list[NearbyBank])
async def nearest_banks(
    session: Session, now: Now,
    recipient: BloodGroup,
    latitude: float = Latitude,
    longitude: float = Longitude,
    component: str = Query(default="red_cells", pattern="^(red_cells|plasma)$"),
    radius_km: float = Query(default=50, gt=0, le=1000),      # MP is ~900 km across; the LIMIT 20 stays
    ownership: Literal["all", "government", "private"] = "all",
    confirmed_within_h: int | None = Query(default=None, ge=1, le=168),      # the "counted in the last 24 h" chip
):
    """Blood banks within radius_km that hold usable compatible units, nearest first.
    ownership=government includes banks whose ownership was inferred from the name (flagged ownership_inferred);
    charitable and unknown banks appear under `all` only. confirmed_within_h: only banks whose stock was counted in
    the last N hours. Every filter runs inside the SQL, before the LIMIT 20 (R1, ADR 022)."""
    lat, lng = coarse(latitude), coarse(longitude)
    groups = [g.value for g in compatible_groups(recipient, component)]
    with_stock = await inventory.banks_with_stock(session, groups, component, now)
    if not with_stock:
        return []
    banks = await organizations.nearby(
        session, "blood_bank", lat, lng, radius_km * 1000, limit=20, ownership=None if ownership == "all" else ownership,
        among=with_stock, confirmed_since=now - timedelta(hours=confirmed_within_h) if confirmed_within_h else None)
    stock = await inventory.available_by_bank(session, [b.id for b in banks], groups, recipient.value, component, now)
    info = await organizations.facilities(session, [b.id for b in banks])
    # order unchanged (nearest first); freshness is shown, not ranked: the family decides who to call
    # a facility's own coordinates are public (it is a building with a signboard); donors never get coordinates
    # `if b.id in stock`: a unit that expired or was reserved between the two queries drops the bank, never a 500
    return [NearbyBank(
        blood_bank_id=b.id, name=info[b.id].name, city=info[b.id].city, phone=info[b.id].phone,
        stock_confirmed_at=info[b.id].stock_confirmed_at, distance_km=round(b.distance_m / 1000, 1),
        units=stock[b.id].units, units_same_group=stock[b.id].same_group, blood_groups=stock[b.id].groups,
        latitude=info[b.id].latitude, longitude=info[b.id].longitude, ownership=info[b.id].ownership,
        ownership_inferred=info[b.id].ownership_inferred, stock_is_demo=info[b.id].is_demo)
        for b in banks if b.id in stock]


@router.get("/donors-nearby", response_model=DonorsNearby)
async def donors_nearby(
    session: Session, now: Now, settings: Config,
    recipient: BloodGroup,
    latitude: float = Latitude,
    longitude: float = Longitude,
):
    """How many eligible, available, compatible donors live within 50 km, per distance band. Counts only."""
    counts = await eligible_donor_counts_by_band(
        session, coarse(latitude), coarse(longitude),
        [g.value for g in compatible_groups(recipient, "red_cells")],     # whole-blood donors give red cells
        cutoffs=eligibility_cutoffs(now, settings.donation_interval_male_days, settings.donation_interval_female_days),
    )
    # 0 is hidden like 1-4 ("none" vs "a few" is itself a signal), and the total is shown only when every band is:
    # otherwise total - visible bands = the hidden band, and the floor means nothing (B1)
    per_band = [counts.get(label, 0) for _, label in DISTANCE_BANDS]
    every_band_shown = all(n >= MIN_SHOWN for n in per_band)
    return DonorsNearby(
        total=sum(per_band) if every_band_shown else None,
        bands=[DonorBand(band=label, donors=n if n >= MIN_SHOWN else None)
               for (_, label), n in zip(DISTANCE_BANDS, per_band, strict=True)],
        how_to_reach_them=("Donors are contacted by the hospital treating the patient, to protect their privacy. "
                           "Ask the hospital to post an emergency request on Rakt Doot: matching donors are alerted in minutes."),
    )
