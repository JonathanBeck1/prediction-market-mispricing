# Event Format Intelligence Guide

Each event format has a distinct phrase probability profile. Apply these constraints
when classifying and assessing phrase multipliers for a specific event.

---

## DIPLOMATIC — Bilateral Meetings, State Dinners, Summits

**Characteristics:**
- Formal setting with foreign head of state or senior official
- Scripted talking points, diplomatic protocol, joint press appearances
- Topics constrained to bilateral agenda: trade, security, alliances
- Trump stays more on-message than at domestic events

**Suppress heavily (0.1x – 0.4x):**
- Partisan attack phrases: "biden", "democrat", "pelosi", "radical left", "crooked"
- Personal insults and nicknames: "sleepy joe", "tampon tim", "fat slob", "crying chuck"
- Domestic political phrases: "election", "rigged", "stolen election", "deep state"
- Pop culture / rally catchphrases: "discombobulator", "mog", "mogged", "cookie"
- Sports references unrelated to the host country
- MAGA rally phrases: "drill baby drill", "lock her up", "build the wall"

**Suppress moderately (0.4x – 0.7x):**
- General US domestic policy phrases when not bilateral-relevant
- Phrases about domestic opponents or media
- Off-topic geopolitical phrases (e.g., if meeting Japan: suppress Ukraine, Middle East)

**Boost (1.1x – 1.8x):**
- Host country name, companies, leaders, cultural references (e.g., meeting Japan: "toyota", "shinzo", "japan", "trade")
- Core bilateral topics (tariffs, trade deals, security alliances, specific treaties)
- NATO, China, Taiwan if contextually relevant to the bilateral relationship
- "Deal", "beautiful", "tremendous" — Trump uses these at all formal meetings

**Key rule:** The host country's name is almost always a strong boost. The host country's
major companies, PM/President's name, and key bilateral issues are strong boosts.

---

## RALLY — MAGA Rallies, Campaign Events, Save America Events

**Characteristics:**
- Unscripted, high-energy, crowd-driven
- Trump goes extensively off-script; all phrase categories are plausible
- Partisan attacks, nicknames, culture war phrases have highest rates here

**Suppress lightly (0.8x – 0.9x):**
- Only suppress phrases with zero relevance to any current topic (e.g., earnings terminology)
- Technical financial/policy jargon

**Boost (1.1x – 1.6x):**
- Phrases matching current news cycle topics
- Attack nicknames matching current political opponents
- Immigration, crime, election integrity phrases if in current news cycle

**Key rule:** Minimal suppression at rallies. Trump treats the rally as a stream-of-consciousness
venue. If a phrase has a 40% historical rate, it's plausible at any rally.

---

## PRESSER — Press Conferences, Media Availabilities

**Characteristics:**
- Reporter-driven Q&A; topics follow breaking news
- Trump responds to questions, can go off-script significantly
- Current news cycle has strong influence

**Suppress (0.5x – 0.8x):**
- Topics with zero presence in current news
- Highly niche catchphrases when no relevant question is likely

**Boost (1.1x – 1.5x):**
- Phrases directly tied to the day's top news stories
- Anything in today's WH schedule or Truth Social posts

---

## BRIEFING — White House Press Briefing (Spokesperson-led)

**Characteristics:**
- Spokesperson (e.g., Karoline Leavitt, Sarah Huckabee Sanders, Kayleigh McEnany), **NOT Trump**
- Formal, policy-focused Q&A driven by reporter questions on the day's news
- Personal nicknames and Trump catchphrases are almost never used by WH staff
- Culture-war and rally-style phrases are rare unless directly asked by reporters

**CRITICAL: Spokesperson ≠ Trump. Do NOT apply Trump rally logic here.**
Leavitt/WH spokespersons communicate in formal policy language. They do NOT say
partisan attack nicknames, rally catchphrases, or culture-war slogans unprompted.
Phrases like "transgender", "DEI", "woke", "witch hunt" have much lower rates at
a structured Q&A than at a Trump rally — use the historical briefing base rate,
not a rally-elevated floor.

