#!/usr/bin/env python3
"""Robot MAX: pilnuje, zeby stany w magazynie BaseLinker nie spadly do zera przez sprzedaz.

Polityka MAX (Swiat/Lecha): w Base kazdy produkt ma stan 100 szt, zeby oferty na
marketplace'ach byly zawsze aktywne. Sprzedaz zmniejsza stan, a magazyn W2S nie
wysyla stanow do Base - bez tego robota stan po kilku tygodniach spada do 0
i oferty sie koncza (tak bylo 10-26.11).

Zasada:
  - stan od 1 do TARGET-1  -> ustaw TARGET (domyslnie 100)
  - stan >= TARGET          -> bez zmian (np. 200 zostaje 200)
  - stan <= 0               -> NIE ruszamy (ktos mogl wyzerowac celowo), tylko raport
  - SKU z SKIP_SKUS         -> nigdy nie ruszamy
  - ONLY_SKUS (opcjonalnie) -> tylko te SKU (test na kilku produktach)
Domyslnie dry-run (tylko raport). Zapis: APPLY=1.
"""
import os, json, urllib.request, urllib.parse

TOKEN = os.environ["BLT"]
APPLY = os.environ.get("APPLY") == "1"
TARGET = int(os.environ.get("TARGET") or "100")
SKIP = {s.strip().upper() for s in (os.environ.get("SKIP_SKUS") or "").split(",") if s.strip()}
ONLY = {s.strip().upper() for s in (os.environ.get("ONLY_SKUS") or "").split(",") if s.strip()}


def call(method, params):
    data = urllib.parse.urlencode({"method": method, "parameters": json.dumps(params)}).encode()
    req = urllib.request.Request("https://api.baselinker.com/connector.php", data=data,
                                 headers={"X-BLToken": TOKEN})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    if r.get("status") != "SUCCESS":
        raise RuntimeError(f"{method}: {r}")
    return r


def build_items(stock, names, wh):
    """(id do zapisu, sku/opis, nazwa, stan) - dla wariantow osobna pozycja na wariant."""
    items = []
    for pid, p in stock.items():
        sku, name = names.get(pid, ("?", "?"))
        variants = p.get("variants") or {}
        if variants:
            for vid, vst in variants.items():
                items.append((str(vid), f"{sku or pid}/WAR.{vid}", name, int(float((vst or {}).get(wh, 0)))))
        else:
            items.append((pid, sku, name, int(float((p.get("stock") or {}).get(wh, 0)))))
    return items


def main():
    invs = call("getInventories", {})["inventories"]
    if len(invs) != 1:
        raise SystemExit(f"STOP: oczekiwano 1 katalogu, jest {len(invs)}: {[i['name'] for i in invs]}")
    inv = invs[0]
    inv_id = inv["inventory_id"]
    whs = inv.get("warehouses") or []
    if len(whs) != 1:
        raise SystemExit(f"STOP: oczekiwano 1 magazynu w katalogu, jest {whs}")
    wh = whs[0]
    print(f"Katalog {inv['name']} ({inv_id}), magazyn {wh}, TARGET={TARGET}, APPLY={APPLY}")

    # SKU i nazwy
    names = {}
    page = 1
    while True:
        r = call("getInventoryProductsList", {"inventory_id": inv_id, "page": page})
        prods = r.get("products") or {}
        for pid, p in prods.items():
            names[str(pid)] = ((p.get("sku") or "").upper(), p.get("name") or "")
        if len(prods) < 1000:
            break
        page += 1

    # stany
    stock = {}
    page = 1
    while True:
        r = call("getInventoryProductsStock", {"inventory_id": inv_id, "page": page})
        prods = r.get("products") or {}
        for pid, p in prods.items():
            stock[str(pid)] = p
        if len(prods) < 1000:
            break
        page += 1

    upd, zero, skipped, ok = {}, [], [], 0
    items = build_items(stock, names, wh)

    for pid, sku, name, q in items:
        base_sku = sku.split("/WAR.")[0]  # wariant: filtr po SKU rodzica
        if ONLY and sku not in ONLY and base_sku not in ONLY:
            continue
        if sku in SKIP or base_sku in SKIP:
            skipped.append(f"{sku} na liscie SKIP")
            continue
        if q <= 0:
            zero.append(f"{sku} {name[:40]} = {q}")
        elif q < TARGET:
            upd[pid] = {wh: TARGET}
            print(f"  {sku:15} {q:>4} -> {TARGET}  {name[:50]}")
        else:
            ok += 1

    print(f"Do uzupelnienia: {len(upd)}, w porzadku (>= {TARGET}): {ok}, zero/minus: {len(zero)}, pominiete: {len(skipped)}")
    for z in zero:
        print("  ZERO (nie ruszam):", z)
    for s in skipped:
        print("  POMINIETE:", s)
    if ONLY:
        missing = ONLY - {names.get(pid, ('?',))[0] for pid in stock}
        if missing:
            print("  UWAGA: nie znaleziono SKU:", ", ".join(sorted(missing)))

    if APPLY and upd:
        r = call("updateInventoryProductsStock", {"inventory_id": inv_id, "products": upd})
        warn = r.get("warnings") or {}
        print(f"ZAPISANO: {r.get('counter', len(upd))} produktow, ostrzezenia: {warn}")
        if warn:
            raise SystemExit("Byly ostrzezenia przy zapisie - sprawdz")
        # kontrola po zapisie
        after = {}
        page = 1
        while True:
            rr = call("getInventoryProductsStock", {"inventory_id": inv_id, "page": page})
            prods = rr.get("products") or {}
            after.update({str(k): v for k, v in prods.items()})
            if len(prods) < 1000:
                break
            page += 1
        flat = {}
        for k, v in after.items():
            flat[k] = int(float((v.get("stock") or {}).get(wh, -1)))
            for vid, vst in (v.get("variants") or {}).items():
                flat[str(vid)] = int(float((vst or {}).get(wh, -1)))
        bad = [pid for pid in upd if flat.get(pid, -1) < TARGET]
        if bad:
            raise SystemExit(f"KONTROLA: po zapisie stan nadal < {TARGET} dla {bad}")
        print("KONTROLA po zapisie: OK, wszystkie uzupelnione produkty maja >=", TARGET)
    elif upd:
        print("DRY-RUN: nic nie zapisano")


if __name__ == "__main__":
    main()
