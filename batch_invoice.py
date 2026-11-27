#!/usr/bin/env python3
"""Robot MAX: hurtowe wyslanie zamowien do faktury Optima (OpSync).
Dla listy ID (env IDS): pomija zamowienia z FS w Pole dodatkowe 2 i zamowienia miesieczne,
NAJPIERW poprawia VAT (jak fix_vat.py), POTEM ustawia status 'Do zafakturowania' (166441).
Pomija tez zamowienia anulowane / w koszu.

Bezpieczniki (audyt 28.11):
  - tylko zamowienia w statusie 'Wyslane' (37715 = magazyn juz przyjal). Zamowienie z 'Nowe' /
    'Do wysylki' ustawione na 'Do zafakturowania' ominie magazyn W2S (incydent 20-23.11, 31 zamowien).
    Inne statusy tylko przez ALLOWED_STATUS_IDS.
  - pomija, gdy w Pole dodatkowe 1 jest [ZAIMPORTOWANE] (OpSync juz zaimportowal, numer FS
    moze dojsc do Pole 2 nawet po dobie -> druga FS) albo gdy w Base jest juz plik faktury.
  - pomija zamowienia bez paczki kuriera (zasada od 26.11: FS dopiero po paczce).
    REQUIRE_PACKAGE=0 wylacza (np. odbior osobisty) - swiadomie.
Domyslnie dry-run. Zapis: APPLY=1.
"""
import os, time
import fix_vat as fv

WYSLANE = 37715


def ids_from_env():
    return [int(x) for x in os.environ.get("IDS", "").replace(" ", "").split(",") if x]


def allowed_statuses():
    extra = [int(x) for x in os.environ.get("ALLOWED_STATUS_IDS", "").replace(" ", "").split(",") if x]
    return {WYSLANE, *extra}


def has_invoice_file(oid):
    try:
        return bool(fv.call("getInvoices", {"order_id": oid}).get("invoices"))
    except RuntimeError as e:
        # brak uprawnien tokena itp. - nie zgadujemy, traktujemy jak "moze juz jest" -> pomijamy
        print(f"{oid}: nie moge sprawdzic faktur w Base ({e}) -> pomijam dla bezpieczenstwa", flush=True)
        return True


def has_package(oid):
    return bool(fv.call("getOrderPackages", {"order_id": oid}).get("packages"))


def skip_reason(o, st_names, allowed, require_package):
    """Zwraca powod pominiecia albo None (mozna wyslac do faktury). Tylko odczyt."""
    oid = o["order_id"]
    if "FS/" in str(o.get("extra_field_2", "")):
        return f"juz ma {o.get('extra_field_2')}"
    if "ZAIMPORTOWANE" in str(o.get("extra_field_1", "")).upper():
        return "Pole 1 = [ZAIMPORTOWANE] (OpSync juz zaimportowal, FS w drodze)"
    sid = int(o.get("order_status_id") or 0)
    sname = st_names.get(sid, "")
    if "anul" in sname.lower() or "kosz" in sname.lower():
        return f"status '{sname}'"
    if sid not in allowed:
        return f"status '{sname or sid}' - nie Wyslane, magazyn jeszcze nie przyjal (ominie W2S)"
    if fv.is_monthly(o):
        return "miesieczne/0 zl"
    if has_invoice_file(oid):
        return "w Base jest juz faktura/plik"
    if require_package and not has_package(oid):
        return "brak paczki (FS dopiero po wysylce)"
    return None


def main():
    ids = ids_from_env()
    apply = os.environ.get("APPLY") == "1"
    require_package = os.environ.get("REQUIRE_PACKAGE", "1") != "0"
    allowed = allowed_statuses()
    done = skipped = vat = 0
    st_names = {int(s["id"]): s["name"] for s in fv.call("getOrderStatusList", {}).get("statuses", [])}
    for oid in ids:
        res = fv.call("getOrders", {"order_id": oid, "get_unconfirmed_orders": True}).get("orders", [])
        if not res:
            print(f"{oid}: nie znaleziono", flush=True); skipped += 1; continue
        o = res[0]
        why = skip_reason(o, st_names, allowed, require_package)
        if why:
            print(f"{oid}: {why} -> pomijam", flush=True); skipped += 1; continue
        upd = fv.fill_invoice_data(o)
        if upd:
            print(f"{oid}: brak nabywcy -> kopiuje z dostawy pola {fv.mask(upd)}", flush=True)
            if apply:
                fv.call("setOrderFields", dict(order_id=oid, **upd))
        for p, t in fv.vat_changes(o):
            print(f"{oid} {o['delivery_country_code']} {p['sku']}: VAT {p['tax_rate']} -> {t}", flush=True)
            vat += 1
            if apply:
                fv.call("setOrderProductFields", {"order_id": oid, "order_product_id": p["order_product_id"], "tax_rate": t})
        print(f"{oid}: -> Do zafakturowania", flush=True)
        done += 1
        if apply:
            fv.call("setOrderStatus", {"order_id": oid, "status_id": fv.STATUS_ID})
        time.sleep(0.7)
    print(f"do faktury: {done}, pominiete: {skipped}, zmiany VAT: {vat}, tryb: {'APPLY' if apply else 'DRY-RUN'}")


if __name__ == "__main__":
    main()
