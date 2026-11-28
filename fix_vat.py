#!/usr/bin/env python3
"""Robot MAX: korekta stawek VAT w zamowieniach BaseLinker przed importem do Optimy (OpSync).

Dla zamowien w statusie STATUS_ID (domyslnie 166441 'Do zafakturowania'):
  - osoba prywatna (brak NIP/firmy) z dostawa do kraju UE (poza PL): stawka = stawka OSS kraju dostawy
  - osoba prywatna z dostawa do PL: 23%
  - polska firma (NIP/firma, kraj PL) z pozycja 0%: 23% (tylko pozycje 0%)
  - firmy z UE (WDT 0%) i kraje spoza UE: bez zmian
  - brak nazwy nabywcy w danych do faktury: kopiowane z adresu dostawy (imie/firma/adres)
  - Nalokalu, grzejniki.pl, Patroniak (egrzejniki), Gardhill, zamowienia BL Connect i zamowienia o wartosci 0: przeniesione do statusu
    'Fakturowanie zbiorcze' (faktura zbiorcza na koniec miesiaca, recznie)
Zamowienie juz zabrane przez OpSync ("[ZAIMPORTOWANE]" w Pole dodatkowe 1 albo numer FS w Pole dodatkowe 2)
jest pomijane: zmiana po imporcie do Optimy rozjechalaby Base z FS. Przed zapisem zamowienie jest czytane
ponownie, zeby nie zapisac, jesli OpSync zabral je w trakcie przebiegu.
Inne wpisy w Pole 1/2 (Amazon: nr zamowienia + "Fulfillment: MFN", Kaufland: "fulfilled_by_merchant",
notatki biura) NIE blokuja korekty - to nie sa znaczniki OpSync.
Cena brutto pozycji NIE jest zmieniana. Pozycje o cenie 0 sa pomijane.
Domyslnie tryb 'dry-run' (tylko raport). Zapis: APPLY=1.
"""
import os, re, sys, json, time, urllib.request, urllib.parse

TOKEN = os.environ["BLT"]
STATUS_ID = int(os.environ.get("STATUS_ID", "166441"))
APPLY = os.environ.get("APPLY") == "1"
OSS = {'AT':20,'BE':21,'BG':20,'CY':19,'CZ':21,'DE':19,'DK':25,'EE':24,'GR':24,'ES':21,'FI':25.5,'FR':20,
       'HR':25,'HU':27,'IE':23,'IT':22,'LT':21,'LU':17,'LV':21,'MT':18,'NL':21,'PT':23,'RO':21,'SE':25,'SI':22,'SK':23}

def call(method, params):
    data = urllib.parse.urlencode({"method": method, "parameters": json.dumps(params)}).encode()
    req = urllib.request.Request("https://api.baselinker.com/connector.php", data=data, headers={"X-BLToken": TOKEN})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    if r.get("status") != "SUCCESS":
        raise RuntimeError(f"{method}: {r}")
    return r

MONTHLY_STATUS_ID = int(os.environ.get("MONTHLY_STATUS_ID", "170391"))
# "egrzejniki" / "grzejniki.pl" zamiast samego "grzejniki": samo slowo "grzejniki" lapalo kazda firme
# z "grzejniki" w nazwie (np. "Grzejniki Kowalski") i przenosilo ja do zbiorczej bez FS.
MONTHLY_KEYS = ("nalokalu", "egrzejniki", "grzejniki.pl", "patroniak", "gardhill", "hill ceramic")
OWN_EMAILS = ("grzejniki.max@gmail.com",)  # wlasna skrzynka MAX - nie jest partnerem

def is_monthly(o):
    txt = " ".join(str(o.get(k, "")) for k in ("invoice_company", "delivery_company", "invoice_fullname", "email")).lower()
    for e in OWN_EMAILS:
        txt = txt.replace(e, " ")
    if any(k in txt for k in MONTHLY_KEYS):
        return True
    if o.get("order_source") == "blconnect":
        return True
    return sum(float(p["price_brutto"]) * float(p["quantity"]) for p in o["products"]) == 0

