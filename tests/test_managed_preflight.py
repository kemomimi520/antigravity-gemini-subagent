"""AST and isolated fixture tests. Never execute the preflight script's main body."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/managed_preflight.ps1"


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell AST parser")
class BundledPreflightTests(unittest.TestCase):
    def powershell(self, suffix, extra_env=None):
        # Extract only definitions; do not dot-source or invoke the actual preflight.
        code = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:PREFLIGHT_SOURCE,[ref]$tokens,[ref]$errors)
if ($errors.Count -ne 0) { throw 'Preflight AST contains syntax errors.' }
$definitions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]},$true)
foreach ($definition in $definitions) { Invoke-Expression $definition.Extent.Text }
""" + suffix
        environment = dict(os.environ, PREFLIGHT_SOURCE=str(SCRIPT))
        environment.update(extra_env or {})
        completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", code],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                   shell=False, env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8-sig"))

    def test_powershell_51_ast_is_valid_without_executing_main(self):
        result = self.powershell("@{valid=$true; functions=@($definitions.Name)} | ConvertTo-Json -Compress")
        self.assertTrue(result["valid"])
        self.assertIn("Copy-PreflightPath", result["functions"])

    def test_recent_new_old_future_and_unparseable_restore_points(self):
        result = self.powershell(r"""
$now=[datetime]::SpecifyKind([datetime]'2030-01-10T12:00:00',[DateTimeKind]::Utc)
$new=[pscustomobject]@{CreationTime=$now.AddMinutes(-1);Description='unique-fixture';SequenceNumber=2}
$old=[pscustomobject]@{CreationTime=$now.AddDays(-8);Description='old';SequenceNumber=1}
$future=[pscustomobject]@{CreationTime=$now.AddDays(1);Description='future';SequenceNumber=3}
$invalid=[pscustomobject]@{CreationTime='not a WMI time';Description='invalid';SequenceNumber=4}
$created=Get-PreflightRestoreEvidence @($new) $now 'unique-fixture' @(1)
$existing=Get-PreflightRestoreEvidence @($new) $now 'unique-fixture' @(1,2)
$none=Get-PreflightRestoreEvidence @($old,$future,$invalid) $now 'unique-fixture' @(1)
@{created=$created;existing=$existing;none=$none} | ConvertTo-Json -Depth 5 -Compress
""")
        self.assertTrue(result["created"]["CreatedNow"])
        self.assertTrue(result["created"]["Recent"])
        self.assertFalse(result["existing"]["CreatedNow"])
        self.assertTrue(result["existing"]["Recent"])
        self.assertFalse(result["none"]["Recent"])

    def test_isolated_directory_copy_is_verified_and_original_preserved(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "source"
            (source / "nested").mkdir(parents=True)
            (source / "nested/fixture.txt").write_text("synthetic backup fixture", encoding="utf-8")
            receipt = root / "receipt"
            receipt.mkdir()
            destination = receipt / "unique-copy"
            result = self.powershell(r"""
Copy-PreflightPath $env:FIXTURE_SOURCE $env:FIXTURE_DESTINATION $env:FIXTURE_RECEIPT | ConvertTo-Json -Depth 8 -Compress
""", {"FIXTURE_SOURCE": str(source), "FIXTURE_DESTINATION": str(destination), "FIXTURE_RECEIPT": str(receipt)})
            self.assertEqual(result["Status"], "verified")
            self.assertEqual((destination / "nested/fixture.txt").read_bytes(),
                             (source / "nested/fixture.txt").read_bytes())

    def test_native_wmi_creation_time_is_accepted(self):
        result = self.powershell(r"""
$now=[datetime]::UtcNow
$point=[pscustomobject]@{CreationTime=[Management.ManagementDateTimeConverter]::ToDmtfDateTime($now.AddMinutes(-1));Description='native-fixture';SequenceNumber=10}
Get-PreflightRestoreEvidence @($point) $now 'native-fixture' @(9) | ConvertTo-Json -Compress
""")
        self.assertTrue(result["CreatedNow"])
        self.assertTrue(result["Recent"])

    def test_source_containing_receipt_is_refused(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            receipt = root / "receipt"
            receipt.mkdir()
            result = self.powershell(r"""
$refused=$false
try { $null=Copy-PreflightPath $env:FIXTURE_SOURCE $env:FIXTURE_DESTINATION $env:FIXTURE_RECEIPT } catch { $refused=$true }
@{refused=$refused} | ConvertTo-Json -Compress
""", {"FIXTURE_SOURCE": str(root), "FIXTURE_DESTINATION": str(receipt / "copy"), "FIXTURE_RECEIPT": str(receipt)})
            self.assertTrue(result["refused"])
            self.assertFalse((receipt / "copy").exists())

    def test_bundled_preflight_has_no_restore_bypass_and_commits_marker_last(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("AllowNoRestorePoint", source)
        for forbidden in ("Enable-ComputerRestore", "Disable-ComputerRestore", "Restore-Computer ",
                          "Set-ItemProperty", "Stop-Process", "Remove-Item"):
            self.assertNotIn(forbidden, source)
        self.assertIn("ValidateSet('gemini-cli-subagent')", source)
        self.assertGreater(source.index("[IO.File]::Move((Join-Path $receipt 'restore-point.pending')"),
                           source.index("Write-PreflightText (Join-Path $receipt 'ROLLBACK.md')"))


if __name__ == "__main__":
    unittest.main()
