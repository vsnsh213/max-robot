#!/usr/bin/env python3
"""Robot MAX: straznik (watchdog). Tylko odczyt Base API i GitHub API.

Sprawdza, czy stany i faktury nie zepsuly sie po cichu:
  1. ostatni udany przebieg keep-stock starszy niz 4 h
  2. ostatni udany przebieg fix-vat starszy niz 30 min
  3. stan produktu lub wariantu w Base < 50
  4. zamowienie w "Do zafakturowania" (166441) dluzej niz 1 h (+ czy e-mail ma "@")
  5. FS w Pole dodatkowe 2 bez PDF (faktura zewnetrzna) dluzej niz 2 h
  6. oplacone zamowienie w "Nowe" (37713) dluzej niz 24 h (lista dla Swiata)

Jesli cos jest nie tak: jeden e-mail na przebieg (SEND_MAIL=1) i kod wyjscia 1 (czerwony przebieg).
Wszystko w porzadku: brak e-maila, kod 0.
W logu i w e-mailu tylko numery zamowien, SKU i ID - bez imion, adresow i e-maili klientow (repo publiczne).
"""
import os, re, sys, json, time, calendar, smtplib, urllib.request, urllib.parse
from email.message import EmailMessage
from email.utils import formatdate

BASE_URL = "https://api.baselinker.com/connector.php"
GH_API = "https://api.github.com"

STATUS_DO_ZAF = 166441
STATUS_NOWE = 37713

KEEP_STOCK_MAX_AGE = 4 * 3600
FIX_VAT_MAX_AGE = 30 * 60
STOCK_MIN = int(os.environ.get("STOCK_MIN", "50"))
DO_ZAF_MAX_AGE = 3600
FS_NO_PDF_MAX_AGE = 2 * 3600
NOWE_MAX_AGE = 24 * 3600
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))
FS_RE = re.compile(r"^\s*FS/\d+/\d+/\d{4}\s*$")


def env_ids(name):
    return {int(x) for x in re.findall(r"\d+", os.environ.get(name, ""))}


# ---------- API (tylko odczyt) ----------

def bl(method, params):
    if not method.startswith("get"):  # straznik nigdy nic nie zapisuje
        raise RuntimeError(f"zablokowano metode zapisu: {method}")
    data = urllib.parse.urlencode({"method": method, "parameters": json.dumps(params)}).encode()
    req = urllib.request.Request(BASE_URL, data=data, headers={"X-BLToken": os.environ["BLT"]})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    if r.get("status") != "SUCCESS":
        raise RuntimeError(f"Base {method}: {r.get('error_code')} {r.get('error_message')}")
    return r