INV_FIELDS = (("invoice_fullname", "delivery_fullname"), ("invoice_company", "delivery_company"),
              ("invoice_address", "delivery_address"), ("invoice_postcode", "delivery_postcode"),
              ("invoice_city", "delivery_city"), ("invoice_country_code", "delivery_country_code"))

def fill_invoice_data(o):
    """Jesli w danych do faktury nie ma ani imienia, ani firmy - przepisz z adresu dostawy."""
    if (o.get("invoice_fullname") or "").strip() or (o.get("invoice_company") or "").strip():
        return {}
    upd = {}
    for inv, dlv in INV_FIELDS:
        if not (o.get(inv) or "").strip() and (o.get(dlv) or "").strip():
            upd[inv] = o[dlv]
    return upd

def is_firm(o):
    return bool((o.get("invoice_nip") or "").strip()) or bool((o.get("invoice_company") or "").strip())

def target_rate(o):
    if is_firm(o):
        return None
    c = o["delivery_country_code"]
    return 23 if c == "PL" else OSS.get(c)

def is_pl_firm(o):
    """Firma z Polski: NIP bez obcego prefiksu i kraj nabywcy (lub dostawy) = PL."""
    if not is_firm(o):
        return False
    nip = re.sub(r"[^0-9A-Za-z]", "", o.get("invoice_nip") or "").upper()
    if nip[:2].isalpha() and nip[:2] != "PL":
        return False
    c = (o.get("invoice_country_code") or o.get("delivery_country_code") or "").upper()
    return c == "PL"

EU = set(OSS) | {"PL"}

def eu_firm_without_vat_id(o):
    """Firma spoza PL w UE bez numeru VAT-UE z prefiksem kraju: 0% (WDT) jest watpliwe.
    Robot tego NIE zmienia (decyzja ksiegowej) - tylko ostrzega w raporcie."""
    if not is_firm(o) or is_pl_firm(o):
        return False
    c = (o.get("invoice_country_code") or o.get("delivery_country_code") or "").upper()
    if c not in EU:
        return False
    nip = re.sub(r"[^0-9A-Za-z]", "", o.get("invoice_nip") or "").upper()
    return not (nip[:2].isalpha() and nip[:2] != "PL")

def vat_changes(o):
    """Lista (pozycja, nowa_stawka). Prywatni: stawka PL/OSS. Polska firma: tylko 0% -> 23%."""
    t = target_rate(o)
    pl_firm = t is None and is_pl_firm(o)
    out = []
    for p in o["products"]:
        if float(p["price_brutto"]) <= 0:
            continue
        cur = float(p["tax_rate"])
        if t is not None and cur != t:
            out.append((p, t))
        elif pl_firm and cur == 0:
            out.append((p, 23))
    return out

def fetch_orders(status_id, since, key="date_add", param="date_from", unconfirmed=True):
    """Pobiera WSZYSTKIE zamowienia ze statusu (getOrders zwraca max 100).
    Kolejna strona od max(data) - bez +1, zeby nie zgubic zamowien z ta sama sekunda
    na granicy stron; duplikaty usuwane po order_id."""
    out, d = {}, since
    while True:
        batch = call("getOrders", {"status_id": status_id, param: d, "get_unconfirmed_orders": unconfirmed}).get("orders", [])
        new = [x for x in batch if x["order_id"] not in out]
        for x in batch:
            out[x["order_id"]] = x
        if len(batch) < 100:
            break
        nd = max(int(x[key]) for x in batch)
        d = nd if new and nd > d else nd + 1  # zabezpieczenie przed petla
    return list(out.values())

