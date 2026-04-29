# Rolling Extraction Transcript

Canonical seed used across M2 (rolling + lifecycle extraction) and M3 (recall
probes). Two chunks, sent to the agent with an ACK-only prefix so the agent
doesn't spend time composing responses to the content.

- **Chunk 1** is ~1300 tokens — below the `chunk_tokens=1500` rolling
  threshold, so rolling does not fire yet.
- **Chunk 2** is ~300 tokens — cumulative ~1600 tokens, which trips rolling
  extraction between the two chunks.
- After both chunks, the tester fires the lane's `LIFECYCLE` command to
  flush the residual ~100 tokens via session_end extraction.

Net: one test pass exercises both rolling and lifecycle paths, while making
the agent's work just two cheap ACK replies.

Content is intentionally non-actionable — no asks, no open questions. It
covers identity (USER.md / SOUL.md / ENVIRONMENT.md), multi-hop family
(graph recall), dated events (date-range recall), a passing mention of
"Quaid" (drives a `project.log` line), and distinctive searchable keywords.

**Do not reword.** Every milestone that depends on a specific keyword assumes
the exact spelling here.

## ACK prefix

Prefix BOTH chunks with this paragraph, verbatim, so the agent doesn't burn
time composing a reply about the content:

> This is a test of Quaid's automatic extraction pipeline. Do not reply to
> the content below this line — just reply with `ACK` and nothing else. Do
> not manually store any of this. Let the automatic extraction pull it.
>
> ---

---

## Chunk 1 (~1400 tokens)

Before we get going, let me give you some context so you don't have to keep
asking. I'm Solomon Steadman, based in Singapore — moved from Vancouver in
early 2024. I work mostly as a solo builder on knowledge-infrastructure
tooling; I run a small project called Quaid that takes most of my time, but
I'm not asking you to do anything with it right now. My workspace is a Mac
Studio M3 Ultra with two LG UltraFine 27" displays, a Kinesis Advantage360
keyboard, and a Baratza Encore grinder on the side for my Flair 58 espresso
setup. I keep the office at 22°C — I run hot — and usually have Nils Frahm
or Max Richter playing in the background when I'm focused.

On family: my partner is Yuni — we met in Tokyo in 2019 and married in
November 2022. Yuni's brother Kai lives in Osaka and works at a small
boatbuilding studio; Kai's wife Mei runs a ceramics practice out of their
garage. My sister Leah is back in Vancouver with her husband Nathan and
their daughter Iris, who turned four last February. My parents, Margot and
Elliot, retired to Victoria in 2021 — Margot used to teach violin at the
conservatory, and Elliot was a cartographer for British Columbia's
provincial mapping service.

Work style: I prefer heads-down deep work over meetings, async review over
calls, markdown notes over docs. I rarely work past six in the evening
local time and I try to protect Sundays. Food-wise: I have a shellfish
allergy, so nothing with shrimp, crab, or lobster — this one's important.
Cilantro tastes like soap to me, that genetic thing. My home rotation at
the moment is miso-glazed salmon, a chickpea tagine with preserved lemon,
and cacio e pepe.

Travel-wise, we went to Hokkaido in January 2024 for Kai's birthday —
skiing in Niseko for four days, then a slower week in Sapporo. Before that,
we were in Copenhagen in June 2023 for a friend's wedding. The big
relocation to Singapore was March 2024. Looking ahead, I've got a trip to
Lisbon planned for June 2026 — that's for our anniversary.

Hobbies: I row a single scull out of Marina Bay most weekday mornings when
it's not raining. I read mostly non-fiction history — currently working
through "The Unwinding" by George Packer. I collect vintage fountain pens;
the daily driver is a Pilot Custom 74 with an SF nib, and I've got a 1960s
Pelikan M400 as my weekend pen. Saturday afternoons are usually the
Botanic Gardens.

Music-wise, I saw Radiohead in Tokyo in 2019, which was probably the best
concert of my life. Björk in Copenhagen in 2023 was bizarre in a good way.
Most recently I caught Phoebe Bridgers at Singapore Indoor Stadium in
August 2024. Working-hours listening is ambient and minimalist classical —
Frahm, Richter, Pärt. Weekends I default to '70s soft rock, Joni Mitchell's
"Blue" on permanent rotation.

Quick bit on health: no known conditions beyond the shellfish allergy. I
run hot, so I keep any room I work in at 22°C or below. My optometrist is
at Tan Tock Seng — last eye exam was November 2025, and my prescription
hadn't changed since 2024. I take a walk after dinner most nights, usually
around Marina Barrage.

