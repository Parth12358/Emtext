# Generate the SAME sentence at three prosody settings, to check that ser.analyze()
# actually responds to how something sounds rather than returning a constant.
# SAPI is not an emotional speaker, so treat this as a "does the signal move at
# all" check, not as ground truth about happy/sad.
param([string]$OutDir = "experiments/prosody")
Add-Type -AssemblyName System.Speech
$null = New-Item -ItemType Directory -Force $OutDir
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$text = "I cannot believe you did that"
$cases = @{
  "flat"  = '<prosody rate="0%" pitch="+0%" volume="default">TEXT</prosody>'
  "up"    = '<prosody rate="+40%" pitch="+40%" volume="loud">TEXT</prosody>'
  "down"  = '<prosody rate="-30%" pitch="-30%" volume="soft">TEXT</prosody>'
}
$dir = (Resolve-Path -LiteralPath $OutDir).Path
foreach ($k in $cases.Keys) {
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.SetOutputToWaveFile("$dir\$k.wav", $fmt)
  $inner = $cases[$k].Replace("TEXT", $text)
  $s.SpeakSsml("<speak version=`"1.0`" xmlns=`"http://www.w3.org/2001/10/synthesis`" xml:lang=`"en-US`">$inner</speak>")
  $s.Dispose()
  Write-Output "wrote $dir\$k.wav"
}
