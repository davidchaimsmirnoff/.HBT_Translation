# Build LocalHBT.apk without Gradle.
#
# A one-Activity WebView app has no library dependencies, so there is nothing for
# a dependency resolver to resolve. Driving the SDK's own tools directly means the
# build needs no network, no Gradle daemon and no version alignment between AGP,
# Gradle and Kotlin - it uses what Android Studio already put on this machine.
#
#   aapt2 compile -> aapt2 link -> javac -> d8 -> aapt add -> zipalign -> apksigner
#
# Output: Android\build\LocalHBT.apk

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Build = Join-Path $Here "build"

function Die($msg) { Write-Host ""; Write-Host "BUILD FAILED: $msg" -ForegroundColor Red; exit 1 }
function Step($n, $msg) { Write-Host ("[{0}/7] {1}" -f $n, $msg) -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { Die $what } }

# javac and d8 read argument files byte for byte, and Windows PowerShell's
# "-Encoding utf8" writes a BOM - which they take to be part of the first
# filename ("Invalid filename: ?C:\..."). Write these without one.
function WriteList($path, $lines) {
  [System.IO.File]::WriteAllLines($path, [string[]]$lines,
                                  (New-Object System.Text.UTF8Encoding($false)))
}

# ---------------------------------------------------------------- locate tools