**Suppress heavily (0.1x – 0.3x):**
- All Trump personal catchphrases ("mog", "discombobulator", "drill baby drill", "MAGA")
- Personal attack nicknames for opponents ("sleepy joe", "crooked", "newscum")
- Media commentator names (unless discussing briefing room access)
- Rally-style culture-war language unless it's the explicit reporter question topic

**Suppress moderately (0.4x – 0.7x):**
- Generic culture-war phrases ("transgender", "DEI", "woke") unless a bill or
  executive order with that specific subject is on the day's schedule
- Partisan political attacks not tied to today's WH schedule

**Boost (1.2x – 1.5x):**
- Formal policy language tied to the day's official WH schedule events
- Phrases matching the specific executive orders, bills, or diplomatic events on today's schedule
- "border", "illegal", "tariff", "china" only if these are today's WH talking points

**P_floor rules for briefing format:**
- p_floors are CAPPED at 0.50 for spokesperson events (hard limit in code)
- Only set p_floors for phrases where the **day's specific WH schedule** structurally
  guarantees the floor — e.g., if today's briefing follows a border security EO signing
- Do NOT set p_floors based on general topic relevance or current news cycle alone
- Do NOT set p_floors for culture-war phrases at a routine policy briefing

**Example correct reasoning:**
> "Leavitt briefing following a DHS deportation announcement → 'deport' p_floor=0.45
>  because DHS actions will dominate the Q&A agenda."
> "Leavitt routine briefing → 'transgender' multiplier=0.5x because no transgender-
>  specific bill is on today's schedule and Leavitt does not use rally language."

---

## TESTIMONY — Congressional Hearings, Senate Confirmation Hearings

**Characteristics:**
- Structured Q&A before a congressional committee
- Speaker is CONSTRAINED to the committee's stated topic
- Off-topic phrases are unlikely unless the speaker goes rogue (rare for non-Trump)

**Suppress heavily (0.1x – 0.3x):**
- Any phrase outside the committee's stated jurisdiction
- Political attack phrases (member being questioned is under oath / scrutiny)
- Entertainment / pop culture references

**Boost (1.3x – 1.8x):**
- Technical terms directly relevant to the committee's focus area
- Phrases matching the specific hearing title

**Example:** Senate Intelligence Committee hearing → boost "intelligence", "surveillance",
"classified"; suppress "economy", "border", "tariff"

---

## EARNINGS — Corporate Earnings Calls

**Characteristics:**
- Corporate CFO/CEO speaking to analysts
- Entirely financial and operational language
- Zero relevance to political phrases, Trump topics, or geopolitics (unless industry-specific)

**Suppress heavily (0.1x – 0.2x):**
- ALL political phrases, Trump-related phrases, partisan language
- Any phrase not related to the company's industry/financials

**Boost (1.3x – 1.8x):**
- The company name, products, sector-specific terms
- Financial metrics: revenue, margin, guidance, tariff (if supply chain relevant)

---

## SIGNING — Bill Signings, Executive Order Signings

**Characteristics:**
- Short, scripted ceremony focused on the specific legislation
- Remarks are on-topic for the specific bill being signed
- Some off-script commentary possible but constrained

**Suppress (0.3x – 0.6x):**
- Phrases unrelated to the bill's subject area
- Heavy partisan attack language (signings are typically celebratory)

**Boost (1.3x – 1.6x):**
- Key phrases from the bill's title and subject matter

---

## INTERVIEW — Media Interviews (TV, Podcast, Radio)

**Characteristics:**
- Conversational, driven by interviewer questions
- Moderate off-script probability; depends on interviewer and outlet
- Fox News interviews → higher partisan phrase probability
- Friendly podcast → more off-script than adversarial interview

**Adjust based on outlet:**
- Fox News / friendly outlet: boost partisan phrases, attack nicknames
- Neutral/adversarial outlet: moderate suppression of attack phrases

---

## ADDRESS — Formal Addresses (SOTU, Rose Garden, Oval Office)

**Characteristics:**
- Teleprompter likely; scripted, ceremonial
- More formal language than rallies
- Off-script insults and catchphrases are suppressed

**Suppress (0.4x – 0.7x):**
- Rally-specific catchphrases and attack nicknames
- Highly informal language

**Boost (1.2x – 1.5x):**
- Phrases aligned with the address's announced theme
