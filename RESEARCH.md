# What the literature actually says about reading emotion from speech

A survey done in September 2026, aimed squarely at emtext's design decisions. `TODO.md`
records what is broken *in this codebase*; this file records what is known *in the field*,
so the next design argument starts from evidence rather than intuition.

**How to read the confidence markers:** **[STRONG]** = meta-analysis, systematic review, or
replication across independent labs · **[SINGLE]** = one study · **[PREPRINT]** = 2026 arXiv,
direction corroborated but numbers not peer-reviewed · **[GAP]** = no research found, stated
as a gap rather than an unknown.

The uncomfortable summary: **three of emtext's core assumptions are not supported, one is
supported more strongly than expected, and the highest-value signal in the literature is one
the project currently throws away.**

---

## 1. The premise — what is actually hard for the listener

### 1.1 Pragmatic difficulty is selective, not general

The single most decision-relevant finding, and it narrows the product considerably.

- **Irony/sarcasm is impaired; indirect requests are not.** Autistic adults showed intact
  contextual comprehension of indirect requests alongside marked difficulty with irony —
  a *dissociation*, not a general pragmatic deficit.
  [Deliens et al., JADD 48(9)](https://doi.org/10.1007/s10803-018-3561-6) **[SINGLE]**
- Hint comprehension was **similar** for autistic and non-autistic adults.
  [Autistic and non-autistic adults use discourse context…](https://pmc.ncbi.nlm.nih.gov/articles/PMC11897084/) **[SINGLE]**
- On TASIT (video of everyday conversation), autistic adults were less accurate at **sarcasm
  and deception** while performance on **literal/sincere items was preserved**.
  [TASIT study](https://www.tandfonline.com/doi/abs/10.1080/09638280600646185) **[SINGLE, well-normed]**

**Consequence for emtext:** the `refusal` category in `eval/tone_cases.jsonl` — polite
declines, hedged "let me check my calendar" — is testing a capability the evidence says is
largely intact. It is not harmful to keep, but it is not the product. The phenomena worth
chasing are the ones where **words and voice disagree**.

### 1.2 The deficit is often speed, not accuracy

- Autistic participants **consistently show longer reaction times** processing metaphor and
  detecting irony **regardless of accuracy**.
  [Lampri et al. 2024, Autism Research](https://onlinelibrary.wiley.com/doi/full/10.1002/aur.3069) **[STRONG, review]**
- *Intact but Protracted Facial and Prosodic Emotion Recognition Among Autistic Adults* —
  accuracy intact, speed reduced.
  [JADD 2025](https://link.springer.com/article/10.1007/s10803-025-06786-z) **[SINGLE]**
- ADHD adults were **equally accurate** at comprehending irony, with an added processing cost.
  [J Attention Disorders 2025](https://doi.org/10.1177/10870547251333819) **[SINGLE]**

**This is the strongest argument for emtext existing at all** — and it reframes the goal. A
tool that *buys processing time* targets a measured phenomenon. A tool that *supplies the
answer* targets a folk one, and risks replacing a capability the user has.

### 1.3 The numbers that most justify a prosody-based tool

Children with autism vs typically developing, irony comprehension by available cue:

| cue condition | TD | ASD | gap |
|---|---|---|---|
| context + prosody | 99.1% | 90.8% | 8.3 |
| **context only** (neutral intonation) | 93.5% | 81.8% | **11.7** |
| **prosody only** (no context) | 86.1% | 82.2% | **3.9** |

[Brain 129(4):932–943](https://pmc.ncbi.nlm.nih.gov/articles/PMC3713234/) **[SINGLE]**

Read the *shape*, not the gap. The groups are **most nearly equal on prosody** and most
unequal on context integration. A tool that surfaces prosody is augmenting the channel the
user is least disadvantaged on — which sounds like an argument against, until you notice the
corollary: the deficit is in *integrating* the channels, and making one channel explicit is
exactly what reduces integration load.

### 1.4 Alexithymia changes what the output should be

- Alexithymia, not autism severity, predicted poor recognition of emotional expressions.
  [Cook et al., Psychological Science](https://journals.sagepub.com/doi/abs/10.1177/0956797612463582) **[SINGLE, highly cited]**
- Extends to the voice: residual emotion-recognition differences were attributable to
  co-occurring alexithymia.
  [Psychological Medicine](https://www.cambridge.org/core/journals/psychological-medicine/article/abs/measuring-the-effects-of-alexithymia-on-perception-of-emotional-vocalizations-in-autistic-spectrum-disorder-and-typical-development/736D7D719DF46D111E0D44D00A60B591) **[SINGLE]**
- Contested in adolescents — a clean null.
  [Moraitopoulou et al. 2024, Autism](https://pmc.ncbi.nlm.nih.gov/articles/PMC11301953/) **[SINGLE]**

Alexithymia is difficulty *identifying and describing* emotions. **An emotion word is a poor
output token for a user with a weak internal referent for that word.** Ranked by how
defensible each output format is:

1. **What the speaker probably wants to happen next** — actionable, checkable, needs no
   emotion vocabulary.
2. **The mismatch, stated as a fact about the signal** — "the words are agreement, the voice
   is flat and quiet; these don't match." Honest about what was actually measured, and defers
   interpretation to the person with the context.
3. **A named emotion.** Weakest, most confidently wrong-able.
4. **A suggested reply.** See §6.4 — this is the automated-masking failure mode.

*(This ranking is [INFERENCE] from the alexithymia literature; no study directly compares
emotion-label output against intent output. It is a hypothesis to test, not a finding.)*

### 1.5 The double empathy problem, and where emtext sits

- Autistic-to-autistic information transfer is as effective as non-autistic-to-non-autistic;
  **mixed chains** show significantly steeper information loss.
  [Crompton et al. 2020, Autism](https://journals.sagepub.com/doi/10.1177/1362361320919286) **[SINGLE, since replicated]**
- Negative first impressions of autistic adults form **within seconds** — and **vanish
  entirely when the audio-visual channel is stripped and only content remains.** "Style, not
  substance."
  [Sasson et al. 2017, Scientific Reports](https://www.nature.com/articles/srep40700) **[STRONG, 3 studies]**
- The critique is real too: a methodology review argues the supporting studies largely do not
  measure cognitive or affective empathy at all.
  [JADD 2026](https://link.springer.com/article/10.1007/s10803-026-07315-2) **[STRONG critique]**

**emtext is on the right side of this by construction** — it points the interpreter at the
*speaker*, not at the user. It is not training the autistic person to perform better.

**But Sasson 2017 is a direct warning about emtext's signal channel.** The bias against
autistic speakers lives *entirely* in the audio-visual style channel and disappears in the
content channel. SER on prosody is precisely that channel. This is fine when the speaker is
non-autistic. Pointed at an autistic speaker — including autistic-to-autistic conversation,
where the literature says communication is already working — the model will report a problem
that isn't there. **[INFERENCE from [STRONG]]**

---

## 2. Sarcasm — the evidence is worse than you would hope

### 2.1 Posed sarcasm is marked. Spontaneous sarcasm is not.

This is the finding that most undercuts acoustic sarcasm detection, and it has been known
since 2000.

- Rockwell: slower tempo, greater intensity, lower pitch significantly indexed sarcasm — **but
  listeners discriminated *posed* sarcasm from non-sarcasm and did NOT discriminate
  *spontaneous* sarcasm.**
  [J Psycholinguistic Research 29:483–495](https://doi.org/10.1023/A:1005120109296) ·
  [method comparison](https://doi.org/10.1007/s10936-006-9049-0) **[SINGLE ×2, same lab]**
- Bryant & Fox Tree analysed spontaneous ironic speech from talk radio: **the only reliable
  acoustic difference was amplitude variability**, and only among the clearest items.
  [Language and Speech 48(3)](https://journals.sagepub.com/doi/10.1177/00238309050480030101) **[SINGLE]**
- Attardo et al.: **no specific "ironical intonation" per se** — pitch is a *contrastive*
  marker relative to the speaker's own baseline.
  [Humor 16(2)](https://doi.org/10.1515/humr.2003.012) **[SINGLE]**

"There is no single ironic tone of voice" is **replicated across independent labs and
stimulus types** (Attardo: sitcom; Bryant & Fox Tree: spontaneous radio). **[STRONG]**

### 2.2 The cues are language-specific and reverse direction

English speakers **lower** mean F0 to mark sarcasm. Cantonese speakers **raise** it.
[JASA 126(3):1394–1405](https://doi.org/10.1121/1.3177275) **[SINGLE]**

A model or prompt rule that encodes "sarcasm sounds lower" is encoding an English-specific,
speaker-relative tendency as if it were physics.

### 2.3 The effect sizes are small

Best current large-corpus estimates: English sarcasm shows lower mean F0 (**d = −0.40**),
longer duration (**d = 0.32**); Mandarin longer duration (**d = 0.76**), greater pitch
complexity (**d ≈ 0.74**).
[arXiv 2608.30204](https://arxiv.org/html/2608.30204v1) **[PREPRINT]**

Small-to-medium. Real in aggregate, **not separable per utterance** — which is the only way
emtext can use them.

### 2.4 Adding audio makes current sarcasm models worse

The sharpest warning for emtext's exact design:

- Text-only F1 ≈ **67% (EN) / 68% (ZH)**; bimodal ≈ 66–72% — improvements are error
  *redistribution*, not gain.
- Adding audio produced **+9.8% false positives (EN), +8.6% (ZH)** while cutting false
  negatives only 7–10 points.
- **Prosody-only collapses to 14–22% F1.**
- Causal test: manipulating **only pitch and pausing** on non-sarcastic clips drove
  false-positive rates to **60%**.
- The learned stereotype is "elevated F0 + irregular pauses = sarcasm" — **the opposite of the
  English ground truth** (lower F0, d = −0.40).

[arXiv 2608.30204](https://arxiv.org/html/2608.30204v1) **[PREPRINT, direction corroborated]**

**For emtext this is the central risk.** A naive "voice disagrees with words → sarcasm" rule
will over-fire, and over-firing sarcasm at a listener calibrating social trust is the most
damaging failure this product can produce. `qwen3:14b`'s measured behaviour — 100% on the
sarcasm category, 2/4 on mismatch pairs because it fails the *sincere* halves — is exactly
this failure mode, already present.

### 2.5 The labels themselves are unreliable

- **MUStARD inter-annotator agreement: Cohen's κ = 0.1463** on the Big Bang Theory pass
  (0.2326 after reconciliation).
  [ACL 2019](https://aclanthology.org/P19-1455/) **[SINGLE]**
- MUStARD++ subsequently **corrected 343 mislabeled instances out of 690** — half the corpus.
  [LREC 2022](https://aclanthology.org/2022.lrec-1.653/) **[SINGLE]**
- **iSarcasm**, labelled by the tweets' own authors: third-party annotators **missed 30% of
  author-intended sarcasm**, and **45% of what they perceived as sarcastic was not intended as
  such.** Model F1 collapses from 0.874 (third-party labels) to 0.364 (author labels).
  [ACL 2020](https://aclanthology.org/2020.acl-main.118/) **[SINGLE, important]**
- Uncomfortable corollary: models fine-tuned on third-party labels **always outperform** those
  fine-tuned on author labels — third-party labels are more *learnable* because they encode a
  shallower, surface-cue notion of sarcasm.
  [arXiv 2404.06357](https://arxiv.org/html/2404.06357) **[PREPRINT]**

### 2.6 The human ceiling

| setting | accuracy |
|---|---|
| Text + full conversation context (SARC, individual raters) | **81.6%** |
| Same, majority vote | 85–92% |
| **Naturalistic video, audio + video + context (RISC, sarcasm specifically)** | **~68%** |
| Jocularity, same inventory | ~86% |

[SARC](https://arxiv.org/abs/1704.05579) · [RISC, PLoS ONE 10(7)](https://doi.org/10.1371/journal.pone.0133902) **[SINGLE each]**

**There is no regime in which humans are near-perfect at this.** Any emtext output implying
certainty about sarcasm is claiming more than trained human raters achieve with more
information than emtext has.

### 2.7 Context vs voice — a genuine, unresolved disagreement

- Deliens et al.: prosody and facial expression are **considerably less reliable cues than
  contextual incongruence**; interpreters privilege "frugal, albeit less reliable" heuristics.
  [J Memory and Language 99:35–48](https://doi.org/10.1016/j.jml.2017.10.001) **[SINGLE]**
- Bryant & Fox Tree: in **written form alone**, originally-ironic and originally-non-ironic
  spontaneous utterances received **statistically indistinguishable** irony ratings. Only when
  heard were the ironic ones rated more sarcastic.
  [Metaphor and Symbol 17(2)](https://doi.org/10.1207/S15327868MS1702_2) **[SINGLE]**

Both are right about their materials. Honest synthesis: **context wins when context is
available and diagnostic; voice is the only signal when it isn't.** emtext has 12 lines of
rolling context and no shared history with the speakers — closer to Bryant & Fox Tree's
condition than Deliens'.

### 2.8 Irony vs sarcasm: a pragmatic distinction, not an acoustic one

The line is drawn on **ridicule of a specific victim**
([Lee & Katz, Metaphor and Symbol 13(1)](https://doi.org/10.1207/s15327868ms1301_1)) — echoing
Kreuz & Glucksberg's echoic reminder theory
([JEP:General 118](https://psycnet.apa.org/doiLanding?doi=10.1037%2F0096-3445.118.4.374)).

**[GAP]** No study acoustically separates ridicule-bearing sarcasm from victimless irony.
Treat any claim that "sarcasm sounds different from irony" as unsupported. `TODO.md`'s entry
on irony should stay a low priority: the distinction is real pragmatically and undetectable
acoustically.

Worth knowing about sibling forms: **jocularity dominates friendly conversation** and is far
easier to read (~86% vs ~68% for sarcasm, RISC). **Understatement is vanishingly rare** — zero
instances in a 197-instance Chilean corpus, 12 of 777 in iSarcasm. **Deadpan is undetectable
by construction** — its marking is the *absence* of marking.
[Languages 9(1):22](https://doi.org/10.3390/languages9010022) **[SINGLE]**

---

## 3. The taxonomy is wrong, and smaller is better

### 3.1 The anti-basic-emotion argument is about faces, not voices

Barrett et al. (2019) is routinely cited as evidence against vocal emotion recognition. **It
contains no analysis of voice, prosody, or speech.** Its findings — mean r = .32 between
configuration and emotion, hypothesised configuration present in only **19%** of episodes —
are about facial movement.
[PSPI 20(1)](https://journals.sagepub.com/doi/10.1177/1529100619832930) **[STRONG, but face-only]**

The voice-side evidence is separate and says something more useful:

- Himba participants free-labelling Western emotional vocalizations **did not produce the
  expected emotion terms**. What they reliably extracted was **valence, and to a lesser degree
  arousal**.
  [Gendron et al. 2014, Psychological Science 25(4)](https://journals.sagepub.com/doi/abs/10.1177/0956797613517239) **[SINGLE, pivotal]**
- Within-culture vocal decoding accuracy ≈ **70% in a 5-alternative forced choice** (chance 20%)
  — well above chance, well below the 90%+ quoted for faces.
  [Juslin & Laukka 2003, meta-analysis](http://www.scholarpedia.org/article/Speech_emotion_analysis) **[STRONG]**

**Valence and arousal survive cross-cultural transfer from voice; emotion categories do not.**
A system whose acoustic front-end emits a categorical label is emitting the *least*
transferable part of what the voice carries.

The counter-case is fair and should be stated: Cowen et al. found 12 emotions recognised
across two cultures from speech prosody, arguing categories predict recognition better than
valence.
[Nature Human Behaviour](https://www.nature.com/articles/s41562-019-0533-6) **[SINGLE]** —
but the stimuli are **posed by actors**, which is the same validity objection Barrett levels
at the basic-emotion literature, and which §2.1 shows is decisive for sarcasm.

### 3.2 The granularity paradox

Five taxonomies compared at increasing granularity against GPT-5:

| | SemEval (4 categories) | GoEmotions (27) | change |
|---|---|---|---|
| Accuracy | 0.657 | 0.422 | −35.8% |
| **Macro F1** | **0.457** | **0.213** | **−53.3%** |

Correlation between category count and accuracy: **r = −0.965, p < 0.001**. Crucially,
**human annotator agreement did not degrade with granularity — only the model's did.**
[Frontiers in Psychology 2026](https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2026.1786724/full) **[PREPRINT]**

A ~5-slot output budget is defensible on evidence, not just engineering pragmatism.

### 3.3 emtext's five tones mix three incompatible axes

`positive / negative / neutral / sarcastic / mixed` forces together:

- `positive/negative/neutral` — **valence**, an affect dimension
- `sarcastic` — a **pragmatic device** (an utterance can be sarcastic *and* negative)
- `mixed` — a **meta-label** about the classifier's own uncertainty, doing work a confidence
  score should do

Because they are mutually exclusive, **sarcasm suppresses the valence reading and vice
versa** — and §2.4 says sarcasm is the label most likely to over-fire.

### 3.4 The right level has a name: interpersonal stance

Scherer's typology separates affective states by design features:

| type | definition | examples |
|---|---|---|
| **Emotion** | brief, synchronised response to a significant event | angry, sad, joyful |
| **Mood** | diffuse, low intensity, long duration | cheerful, irritable |
| **Interpersonal stance** | **affective stance toward another person in a specific interaction** | **distant, cold, warm, supportive, contemptuous** |
| **Attitude** | enduring affectively-coloured belief | liking, hating |

[Scherer 2005](https://web.stanford.edu/~jurafsky/slp3/old_dec21/20.pdf)

**A live conversation interpreter is not primarily an emotion detector — it is an
interpersonal stance detector.** That reframing makes the taxonomy question tractable, because
stance categories (warm/cold, affiliative/distancing) are a smaller and more stable set than
emotion categories.

---

## 4. What is actually detectable from voice

```
arousal / activation      ████████████  strong, consistent across the SER literature
valence                   ██████        real but weak — and see the caveat below
emotion category          ████          acted speech only; collapses on natural speech
interpersonal stance      █             essentially no acoustic evidence
lexical/pragmatic acts    ·             none — these live in the words
```

### 4.1 The naturalistic ceiling

**Interspeech 2025 SER Challenge** (MSP-Podcast, spontaneous speech, ≥5 annotators per turn,
speaker-independent splits):

- best categorical **macro-F1 ≈ 0.378** (baseline 0.329)
- best dimensional **average CCC ≈ 0.60** (baseline 0.58)

[ISCA archive](https://www.isca-archive.org/interspeech_2025/naini25_interspeech.html) **[STRONG, field-wide benchmark]**

**The global state of the art on spontaneous-speech categorical emotion is under 0.4 F1.**
Dimensional attributes are the healthier signal — which is a real argument for the VAD path,
and it aligns with the mismatch rule's design.

This reconciles a confusing local result. emtext's own `eval/results/pipeline.csv` shows
emotion2vec at **86% average recall** — but that is **RAVDESS, which is acted**. Cross-corpus
SER figures drop to around **61.9% UA**
([PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC9858266/)), and the challenge number above is
the honest expectation for a real room. **A model trained and tested on acted corpora is
measuring acting.**

### 4.2 Text beats audio where you would least expect it

Continuous satisfaction–frustration tracking on **real** French call-centre conversations:

| representation | CCC |
|---|---|
| Acoustic only (wav2vec) | 0.806 |
| **Linguistic only (CamemBERT)** | **0.924** |
| Best fusion | 0.920 |

**Fusion did not beat text alone.**
[arXiv 2310.04481](https://arxiv.org/abs/2310.04481) **[SINGLE]**

This directly contradicts the industry folk claim that "customers mask dissatisfaction in
words but reveal it in tone." For emtext it is a caution: the transcript is doing more work
than it gets credit for, and the voice channel's marginal contribution should be *measured*,
not assumed.

### 4.3 Audio LLMs read rather than listen — which validates emtext's architecture

2,000 adversarial examples pairing audio with a transcript asserting the *wrong* attribute:

| model | ground-truth accuracy | agreement with the misleading transcript |
|---|---|---|
| GPT-4o Audio | **8.60%** | **81.55%** |
| Qwen3-Omni | 10.60% | 80.65% |
| mean across 12 models | 15.33% | 64.34% |

[arXiv 2605.27772](https://arxiv.org/html/2605.27772v1) **[PREPRINT]**, corroborated by the
independent [LISTEN benchmark](https://arxiv.org/pdf/2510.10444) **[SINGLE]**

**This is the strongest available argument for emtext's current design.** If the voice signal
came from an audio LLM, it would largely be a laundered transcript — and because the
interpreter LLM *also* receives the transcript, the mismatch rule would be comparing the text
against itself, producing confident agreement rather than information. **A dedicated
acoustic-only SER model is architecturally correct precisely because it cannot cheat.**

It is also a direct argument against the `TODO.md` entry proposing an audio-native LLM to
replace Whisper: that change would collapse two independent channels into one.

### 4.4 Where voice genuinely adds information over the transcript

| phenomenon | voice adds? | evidence |
|---|---|---|
| Arousal / activation | **Yes, substantially** | consistent across SER literature |
| Anger / agitation | **Yes** | intensity + voice quality genuinely diagnostic |
| Authenticity of laughter and vocal bursts | **Yes — voice is the only channel** | [Anikin & Lima 2018](https://pubmed.ncbi.nlm.nih.gov/27937389/), 65% human accuracy **[SINGLE]** |
| Valence | **Marginally** | valence gap; Gendron cross-cultural |
| Sarcasm | **Currently negative** | +8–13 FP points; prosody-only 14–22% F1 |
| Politeness | **Within-culture only** | Japanese *kyoshuku* is misread as *impoliteness* by French listeners |
| Contempt | **In principle yes, in practice no** | perceptually real; no corpus, no model |
| Condescension, passive aggression, hedging, dismissiveness | **No** | no acoustic literature exists |
| Reluctance, disengagement, interruption | **Yes — via timing, not spectrum** | §5 |

### 4.5 Acoustic valence is substantially laundered text — which reframes everything

This is the most consequential finding in the whole survey, and it arrived last.

Wagner et al. closed the valence gap to **CCC 0.638** on MSP-Podcast (vs arousal 0.745,
dominance 0.655) — and then showed *why* it worked. From their abstract, verbatim:

> "their extraordinary success on valence is based on **implicit linguistic information**
> learnt during fine-tuning of the transformer layers, which explains why they perform on-par
> with recent multimodal approaches that explicitly utilise textual information."

[arXiv:2203.07378](https://arxiv.org/abs/2203.07378) **[STRONG, peer-reviewed TPAMI]**

Three independent lines of evidence:

1. **The TTS probe.** They re-synthesised the test transcripts as prosodically *flat* TTS.
   Models scoring high on real audio also scored high on the synthetic versions — performance
   survives removing the original prosody. **[approx, figure-read]**
2. **Freeze the transformer and the effect vanishes.** With layers frozen, correlation with the
   synthetic condition "drops to almost zero." Fine-tuning is what installs the lexical
   capability. **[approx]**
3. **The distillation table settles it.** Wav2Small distils a large VAD teacher into 72K
   parameters:

   | | arousal | dominance | **valence** |
   |---|---|---|---|
   | Teacher | ~0.73 | ~0.63 | **0.676** |
   | Wav2Small (72K) | ~0.66 | ~0.56 | **~0.37** |
   | relative loss | −10% | −11% | **−45%** |

   [arXiv:2408.13920](https://arxiv.org/abs/2408.13920) **[approx, figure-read]**

   Arousal survives distillation. Valence collapses. **You cannot distil lexical understanding
   into 72K parameters** — which is exactly what you would predict if valence-from-audio is
   mostly valence-from-implicit-ASR.

A companion probing study confirms the mechanism directly: valence predictions are "very
reactive to positive and negative sentiment content, as well as negations, but not to
intensifiers or reducers, while **none of those linguistic features impact arousal or
dominance**."
[arXiv:2204.00400](https://arxiv.org/abs/2204.00400) **[SINGLE]**

And the corroborating numbers from the ASR-transcript literature: adding text moves valence
**+0.08 CCC** (0.531 → 0.613) and does essentially nothing for arousal. Notably, **ASR at
~12–15% WER is as good as ground-truth transcripts** for this purpose.
[arXiv:2406.08353](https://arxiv.org/html/2406.08353v2) **[approx]**

### 4.6 The consequence: emtext is chasing the wrong dimension

If acoustic valence is largely lexical sentiment, then an acoustic valence model is **doing
badly what emtext's LLM already does well**, from a transcript it already has in cleaner form.

Worse for the mismatch rule specifically: **if the acoustic valence head is partly lexical, it
will tend to *agree* with the text and therefore under-fire on sarcasm** — the one case where
the two channels are supposed to diverge. That is a plausible mechanistic explanation for
MERaLiON's measured happy-vs-angry valence separation of only 0.143 on this repo's own
RAVDESS run, and it is directly testable: *check whether the model's valence output correlates
more with a text sentiment score than with any prosodic feature.*

**What the audio genuinely provides that text cannot: arousal (CCC 0.745) and dominance
(0.655).** Both are robust, both are paralinguistic, and arousal saturates at ~25% of training
data while valence needs all of it — another fingerprint of the same asymmetry.

So the mismatch rule should be rebuilt on the dimensions the audio can actually deliver:

| words say | arousal | dominance | likely reading |
|---|---|---|---|
| positive | **low** | **low** | masking, flat compliance, "fine" that isn't fine |
| positive | high | high | genuine enthusiasm |
| negative | high | high | real anger / complaint |
| negative | low | low | sadness, resignation |

This is a *different rule* from the current valence-keyed one, and it has the advantage of
keying on the signal that actually exists.

### 4.7 One model could serve both ASR and SER

emtext already runs a Whisper encoder for transcription. The evidence says that forward pass
carries usable emotion information:

- A **frozen** Whisper-Small encoder (88M) with only an attentive-pooling head trained reaches
  **72.96% UA on IEMOCAP** — within **1.6 points** of HuBERT-X-Large at 1B parameters, an ~11×
  parameter saving. [arXiv:2602.06000](https://arxiv.org/html/2602.06000) **[PREPRINT]**
- **Intermediate encoder layers beat the final layer** for emotion, so the encoder can be
  truncated for further savings.
- *A Tiny Whisper-SER* demonstrates ASR and SER sharing one encoder *and* decoder, with
  **29.75%/63.58% relative WER reduction** at a cost of only **4.28%/1.68%** relative macro-F1.
  The catch is central: **naive joint training with equal loss weights causes catastrophic
  forgetting of ASR** — a two-stage weighted recipe is mandatory.
  [IEEE](https://ieeexplore.ieee.org/iel8/10848542/10848533/10848651.pdf) **[SINGLE]**
- Note MERaLiON-SER is itself a Whisper-Medium encoder with LoRA adapters — the "too slow"
  model was already a Whisper-encoder SER model.

**Blockers before this is actionable:** no pre-trained dimensional head exists for a
CTranslate2 Whisper encoder (you would train one on MSP-Podcast), and it is unverified whether
`ctranslate2.models.Whisper.encode()` exposes usable hidden states in the installed
faster-whisper. This is a training project, not an integration — but it is the only path where
the marginal cost of SER approaches **zero**.

### 4.8 Confidence is uncalibrated, and abstention is a solved problem

`SER_MIN_CONFIDENCE` currently thresholds a raw softmax, and the literature says that number
does not mean what it looks like.

- Calibration work on multi-label SER exists precisely because the assumption that "highly
  confident predictions are often right" **fails**. The fix is **post-hoc temperature
  scaling** — cheap, needs only a small labelled calibration set, and **does not change the
  argmax**. [NSF PAR #10441271](https://par.nsf.gov/biblio/10441271) **[SINGLE]**
- Abstention has a formal treatment going back to *SER with a Reject Option*
  ([Interspeech 2019](https://www.isca-archive.org/interspeech_2019/sridhar19_interspeech.html)),
  and a modern one: **conformal prediction** gives a distribution-free guarantee of "say
  nothing at risk level α," holding up under cross-dataset shift.
  [arXiv:2503.22712](https://arxiv.org/abs/2503.22712) **[SINGLE]**
- The deeper reason confidences are poor: hard consensus labels **collapse annotator
  disagreement** and force models to express false confidence on genuinely ambiguous cases.
  MSP-Podcast uses ≥5 raters per sample; a model that never abstains claims certainty the
  *labels* do not have.

### 4.9 Demographic bias is large enough to matter for one user

CREMA-D, unmitigated baseline, true-positive-rate gaps **[approx]**:

| attribute | TPR gap |
|---|---|
| **Gender (M/F)** | **0.278** |
| Race | 0.183 |
| Age | 0.153 |

[arXiv:2505.14449](https://arxiv.org/abs/2505.14449)

A 0.278 TPR gap means the model is ~28 points better at detecting an emotion in one group than
another. And Wagner et al.'s own fairness analysis found their model "fair with respect to
biological sex groups, but **not towards individual speakers**" — some speakers show
dramatically reduced CCC despite good aggregate numbers.

**For a single-user assistive device, aggregate fairness is cold comfort.** emtext's user could
be an outlier and nothing in the published metrics would reveal it.

Compounding risk: since transformer valence *is* substantially ASR, accent-driven ASR
degradation propagates straight into valence — one non-standard accent degrades the transcript
*and* the acoustic reading through the same mechanism.

### 4.10 Short utterances

MERaLiON's own published numbers, on a model this repo has already run: 7-class UAR **53.9% at
2 seconds vs 60.2%** on merged longer segments (4-class: 65.1% vs 70.0%). A **5–6 point UAR
penalty** for operating at 2 s.

emtext's `MIN_UTTERANCE_MS` is 350 ms. Everything between 350 ms and ~1.5 s is being scored by
SER in its worst regime. **Abstaining from the voice reading (not the transcript) below ~1.5 s
is a cheap, evidence-backed win.**

---

## 5. Timing — the signal emtext already has and throws away

This is the most under-exploited finding in the whole survey.

### 5.1 A >700 ms gap predicts a coming refusal

- Within −100 ms to +700 ms, preference makes little timing difference. **Beyond ~700–800 ms,
  responses are, with very few exceptions, dispreferred** (refusals, declines, disagreements).
  After 750 ms: 15.3% of dispreferred vs 4.1% of preferred responses.
  [Kendrick & Torreira 2015, Discourse Processes 52(4)](https://doi.org/10.1080/0163853X.2014.955997) **[SINGLE, large corpus]**
  - Note it is **not linear** — a naive "longer gap = more reluctant" mapping is wrong below
    the threshold, where dispreferred responses are marginally *earlier*.
- **EEG evidence that neurotypical brains use this pre-consciously:** at a 300 ms gap, a fast
  "no" evokes an **N400** relative to a fast "yes". At **1000 ms that N400 difference
  disappears entirely** — the delay has already shifted the listener's expectation to "no".
  [Bögels et al., PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0145474) **[SINGLE]**
- Robust cross-language baseline: response-time mode is **0–200 ms**, and cross-language means
  vary by **no more than ~250 ms** across ten languages.
  [Stivers et al., PNAS 106(26)](https://www.pnas.org/doi/full/10.1073/pnas.0903616106) **[STRONG]**

**Why this matters more than anything else here:** it is a cue neurotypical brains process
automatically and pre-consciously. Surfacing it is **restoring parity**, not adding
speculation. It requires **no model at all** — two timestamps and a subtraction.

### 5.2 The catch, stated honestly

**emtext has no diarization.** `server/segmenter.py` maintains a frame clock
(`frames_seen × FRAME_MS`), so inter-utterance gaps are already derivable — but a gap in a
single undifferentiated stream could be a turn transition, the same speaker pausing
mid-thought, or the user themselves.

The dispreference finding requires knowing that A asked and B answered. So this is **free in
compute and not free in architecture**. What *is* free today:

- intra-utterance silence ratio and hesitation (reluctance markers)
- `last_close_reason` already distinguishes `end_silence` / `max_length` / `too_short`
- utterance duration and voiced fraction, both already tracked

### 5.3 Intensity entrainment beats pitch entrainment

From CANDOR (1,500+ spontaneous 30-minute dyadic conversations with post-hoc quality ratings):

| marker | finding |
|---|---|
| **Intensity entrainment** | **strongest acoustic predictor** (Cliff's δ −0.273 to −0.495) |
| Pitch entrainment | weaker (δ −0.088 to −0.326) |
| Turn duration and count | longer turns, more turns → higher success |
| Pause duration | longer total pause → lower success |

[arXiv 2604.15322](https://arxiv.org/html/2604.15322v1) **[PREPRINT]**

Entrainment *breakdown* signals increased social distance and negative attitude toward the
interlocutor
([Levitan et al., NAACL-HLT 2012](https://aclanthology.org/N12-1002.pdf)) **[SINGLE]**. Both
are running statistics over RMS — no model.

---

## 6. What this means for emtext

### 6.1 Keep — supported more strongly than expected

- **The words-vs-voice mismatch framing.** It is the one place the autism literature (§1.1),
  the double-empathy frame (§1.5) and the architecture all converge.
- **A dedicated acoustic SER model rather than an audio LLM** (§4.3). This is now an
  evidence-backed architectural decision, not just a latency one.
- **Pointing the interpreter at the speaker, not the user** (§1.5).
- **`SER_MIN_CONFIDENCE` downweighting.** The right instinct; §6.4 argues for going further.

### 6.2 Change — stop chasing valence, key the mismatch rule on arousal and dominance

**This supersedes the "restore a valence signal" plan in `TODO.md`.** The evidence in §4.5–4.6
says acoustic valence is substantially implicit lexical sentiment — so an acoustic valence
model spends CPU re-deriving, worse, a signal the interpreter LLM already has from the
transcript. And because a partly-lexical valence head tends to *agree* with the text, it will
**under-fire on exactly the sarcasm cases the rule exists to catch**.

Ranked options for this constraint set (CPU-only, <0.5 s, transcript already available):

| # | option | cost | verdict |
|---|---|---|---|
| **1** | **Keep emotion2vec; let the LLM own valence from the transcript** | **zero** | **Recommended.** Stop paying CPU for a worse copy of a signal you have. |
| **2** | **Add arousal + dominance** — the genuinely acoustic dimensions | ~9 ms (Wav2Small) or free (map emotion2vec's 7 classes to A/D anchors) | **Recommended alongside 1.** Rebuild the mismatch rule on these. Ignore Wav2Small's valence output entirely. |
| 3 | Share the Whisper encoder (§4.7) | multi-week training project | Highest ceiling. Only if SER becomes the limiting factor. |
| 4 | audeering `w2v2-...-msp-dim` | 0.3–1.5 s, **CC-BY-NC-SA (research only)** | Use as a *measurement instrument* only: run it offline over RAVDESS to settle whether a good acoustic valence model beats MERaLiON's 0.143 separation. If it doesn't, the acoustic-valence question is closed permanently. |
| 5 | Odyssey WavLM baselines (**MIT licence**) | 0.3B params — slower than the MERaLiON already rejected | Licence-clean reference / distillation teacher, not a runtime option. |
| 6 | SenseVoice-Small | replaces faster-whisper | Categorical only, so it does not solve valence — but it emits **laughter and crying** as audio events in one pass, which are unambiguous affective signals no VAD regressor provides. |

A licence fact worth recording: **audeering's model is CC-BY-NC-SA-4.0, research use only.**
That is fatal if emtext ever ships as anything but a personal project. The Odyssey WavLM
baselines are MIT — the only permissively-licensed quality dimensional models found — but they
are 0.3B parameters.

One data-hygiene warning: a 2026 vendor blog claims this checkpoint reaches "CCC 0.76–0.82 on
MSP-Podcast." That contradicts the authors' own paper by a wide margin and appears fabricated
or misread from the arousal figure. **Do not use that number.**

### 6.2b Two cheap wins available regardless of which option is chosen

1. **Calibrate the confidence, then abstain.** `SER_MIN_CONFIDENCE` thresholds a raw,
   uncalibrated softmax (§4.8). Post-hoc temperature scaling is cheap, needs a small labelled
   set, and does not change any prediction — it just makes the threshold mean something.
2. **Abstain below ~1.5 s.** MERaLiON's own numbers show a 5–6 point UAR penalty at 2 s, and
   `MIN_UTTERANCE_MS` is 350 ms (§4.10). Drop the *voice* reading, keep the transcript.

### 6.3 Change — the taxonomy

The current five tones mix three axes (§3.3). Same output budget, re-partitioned:

1. **Affect** — coarse arousal × dominance from the voice, valence from the transcript.
   Per §4.6 this is the split the signals actually support.
2. **Interpersonal stance** — sparse, emitted only above threshold: `warm/affiliative`,
   `cold/distancing`, `guarded/softening`, `pressing/impatient`, default unmarked.
3. **Words-vs-voice relationship** — `aligned` / `voice-flatter-than-words` /
   `voice-more-intense-than-words` / `undetermined`. Phrased on arousal/dominance, which is
   what the audio measures, rather than on valence, which it mostly infers from words. **Describe the mismatch; do not name it
   "sarcasm."** "Teasing", "masking" and "sarcasm" are competing *interpretations* of the same
   evidence, and the listener has context the model does not.
4. **Conversational-dynamics flags** — from timing only (§5), gated on diarization.
5. **Abstain** — a real output, not a fallback.

### 6.4 Measure — the honest baselines

- emtext's 86% SER recall is an **acted-corpus** number. Expect **~0.38 macro-F1** territory
  on real conversational speech (§4.1).
- Any sarcasm claim should be read against a **~68% human ceiling** on naturalistic video
  (§2.6).
- Before adding an acoustic valence model, **measure the marginal gain over the transcript
  alone** — §4.2 shows text winning outright on a comparable task.

### 6.5 Output design — the confidence paradox

- Participants rated hedged and unhedged AI **equally trustworthy**, but were **significantly
  less likely to follow hedged advice**.
  [ACM CUI 2025](https://dl.acm.org/doi/10.1145/3816046.3816231) **[SINGLE]**
- **Most participants could not detect miscalibration**, producing over-reliance on
  overconfident AI.
  [arXiv 2402.07632](https://arxiv.org/abs/2402.07632) **[SINGLE]**

There is no output style that is both honest and maximally followed. The escape the evidence
supports: **suppress below threshold rather than hedge.** A hedged reading is followed less
anyway, so omission at least does not spend the user's attention.

And **do not ship suggested replies.** Autistic users independently name this as "the ultimate
masking"
([arXiv 2601.17946](https://arxiv.org/html/2601.17946), 3,984-post corpus) **[SINGLE]**, and
AI-mediated communication carries a measured penalty concentrated in **perceived
trustworthiness, caring and authenticity** — worst in emotional and interpersonal registers
([ScienceDirect](https://www.sciencedirect.com/science/article/pii/S2949882126000551)) **[SINGLE]**.
Interpretation is invisible to the other party; generated replies are not.

### 6.6 Do not build

| feature | why not |
|---|---|
| Politeness / hedging / indirect-request interpretation | evidence says largely intact (§1.1) |
| Boredom / "they want to leave" | best comparable detectors 69–71%; one disengagement detector was **18.5% precise** on its positive class |
| Confident detection of suppressed affect | **[GAP]** no benchmark, no dataset, and a structural reason: annotators label from the same signal the model sees, so a successful mask is labelled as the mask |
| A `contempt` label | perceptually real, but no corpus and no model — and it is exactly the label a person will act on |
| Suggested replies | §6.4 |
| Running SER on an autistic speaker | Sasson 2017 — the bias lives in this channel (§1.5) |

### 6.7 One legal note

**EU AI Act Article 5(1)(f)**, in force February 2025, prohibits AI systems inferring emotions
of a natural person in **workplace and education** contexts, explicitly covering inference
from biometric data **including voice**. Recital 44 cites the "limited reliability, lack of
specificity and limited generalisability" of such systems.
[FPF analysis](https://fpf.org/blog/red-lines-under-eu-ai-act-unpacking-the-prohibition-of-emotion-recognition-in-the-workplace-and-education-institutions/)

Note the asymmetry emtext sits inside: the ban targets *biometric* inference, so a
transcript→LLM path is treated differently from a prosody→SER path. Personal use is outside
the Act's core scope, but a disability accommodation used at work is exactly the contested
boundary.

---

## 7. Where the evidence is thin

Stated as gaps, not as things I failed to find.

| topic | status |
|---|---|
| Acoustic comparison of sarcastic vs victimless irony | **[GAP]** — the distinction is pragmatic only |
| Passive aggression detection | **[GAP]** — no peer-reviewed dataset or benchmark |
| Contempt / stonewalling / defensiveness detection | **[GAP]** — Gottman's constructs have no detector |
| Detecting deliberately suppressed affect from voice | **[GAP]** — and structurally hard to label |
| Topic avoidance | **[GAP]** |
| Understatement acoustics | **[GAP]**, and empirically rare enough not to matter |
| Emotion-label output vs intent output for alexithymic users | **[GAP]** — §1.4's ranking is a hypothesis |
| Deadpan irony | undetectable by construction — the marking is absence of marking |

Also worth flagging: Gottman's "93% divorce prediction" figure should **not** be carried into
any product claim — the standing methodological critique is that it reflects post-hoc model
fitting rather than cross-validated prediction.

---

## Method

Four parallel literature surveys (autistic-listener needs and assistive-tech ethics; emotion
taxonomy and interpersonal stance; irony/sarcasm pragmatics and the context problem; SER model
state of the art), plus local recomputation from this repo's own
`eval/results/{ser,pipeline,model}.csv`. Claims are tagged by strength; 2026 arXiv preprints
are marked as such and were only retained where an independent source corroborated the
direction. Where sources disagree — prosody vs context primacy (§2.7), categories vs
dimensions (§3.1) — both sides are given rather than smoothed.
