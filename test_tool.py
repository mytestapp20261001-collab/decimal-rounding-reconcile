import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(HERE.name.replace("-", "_"), HERE / "tool.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

class CommonTests(unittest.TestCase):
    def test_json_decimal_ignores_caller_traps(self):
        import decimal
        with decimal.localcontext() as caller:
            caller.prec = 2
            caller.Emax, caller.Emin = 2, -2
            caller.traps[decimal.InvalidOperation] = False
            before = dict(caller.flags)
            with self.assertRaises(tool.InputError):
                tool.parse_json('[1e99999999999999999999999999]')
            self.assertEqual(tool.parse_json('[1.2345]')[0], decimal.Decimal('1.2345'))
            self.assertEqual(dict(caller.flags), before)


    def cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "tool.py"), *args], cwd=HERE,
                              capture_output=True, text=True, timeout=10)
    def test_cli_pass(self):
        result = self.cli("examples/" + PASS, *ARGS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")
        self.assertEqual(result.stderr, "")
    def test_cli_fail(self):
        result = self.cli("examples/" + FAIL, *ARGS)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "fail")
    def test_cli_malformed(self):
        result = self.cli("examples/" + MALFORMED, *ARGS)
        self.assertEqual(result.returncode, MALFORMED_CODE, result.stdout + result.stderr)
        self.assertIn(json.loads(result.stdout)["status"], ("fail", "error"))
    def test_cli_missing_file(self):
        result = self.cli("nonexistent-input", *ARGS)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "unreadable_utf8_input")
    def test_cli_invalid_arguments_are_redacted(self):
        result = self.cli("--unrecognized-secret-value")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "invalid_arguments")
        self.assertNotIn("unrecognized-secret-value", result.stdout + result.stderr)
    def test_cli_help(self):
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)
    def test_cli_stability(self):
        a = self.cli("examples/" + FAIL, *ARGS)
        b = self.cli("examples/" + FAIL, *ARGS)
        self.assertEqual(a.stdout, b.stdout)
    def test_file_limit_and_utf8(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "input"
            path.write_bytes(b"x" * (tool.MAX_BYTES + 1))
            with self.assertRaises(tool.InputError):
                tool.read_text(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(UnicodeDecodeError):
                tool.read_text(path)
    def test_json_parser_safety(self):
        for text in ('{"x":1,"x":2}', '[NaN]', '[' * 130 + '0' + ']' * 130):
            with self.subTest(text=text[:20]), self.assertRaises(tool.InputError):
                tool.parse_json(text)
    def test_report_cap_preserves_count(self):
        report = tool.Report("test")
        for i in range(101):
            report.add("sample", record=i)
        result = report.finish()
        self.assertEqual(len(result["findings"]), 100)
        self.assertEqual(result["summary"]["finding_count"], 101)
        self.assertTrue(result["findings_truncated"])

    def test_adversarial_cli_and_read_only_corpus(self):
        samples = ['', 'null', 'true', '0', '[]', '{}', '[null]', '[true]', '[1]',
                   '["NaN"]', '[{}]', '[[]]', '{"x":1,"x":2}',
                   '[1e99999999999999999999999999]', '[NaN]', '[Infinity]',
                   '"\\ud800"', '["\\ud800"]', '{', '[', '["a","b"]',
                   '[1e-99999999999999999999999999]']
        before = {path: path.read_bytes() for path in (HERE / 'examples').iterdir()}
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / 'input.txt'
            for sample in samples:
                with self.subTest(sample=sample):
                    path.write_text(sample, encoding='utf-8')
                    result = self.cli(str(path), *ARGS)
                    self.assertIn(result.returncode, (0, 1, 2), result.stderr)
                    self.assertEqual(result.stderr, '')
                    self.assertIn(json.loads(result.stdout)['status'], ('pass','fail','error'))
                    self.assertEqual(path.read_text(encoding='utf-8'), sample)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

PASS = 'pass.json'
FAIL = 'fail.json'
MALFORMED = 'malformed.json'
MALFORMED_CODE = 2
ARGS = ['--places', '2', '--rounding', 'half-even']

class AuditTests(unittest.TestCase):
    def run_audit(self, values, **options):
        return tool.audit(values, places=options.pop('places',2), rounding=options.pop('rounding','half-even'), **options)
    def test_half_even_difference(self):
        result=self.run_audit(['0.005','0.005'],show_totals=True)
        self.assertEqual(result['totals'],{'sum_of_rounded_items':'0.00','rounded_sum':'0.01','difference':'-0.01'})
    def test_half_up_difference(self):
        result=self.run_audit(['0.005','0.005'],rounding='half-up',show_totals=True)
        self.assertEqual(result['totals']['difference'],'0.01')
    def test_negative_values(self):
        result=self.run_audit(['-0.005','-0.005'],rounding='half-up',show_totals=True)
        self.assertEqual(result['totals']['difference'],'-0.01')
    def test_all_modes(self):
        for rounding in tool.ROUNDINGS:
            with self.subTest(rounding=rounding): self.assertEqual(self.run_audit(['1.23'],rounding=rounding)['status'],'pass')
    def test_zero_places_and_max_places(self):
        self.assertEqual(self.run_audit(['1.5','1.5'],places=0)['status'],'fail')
        self.assertEqual(self.run_audit(['1e-12'],places=12)['status'],'pass')
    def test_raw_float_rejected(self):
        with self.assertRaises(tool.InputError): self.run_audit([0.1])
    def test_nonfinite_and_huge_rejected(self):
        for value in ('NaN','Infinity','1e101','1e-101','1e9999999999999999999999999',' 1 '):
            with self.subTest(value=value),self.assertRaises(tool.InputError): self.run_audit([value])
    def test_bad_policy(self):
        for options in ({'places':13},{'places':True},{'rounding':'unknown'}):
            with self.assertRaises(tool.InputError): self.run_audit(['1'],**options)
    def test_exact_extreme_sum(self):
        result=self.run_audit(['1e100','-1e100','0.005','0.005'],show_totals=True)
        self.assertEqual(result['totals']['rounded_sum'],'0.01')
    def test_no_totals_by_default(self):
        result=self.run_audit(['1234.56'])
        self.assertNotIn('totals',result)
        self.assertNotIn('1234.56',json.dumps(result))
    def test_host_context_does_not_change_results_or_flags(self):
        import decimal
        expected = self.run_audit(['0.005', '0.005', '1000'], show_totals=True)
        with decimal.localcontext() as caller:
            caller.prec = 2
            caller.Emax, caller.Emin = 2, -2
            caller.rounding = decimal.ROUND_UP
            caller.clamp = 1
            for signal in caller.traps:
                caller.traps[signal] = True
            caller.flags[decimal.Rounded] = True
            before = caller.copy()
            self.assertEqual(self.run_audit(['0.005', '0.005', '1000'], show_totals=True), expected)
            self.assertEqual(str(caller), str(before))
            self.assertEqual(dict(caller.flags), dict(before.flags))
            self.assertEqual(dict(caller.traps), dict(before.traps))

    def test_invalid_decimal_ignores_disabled_caller_trap(self):
        import decimal
        with decimal.localcontext() as caller:
            caller.traps[decimal.InvalidOperation] = False
            before = dict(caller.flags)
            with self.assertRaises(tool.InputError):
                self.run_audit(['1e9999999999999999999999999'])
            self.assertEqual(dict(caller.flags), before)

    def test_default_context_does_not_change_results(self):
        import decimal
        original = decimal.DefaultContext.copy()
        expected = self.run_audit(['1e100', '-1e100', '0.005', '0.005'], show_totals=True)
        try:
            decimal.DefaultContext.Emax = 2
            decimal.DefaultContext.Emin = -2
            decimal.DefaultContext.clamp = 1
            for signal in decimal.DefaultContext.traps:
                decimal.DefaultContext.traps[signal] = True
            self.assertEqual(self.run_audit(['1e100', '-1e100', '0.005', '0.005'], show_totals=True), expected)
        finally:
            for attr in ('prec', 'rounding', 'Emin', 'Emax', 'capitals', 'clamp'):
                setattr(decimal.DefaultContext, attr, getattr(original, attr))
            decimal.DefaultContext.traps = original.traps.copy()
            decimal.DefaultContext.flags = original.flags.copy()

    def test_empty(self):
        with self.assertRaises(tool.InputError): self.run_audit([])


class MaintenanceReceiptTests(unittest.TestCase):
    def test_maintenance_receipt_is_fail_closed(self):
        import copy
        import importlib.util
        root = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location('release_validation', root / 'scripts/validate_release.py')
        validation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validation)
        record = json.loads((root / 'docs/MIGRATION.json').read_text())
        receipt = json.loads((root / 'docs/MAINTENANCE_2026-10-21.json').read_text())
        original = {entry['path']: entry['sha256'] for entry in record['preserved_files']}
        self.assertEqual(validation.maintenance_hashes(record, None), original)
        result = validation.maintenance_hashes(record, receipt)
        for change in receipt['changes']:
            self.assertEqual(result[change['path']], change['current_sha256'])
        invalid = []
        for field, value in [('path', '../tool.py'), ('path', 'README.md'),
                             ('original_sha256', '0' * 64), ('current_sha256', 'invalid'), ('reason', '')]:
            candidate = copy.deepcopy(receipt)
            candidate['changes'][0][field] = value
            invalid.append(candidate)
        candidate = copy.deepcopy(receipt)
        candidate['changes'].append(candidate['changes'][0].copy())
        invalid.append(candidate)
        candidate = copy.deepcopy(receipt)
        candidate['repository_id'] = 0
        invalid.append(candidate)
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validation.maintenance_hashes(record, candidate)

if __name__ == '__main__': unittest.main()
