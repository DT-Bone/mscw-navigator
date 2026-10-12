# OCR an image (optionally a cropped, upscaled region) with the built-in Windows OCR engine.
# Usage: powershell -File ocr.ps1 -Path img.png [-X 0 -Y 0 -W 400 -H 120] [-Scale 3]
#        powershell -File ocr.ps1 -Server   # read image paths from stdin, reply with lines then <<END>>
param([string]$Path, [int]$X = 0, [int]$Y = 0, [int]$W = 0, [int]$H = 0, [int]$Scale = 1, [switch]$Server)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]

$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(); $task.Result }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()

function Invoke-Ocr([string]$Path, [int]$X, [int]$Y, [int]$W, [int]$H, [int]$Scale, [switch]$Words) {
  # Crop + upscale first: small in-game fonts OCR much better when enlarged.
  $src = [System.Drawing.Bitmap]::FromFile((Resolve-Path $Path))
  if ($W -le 0) { $W = $src.Width - $X }; if ($H -le 0) { $H = $src.Height - $Y }
  $dst = New-Object System.Drawing.Bitmap ($W * $Scale), ($H * $Scale)
  $g = [System.Drawing.Graphics]::FromImage($dst)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.DrawImage($src, (New-Object System.Drawing.Rectangle 0, 0, $dst.Width, $dst.Height), (New-Object System.Drawing.Rectangle $X, $Y, $W, $H), [System.Drawing.GraphicsUnit]::Pixel)
  $g.Dispose(); $src.Dispose()
  $tmp = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "ocr_crop_$PID.png")
  $dst.Save($tmp, [System.Drawing.Imaging.ImageFormat]::Png); $dst.Dispose()

  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($tmp)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $stream.Dispose()
  Remove-Item $tmp -ErrorAction SilentlyContinue
  if ($Words) {
    # one word per line: text, then its box in source-image pixels (x y w h)
    foreach ($line in $result.Lines) { foreach ($wd in $line.Words) {
      $r = $wd.BoundingRect
      "{0}`t{1:0}`t{2:0}`t{3:0}`t{4:0}" -f $wd.Text, ($r.X / $Scale + $X), ($r.Y / $Scale + $Y), ($r.Width / $Scale), ($r.Height / $Scale)
    } }
  } else {
    foreach ($line in $result.Lines) { $line.Text }
  }
}

if ($Server) {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  while ($null -ne ($req = [Console]::In.ReadLine())) {
    # "W:<path>" returns word boxes (image upscaled 2x for small text); a bare path returns lines
    try {
      $out = if ($req.StartsWith('W:')) { Invoke-Ocr $req.Substring(2) 0 0 0 0 2 -Words } else { Invoke-Ocr $req 0 0 0 0 1 }
      foreach ($l in $out) { [Console]::Out.WriteLine($l) }
    } catch { [Console]::Out.WriteLine("!error $_") }
    [Console]::Out.WriteLine('<<END>>'); [Console]::Out.Flush()
  }
} else {
  Invoke-Ocr $Path $X $Y $W $H $Scale
}
