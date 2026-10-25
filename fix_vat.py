#!/usr/bin/env python3
"""Robot MAX: korekta stawek VAT w zamowieniach BaseLinker przed importem do Optimy (OpSync).

Dla zamowien w statusie STATUS_ID (domyslnie 166441 'Do zafakturowania'):
  - osoba prywatna (brak NIP/firmy) z dostawa do kraju UE (poza PL): stawka = stawka OSS kraju dostawy
  - osoba prywatna z dostawa do PL: 23%
  - firmy (NIP) i kraje spoza UE: bez zmian
Cena brutto pozycji NIE jest zmieniana. Pozycje o cenie 0 sa pomijane.
Domyslnie tryb 'dry-run' (tylko raport). Zapis: APPLY=1.
"""
import os, sys, json, time, urllib.request, urllib.parse

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

def target_rate(o):
    firm = bool((o.get("invoice_nip") or "").strip()) or bool((o.get("invoice_company") or "").strip())
    if firm:
        return None
    c = o["delivery_country_code"]
    return 23 if c == "PL" else OSS.get(c)

def main():
    since = int(time.time()) - 45 * 86400
    orders, d = [], since
    while True:
        o = call("getOrders", {"status_id": STATUS_ID, "date_from": d, "get_unconfirmed_orders": True}).get("orders", [])
        if not o:
            break
        orders += o
        d = max(x["date_add"] for x in o) + 1
        if len(o) < 100:
            break
    changes = 0
    for o in orders:
        t = target_rate(o)
        if t is None:
            continue
        for p in o["products"]:
            if float(p["price_brutto"]) > 0 and float(p["tax_rate"]) != t:
                print(f"order {o['order_id']} {o['delivery_country_code']} {p['sku']}: VAT {p['tax_rate']} -> {t}", flush=True)
                changes += 1
                if APPLY:
                    call("setOrderProductFields", {"order_id": o["order_id"], "order_product_id": p["order_product_id"], "tax_rate": t})
    print(f"orders in status: {len(orders)}, lines to change: {changes}, mode: {'APPLY' if APPLY else 'DRY-RUN'}")

if __name__ == "__main__":
    main()
