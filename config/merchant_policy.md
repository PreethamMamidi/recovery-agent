# Merchant recovery policy

Retrievable chunks. The agent may only offer what a retrieved chunk permits.
If retrieval returns nothing, no offer is made.

---

POL-001  discount_authority
  failure_class: insufficient_funds
  amount_band:   under_5000
  permitted:     no discount; payment link only
  tone:          gentle, non-urgent
  Most failed payments in this band recover on a later debit or a plain
  link. Do not invent a waiver. Ask once, then once more at +6h.

POL-002  discount_authority
  failure_class: insufficient_funds
  amount_band:   over_25000
  permitted:     one-time 5% waiver, requires review flag
  tone:          formal
  High-value NSF only. A one-time 5% waiver may be mentioned after the
  review flag is set. Never quote a higher percentage. Never apply the
  waiver in the message itself — review decides.

POL-003  discount_authority
  failure_class: insufficient_funds
  amount_band:   5000_to_25000
  permitted:     no offer permitted
  tone:          gentle, non-urgent
  Mid-ticket NSF. Payment link only. No waiver, no cashback, no “special
  arrangement.”

POL-004  discount_authority
  failure_class: customer_input_error
  amount_band:   any
  permitted:     no offer permitted
  tone:          calm, practical
  The instrument is fine. Send a payment link. Do not apologise with a
  discount.

POL-005  discount_authority
  failure_class: issuer_decline
  amount_band:   any
  permitted:     no offer permitted
  tone:          neutral
  The issuer refused. Same card will fail again. No incentive changes
  that. Direct them to their bank or a different method. No discount.

POL-006  discount_authority
  failure_class: mandate_failure
  amount_band:   any
  permitted:     no offer permitted
  tone:          instructional
  Setup failure. Re-authorisation link only. No discount for completing
  a mandate.

POL-007  prohibited_claims
  never_say: account closure, legal action, credit score impact, service suspension without notice, police, recover from salary, blacklist
  Never threaten. Never imply RBI, NPCI, or the bank will penalise the
  customer. Never invent a deadline that is not on the invoice.

POL-008  escalation
  amount_band:   over_25000
  permitted:     flag for review; do not invent an offer
  Payments at or above Rs 25,000 are flagged for a human. The agent
  still sends the scheduled message. It does not escalate the offer.

POL-009  quiet_hours
  permitted:     promotional only, 09:00–21:00; service/transactional unrestricted
  Quiet hours are a TRAI category rule, not a copy choice. Transactional and
  service SMS have no time restriction and reach DND subscribers. Mixing an
  offer into a recovery message reclassifies it as Promotional: 09:00–21:00
  and DND-scrubbed. If a promotional send is shifted, do not mention
  “we waited overnight” or imply surveillance.

POL-010  opt_out
  permitted:     honour opt-out immediately; no further messages
  Every commercial message may include: “Reply STOP to opt out.”
  After opt-out, no reminder, no “are you sure,” no last offer.

POL-011  instrument_update
  failure_class: instrument_invalid
  amount_band:   any
  permitted:     update link only, no incentive
  tone:          neutral, instructional
  The instrument is dead. Request an update. Do not offer a discount
  for updating a card. Do not say the old method “might still work.”

POL-012  discount_authority
  failure_class: technical_downtime
  amount_band:   any
  permitted:     no offer permitted
  tone:          reassuring, no blame
  Do not message a mandated customer during the outage. If a no-mandate
  link is sent after the window, do not offer compensation for the rail
  failure.

POL-013  discount_authority
  failure_class: temporary_lockout
  amount_band:   any
  permitted:     no offer permitted
  tone:          patient
  Time clears a lockout. No discount for waiting. Do not tell them to
  “try the same card now.”

POL-014  discount_authority
  failure_class: session_expiry
  amount_band:   any
  permitted:     no offer permitted
  tone:          brief, immediate
  The clock ran out. Payment link. No discount for coming back.

POL-015  discount_authority
  failure_class: limit_exceeded
  amount_band:   any
  permitted:     no offer permitted
  tone:          factual
  Daily caps reset; structural caps do not. No waiver of a bank limit.

POL-016  tone
  customer_tier: standard
  tone:          plain, concise, no flattery
  Standard-tier copy is short. Do not mention loyalty or tenure.

POL-017  tone
  customer_tier: silver
  tone:          polite, slightly warmer
  Acknowledge the relationship in one clause at most. Still no offer
  unless a discount_authority chunk permits one.

POL-018  tone
  customer_tier: gold
  tone:          formal, respectful
  Gold-tier tone is not a discount. High LTV does not authorise a
  waiver; POL-002 does, and only for high-value NSF.

POL-019  opt_out
  customer_tier: gold
  permitted:     honour opt-out immediately; same STOP language
  Gold-tier opt-out is identical. Do not add a “relationship manager
  will call” promise.
