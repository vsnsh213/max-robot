"""Robot VAT nie zmienia zamowien zabranych przez OpSync (bez sieci).
python3 -m unittest discover -s tests -v"""
import os, sys, io, unittest, contextlib
from unittest import mock

os.environ.setdefault("BLT", "test-token-niezywy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fix_vat as fv

DO_ZAF = 166441


def order(**kw):
    o = {"order_id": 1, "order_status_id": DO_ZAF, "delivery_country_code": "DE", "invoice_country_code": "",
         "invoice_nip": "", "invoice_company": "", "invoice_fullname": "Jan Test", "delivery_fullname": "Jan Test",
         "delivery_company": "", "email": "jan@example.com", "order_source": "shop",
         "products": [{"tax_rate": 0, "price_brutto": 100.0, "quantity": 1, "sku": "BE20406", "order_product_id": 11}],
         "date_add": 1000, "extra_field_1": "", "extra_field_2": ""}
    o.update(kw)
    return o


class Api:
    """Podmienia fv.call. listing = to, co zwraca getOrders po statusie; fresh = odczyt po order_id."""
    def __init__(self, listing, fresh=None):
        self.listing, self.fresh, self.calls = listing, fresh, []

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "getOrders":
            if "order_id" in params:
                f = self.fresh if self.fresh is not None else self.listing
                return {"orders": [x for x in f if x["order_id"] == params["order_id"]]}
            return {"orders": self.listing}
        return {"status": "SUCCESS"}

    def writes(self):
        return [c for c in self.calls if c[0].startswith(("set", "add", "update", "delete"))]


def run(api, apply=True):
    out = io.StringIO()
    with mock.patch.object(fv, "call", api), mock.patch.object(fv, "APPLY", apply), contextlib.redirect_stdout(out):
        fv.main()
    return out.getvalue()


class TakenByOpsync(unittest.TestCase):
    def test_markers(self):
        self.assertTrue(fv.taken_by_opsync(order(extra_field_1="[ZAIMPORTOWANE]")))
        self.assertTrue(fv.taken_by_opsync(order(extra_field_1="[zaimportowane] 28.11")))
        self.assertTrue(fv.taken_by_opsync(order(extra_field_2="FS/264/11/2026")))
        self.assertTrue(fv.taken_by_opsync(order(extra_field_2="FS/1/2026")))  # stary format

    def test_not_markers(self):
        for kw in ({}, {"extra_field_1": "303-1234567-1234567", "extra_field_2": "Fulfillment: MFN"},  # Amazon
                   {"extra_field_2": "fulfilled_by_merchant"},                                     # Kaufland
                   {"extra_field_1": "DOLCEVITA"},                                                  # partner sklepu
                   {"extra_field_1": "NIEOPLACONE w Allegro - nie wysylac"},                        # notatka biura
                   {"extra_field_2": "332/10/2026"}):                                               # nie FS
            self.assertFalse(fv.taken_by_opsync(order(**kw)), kw)


class Guard(unittest.TestCase):
    def test_imported_order_skipped_no_write(self):
        api = Api([order(extra_field_1="[ZAIMPORTOWANE]")])
        out = run(api)
        self.assertEqual(api.writes(), [])
        self.assertIn("POMINIETO", out)

    def test_fs_in_pole2_skipped_even_monthly(self):
        api = Api([order(extra_field_2="FS/10/11/2026", email="zamowienia@nalokalu.pl")])
        run(api)
        self.assertEqual(api.writes(), [])  # ani VAT, ani zmiana statusu na zbiorcza

    def test_normal_order_still_fixed(self):
        api = Api([order()])
        run(api)
        self.assertEqual([(m, p.get("tax_rate")) for m, p in api.writes()], [("setOrderProductFields", 19)])

    def test_amazon_mfn_still_fixed(self):
        api = Api([order(order_source="amazon", extra_field_1="303-1234567-1234567", extra_field_2="Fulfillment: MFN")])
        run(api)
        self.assertEqual(len(api.writes()), 1)

    def test_opsync_takes_order_during_run_no_write(self):
        # w liscie puste Pole 1, a przy ponownym odczycie tuz przed zapisem OpSync juz wpisal [ZAIMPORTOWANE]
        api = Api([order()], fresh=[order(extra_field_1="[ZAIMPORTOWANE]")])
        out = run(api)
        self.assertEqual(api.writes(), [])
        self.assertIn("w trakcie przebiegu", out)

    def test_status_changed_during_run_no_write(self):
        api = Api([order()], fresh=[order(order_status_id=37715)])
        run(api)
        self.assertEqual(api.writes(), [])

    def test_order_gone_during_run_no_write(self):
        api = Api([order()], fresh=[])
        run(api)
        self.assertEqual(api.writes(), [])

    def test_dry_run_no_recheck_no_write(self):
        api = Api([order()])
        run(api, apply=False)
        self.assertEqual(api.writes(), [])
        self.assertEqual([c for c in api.calls if "order_id" in c[1]], [])

    def test_recheck_only_when_something_to_change(self):
        api = Api([order(products=[{"tax_rate": 19, "price_brutto": 100.0, "quantity": 1, "sku": "X", "order_product_id": 1}])])
        run(api)
        self.assertEqual([c for c in api.calls if "order_id" in c[1]], [])


if __name__ == "__main__":
    unittest.main()
