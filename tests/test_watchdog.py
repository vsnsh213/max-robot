"""Testy straznika bez sieci: python3 -m unittest discover -s tests -v
Kazda kontrola na podstawionych danych; API Base / GitHub / SMTP nie sa wywolywane."""
import os, sys, unittest
from unittest import mock

os.environ.setdefault("BLT", "test-token-niezywy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import watchdog as wd

NOW = 2_000_000_000
H = 3600
# znaczniki czasu (UTC) dla okna porannego w Warszawie, zima = UTC+1, lato = UTC+2
W_0859 = 1795852740   # 28.11.2026 08:59 Warszawa
W_0900 = 1795852800   # 09:00
MORNING = 1795858800  # 10:40 - zimowy przebieg cron "40 */3" (09:40 UTC) wpada w okno
W_1159 = 1795863540   # 11:59
W_1200 = 1795863600   # 12:00
NIGHT = 1795826400    # 01:40
SUMMER_1040 = 1782376800  # 25.06.2026 10:40 Warszawa (UTC+2)


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

    def _run_main(self, sections, send, now=None):
        sent = []
        env = {"SEND_MAIL": send, "SMTP_USER": "u", "SMTP_PASS": "p"}
        with mock.patch.object(wd, "collect", return_value=sections), \
             mock.patch.object(wd, "send_mail", side_effect=lambda t, n: sent.append((n, t))), \
             mock.patch.dict(os.environ, env), mock.patch("sys.stdout"):
            rc = wd.main(now or NIGHT)
        return rc, sent

    def test_all_ok_no_mail_green(self):
        self.assertEqual(self._run_main([("a", [], "x", wd.URGENT)], "1"), (0, []))

    def test_problem_one_mail_red(self):
        rc, sent = self._run_main([("a", ["1"], "x", wd.URGENT), ("b", ["2", "3"], "y", wd.URGENT),
                                   ("c", [], "z", wd.URGENT)], "1")
        self.assertEqual((rc, [n for n, _ in sent]), (1, [2]))

    def test_dry_run_no_mail_but_red(self):
        self.assertEqual(self._run_main([("a", ["1"], "x", wd.URGENT)], "0"), (1, []))

    # --- okno poranne 09:00-11:59 Europe/Warsaw ---
    def test_is_morning_boundaries(self):
        self.assertFalse(wd.is_morning(W_0859))
        self.assertTrue(wd.is_morning(W_0900))
        self.assertTrue(wd.is_morning(MORNING))
        self.assertTrue(wd.is_morning(W_1159))
        self.assertFalse(wd.is_morning(W_1200))
        self.assertTrue(wd.is_morning(SUMMER_1040))  # czas letni (UTC+2)

    def test_morning_only_problem_not_morning_green_no_mail(self):
        rc, sent = self._run_main([("Nowe", ["84114540: 29 ч"], "x", wd.MORNING)], "1", NIGHT)
        self.assertEqual((rc, sent), (0, []))

    def test_morning_only_problem_in_morning_red_mail(self):
        rc, sent = self._run_main([("Nowe", ["84114540: 29 ч"], "x", wd.MORNING)], "1", MORNING)
        self.assertEqual(rc, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("84114540", sent[0][1])

    def test_urgent_problem_not_morning_mail_without_morning_items(self):
        secs = [("Остаток", ["BE20406: 10 шт"], "x", wd.URGENT), ("Nowe", ["84114540: 29 ч"], "y", wd.MORNING)]
        rc, sent = self._run_main(secs, "1", NIGHT)
        self.assertEqual((rc, len(sent)), (1, 1))
        self.assertIn("BE20406", sent[0][1])
        self.assertNotIn("84114540", sent[0][1])

    def test_urgent_and_morning_in_morning_one_mail_with_both(self):
        secs = [("Остаток", ["BE20406: 10 шт"], "x", wd.URGENT), ("Nowe", ["84114540: 29 ч"], "y", wd.MORNING)]
        rc, sent = self._run_main(secs, "1", MORNING)
        self.assertEqual((rc, len(sent), sent[0][0]), (1, 1, 2))
        self.assertIn("BE20406", sent[0][1])
        self.assertIn("84114540", sent[0][1])

    def test_collect_kinds(self):
        def boom(*a, **k):
            raise RuntimeError("siec")
        with mock.patch.object(wd, "last_success_time", side_effect=boom), \
             mock.patch.object(wd, "bl", side_effect=boom), \
             mock.patch.object(wd, "fetch_stock", side_effect=boom):
            kinds = {title: kind for title, _, _, kind in wd.collect(NOW)}
        self.assertEqual(kinds["Роботы не запускаются"], wd.URGENT)
        self.assertEqual(kinds["Сторож не смог проверить"], wd.URGENT)
        self.assertEqual(kinds[f"Остаток в Base ниже {wd.STOCK_MIN}"], wd.URGENT)
        self.assertEqual(kinds["Заказ висит в «Do zafakturowania» дольше 1 ч"], wd.MORNING)
        self.assertEqual(kinds["FS есть в Pole 2, а PDF Optima в заказе нет дольше 2 ч"], wd.MORNING)
        self.assertEqual(kinds["Оплаченные заказы в «Nowe» дольше 24 ч (для Свята)"], wd.MORNING)

    def test_check_error_reported_not_crash(self):
        def boom(*a, **k):
            raise RuntimeError("siec")
        with mock.patch.object(wd, "last_success_time", side_effect=boom), \
             mock.patch.object(wd, "bl", side_effect=boom), \
             mock.patch.object(wd, "fetch_stock", side_effect=boom):
            secs = wd.collect(NOW)
        errors = [lines for title, lines, *_ in secs if title == "Сторож не смог проверить"][0]
        self.assertEqual(len(errors), 6)


class Retries(unittest.TestCase):
    """Zapytania do Base / GitHub: do 3 prob z przerwa, dopiero potem blad."""

    class Resp:
        def __init__(self, body):
            self.body = body
        def read(self, *a):
            return self.body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _urlopen(self, outcomes):
        seq = list(outcomes)
        calls = []
        def fake(req, timeout=None, **k):
            calls.append(req.full_url)
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return self.Resp(item)
        return fake, calls

    def _patch(self, outcomes):
        fake, calls = self._urlopen(outcomes)
        sleeps = []
        p1 = mock.patch.object(wd.urllib.request, "urlopen", side_effect=fake)
        p2 = mock.patch.object(wd.time, "sleep", side_effect=lambda s: sleeps.append(s))
        p3 = mock.patch("sys.stdout")
        return p1, p2, p3, calls, sleeps

    def test_github_first_fails_second_ok(self):
        ok = b'{"workflow_runs": [{"updated_at": "2026-11-28T11:14:30Z"}]}'
        p1, p2, p3, calls, sleeps = self._patch([OSError("timed out"), ok])
        with p1, p2, p3:
            self.assertEqual(wd.last_success_time("r", "keep-stock.yml"), 1795864470)
        self.assertEqual((len(calls), sleeps), (2, [wd.RETRY_PAUSE]))

    def test_base_first_fails_second_ok(self):
        ok = b'{"status": "SUCCESS", "products": {}}'
        p1, p2, p3, calls, sleeps = self._patch([OSError("reset"), ok])
        with p1, p2, p3:
            self.assertEqual(wd.fetch_stock(6095), {})
        self.assertEqual((len(calls), sleeps), (2, [10]))

    def test_base_error_status_retried(self):
        bad = b'{"status": "ERROR", "error_code": "X", "error_message": "chwilowy"}'
        ok = b'{"status": "SUCCESS", "products": {}}'
        p1, p2, p3, calls, sleeps = self._patch([bad, bad, ok])
        with p1, p2, p3:
            self.assertEqual(wd.fetch_stock(6095), {})
        self.assertEqual(len(calls), 3)

    def test_three_failures_go_to_watchdog_errors(self):
        # bezposrednio: 3 nieudane proby -> wyjatek, potem trafia do sekcji bledow
        p1, p2, p3, calls, sleeps = self._patch([OSError("timed out")] * 3)
        with p1, p2, p3:
            with self.assertRaises(OSError):
                wd.gh("/repos/r/actions/workflows/keep-stock.yml/runs")
        self.assertEqual((len(calls), sleeps), (3, [10, 10]))

        p1, p2, p3, calls, sleeps = self._patch([OSError("timed out")] * 3 +
                                                 [b'{"workflow_runs": [{"updated_at": "2033-05-18T03:33:20Z"}]}'])
        with p1, p2, p3, \
             mock.patch.object(wd, "fetch_stock", return_value={}), \
             mock.patch.object(wd, "fetch_orders", return_value=[]), \
             mock.patch.object(wd, "fetch_external_invoice_orders", return_value=set()):
            secs = wd.collect(NOW)
        errors = [lines for title, lines, *_ in secs if title == "Сторож не смог проверить"][0]
        self.assertEqual(len(errors), 1)
        self.assertIn("GitHub keep-stock", errors[0])

    def test_write_method_not_retried(self):
        p1, p2, p3, calls, sleeps = self._patch([])
        with p1, p2, p3:
            with self.assertRaises(RuntimeError):
                wd.bl("setOrderStatus", {})
        self.assertEqual((calls, sleeps), ([], []))


if __name__ == "__main__":
    unittest.main()
