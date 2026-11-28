"""Testy straznika bez sieci: python3 -m unittest discover -s tests -v
Kazda kontrola na podstawionych danych; API Base / GitHub / SMTP nie sa wywolywane."""
import os, sys, unittest
from unittest import mock

os.environ.setdefault("BLT", "test-token-niezywy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import watchdog as wd

NOW = 2_000_000_000
H = 3600


def o(oid, status, age, **kw):
    d = {"order_id": oid, "order_status_id": status, "date_in_status": NOW - age, "email": "a@b.pl",
         "extra_field_2": "", "payment_done": 0, "delivery_price": 0,
         "products": [{"price_brutto": 100, "quantity": 1}]}
    d.update(kw)
    return d


class RunAge(unittest.TestCase):
    def test_fresh_ok(self):
        self.assertEqual(wd.check_run_age("x", NOW - 3 * H, 4 * H, NOW), [])

    def test_old_alert(self):
        self.assertEqual(len(wd.check_run_age("x", NOW - 5 * H, 4 * H, NOW)), 1)

    def test_fix_vat_31_min(self):
        self.assertEqual(len(wd.check_run_age("fv", NOW - 31 * 60, wd.FIX_VAT_MAX_AGE, NOW)), 1)
        self.assertEqual(wd.check_run_age("fv", NOW - 29 * 60, wd.FIX_VAT_MAX_AGE, NOW), [])

    def test_never_ran(self):
        self.assertIn("нет ни одного", wd.check_run_age("x", None, H, NOW)[0])

    def test_github_time_is_utc(self):
        resp = {"workflow_runs": [{"updated_at": "2026-11-28T11:14:30Z"}]}
        with mock.patch.object(wd, "gh", return_value=resp):
            self.assertEqual(wd.last_success_time("r", "fix-vat.yml"), 1795864470)


class Stock(unittest.TestCase):
    def test_simple_and_variants(self):
        stock = {
            "1": {"stock": {"bl_1": 100}, "variants": []},
            "2": {"stock": {"bl_1": 49}, "variants": []},
            "3": {"stock": {"bl_1": 120}, "variants": {"31": {"bl_1": 100}, "32": {"bl_1": 20}}},
            "4": {"stock": {"bl_1": -2}, "variants": {}},
        }
        bad = wd.check_stock(stock, 50)
        self.assertEqual(bad, [(2, None, 49.0), (32, 3, 20.0), (4, None, -2.0)])
        lines = wd.format_stock(bad, {"2": "BE20406", "32": "GE20406SATIN"})
        self.assertIn("BE20406 — товар 2: 49 шт", lines[0])
        self.assertIn("вариант 32 товара 3", lines[1])
        self.assertIn("без SKU", lines[2])

    def test_exactly_50_ok(self):
        self.assertEqual(wd.check_stock({"1": {"stock": {"bl_1": 50}, "variants": []}}, 50), [])


class DoZaf(unittest.TestCase):
    def test_age_and_email(self):
        orders = [o(1, wd.STATUS_DO_ZAF, 30 * 60), o(2, wd.STATUS_DO_ZAF, 2 * H),
                  o(3, wd.STATUS_DO_ZAF, 22 * H, email="brak-malpy.pl")]
        res = wd.check_do_zaf(orders, NOW)
        self.assertEqual([(r[0], r[2]) for r in res], [(2, True), (3, False)])


class FsNoPdf(unittest.TestCase):
    def test_rules(self):
        orders = [o(1, 37715, 3 * H, extra_field_2="FS/10/11/2026"),   # bez PDF -> alarm
                  o(2, 37715, 3 * H, extra_field_2="FS/11/11/2026"),   # PDF jest
                  o(3, 37715, 1 * H, extra_field_2="FS/12/11/2026"),   # za wczesnie
                  o(4, 37715, 9 * H, extra_field_2="332/10/2026"),     # nie FS
                  o(5, 37715, 9 * H, extra_field_2="FS/13/11/2026")]   # na liscie ignorowanych
        res = wd.check_fs_without_pdf(orders, {2}, NOW, ignore={5})
        self.assertEqual([(r[0], r[1]) for r in res], [(1, "FS/10/11/2026")])


class NowePaid(unittest.TestCase):
    def test_paid_unpaid_age(self):
        orders = [o(1, wd.STATUS_NOWE, 30 * H, payment_done=100),          # alarm
                  o(2, wd.STATUS_NOWE, 30 * H, payment_done=0),            # nieoplacone
                  o(3, wd.STATUS_NOWE, 2 * H, payment_done=100),           # swieze
                  o(4, wd.STATUS_NOWE, 30 * H, payment_done=0, products=[{"price_brutto": 0, "quantity": 1}]),
                  o(5, wd.STATUS_NOWE, 30 * H, payment_done=110, delivery_price=10)]
        self.assertEqual([r[0] for r in wd.check_nowe_paid(orders, NOW)], [1, 5])


class ReportAndMain(unittest.TestCase):
    def test_empty_report(self):
        self.assertEqual(wd.build_report([("a", [], "x"), ("b", [], "y")]), "")

    def test_report_has_only_numbers(self):
        txt = wd.build_report([("Заказ висит", ["84004112: 22.0 ч, e-mail БЕЗ @"], "проверить")])
        self.assertIn("84004112", txt)
        self.assertIn("Что сделать: проверить", txt)

    def test_write_methods_blocked(self):
        with self.assertRaises(RuntimeError):
            wd.bl("setOrderStatus", {})

    def _run_main(self, sections, send):
        sent = []
        env = {"SEND_MAIL": send, "SMTP_USER": "u", "SMTP_PASS": "p"}
        with mock.patch.object(wd, "collect", return_value=sections), \
             mock.patch.object(wd, "send_mail", side_effect=lambda t, n: sent.append(n)), \
             mock.patch.dict(os.environ, env), mock.patch("sys.stdout"):
            rc = wd.main()
        return rc, sent

    def test_all_ok_no_mail_green(self):
        self.assertEqual(self._run_main([("a", [], "x")], "1"), (0, []))

    def test_problem_one_mail_red(self):
        rc, sent = self._run_main([("a", ["1"], "x"), ("b", ["2", "3"], "y"), ("c", [], "z")], "1")
        self.assertEqual((rc, sent), (1, [2]))

    def test_dry_run_no_mail_but_red(self):
        self.assertEqual(self._run_main([("a", ["1"], "x")], "0"), (1, []))

    def test_check_error_reported_not_crash(self):
        def boom(*a, **k):
            raise RuntimeError("siec")
        with mock.patch.object(wd, "last_success_time", side_effect=boom), \
             mock.patch.object(wd, "bl", side_effect=boom), \
             mock.patch.object(wd, "fetch_stock", side_effect=boom):
            secs = wd.collect(NOW)
        errors = [lines for title, lines, _ in secs if title == "Сторож не смог проверить"][0]
        self.assertEqual(len(errors), 6)


if __name__ == "__main__":
    unittest.main()
