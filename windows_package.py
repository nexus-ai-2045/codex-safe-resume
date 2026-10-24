import ctypes
from ctypes import wintypes as wt
import json
import os
from pathlib import Path
import subprocess
import sys

import cli_security
from core import reject_links
import windows_platform


FAMILY = 'OpenAI.Codex_2p2nqsd0c76g0'
_ERROR = 'Windowsの公式アプリパッケージを検証できません'
_REQUIRED = (
    'store_signed', 'status_ok', 'non_development', 'non_stub', 'application_package',
    'non_mutable', 'no_external', 'effective_is_installed', 'manifest_safe',
    'integrity_valid', 'executable_signature_valid',
)
_VERIFY_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PSModulePath = [System.IO.Path]::Combine($PSHOME, 'Modules')
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Management.Deployment.PackageManager,Windows.Management.Deployment,ContentType=WindowsRuntime]
    $manager = [Windows.Management.Deployment.PackageManager]::new()
    $package = $manager.FindPackageForUser('', $env:CSR_PACKAGE_FULL_NAME)
    if ($null -eq $package -or $package.Id.FullName -cne $env:CSR_PACKAGE_FULL_NAME -or
        $package.Id.FamilyName -cne 'OpenAI.Codex_2p2nqsd0c76g0' -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals($package.InstalledPath, $env:CSR_PACKAGE_ROOT)) {
        throw 'invalid'
    }
    $result = [ordered]@{
        root = $package.InstalledPath
        full_name = $package.Id.FullName
        family = $package.Id.FamilyName
        store_signed = ([string]$package.SignatureKind -ceq 'Store')
        status_ok = $package.Status.VerifyIsOK()
        non_development = ($package.IsDevelopmentMode -eq $false)
        non_stub = ($package.IsStub -eq $false)
        application_package = (-not ($package.IsFramework -or $package.IsOptional -or $package.IsResourcePackage))
        non_mutable = [string]::IsNullOrWhiteSpace($package.MutablePath)
        no_external = ([string]::IsNullOrWhiteSpace($package.EffectiveExternalPath) -and
            [string]::IsNullOrWhiteSpace($package.MachineExternalPath) -and
            [string]::IsNullOrWhiteSpace($package.UserExternalPath))
        effective_is_installed = [StringComparer]::OrdinalIgnoreCase.Equals($package.EffectivePath, $package.InstalledPath)
        manifest_safe = $false
        integrity_valid = $false
        executable_signature_valid = $false
    }
    foreach ($key in @('store_signed', 'status_ok', 'non_development', 'non_stub',
                      'application_package', 'non_mutable', 'no_external', 'effective_is_installed')) {
        if ($result[$key] -ne $true) { throw 'invalid' }
    }
    $operation = $package.VerifyContentIntegrityAsync()
    $methods = @([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
        $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })
    if ($methods.Count -ne 1) { throw 'invalid' }
    $task = $methods[0].MakeGenericMethod([bool]).Invoke($null, @($operation))
    if (-not $task.Wait(20000)) { throw 'invalid' }
    $result.integrity_valid = [bool]$task.GetAwaiter().GetResult()
    if (-not $result.integrity_valid) { throw 'invalid' }
    $settings = [System.Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $reader = [System.Xml.XmlReader]::Create(
        [IO.Path]::Combine($package.InstalledPath, 'AppxManifest.xml'), $settings)
    try {
        $manifest = [System.Xml.XmlDocument]::new()
        $manifest.XmlResolver = $null
        $manifest.Load($reader)
    } finally { $reader.Dispose() }
    $external = @($manifest.SelectNodes('//*[local-name()="AllowExternalContent"]') |
        Where-Object { $_.InnerText.Trim() -ine 'false' })
    $mutable = @($manifest.SelectNodes(
        '//*[local-name()="MutablePackageDirectory" or local-name()="MutablePackageDirectories"]'))
    $result.manifest_safe = ($external.Count -eq 0 -and $mutable.Count -eq 0)
    $signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $env:CSR_PACKAGE_EXECUTABLE
    $result.executable_signature_valid = ($signature.Status -eq 'Valid' -and
        $signature.SignerCertificate.GetNameInfo(
            [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ceq 'OpenAI OpCo, LLC')
    [Console]::Out.Write(($result | ConvertTo-Json -Compress))
} catch { exit 1 }
'''


def _package_string(function, first):
    length = wt.UINT()
    if function(first, ctypes.byref(length), None) != 122 or not 1 < length.value <= 32768:
        raise ValueError(_ERROR)
    value = ctypes.create_unicode_buffer(length.value)
    if function(first, ctypes.byref(length), value) != 0 or not value.value:
        raise ValueError(_ERROR)
    return value.value


def _native_package(handle):
    kernel = windows_platform._native()[0]
    for name in ('GetPackageFullName', 'GetPackageFamilyName'):
        function = getattr(kernel, name)
        function.argtypes = [wt.HANDLE, ctypes.POINTER(wt.UINT), wt.LPWSTR]
        function.restype = wt.LONG
    kernel.GetPackagePathByFullName.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.UINT), wt.LPWSTR]
    kernel.GetPackagePathByFullName.restype = wt.LONG
    full_name = _package_string(kernel.GetPackageFullName, handle)
    family = _package_string(kernel.GetPackageFamilyName, handle)
    root = _package_string(kernel.GetPackagePathByFullName, full_name)
    return dict(root=root, full_name=full_name, family=family)


def _package_record(native, executable):
    result = subprocess.run(
        [str(cli_security._system_powershell()), '-NoProfile', '-NonInteractive',
         '-Command', _VERIFY_SCRIPT], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, shell=False, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        env=dict(os.environ, CSR_PACKAGE_FULL_NAME=native['full_name'],
                 CSR_PACKAGE_ROOT=native['root'], CSR_PACKAGE_EXECUTABLE=str(executable)))
    if result.returncode != 0 or not 0 < len(result.stdout) <= 32768:
        raise ValueError(_ERROR)
    return json.loads(result.stdout)


def verify_package(pid, executable):
    try:
        if sys.platform != 'win32' or type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError(_ERROR)
        executable = Path(executable)
        if not executable.is_absolute() or executable.name.casefold() not in ('chatgpt.exe', 'codex.exe'):
            raise ValueError(_ERROR)
        reject_links(executable)
        executable = executable.resolve(strict=True)
        with windows_platform._process(pid) as (handle, identity):
            if Path(identity['executable']) != executable:
                raise ValueError(_ERROR)
            native = _native_package(handle)
            if (not isinstance(native, dict) or native.get('family') != FAMILY
                    or not isinstance(native.get('full_name'), str) or not native['full_name']):
                raise ValueError(_ERROR)
            root = Path(native['root'])
            if not root.is_absolute() or not executable.is_relative_to(root):
                raise ValueError(_ERROR)
            reject_links(root)
            if root.resolve(strict=True) != root or not root.is_dir():
                raise ValueError(_ERROR)
            record = _package_record(native, executable)
            if (not isinstance(record, dict)
                    or any(record.get(key) is not True for key in _REQUIRED)
                    or record.get('full_name') != native['full_name']
                    or record.get('family') != native['family']
                    or not isinstance(record.get('root'), str)
                    or Path(record['root']) != root
                    or windows_platform.process_identity(pid) != identity):
                raise ValueError(_ERROR)
            reject_links(executable)
            return dict(root=str(root), full_name=native['full_name'], family=native['family'])
    except (OSError, AttributeError, TypeError, KeyError, ValueError, RuntimeError,
            subprocess.SubprocessError):
        raise ValueError(_ERROR) from None