# znaczniki OpSync: Pole 1 "[ZAIMPORTOWANE]", Pole 2 numer FS (FS/12/11/2026, tez stary format FS/1/2026)
OPSYNC_FS_RE = re.compile(r"\bFS/\d+/(\d+/)?\d{4}\b")

def taken_by_opsync(o):
    """True, jesli OpSync juz zaimportowal zamowienie do Optimy (nie wolno go zmieniac)."""
    p1 = str(o.get("extra_field_1") or "").upper()
    p2 = str(o.get("extra_field_2") or "")
    return "ZAIMPORTOWANE" in p1 or bool(OPSYNC_FS_RE.search(p2))

def fresh_order(order_id):
    """Swieze dane zamowienia tuz przed zapisem (None, jesli nie ma)."""
    for x in call("getOrders", {"order_id": order_id, "get_unconfirmed_orders": True}).get("orders", []):
        if x["order_id"] == order_id:
            return x
    return None

def still_safe(o):
    """Ponowny odczyt przed zapisem: nadal w statusie STATUS_ID i nie zabrane przez OpSync."""
    f = fresh_order(o["order_id"])
    if f is None or int(f.get("order_status_id", STATUS_ID)) != STATUS_ID or taken_by_opsync(f):
        print(f"order {o['order_id']}: POMINIETO - w trakcie przebiegu zabrane przez OpSync lub zmieniony status", flush=True)
        return False
    return True

def mask(upd):
    """Do logu (repo publiczne!) tylko nazwy pol, bez danych osobowych."""
    return sorted(upd)

def main():
    since = int(time.time()) - 45 * 86400
    orders = fetch_orders(STATUS_ID, since)
    changes = 0
    errors = []
    for o in orders:
        # blad API na jednym zamowieniu nie zatrzymuje pozostalych (OpSync i tak je zaraz zabierze)
        try:
            changes += process_order(o)
        except Exception as e:
            errors.append(o["order_id"])
            print(f"order {o['order_id']}: BLAD {e}", flush=True)
    print(f"orders in status: {len(orders)}, lines to change: {changes}, errors: {len(errors)}, mode: {'APPLY' if APPLY else 'DRY-RUN'}")
    if errors:
        sys.exit(f"Bledy w zamowieniach: {errors}")

def process_order(o):
    if taken_by_opsync(o):
        print(f"order {o['order_id']}: POMINIETO - juz zaimportowane przez OpSync (Pole 1/2), bez zmian", flush=True)
        return 0
    if is_monthly(o):
        print(f"order {o['order_id']} -> status 'Fakturowanie zbiorcze'", flush=True)
        if APPLY and still_safe(o):
            call("setOrderStatus", {"order_id": o["order_id"], "status_id": MONTHLY_STATUS_ID})
        return 1
    upd = fill_invoice_data(o)
    if eu_firm_without_vat_id(o):
        print(f"order {o['order_id']}: UWAGA firma z UE bez numeru VAT-UE (prefiks kraju) - 0% WDT watpliwe, sprawdzic", flush=True)
    # stawka liczona z danych PRZED skopiowaniem nabywcy: firma z adresu dostawy nie ma NIP,
    # wiec dla niej poprawna jest stawka jak dla osoby prywatnej (OSS), nie WDT 0%
    vat = vat_changes(o)
    if upd:
        print(f"order {o['order_id']}: brak nabywcy -> kopiuje z dostawy pola {mask(upd)}", flush=True)
    for p, t in vat:
        print(f"order {o['order_id']} {o['delivery_country_code']} {p['sku']}: VAT {p['tax_rate']} -> {t}", flush=True)
    changes = (1 if upd else 0) + len(vat)
    if APPLY and changes and still_safe(o):
        if upd:
            call("setOrderFields", dict(order_id=o["order_id"], **upd))
        for p, t in vat:
            call("setOrderProductFields", {"order_id": o["order_id"], "order_product_id": p["order_product_id"], "tax_rate": t})
    return changes

if __name__ == "__main__":
    main()
