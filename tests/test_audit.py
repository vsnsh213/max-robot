"""Testy bez sieci (audyt 28.11). Uruchomienie: python3 -m unittest discover -s tests -v
Kazde wywolanie API Base / Mirakl jest podmienione - nic nie idzie do zywych systemow."""
import os, sys, io, base64, unittest, contextlib

os.environ.setdefault("BLT", "test-token-niezywy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fix_vat as fv
import batch_invoice as bi
import invoices_mirakl as im
import keep_stock as ks


def prod(rate, price=100.0, sku="SKU1", opid=1):
    return {"tax_rate": rate, "price_brutto": price, "quantity": 1, "sku": sku, "order_product_id": opid}


def order(**kw):
    o = {"order_id": 1, "delivery_country_code": "PL", "invoice_country_code": "", "invoice_nip": "",
         "invoice_company": "", "invoice_fullname": "Jan Test", "delivery_fullname": "Jan Test",
         "delivery_company": "", "email": "jan@example.com", "order_source": "shop",
         "products": [prod(0)], "date_add": 1000, "extra_field_1": "", "extra_field_2": "",
         "order_status_id": 37715}
    o.update(kw)
    return o


class FakeApi:
    """Podmienia call(): odpowiada z przygotowanych danych, zapisuje wszystkie wywolania."""
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def __call__(self, method, params):
        self.calls.append((method, params))
        return self.handler(method, params)

    def writes(self):
        return [c for c in self.calls if c[0].startswith(("set", "add", "update", "delete"))]


# ---------------------------------------------------------------- stawki VAT
class TestVatRates(unittest.TestCase):
    def rates(self, o):
        return [t for _, t in fv.vat_changes(o)]

    def test_private_pl_23(self):
        self.assertEqual(self.rates(order()), [23])

    def test_private_eu_oss(self):
        self.assertEqual(self.rates(order(delivery_country_code="DE")), [19])
        self.assertEqual(self.rates(order(delivery_country_code="CZ")), [21])
        self.assertEqual(self.rates(order(delivery_country_code="FI")), [25.5])

    def test_private_non_eu_untouched(self):
        self.assertEqual(self.rates(order(delivery_country_code="CH")), [])
        self.assertEqual(self.rates(order(delivery_country_code="GB")), [])

    def test_pl_firm_0_to_23_only_zero_lines(self):
        o = order(invoice_nip="5130314604", invoice_company="Firma", invoice_country_code="PL",
                  products=[prod(0, opid=1), prod(8, opid=2), prod(23, opid=3)])
        self.assertEqual([(p["order_product_id"], t) for p, t in fv.vat_changes(o)], [(1, 23)])

    def test_pl_firm_with_pl_prefix(self):
        o = order(invoice_nip="PL 513-031-46-04", invoice_company="Firma", invoice_country_code="PL")
        self.assertEqual(self.rates(o), [23])

    def test_eu_firm_wdt_untouched(self):
        o = order(invoice_nip="EE102645062", invoice_company="OU", invoice_country_code="EE",
                  delivery_country_code="EE")
        self.assertEqual(self.rates(o), [])
        self.assertFalse(fv.eu_firm_without_vat_id(o))

    def test_eu_firm_without_vat_prefix_warns_but_no_change(self):
        # przypadek WESTMAN 82761983: w polu NIP kod rejestrowy bez prefiksu EE
        o = order(invoice_nip="16795639", invoice_company="WESTMAN EHITUS OU", invoice_country_code="EE",
                  delivery_country_code="EE")
        self.assertEqual(self.rates(o), [])          # robot nie zmienia (decyzja ksiegowej)
        self.assertTrue(fv.eu_firm_without_vat_id(o))  # ale ostrzega w raporcie

    def test_eu_company_name_without_nip_warns(self):
        o = order(invoice_company="Some GmbH", invoice_country_code="DE", delivery_country_code="DE")
        self.assertEqual(self.rates(o), [])
        self.assertTrue(fv.eu_firm_without_vat_id(o))

    def test_zero_price_lines_skipped(self):
        self.assertEqual(self.rates(order(products=[prod(0, price=0)])), [])


# ---------------------------------------------------------------- partnerzy / zbiorcza
class TestMonthly(unittest.TestCase):
    def test_partners(self):
        for kw in ({"email": "sklep@egrzejniki.pl"}, {"invoice_company": "NALOKALU sp z o.o."},
                   {"delivery_company": "Gardhill AB"}, {"invoice_fullname": "Olga Patroniak"},
                   {"email": "biuro@grzejniki.pl"}, {"order_source": "blconnect"}):
            self.assertTrue(fv.is_monthly(order(**kw)), kw)

    def test_zero_value(self):
        self.assertTrue(fv.is_monthly(order(products=[prod(23, price=0)])))

    def test_own_mailbox_not_partner(self):
        self.assertFalse(fv.is_monthly(order(email="grzejniki.max@gmail.com")))

    def test_random_firm_with_grzejniki_in_name_is_not_partner(self):
        # wczesniej klucz "grzejniki" przenosil taka firme do zbiorczej -> brak FS
        self.assertFalse(fv.is_monthly(order(invoice_company="Grzejniki Kowalski s.c.", invoice_nip="1234563218")))


# ---------------------------------------------------------------- paginacja getOrders
class TestPagination(unittest.TestCase):
    def test_same_second_on_page_boundary_not_lost(self):
        # 150 zamowien; 100. i 101.-110. maja te sama date_add -> stary kod (max+1) gubil 101-110
        allo = [order(order_id=i, date_add=1000 + min(i, 100)) for i in range(1, 151)]

        def h(method, params):
            d = params["date_from"]
            return {"orders": [o for o in allo if o["date_add"] >= d][:100]}
        api = FakeApi(h)
        with unittest.mock.patch.object(fv, "call", api):
            got = fv.fetch_orders(166441, 0)
        self.assertEqual(sorted(o["order_id"] for o in got), list(range(1, 151)))

    def test_no_infinite_loop_when_100_same_second(self):
        allo = [order(order_id=i, date_add=5000) for i in range(1, 101)]
        api = FakeApi(lambda m, p: {"orders": [o for o in allo if o["date_add"] >= p["date_from"]][:100]})
        with unittest.mock.patch.object(fv, "call", api):
            got = fv.fetch_orders(166441, 0)
        self.assertEqual(len(got), 100)
        self.assertLess(len(api.calls), 5)

    def test_mirakl_pagination_same_second(self):
        import time as _t
        t0 = int(_t.time()) - 86400
        allo = [{"order_id": i, "date_confirmed": t0 + min(i, 100)} for i in range(1, 151)]

        def h(method, params):
            return {"orders": [o for o in allo if o["date_confirmed"] >= params["date_confirmed_from"]][:100]}
        with unittest.mock.patch.object(im, "bl", FakeApi(h)):
            got = im.orders_from_base()
        self.assertEqual(len(got), 150)


# ---------------------------------------------------------------- fix_vat main: bledy API, dane osobowe w logu
class TestFixVatMain(unittest.TestCase):
    def test_api_error_on_one_order_does_not_stop_others_and_fails_run(self):
        orders = [order(order_id=1), order(order_id=2)]

        def h(method, params):
            if method == "getOrders":
                return {"orders": orders}
            if method == "setOrderProductFields" and params["order_id"] == 1:
                raise RuntimeError("ERROR_API timeout")
            return {"status": "SUCCESS"}
        api = FakeApi(h)
        out = io.StringIO()
        with unittest.mock.patch.object(fv, "call", api), unittest.mock.patch.object(fv, "APPLY", True), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                fv.main()
        self.assertIn(2, [p["order_id"] for m, p in api.writes()])  # zamowienie 2 poprawione mimo bledu na 1

    def test_buyer_copy_not_logged(self):
        o = order(invoice_fullname="", invoice_company="", delivery_fullname="Anna Tajna",
                  delivery_address="ul. Prywatna 5", products=[prod(23)])
        api = FakeApi(lambda m, p: {"orders": [o]} if m == "getOrders" else {})
        out = io.StringIO()
        with unittest.mock.patch.object(fv, "call", api), contextlib.redirect_stdout(out):
            fv.main()  # dry-run
        self.assertNotIn("Anna Tajna", out.getvalue())
        self.assertNotIn("Prywatna", out.getvalue())
        self.assertIn("invoice_fullname", out.getvalue())
        self.assertEqual(api.writes(), [])  # dry-run nic nie zapisuje


# ---------------------------------------------------------------- batch_invoice: bezpieczniki
class TestBatchInvoiceGuards(unittest.TestCase):
    ST = {37715: "Wysłane", 166441: "Do zafakturowania", 1: "Nowe", 2: "Do wysłania", 9: "Anulowane"}

    def reason(self, o, invoices=(), packages=("p",), require_package=True):
        def h(method, params):
            if method == "getInvoices":
                return {"invoices": list(invoices)}
            if method == "getOrderPackages":
                return {"packages": list(packages)}
            raise AssertionError(method)
        with unittest.mock.patch.object(fv, "call", FakeApi(h)):
            return bi.skip_reason(o, self.ST, {37715}, require_package)

    def test_ok(self):
        self.assertIsNone(self.reason(order()))

    def test_new_order_blocked_would_bypass_warehouse(self):
        self.assertIn("nie Wyslane", self.reason(order(order_status_id=2)))
        self.assertIn("nie Wyslane", self.reason(order(order_status_id=1)))

    def test_zaimportowane_blocked_double_fs(self):
        self.assertIn("ZAIMPORTOWANE", self.reason(order(extra_field_1="[ZAIMPORTOWANE]")))

    def test_existing_fs(self):
        self.assertIn("juz ma", self.reason(order(extra_field_2="FS/12/11/2026")))

    def test_existing_invoice_file(self):
        self.assertIn("juz faktura", self.reason(order(), invoices=[{"invoice_id": 5}]))

    def test_no_package(self):
        self.assertIn("brak paczki", self.reason(order(), packages=()))
        self.assertIsNone(self.reason(order(), packages=(), require_package=False))

    def test_cancelled(self):
        self.assertIn("Anulowane", self.reason(order(order_status_id=9)))

    def test_invoice_check_error_means_skip(self):
        def h(method, params):
            raise RuntimeError("ERROR_METHOD_NOT_ALLOWED_FOR_TOKEN")
        with unittest.mock.patch.object(fv, "call", FakeApi(h)), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(bi.has_invoice_file(1))


# ---------------------------------------------------------------- invoices_mirakl: tylko prawdziwa FS
class TestMiraklInvoiceNumber(unittest.TestCase):
    def test_fs_ok(self):
        self.assertEqual(im.invoice_number_problem("FS/69/11/2026", {"extra_field_2": "FS/69/11/2026"}), "")

    def test_base_blank_or_fakturaxl_rejected(self):
        # 78403761 w dry-run 27.11 mial "332/10/2026" - robot by go wyslal jako fakture klienta
        self.assertTrue(im.invoice_number_problem("332/10/2026", {}))
        self.assertTrue(im.invoice_number_problem("228/11/2026", {"extra_field_2": "FS/248/11/2026"}))

    def test_mismatch_with_pole2_rejected(self):
        self.assertTrue(im.invoice_number_problem("FS/63/11/2026", {"extra_field_2": "FS/64/11/2026"}))

    def test_data_uri_pdf_decoding(self):
        pdf = b"%PDF-1.4 test"
        b64 = "data:application/pdf;base64," + base64.b64encode(pdf).decode()

        def h(method, params):
            if method == "getInvoices":
                return {"invoices": [{"invoice_id": 7, "date_add": 1}]}
            return {"invoice": b64, "invoice_number": "FS/1/11/2026"}
        with unittest.mock.patch.object(im, "bl", FakeApi(h)):
            got, num, err = im.invoice_pdf(1)
        self.assertEqual((got, num, err), (pdf, "FS/1/11/2026", ""))

    def test_main_dry_run_skips_non_fs_and_logs_no_names(self):
        orders = [{"order_id": 78403761, "date_confirmed": 10, "order_source": "empik", "order_source_info": "",
                   "external_order_id": "40102427564368-A", "invoice_fullname": "Ewa Tajna", "extra_field_2": ""}]
        pdf = base64.b64encode(b"%PDF-1.4").decode()

        def bl(method, params):
            if method == "getOrders":
                return {"orders": orders}
            if method == "getInvoices":
                return {"invoices": [{"invoice_id": 1, "date_add": 1}]}
            if method == "getInvoiceFile":
                return {"invoice": pdf, "invoice_number": "332/10/2026"}
            raise AssertionError(method)

        def mk(m, path, method="GET", body=None, ctype=None):
            assert method == "GET", "dry-run nie moze nic wysylac"
            if path == "/api/documents":
                return 200, {"documents": [{"code": "CUSTOMER_INVOICE"}]}
            return 200, {"order_documents": []}
        markets = {"empik": dict(im.MARKETS["empik"], key="k")}
        out = io.StringIO()
        with unittest.mock.patch.object(im, "bl", bl), unittest.mock.patch.object(im, "mk", mk), \
                unittest.mock.patch.object(im, "MARKETS", markets), unittest.mock.patch.object(im, "WHICH", "empik"), \
                contextlib.redirect_stdout(out):
            im.main()
        self.assertIn("nie wysylam", out.getvalue())
        self.assertNotIn("Ewa Tajna", out.getvalue())


# ---------------------------------------------------------------- keep_stock
class TestKeepStock(unittest.TestCase):
    def test_variant_items_and_filter_by_parent_sku(self):
        names = {"10": ("FLUGER", "Fluger"), "20": ("ATRIA", "Atria")}
        stock = {"10": {"stock": {"bl_1": 50}}, "20": {"variants": {"21": {"bl_1": 5}, "22": {"bl_1": 0}}}}
        items = ks.build_items(stock, names, "bl_1")
        self.assertIn(("21", "ATRIA/WAR.21", "Atria", 5), items)
        self.assertEqual(items[0][3], 50)
        self.assertEqual(sum(1 for i in items if i[1].split("/WAR.")[0] == "ATRIA"), 2)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
