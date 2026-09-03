# Generate single-utterance WAVs whose exact text we know, so transcription
# accuracy can be measured (WER) rather than eyeballed. One file per line; the
# text is written alongside as .txt to serve as ground truth.
param([string]$OutDir = "experiments/asr")
Add-Type -AssemblyName System.Speech
$null = New-Item -ItemType Directory -Force $OutDir | Out-Null
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$lines = @(
  "The train leaves at four fifteen from platform two",
  "Oh great another meeting that could have been an email",
  "I would love to but I have a lot on my plate right now",
  "Thanks so much for your help today it made a real difference",
  "We tried that approach last quarter and it did not work",
  "Can you send me the file when you get a chance",
  "Fine whatever you think is best",
  "This is the best news I have had all week"
)
$dir = (Resolve-Path -LiteralPath $OutDir).Path
for ($i = 0; $i -lt $lines.Count; $i++) {
  $name = "line{0:d2}" -f $i
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.SetOutputToWaveFile("$dir\$name.wav", $fmt)
  $s.Speak($lines[$i])
  $s.Dispose()
  Set-Content -Path "$dir\$name.txt" -Value $lines[$i] -Encoding utf8
}
Write-Output "wrote $($lines.Count) utterances to $dir"