$Sdk = $env:ANDROID_SDK_ROOT
if (-not $Sdk) { $Sdk = $env:ANDROID_HOME }
if (-not $Sdk) { $Sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk" }
if (-not (Test-Path $Sdk)) { Die "No Android SDK. Looked in $Sdk - set ANDROID_SDK_ROOT." }

$BtDir = Get-ChildItem (Join-Path $Sdk "build-tools") -Directory |
         Sort-Object { [version]($_.Name -replace '[^0-9.]','') } | Select-Object -Last 1
if (-not $BtDir) { Die "No build-tools in $Sdk. Install one from Android Studio's SDK Manager." }

$PlatDir = Get-ChildItem (Join-Path $Sdk "platforms") -Directory |
           Where-Object { Test-Path (Join-Path $_.FullName "android.jar") } |
           Sort-Object { [int]($_.Name -replace '[^0-9]','') } | Select-Object -Last 1
if (-not $PlatDir) { Die "No platform with an android.jar in $Sdk\platforms." }

$AndroidJar = Join-Path $PlatDir.FullName "android.jar"
$Aapt2      = Join-Path $BtDir.FullName "aapt2.exe"
$Aapt       = Join-Path $BtDir.FullName "aapt.exe"
$D8         = Join-Path $BtDir.FullName "d8.bat"
$ZipAlign   = Join-Path $BtDir.FullName "zipalign.exe"
$ApkSigner  = Join-Path $BtDir.FullName "apksigner.bat"

# javac and keytool: prefer a real JDK on PATH, else the one Android Studio ships
$JavaBin = $null
$jc = Get-Command javac -ErrorAction SilentlyContinue
if ($jc) { $JavaBin = Split-Path $jc.Source }
if (-not $JavaBin) {
  foreach ($p in @("$env:ProgramFiles\Android\Android Studio\jbr\bin",
                   "${env:ProgramFiles(x86)}\Android\Android Studio\jbr\bin",
                   "$env:LOCALAPPDATA\Programs\Android Studio\jbr\bin")) {
    if (Test-Path (Join-Path $p "javac.exe")) { $JavaBin = $p; break }
  }
}
if (-not $JavaBin) { Die "No javac. Install a JDK 17+ or Android Studio." }
$Javac   = Join-Path $JavaBin "javac.exe"
$Keytool = Join-Path $JavaBin "keytool.exe"

# d8.bat and apksigner.bat are launcher scripts that look for JAVA_HOME, which is
# unset on a machine whose only JDK arrived inside Android Studio. Point them at
# the same one javac came from, for this process only.
$env:JAVA_HOME = Split-Path $JavaBin -Parent
$env:PATH = "$JavaBin;$env:PATH"

Write-Host "SDK          $Sdk"
Write-Host "build-tools  $($BtDir.Name)"
Write-Host "platform     $($PlatDir.Name)"
Write-Host "javac        $JavaBin"
Write-Host ""

# Windows PowerShell turns anything a native .exe writes to stderr into an
# ErrorRecord, and with $ErrorActionPreference = "Stop" a mere javac deprecation
# note would abort the build. From here on every step is checked by exit code
# instead, which is the only signal that actually means failure.
$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------- clean slate

if (Test-Path $Build) { Remove-Item $Build -Recurse -Force }
New-Item -ItemType Directory $Build | Out-Null
$Gen     = Join-Path $Build "gen"
$Classes = Join-Path $Build "classes"
$DexDir  = Join-Path $Build "dex"
New-Item -ItemType Directory $Gen, $Classes, $DexDir | Out-Null

# ---------------------------------------------------------------- 1. resources

Step 1 "compiling resources"
& $Aapt2 compile --dir (Join-Path $Here "app\res") -o (Join-Path $Build "res.zip")
Check "aapt2 compile"

Step 2 "linking resources"
& $Aapt2 link `
    -o (Join-Path $Build "base.apk") `
    -I $AndroidJar `
    --manifest (Join-Path $Here "app\AndroidManifest.xml") `
    -R (Join-Path $Build "res.zip") `
    --java $Gen `
    --min-sdk-version 26 `
    --target-sdk-version 36 `
    --auto-add-overlay
Check "aapt2 link"

# ---------------------------------------------------------------- 2. java

Step 3 "compiling java"
$Sources = @()
$Sources += (Get-ChildItem (Join-Path $Here "app\java") -Recurse -Filter *.java).FullName
$Sources += (Get-ChildItem $Gen -Recurse -Filter *.java).FullName
$SrcList = Join-Path $Build "sources.txt"
WriteList $SrcList $Sources

& $Javac -nowarn -encoding UTF-8 -source 17 -target 17 `
         -classpath $AndroidJar -d $Classes "@$SrcList"
Check "javac"
if (-not (Get-ChildItem $Classes -Recurse -Filter *.class -ErrorAction SilentlyContinue)) {
  Die "javac produced no classes"
}

# ---------------------------------------------------------------- 3. dex

Step 4 "dexing"
$ClassFiles = (Get-ChildItem $Classes -Recurse -Filter *.class).FullName
$ClsList = Join-Path $Build "classes.txt"
WriteList $ClsList $ClassFiles
& $D8 --min-api 26 --lib $AndroidJar --output $DexDir "@$ClsList"
Check "d8"

# ---------------------------------------------------------------- 4. package

Step 5 "packaging"
$Unsigned = Join-Path $Build "unsigned.apk"
Copy-Item (Join-Path $Build "base.apk") $Unsigned
# aapt stores the entry under the path it is given, so run it from the dex folder
# to get "classes.dex" at the APK root rather than "build/dex/classes.dex".
Push-Location $DexDir
& $Aapt add -f $Unsigned classes.dex | Out-Null
$addRc = $LASTEXITCODE
Pop-Location
if ($addRc -ne 0) { Die "aapt add classes.dex" }

Step 6 "aligning"
$Aligned = Join-Path $Build "aligned.apk"
& $ZipAlign -f -p 4 $Unsigned $Aligned
Check "zipalign"

# ---------------------------------------------------------------- 5. sign

Step 7 "signing"
$Keystore = Join-Path $Here "debug.keystore"
if (-not (Test-Path $Keystore)) {
  Write-Host "      creating a debug keystore (first run only)"
  & $Keytool -genkeypair -keystore $Keystore -storepass android -keypass android `
             -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 `
             -dname "CN=LocalHBT Debug,O=LocalHBT,C=US"
  if (-not (Test-Path $Keystore)) { Die "keytool could not create debug.keystore" }
}

$Apk = Join-Path $Build "LocalHBT.apk"
& $ApkSigner sign --ks $Keystore --ks-pass pass:android --key-pass pass:android `
                  --ks-key-alias androiddebugkey --out $Apk $Aligned
Check "apksigner"

Remove-Item $Unsigned, $Aligned, (Join-Path $Build "base.apk"), (Join-Path $Build "res.zip") -ErrorAction SilentlyContinue

$size = [math]::Round((Get-Item $Apk).Length / 1KB)
Write-Host ""
Write-Host "BUILT  $Apk  ($size KB)" -ForegroundColor Green
Write-Host ""
Write-Host "Put it on the phone with either:"
Write-Host "  - USB debugging:  run  `"Install to phone.bat`""
Write-Host "  - No cable:       copy the .apk to the phone and tap it"