def gh(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "max-robot-watchdog"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(GH_API + path, headers=headers)  # tylko GET
    return json.load(urllib.request.urlopen(req, timeout=30))


# ---------- pobieranie danych ----------

def last_success_time(repo, workflow):
    """Czas zakonczenia ostatniego udanego przebiegu workflow (unix) albo None."""
    r = gh(f"/repos/{repo}/actions/workflows/{workflow}/runs?status=success&per_page=1")
    runs = r.get("workflow_runs") or []
    if not runs:
        return None
    ts = runs[0].get("updated_at") or runs[0].get("created_at")
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def fetch_stock(inventory_id):
    out, page = {}, 1
    while True:
        r = bl("getInventoryProductsStock", {"inventory_id": inventory_id, "page": page})
        prods = r.get("products") or {}
        out.update(prods)
        if len(prods) < 1000:
            return out
        page += 1


def fetch_skus(inventory_id, product_ids):
    """SKU produktow i wariantow (tylko dla tych z problemem)."""
    skus = {}
    ids = sorted({int(p) for p in product_ids})
    for i in range(0, len(ids), 100):
        r = bl("getInventoryProductsData", {"inventory_id": inventory_id, "products": ids[i:i + 100]})
        for pid, p in (r.get("products") or {}).items():
            skus[str(pid)] = p.get("sku") or ""
            for vid, v in (p.get("variants") or {}).items():
                skus[str(vid)] = v.get("sku") or ""
    return skus


def fetch_orders(params, key):
    """Wszystkie zamowienia (getOrders zwraca max 100), bez gubienia tej samej sekundy na granicy."""
    out, p = {}, dict(params)
    field = "date_confirmed_from" if "date_confirmed_from" in p else "date_from"
    while True:
        batch = bl("getOrders", p).get("orders", [])
        new = [x for x in batch if x["order_id"] not in out]
        for x in batch:
            out[x["order_id"]] = x
        if len(batch) < 100:
            return list(out.values())
        nd = max(int(x[key]) for x in batch)
        p[field] = nd if new and nd > p[field] else nd + 1


def fetch_external_invoice_orders(since):
    """order_id zamowien, do ktorych wgrano PDF z Optimy (faktura z external_invoice_number)."""
    out, last = set(), None
    while True:
        p = {"date_from": since, "get_external_invoices": True}
        if last:
            p["id_from"] = last
        inv = bl("getInvoices", p).get("invoices", [])
        for x in inv:
            if (x.get("external_invoice_number") or "").strip():
                out.add(int(x["order_id"]))
        if len(inv) < 100:
            return out
        nl = max(int(x["invoice_id"]) for x in inv) + 1
        if last and nl <= last:
            return out
        last = nl


# ---------- sprawdzenia (czyste funkcje, testowane bez sieci) ----------

def order_total(o):
    return round(sum(float(p["price_brutto"]) * float(p["quantity"]) for p in o.get("products", []))
                 + float(o.get("delivery_price") or 0), 2)


def hours(sec):
    return f"{sec / 3600:.1f} ч"


def check_run_age(name, finished_at, max_age, now):
    if finished_at is None:
        return [f"{name}: нет ни одного успешного запуска"]
    age = now - finished_at
    if age > max_age:
        return [f"{name}: последний успешный запуск {hours(age)} назад (норма до {hours(max_age)})"]
    return []


def check_stock(stock, minimum):
    """stock: wynik getInventoryProductsStock. Produkt z wariantami - sprawdzamy warianty
    (stan rodzica to suma wariantow). Zwraca liste (id, id_rodzica albo None, stan) ponizej minimum."""
    bad = []
    for pid, s in sorted(stock.items(), key=lambda kv: int(kv[0])):
        variants = s.get("variants") or {}
        if variants:
            for vid, vs in sorted(variants.items(), key=lambda kv: int(kv[0])):
                q = sum(float(v) for v in (vs or {}).values())
                if q < minimum:
                    bad.append((int(vid), int(pid), q))
        else:
            q = sum(float(v) for v in (s.get("stock") or {}).values())
            if q < minimum:
                bad.append((int(pid), None, q))
    return bad


def format_stock(bad, skus):
    out = []
    for iid, parent, q in bad:
        where = f"вариант {iid} товара {parent}" if parent else f"товар {iid}"
        out.append(f"{skus.get(str(iid)) or 'без SKU'} — {where}: {q:g} шт")
    return out


def check_do_zaf(orders, now, max_age=DO_ZAF_MAX_AGE):
    out = []
    for o in sorted(orders, key=lambda o: o["order_id"]):
        if o.get("order_status_id") not in (None, STATUS_DO_ZAF):
            continue
        age = now - int(o.get("date_in_status") or now)
        if age > max_age:
            out.append((o["order_id"], age, "@" in (o.get("email") or "")))
    return out


def check_fs_without_pdf(orders, pdf_order_ids, now, ignore=frozenset(), max_age=FS_NO_PDF_MAX_AGE):
    """FS w Pole dodatkowe 2, a PDF z Optimy nie wgrany. Wiek liczony od date_in_status
    (OpSync wpisuje FS i zmienia status w tym samym kroku)."""
    out = []
    for o in sorted(orders, key=lambda o: o["order_id"]):
        fs = (o.get("extra_field_2") or "").strip()
        if not FS_RE.match(fs) or o["order_id"] in pdf_order_ids or o["order_id"] in ignore:
            continue
        age = now - int(o.get("date_in_status") or now)
        if age > max_age:
            out.append((o["order_id"], fs, age))
    return out


def check_nowe_paid(orders, now, max_age=NOWE_MAX_AGE):
    out = []
    for o in sorted(orders, key=lambda o: o["order_id"]):
        if o.get("order_status_id") not in (None, STATUS_NOWE):
            continue
        total = order_total(o)
        paid = float(o.get("payment_done") or 0)
        if total > 0 and paid + 0.01 >= total:
            age = now - int(o.get("date_in_status") or now)
            if age > max_age:
                out.append((o["order_id"], age))
    return out


# ---------- raport ----------

def build_report(sections):
    """sections: lista (tytul, [linie], co zrobic). Puste sekcje pomijane. Zwraca tekst albo ''."""
    parts = []
    for title, lines, todo in sections:
        if not lines:
            continue
        parts.append(f"❗ {title}\n" + "\n".join(f"  - {l}" for l in lines) + f"\n  Что сделать: {todo}")
    if not parts:
        return ""
    head = f"Сторож MAX, {time.strftime('%d.%m.%Y %H:%M', time.localtime())}: найдено проблем: {len(parts)}."
    return head + "\n\n" + "\n\n".join(parts) + "\n\n(письмо автоматическое, репо max-robot, workflow watchdog.yml)"


def send_mail(text, n):
    msg = EmailMessage()
    msg["Subject"] = f"[MAX сторож] проблем: {n}"
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = os.environ.get("MAIL_TO", "biuro@elna-service.pl")
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                          int(os.environ.get("SMTP_PORT", "465")), timeout=30) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def collect(now):
    repo = os.environ.get("REPO", "vsnsh213/max-robot")
    inv_id = int(os.environ.get("INVENTORY_ID", "6095"))
    sections, errors = [], []

    def safe(label, fn):
        try:
            return fn()
        except Exception as e:  # blad jednej kontroli nie zatrzymuje pozostalych
            errors.append(f"{label}: {type(e).__name__}: {str(e)[:200]}")
            return None

    ks = safe("GitHub keep-stock", lambda: check_run_age("Робот «Stany 100» (keep-stock)",
              last_success_time(repo, "keep-stock.yml"), KEEP_STOCK_MAX_AGE, now))
    fv = safe("GitHub fix-vat", lambda: check_run_age("Робот НДС (fix-vat)",
              last_success_time(repo, "fix-vat.yml"), FIX_VAT_MAX_AGE, now))
    sections.append(("Роботы не запускаются", (ks or []) + (fv or []),
                     "проверить cron-job.org (Robot VAT / Robot Stany 100) и вкладку Actions в GitHub"))

    def stock_check():
        bad = check_stock(fetch_stock(inv_id), STOCK_MIN)
        skus = fetch_skus(inv_id, {parent or iid for iid, parent, _ in bad}) if bad else {}
        return format_stock(bad, skus)
    sections.append((f"Остаток в Base ниже {STOCK_MIN}", safe("Base остатки", stock_check) or [],
                     "проверить, пишет ли склад остатки и работает ли «Stany 100»; товар может выпасть из продажи"))

    dz = safe("Base Do zafakturowania", lambda: check_do_zaf(
        fetch_orders({"status_id": STATUS_DO_ZAF, "date_from": now - 90 * 86400, "get_unconfirmed_orders": True},
                     "date_add"), now))
    sections.append(("Заказ висит в «Do zafakturowania» дольше 1 ч",
                     [f"{oid}: {hours(age)}, e-mail {'с @' if at else 'БЕЗ @ (OpSync падает на пустом/неверном e-mail)'}"
                      for oid, age, at in (dz or [])],
                     "проверить e-mail и данные заказа; посмотреть журнал OpSync на сервере Optima"))

    def fs_check():
        since = now - LOOKBACK_DAYS * 86400
        orders = fetch_orders({"date_confirmed_from": since, "get_unconfirmed_orders": False}, "date_confirmed")
        pdf = fetch_external_invoice_orders(since)
        return check_fs_without_pdf(orders, pdf, now, env_ids("IGNORE_FS_ORDERS"))
    fs = safe("Base FS bez PDF", fs_check)
    sections.append(("FS есть в Pole 2, а PDF Optima в заказе нет дольше 2 ч",
                     [f"{oid}: {num}, {hours(age)}" for oid, num, age in (fs or [])],
                     "Лёхе: проверить FS в Optima (KSeF/буфер) и почему OpSync не загрузил PDF"))

    nw = safe("Base Nowe", lambda: check_nowe_paid(
        fetch_orders({"status_id": STATUS_NOWE, "date_from": now - 90 * 86400, "get_unconfirmed_orders": True},
                     "date_add"), now))
    sections.append(("Оплаченные заказы в «Nowe» дольше 24 ч (для Свята)",
                     [f"{oid}: {hours(age)}" for oid, age in (nw or [])],
                     "перевести в «Do wysłania», чтобы склад их забрал"))

    sections.append(("Сторож не смог проверить", errors,
                     "посмотреть лог запуска watchdog в GitHub Actions"))
    return sections


def main():
    now = int(time.time())
    sections = collect(now)
    text = build_report(sections)
    if not text:
        print("Все проверки в норме, письмо не отправляется.")
        return 0
    n = sum(1 for _, lines, _ in sections if lines)
    print(text)
    if os.environ.get("SEND_MAIL") == "1":
        try:
            send_mail(text, n)
            print(f"\nПисьмо отправлено на {os.environ.get('MAIL_TO', 'biuro@elna-service.pl')}.")
        except Exception as e:
            print(f"\nПИСЬМО НЕ ОТПРАВЛЕНО: {type(e).__name__}: {str(e)[:200]}")
    else:
        print("\n(SEND_MAIL != 1: письмо не отправляется, только лог)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