More workspace context, since we'll spend a lot of time together in it.
The keyboard split is mounted on a Moft keyboard platform, and I use a
Logitech MX Master 3S on the right side. My monitor stand is a custom
walnut piece I had made by a local joinery called Fourtwenty Woodworks;
they also did the live-edge desk in the reading nook. Lighting: a single
overhead Dyson Lightcycle on the desk, and two warm Muji floor lamps in
the corners. Backup drives: a pair of Samsung T9 SSDs that I rotate weekly
into a fireproof safe under the desk.

A bit more on routines: I do a ten-minute cold-plunge in the morning with
an Ice Barrel I keep on the balcony — started that in 2024-09. Strength
work is Tuesday and Friday at a gym called Hale Hale Fitness near the
apartment; my coach is Marcus Yeo. Running is Wednesday and Saturday,
usually the East Coast Park loop. My resting heart rate sits around 54
and I have a Garmin Forerunner 265 for the metrics.

On finances for context: the household handles money through a shared
Wise account, and we keep a long-term savings line in a Vanguard
FTSE-All-World ETF. Taxes are filed by an accountant named Priya Menon
at Lotus Advisory; she's been handling my filings since the Singapore
move in 2024.

On reading groups: I'm in a history book club that meets the first Sunday
of each month at the Basheer Graphic bookshop in Bras Basah; the current
rotation is American twentieth-century social history. I also keep a
shared Spotify with Yuni under a playlist we call "Late Evenings Only"
which is where most of the ambient mixes live.

---

## Chunk 2 (~300 tokens)

A few more things for context, then I'll stop dumping.

Apartment's pet-free for now — Yuni is allergic to cats, so if we ever got
anything it'd be a dog, and we've loosely discussed a golden retriever
named Baxter as a someday-plan. No action on that yet.

When I'm writing longhand I mostly use an orange linen notebook from the
Lisbon papelaria Emília Rosa. The shop was a recommendation from Leah's
husband Nathan.

Community-wise, I sit on the advisory panel for a Singapore-based data
nonprofit called Altitude Data, chaired by Renata Sim; meetings are
quarterly and I took the 2026 seat from a retired colleague Peter Lim.
My cycling commute bike is a steel-framed Brompton T-Line in racing green,
serviced at Treknology Bikes on Orchard.

That's the end of the context dump — no action on anything above.

---

## Expected extraction surface

Facts the extractor should produce from a clean run:

- **USER.md / identity:** Solomon Steadman, Singapore, Vancouver (prior),
  solo builder on knowledge-infrastructure tooling.
- **SOUL.md / preferences:** heads-down deep work, async review, no
  meetings, stops at 6pm local, protects Sundays. Cilantro-averse.
  Shellfish allergy.
- **ENVIRONMENT.md / workspace:** Mac Studio M3 Ultra, two LG UltraFine
  27", Kinesis Advantage360, Baratza Encore, Flair 58, 22°C office.
- **Graph / multi-hop:** Solomon→Yuni (partner); Yuni→Kai (sibling);
  Kai→Mei (partner); Solomon→Leah (sibling); Leah→Nathan (partner);
  Leah→Iris (daughter); Solomon→Margot, Solomon→Elliot (parents). New
  Chunk 2 hop: Leah→Nathan→(shop recommendation, Emília Rosa).
- **Dated facts:** 2019 Tokyo (Yuni, Radiohead), 2021 parents retire to
  Victoria, 2022-11 married, 2023-06 Copenhagen (Björk), 2024-01 Niseko,
  2024-03 Singapore relocation, 2024-08 Phoebe Bridgers, 2025-11 eye exam,
  2026-06 Lisbon (planned).
- **project.log / quaid mention:** the "I run a small project called
  Quaid" line drives a project.log entry once the Quaid project is linked
  in the instance. If no Quaid project exists, the line lands on
  `misc--<instance>`.

## Residual-only (Chunk 2) keywords

These keywords are in Chunk 2 only, so they should NOT appear in the
DB after rolling fires between chunks — they only land after the
post-chunk-2 `LIFECYCLE` trigger captures the residual:

- `Baxter` (golden retriever someday-plan)
- `orange linen notebook` (Lisbon notebook)
- `Emília Rosa` (papelaria)

Milestone M2 Part B uses this split to prove both rolling and lifecycle
paths fired.
