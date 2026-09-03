# Positive WORDS delivered with flat/low prosody -- the words-vs-voice mismatch
# that SER exists to expose. Without SER the interpreter sees only cheerful text.
param([string]$OutFile = "experiments/mismatch.wav")
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$dir = (Resolve-Path -LiteralPath (Split-Path -Parent $OutFile)).Path
$s.SetOutputToWaveFile("$dir\" + (Split-Path -Leaf $OutFile), $fmt)
$lines = @(
  'Oh wonderful. That is just perfect.',
  'No really, I am thrilled for you.',
  'Thanks so much for your help today.'
)
foreach ($l in $lines) {
  $ssml = '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">' +
          '<prosody rate="-25%" pitch="-25%" volume="soft">' + $l + '</prosody>' +
          '<break time="1200ms"/></speak>'
  $s.SpeakSsml($ssml)
}
$s.Dispose()
Write-Output "wrote $OutFile"
