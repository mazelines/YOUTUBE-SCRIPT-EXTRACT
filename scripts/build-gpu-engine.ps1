# Build a reproducible Vulkan + AVX2 wheel. Never reuse pip's CPU wheel cache.
[CmdletBinding()]
param(
    [string]$Python = '.venv/Scripts/python.exe',
    [string]$VulkanSdk = $env:VULKAN_SDK,
    [string]$Source = 'llama-cpp-python==0.3.34',
    [string]$WheelDir = '.zcode/gpu-wheels'
)
$ErrorActionPreference = 'Stop'
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not $VulkanSdk -or -not (Test-Path -LiteralPath "$VulkanSdk/Bin/glslc.exe")) {
    throw 'Set VULKAN_SDK to a LunarG Vulkan SDK containing Bin/glslc.exe.'
}
$vswhere = "${env:ProgramFiles(x86)}/Microsoft Visual Studio/Installer/vswhere.exe"
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'Visual Studio x64 C++ build tools are required.' }
& "$vs/Common7/Tools/Launch-VsDevShell.ps1" -Arch amd64 -HostArch amd64 -SkipAutomaticLocation
$cmakeRoot = "$vs/Common7/IDE/CommonExtensions/Microsoft/CMake"
$env:PATH = "$cmakeRoot/CMake/bin;$cmakeRoot/Ninja;$VulkanSdk/Bin;$env:PATH"
$env:VULKAN_SDK = $VulkanSdk
$env:CMAKE_GENERATOR = 'Ninja'
$env:CMAKE_BUILD_PARALLEL_LEVEL = '8'
$env:CMAKE_ARGS = '-DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DGGML_AVX512=OFF -DGGML_VULKAN=ON -DCMAKE_C_FLAGS=/utf-8 -DCMAKE_CXX_FLAGS=/utf-8'
& $Python -m pip wheel $Source --no-deps --no-cache-dir --no-binary=llama-cpp-python --wheel-dir $WheelDir
if ($LASTEXITCODE -ne 0) { throw "Vulkan engine build failed: $LASTEXITCODE" }
Get-ChildItem -LiteralPath $WheelDir -Filter '*.whl' | Get-FileHash -Algorithm SHA256
