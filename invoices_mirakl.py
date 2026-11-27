#!/usr/bin/env python3
"""Robot MAX: faktury PDF z Base -> Empik / Media Expert (Mirakl).

Dla zamowien z Base (status STATUS_IDS, domyslnie Wyslane) z danego marketplace:
  1. bierze numer zamowienia z marketplace (external_order_id),
  2. sprawdza w Mirakl, czy w zamowieniu jest juz faktura (OR72) - jesli jest, pomija,
  3. bierze PDF faktury z Base (getInvoices + getInvoiceFile get_external=true),
  4. tylko przy APPLY=1 wysyla PDF do zamowienia w Mirakl (OR74).
Domyslnie dry-run (APPLY=0): nic nie wysyla, tylko raport.
Bezpieczniki: ONLY_ORDERS (lista ID z Base), MAX_UPLOADS (domyslnie 1).
"""
import os, re, sys, json, time, base64, uuid, urllib.request, urllib.parse, urllib.error

BLT = os.environ["BLT"]
APPLY = os.environ.get("APPLY") == "1"
MAX_UPLOADS = int(os.environ.get("MAX_UPLOADS", "1") or "1")
ONLY = {x.strip() for x in os.environ.get("ONLY_ORDERS", "").split(",") if x.strip()}
DAYS = int(os.environ.get("DAYS", "40") or "40")
STATUS_IDS = [int(x) for x in os.environ.get("STATUS_IDS", "37715").split(",") if x.strip()]
TYPE_CODE = os.environ.get("TYPE_CODE", "CUSTOMER_INVOICE")
WHICH = os.environ.get("MARKETS", "empik,mediaexpert")

MARKETS = {
    "empik": {"base": "https://marketplace.empik.com", "key": os.environ.get("EMPIK_API_KEY", ""), "src": ("empik",)},
    "mediaexpert": {"base": "https://tergpl-prod.mirakl.net", "key": os.environ.get("MEDIAEXPERT_API_KEY", ""), "src": ("tergpl", "mediaexpert", "media expert")},
}

def bl(method, params):
    data = urllib.parse.urlencode({"method": method, "parameters": json.dumps(params)}).encode()
    req = urllib.request.Request("https://api.baselinker.com/connector.php", data=data, headers={"X-BLToken": BLT})
    r = json.load(urllib.request.urlopen(req, timeout=90))
    if r.get("status") != "SUCCESS":
        raise RuntimeError(f"{method}: {r.get('error_code')} {r.get('error_message')}")
    return r

def mk(m, path, method="GET", body=None, ctype=None):
    req = urllib.request.Request(m["base"] + path, data=body, method=method,
                                 headers={"Authorization": m["key"], "Accept": "application/json"})
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip().startswith(b"{") else raw.decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:500]

def orders_from_base():
    since = int(time.time()) - DAYS * 86400
    out = {}
    for sid in STATUS_IDS:
        frm = since
        while True:
            r = bl("getOrders", {"status_id": sid, "date_confirmed_from": frm, "get_unconfirmed_orders": False})
            batch = r.get("orders", [])
            for o in batch:
                out[o["order_id"]] = o
            if len(batch) < 100:
                break
            frm = max(o["date_confirmed"] for o in batch) + 1
    return list(out.values())

def market_of(o):
    src = f"{o.get('order_source','')} {o.get('order_source_info','')}".lower()
    for name, m in MARKETS.items():
        if any(s in src for s in m["src"]):
            return name
    return None

def invoice_pdf(order_id):
    inv = bl("getInvoices", {"order_id": order_id}).get("invoices", [])
    if not inv:
        return None, None, "brak faktury w Base"
    inv = sorted(inv, key=lambda i: i.get("date_add", 0))[-1]
    f = bl("getInvoiceFile", {"invoice_id": inv["invoice_id"], "get_external": True})
    b64 = f.get("invoice", "")
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    pdf = base64.b64decode(b64) if b64 else b""
    num = f.get("invoice_number") or inv.get("number") or str(inv["invoice_id"])
    if not pdf.startswith(b"%PDF"):
        return None, num, "plik z Base to nie PDF"
    return pdf, num, ""

