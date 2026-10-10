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
    def test_empty(self):
        with self.assertRaises(tool.InputError): self.run_audit([])

if __name__ == '__main__': unittest.main()
