#!/usr/bin/env python3
"""Robot MAX: hurtowe wyslanie zamowien do faktury Optima (OpSync).
Dla listy ID (env IDS): pomija zamowienia z FS w Pole dodatkowe 2 i zamowienia miesieczne,
NAJPIERW poprawia VAT (jak fix_vat.py), POTEM ustawia status 'Do zafakturowania' (166441).
Domyslnie dry-run. Zapis: APPLY=1.
"""
import os, time
import fix_vat as fv

IDS = [int(x) for x in os.environ.get("IDS", "").replace(" ", "").split(",") if x]
APPLY = os.environ.get("APPLY") == "1"
done = skipped = vat = 0
for oid in IDS:
    res = fv.call("getOrders", {"order_id": oid, "get_unconfirmed_orders": True}).get("orders", [])
    if not res:
        print(f"{oid}: nie znaleziono", flush=True); skipped += 1; continue
    o = res[0]
    if "FS/" in str(o.get("extra_field_2", "")):
        print(f"{oid}: juz ma {o.get('extra_field_2')}", flush=True); skipped += 1; continue
    if fv.is_monthly(o):
        print(f"{oid}: miesieczne/0 zl -> pomijam", flush=True); skipped += 1; continue
    t = fv.target_rate(o)
    if t is not None:
        for p in o["products"]:
            if float(p["price_brutto"]) > 0 and float(p["tax_rate"]) != t:
                print(f"{oid} {o['delivery_country_code']} {p['sku']}: VAT {p['tax_rate']} -> {t}", flush=True)
                vat += 1
                if APPLY:
                    fv.call("setOrderProductFields", {"order_id": oid, "order_product_id": p["order_product_id"], "tax_rate": t})
    print(f"{oid}: -> Do zafakturowania", flush=True)
    done += 1
    if APPLY:
        fv.call("setOrderStatus", {"order_id": oid, "status_id": fv.STATUS_ID})
    time.sleep(0.7)
print(f"do faktury: {done}, pominiete: {skipped}, zmiany VAT: {vat}, tryb: {'APPLY' if APPLY else 'DRY-RUN'}")