def existing_docs(m, ext_id):
    st, js = mk(m, "/api/orders/documents?order_ids=" + urllib.parse.quote(ext_id))
    if st != 200 or not isinstance(js, dict):
        return None, f"OR72 blad {st}: {str(js)[:200]}"
    return js.get("order_documents", []), ""

def upload(m, ext_id, pdf, fname):
    bnd = "----max" + uuid.uuid4().hex
    meta = json.dumps({"order_documents": [{"file_name": fname, "type_code": TYPE_CODE}]})
    body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{fname}\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n").encode() + pdf + (
            f"\r\n--{bnd}\r\nContent-Disposition: form-data; name=\"order_documents\"\r\n"
            f"Content-Type: application/json\r\n\r\n{meta}\r\n--{bnd}--\r\n").encode()
    return mk(m, f"/api/orders/{urllib.parse.quote(ext_id)}/documents", "POST", body, f"multipart/form-data; boundary={bnd}")

def main():
    print(f"Tryb: {'ZAPIS' if APPLY else 'DRY-RUN (nic nie wysylam)'}; MAX_UPLOADS={MAX_UPLOADS}; ONLY={sorted(ONLY) or '-'}")
    active = [n for n in WHICH.split(",") if n in MARKETS]
    for n in active:
        m = MARKETS[n]
        if not m["key"]:
            print(f"[{n}] BRAK KLUCZA - pomijam"); continue
        st, js = mk(m, "/api/documents")
        codes = [d.get("code") for d in js.get("documents", [])] if isinstance(js, dict) else []
        print(f"[{n}] polaczenie: {st}; typy dokumentow: {codes}")
        if APPLY and TYPE_CODE not in codes:
            print(f"[{n}] STOP: typ {TYPE_CODE} nie istnieje w tym marketplace"); return 1
    orders = orders_from_base()
    srcs = sorted({f"{o.get('order_source')}|{o.get('order_source_id')}" for o in orders})
    print(f"Zamowien w Base ({DAYS} dni, statusy {STATUS_IDS}): {len(orders)}; zrodla: {srcs}")
    done = 0
    for o in sorted(orders, key=lambda x: x["date_confirmed"]):
        n = market_of(o)
        if n not in active:
            continue
        oid = str(o["order_id"])
        if ONLY and oid not in ONLY:
            continue
        m = MARKETS[n]
        ext = (o.get("external_order_id") or "").strip()
        tag = f"[{n}] Base {oid} / {ext or '?'} / {o.get('invoice_fullname') or o.get('delivery_fullname')}"
        if not ext:
            print(f"{tag}: brak numeru marketplace - pomijam"); continue
        docs, err = existing_docs(m, ext)
        if docs is None:
            print(f"{tag}: {err}"); continue
        have = [d.get("file_name") for d in docs if d.get("type") == TYPE_CODE or "invoice" in str(d.get("type", "")).lower()]
        if have:
            print(f"{tag}: faktura juz jest {have} - pomijam"); continue
        pdf, num, err = invoice_pdf(o["order_id"])
        if not pdf:
            print(f"{tag}: {err} ({num or '-'})"); continue
        fname = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{num}.pdf")
        if not APPLY:
            print(f"{tag}: DO WYSLANIA {num} ({len(pdf)} B), dokumenty w zamowieniu: {[d.get('type') for d in docs]}")
            continue
        if done >= MAX_UPLOADS:
            print(f"{tag}: limit MAX_UPLOADS - zostawiam na pozniej"); continue
        st, js = upload(m, ext, pdf, fname)
        docs2, _ = existing_docs(m, ext)
        ok = st in (200, 201, 204) and any(d.get("file_name") == fname for d in (docs2 or []))
        print(f"{tag}: WYSLANO {num} -> HTTP {st}; sprawdzenie OR72: {'OK' if ok else 'BRAK'} {str(js)[:200]}")
        done += 1
        if not ok:
            print("STOP: wysylka niepotwierdzona"); return 1
    print(f"Koniec. Wyslano: {done}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
