"""Load demo data: ``python -m agri_ai.seed``.

Creates a co-operative, a telco partner and a buyer around Nakuru, Kenya, with
farmers, plots, two weeks of market prices, some diagnoses and open supply, and
prints API keys and a farmer phone number to log in with.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from sqlalchemy import select

from .db import create_all, session_factory
from .models import ApiKey, Diagnosis, DiagnosisStatus, Farmer, MarketPrice, Organization, Plot, Role, utcnow
from .security import generate_api_key
from .services import marketplace
from .services.farmers import create_listing

MARKETS = [("Nakuru", -0.283, 36.067), ("Njoro", -0.330, 35.944), ("Molo", -0.249, 35.732), ("Eldoret", 0.514, 35.270)]
BASE_PRICE = {"maize": 42, "beans": 110, "potato": 35, "tomato": 60}
NAMES = ["Wanjiku Kamau", "Otieno Ochieng", "Achieng Atieno", "Kiprono Kirui", "Mwangi Njoroge", "Chebet Jepkosgei", "Nyambura Wairimu",
         "Kibet Langat", "Akinyi Odhiambo", "Mutua Musyoka", "Jeptoo Rotich", "Wafula Barasa"]


def _org(db, **fields) -> tuple[Organization, str]:
    org = Organization(**fields)
    db.add(org)
    db.flush()
    key, prefix, digest = generate_api_key()
    db.add(ApiKey(org_id=org.id, name="demo admin", role=Role.admin.value, prefix=prefix, key_hash=digest))
    return org, key


def main() -> None:
    create_all()
    rnd = random.Random(7)
    with session_factory()() as db:
        if db.scalar(select(Organization.id)):
            print("Database already has data; seed skipped. Delete agri_ai.db to reseed.")
            return
        coop, coop_key = _org(
            db, name="Rift Valley Farmers Co-operative", brand_name="RiftFarm", kind="cooperative", country="KE", currency="KES",
            sms_shortcode="22384", ussd_code="*384*1#", contact_phone="+254700100100",
        )
        telco, telco_key = _org(db, name="Safari Mobile", brand_name="Safari Kilimo", kind="telco", country="KE", currency="KES",
                                sms_shortcode="40404", ussd_code="*404#")
        buyer, buyer_key = _org(db, name="Unga Millers Ltd", kind="buyer", country="KE", currency="KES", contact_phone="+254700999999")

        today = date.today()
        farmers = []
        for i, name in enumerate(NAMES):
            org = coop if i < 9 else telco
            lat, lon = -0.33 + rnd.uniform(-0.12, 0.12), 35.94 + rnd.uniform(-0.15, 0.15)
            f = Farmer(
                org_id=org.id, phone=f"+2547110000{i:02d}", name=name, language=["en", "sw", "sw", "en"][i % 4],
                gender="female" if i % 2 == 0 else "male", region="Nakuru", village=["Njoro", "Mau Narok", "Molo", "Rongai"][i % 4],
                lat=round(lat, 4), lon=round(lon, 4), consent_marketplace=i % 3 != 2, last_active_at=utcnow() - timedelta(days=rnd.randint(0, 20)),
            )
            db.add(f)
            db.flush()
            crops = [["maize", "beans"], ["maize"], ["potato", "beans"], ["tomato"]][i % 4]
            for c in crops:
                db.add(Plot(farmer_id=f.id, crop=c, area_ha=round(rnd.uniform(0.3, 2.0), 1), planting_date=today - timedelta(days=rnd.randint(15, 80))))
            farmers.append(f)
        db.flush()

        for days_ago in range(14, -1, -1):
            for market, lat, lon in MARKETS:
                for crop, base in BASE_PRICE.items():
                    drift = 1 + (14 - days_ago) * (0.012 if crop == "maize" else -0.004)
                    db.add(MarketPrice(crop=crop, market=market, lat=lat, lon=lon, country="KE", currency="KES", source="demo",
                                       org_id=coop.id, price_per_kg=round(base * drift * rnd.uniform(0.95, 1.05), 1),
                                       observed_on=today - timedelta(days=days_ago)))

        for f, pest, status, conf in [
            (farmers[0], "fall_armyworm", DiagnosisStatus.auto, 0.91),
            (farmers[1], "fall_armyworm", DiagnosisStatus.confirmed, 1.0),
            (farmers[4], "fall_armyworm", DiagnosisStatus.auto, 0.86),
            (farmers[2], "bean_rust", DiagnosisStatus.needs_review, 0.48),
            (farmers[3], "tomato_late_blight", DiagnosisStatus.needs_review, 0.55),
        ]:
            db.add(Diagnosis(org_id=f.org_id, farmer_id=f.id, crop="maize" if pest == "fall_armyworm" else ("beans" if pest == "bean_rust" else "tomato"),
                             source="photo", engine="llm", status=status.value, pest_id=pest, label=pest.replace("_", " ").title(),
                             confidence=conf, lat=f.lat, lon=f.lon, symptoms_text="leaves damaged",
                             result={"visible_evidence": "demo record", "actions": []}))
        db.flush()

        for f in farmers:
            if f.consent_marketplace and "maize" in f.crops:
                create_listing(db, f, "maize", rnd.choice([270, 450, 900]), min_price_per_kg=rnd.choice([None, 38, 40]))
        db.flush()
        from .models import BuyRequest

        req = BuyRequest(buyer_org_id=buyer.id, crop="maize", quantity_kg=2000, price_per_kg=44, currency="KES", lat=-0.30, lon=35.95, radius_km=60,
                         min_grade="C", notes="Delivery to Nakuru mill")
        db.add(req)
        db.flush()
        marketplace.run_matching(db, req, notify=False)
        db.commit()

    print("Demo data loaded.\n")
    print(f"  Co-operative console key : {coop_key}")
    print(f"  Telco console key        : {telco_key}")
    print(f"  Buyer console key        : {buyer_key}")
    print("  Farmer app login         : phone 0711000000 (code is shown in dev mode)")
    print("\nOpen http://localhost:8000/dashboard and http://localhost:8000/app")


if __name__ == "__main__":
    main()
