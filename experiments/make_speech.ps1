# Synthesize test utterances to a single 16 kHz mono 16-bit WAV using Windows SAPI.
# Lets us exercise the full pipeline (VAD -> whisper -> LLM) with no microphone.
param(
  [string]$OutFile = "experiments/speech.wav",
  [string[]]$Lines = @(
    "Oh great, another meeting that could have been an email.",
    "The train leaves at four fifteen from platform two.",
    "Fine. Whatever you think is best."
  )
)
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToWaveFile((Resolve-Path -LiteralPath (Split-Path -Parent $OutFile)).Path + "\" + (Split-Path -Leaf $OutFile), $fmt)
$s.Rate = 0
foreach ($line in $Lines) {
  # A pause between lines gives the energy VAD the trailing silence it needs
  # to close each utterance (END_SILENCE_MS, default 650 ms).
  $s.Speak($line)
  $s.SpeakSsml('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US"><break time="1200ms"/></speak>')
}
$s.Dispose()
Write-Output "wrote $OutFile"
